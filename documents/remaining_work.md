# Analyst Agent — Remaining Work

Forward plan. The historical phase ledger stays in `migration_plan.md` (P0–P8);
this document covers everything still outstanding, including issues found by code
audit rather than by the migration itself.

Same discipline as the migration: **every phase has a verification gate, and a
phase is done only when its check passes against live services.**

---

## Decisions (2026-07-18) — these supersede `technical_architecture.md` §8

The Analyst **owns delivering a complete set at or above threshold**. It does not
report problems and hand them over; it drives them to closure and demands human
input when — and only when — the information is genuinely external.

1. **Absolute quality floor, no override.** Release is blocked until *every*
   requirement genuinely scores at/above the acceptance threshold. `needs_human`
   is a hard blocker, not an acceptable terminal state. §8's "or explicitly
   accepted by a human despite being below" is **removed**.
2. **The Analyst closes coverage gaps.** It authors requirements to fill them,
   scores and refines them to threshold like any other, and marks them
   analyst-authored (no source-document provenance). A human ratifies before
   release. *Analyst owns completeness; human owns truth.*
3. **All gap severities must be closed**, not just critical.
4. **Convergence is iterative.** Quality and coverage interact — authored
   requirements change the set, which changes coverage and set-level overlap — so
   the Analyst runs a bounded **convergence loop** to a fixpoint rather than a
   one-shot pipeline.

---

## A — Scoring integrity  *(prerequisite: the loop's exit test depends on it)*

The convergence loop terminates on "every requirement ≥ threshold", so `overall`
must be trustworthy before the loop is built. Today it is not:

1. **Partial judging is invisible.** If 3 of 9 judges time out, the mean of the
   surviving 6 is emitted in a shape identical to a complete result.
2. **Falsy filtering** drops a literal `0` as though the judge never answered.
3. **`or 5` conflates failure with excellence** — a requirement whose judges died
   is never flagged for review.

Not yet realised (audit: 734 stored requirements, 0 nulls, 0 errors) but the
mechanism is live and the box has already produced 180 s+ LLM timeouts under load.
A partial score silently satisfying the loop's exit test would be a false release.

**Tasks**
- `is not None` filtering in `jobs._finalize_scores`, `jobs._aggregates`,
  `assess._overall`, `assess._review`.  ✅
- Both `_needs_review` implementations: a failed judge *forces* review.  ✅
- `judges_ok` / `judges_total` per requirement → scorecard aggregates.  ✅
- `package.py`: incomplete judging is a release blocker.
- Loop exit test must require `judges_ok == judges_total`.

**Gate:** unit tests prove a 6-of-9 result is flagged and not silently averaged;
a live run still yields `judges_ok: 9` for every requirement.

## B — Test harness

Zero tests today; every gate so far was a hand-run curl. The convergence loop has
real termination logic (fixpoint, no-progress, round cap) that must not be
verified by eye.

**Tasks**
- `pytest` + `conftest.py` + `pytest.ini`; dev deps.
- Pure-function cover: `normalize_rule_ids`, `score.deterministic` matching,
  `segment.verify` traceability, `assess._overall`/`_needs_review`/`_judge_health`,
  `chunker` bounds, `dedup` guards, `classify._coerce`, `package` blockers,
  `store` round-trips.
- **Loop termination tests with a stubbed LLM** — converges, hits the round cap,
  detects no-progress, halts on `needs_input`.
- `README.md` (absent).

**Gate:** `pytest` green in-container, no network required.

## C — Gap authoring  *(new capability)*

`coverage/` today reports gaps. It must also close them.

**Tasks**
- New agent_server preset `incose_gap_author`: gap + domain + problem statement +
  existing set → a candidate requirement.
- New `authoring.py`: draft → score → refine to threshold → attach
  `provenance: {origin: "analyst_authored", gap_id, domain}`.
- Authored requirements are `ratified: false` until a human accepts; ratification
  is a release blocker (decision 2).
- Dedup authored candidates against the existing set (reuse `segment.dedup`) so
  the loop cannot author a requirement the set already has.

**Gate:** every gap in a real coverage run yields a candidate that itself clears
threshold; no authored requirement duplicates an existing one.

## D — Convergence loop  *(the defining capability)*

A resumable state machine, not a blocking call.

```
draft → converging ⇄ needs_input → converged → proposed → validated
```

Each round:
1. Score requirements that are new or changed (9 judges each).
2. Refine everything below threshold (bounded attempts, existing `refine.py`).
3. Still below threshold and the reviewer is asking for information external to
   the documents → collect as **questions**, transition `needs_input`, halt.
4. Run coverage over the current set.
5. Author + refine a requirement for every open gap (Phase C).
6. Re-run set-level (new requirements can introduce overlaps).
7. Repeat while the round changed anything.

**Termination** — converged when: every requirement ≥ threshold, every
requirement fully judged (`judges_ok == judges_total`), no open gaps, and the
round added/changed nothing. Bounded by `MAX_ROUNDS`; abort on no-progress so a
plateau cannot spin. Every round is expensive (85 reqs × 9 judges + 16 domain
judges), so **only re-judge what changed**.

**Human input** — `needs_input` surfaces specific questions ("what is the maximum
response time?"). An answer endpoint feeds them back and resumes the loop. This is
the loop correctly demanding what it cannot know, and is the *only* legitimate way
a below-threshold requirement is resolved (decision 1).

**Tasks**
- `converge.py`: the round orchestrator + termination + no-progress detection.
- `POST /projects/{pid}/converge:run`, `GET …/convergence` (state, round, blockers,
  open questions), `POST …/questions/{qid}:answer`.
- Persist loop state so a restart resumes rather than restarts (see G).
- socket.io progress: round boundaries, per-stage events, `needs_input`.

**Gate:** a real project with known-weak requirements and known gaps runs to
`converged` without human intervention where the information is present; halts at
`needs_input` with a specific answerable question where it is not; resumes and
converges once answered; the round cap and no-progress guard both provably fire.

## E — Release gate + package

**Tasks**
- State machine `converged → proposed → validated`; the Analyst never self-promotes.
- `GET /projects/{pid}/release`; `POST /projects/{pid}/release:approve`.
- Blockers: any requirement below threshold, any incompletely judged, any open
  gap, any unratified authored requirement, no human sign-off.
- **Freeze the package at approval** — classification is not reproducible
  (~7/85 labels move between runs), so a released set is stored, not recomputed.
- Emit `requirements.json`, `requirements.md`, `problem_statement.json`,
  `coverage.json`, `manifest.json`.

**Gate:** a real project reaches `validated` only via human approval, only from
`converged`, and the emitted `requirements.json` validates against the Architect's
input shape.

## F — P7 reissue

Content-complete corrected specification document (headings, prose, tables in
order, corrected text substituted) + server-side PDF (WeasyPrint). Must include
analyst-authored requirements, marked as additions.

**Gate:** exported doc contains all source sections + corrected + authored requirements.

## G — Robustness / operations

- **Job state is memory-only** (`JobManager.jobs` dict). A restart loses every
  in-flight run; `GET /jobs/{id}` then 404s indistinguishably from "never existed".
  A multi-round convergence run is long, so this becomes load-bearing: persist job
  + loop state, mark orphans `interrupted` on boot, resume.
- **Timeouts**: the 180 s client default turns a slow call into a lost result.
  Configurable, and higher for batch work.
- **Untested abort paths**: refine abort, classify abort (checked per wave of 8).
- Deployed WebSocket path; multipart upload + large PDF download through the BFF.

## H — Contract & hygiene

- `technical_architecture.md`: §7 shows `"id"`, code emits **`req_id`**; §7 says
  single-label `type`, code emits `classes[]` **and** `type`; §8's human-override
  clause is superseded by decision 1. Architect's `implementation.md` §2.1.1 still
  says the Analyst supplies no `type`.
- `store/` is committed to git (57 files incl. 2 PDFs), no `.gitignore`.
- Auth on write endpoints — **deferred by explicit decision**; `DELETE /projects/{pid}`
  ungated. Listed for visibility, not scheduled.

---

## Order

A → B → C → D → E → F, with G and H folded in where they touch the same files.
A and B come first because D's termination test is only as trustworthy as the
scores it reads and the tests that pin its exit conditions.

## Status log
- 2026-07-18 — plan written; decisions 1–4 recorded.
- 2026-07-18 — **A DONE (gate passed).** `is not None` filtering in
  `jobs._finalize_scores`/`_aggregates` and `assess._overall`/`_review`; both
  `_needs_review` implementations now force review on a failed judge instead of
  treating `None` as 5; `judges_ok`/`judges_total` per requirement, surfaced in
  the scorecard aggregates (`incompletely_judged`) and in `package.analysis`.
  `package.py`: below-threshold is now an **absolute** blocker (the
  `_TERMINAL_OK` human-acceptance exemption is deleted, per decision 1), plus new
  blockers for incomplete judging and unratified authored requirements.
  **Gates:** (a) 23 unit tests green offline, incl. one proving a 6-of-9 mean and
  a 9-of-9 mean are numerically identical and separable only by the health
  counters; (b) fresh live run on a 3-requirement SRS → all `judges 9/9`,
  `incompletely_judged: []`, scores 5 / 4.89 / 4.89; (c) real NIST project package
  re-checked → `75 requirement(s) below threshold 4.3` (no longer qualified by
  "and not human-accepted"), `incompletely_judged: 0`. Pre-existing scorecards
  carry `judges_ok: null` and correctly do **not** trip the blocker — the field
  cannot be reconstructed retroactively. Smoke project deleted; the 3 real
  projects verified intact.
- 2026-07-18 — **B started.** `pytest.ini`, `tests/`, `requirements-dev.txt`,
  `README.md` (repo had none). 23 tests, offline, ~0.1 s.
- 2026-07-18 — **Decision 3 confirmed by the user: ALL 78 gaps**, and generated
  requirements **must be clearly flagged as generated**.
- 2026-07-18 — **C (gap authoring) built, partially verified.** `incose_gap_author`
  preset (registered, hot-reload, HTTP 201) + `src/analyst_agent/authoring.py` +
  `_run_author`/`create_author_run` + `POST /projects/{pid}/author:run` (33 routes).
  Flagging contract: `provenance.origin = "analyst_authored"`,
  `generated_to_fill_coverage_gap: true`, `ratified: false`, gap_id/title/severity/
  domain/grounding/rationale/assumptions, and deliberately **no** source_document/
  page/bbox. `package.render_markdown` marks them `⚠️ GENERATED` with a callout,
  "no stakeholder wrote this", and "generated, not extracted from any document";
  manifest gains `counts.analyst_authored`. 49 tests green.
  **Live check on a real NIST gap** (read-only, project not mutated): drafted
  *"The system shall enforce data retention periods for all distributed data
  items."* — correctly refused to invent a retention number, recorded the
  assumption, and set `needs_input` with the precise question.
  **Design flaw found by running it:** `needs_input` and a usable draft are not
  mutually exclusive; the original code discarded the text whenever `needs_input`
  was set, losing a real requirement and leaving the gap open. Now the text is
  kept and the question rides along as `provenance.open_question` — the
  under-specified requirement scores low on Complete/Verifiable, plateaus in
  refinement, and lands `needs_human`, which under the absolute floor blocks
  release until someone answers. Only a text-less draft is skipped.
  **NOT verified:** `iter_author_for_project` end-to-end (persistence, dedup
  against the live set, refine-to-threshold) has not been run against a real
  project — only its parts are unit-tested.
- 2026-07-18 — **B: pure-function + store coverage added.** `tests/
  test_pure_functions.py` (characteristics/rule-id normalization, `verify`
  traceability incl. the invented-text rejection, deterministic rule matching
  incl. word-boundary and symbol handling, the three classification
  vocabularies) and `tests/test_store.py` (project/document/run/review round
  trips, latest-run selection, `reviewed_at` not forged by classification,
  atomic writes leaving no `.tmp`, corrupt JSON degrading to `None`).
  **109 tests, offline, 0.24 s.**
  *One test was wrong, not the code:* `check_requirement` on a well-formed
  requirement is NOT empty — R5 fires on the indefinite article "an" and R35 on
  "after". The checker is deliberately high-recall and the characteristic judge
  decides severity, so the test now asserts that behaviour explicitly rather
  than asserting a clean requirement trips nothing.
- 2026-07-18 — **C gate PASSED end-to-end on a NIST clone** (throwaway project,
  store-level copy so quality+coverage were not re-run; the real NIST project was
  never touched — re-verified at 85 requirements, zero `GAP-` ids).
  **78 gaps → 9–10 minutes** (~8 gaps/min — the first throughput measurement this
  project has; two independent timers read 9 and 10 min): **52 authored, 26 duplicates suppressed, 0 failed, 0 text-less**.
  Set grew 85 → 137 (+61 %, not the +92 % projected — dedup removed a third).
  Authored mean 4.5; 42/52 at/above 4.3; **52/52 judged 9/9**; 52 review entries
  seeded; flagging correct (`origin: analyst_authored`,
  `generated_to_fill_coverage_gap: true`, `ratified: false`, gap traced, and
  **no** invented source_document/page/bbox). Markdown renders `⚠️ GENERATED`
  with "no stakeholder wrote this".
  **Two defects found by running it, both fixed and tested:**
  (a) **Placeholders clear the quality floor.** `"...latency of less than
  [LATENCY_VALUE]..."` scored **4.56** — above threshold. The nine judges rate the
  *form* of a statement and a parameterized statement is well-formed; they cannot
  know the value was never filled. 4 of 52 authored requirements carry one (8
  across the whole set). Added deterministic `unresolved_placeholders` (not a
  judge — no LLM variance) + a release blocker over **every** requirement, since a
  human edit can introduce one too. Confirmed live: *"8 requirement(s) contain
  unfilled placeholders"*.
  (b) **All-or-nothing persistence.** `_persist` ran only after all 78 gaps, so a
  crash or cancel at gap 77 would discard 76 paid-for requirements. Now flushes
  every `FLUSH_EVERY=10` and on cancel.
  **125 tests green.**
- 2026-07-18 — **H (partial).** `technical_architecture.md` corrected: §7 example
  key `id` → **`req_id`**, §7 now shows `classes[]` alongside `type`, §9.2's
  guarantee lists both, and §8's human-override clause is replaced by the absolute
  floor (+ the incomplete-judging and placeholder blockers). Added `.gitignore`
  for `store/` and `__pycache__` — **88 files are still tracked**; untracking is a
  git index operation left to the user (`git rm -r --cached store`).
