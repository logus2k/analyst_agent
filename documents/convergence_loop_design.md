# Convergence Loop — Design (Phase D)

Draft for review. Implements decisions 1–4 of `remaining_work.md`: the Analyst
drives a set to *complete and at/above threshold* rather than reporting problems
and handing them over.

---

## 1. State machine

```
draft ──► converging ⇄ needs_input ──► converged ──► proposed ──► validated
                │                                                    ▲
                └──────────── round cap / no-progress ──► stalled ────┘
                                                          (human intervenes)
```

`converged` is machine-determined; `proposed → validated` still requires human
sign-off. The Analyst never self-promotes past `converged`.

`stalled` is a distinct terminal state from `needs_input`: `needs_input` has a
specific answerable question, `stalled` means the loop stopped making progress
without being able to say why. Collapsing them would hide the difference between
"I need a number from you" and "I am going in circles".

## 2. Round anatomy

```
round N:
  1. score      — only requirements whose text_hash changed or judges_ok < 9
  2. refine     — every requirement below threshold (bounded attempts)
  3. triage     — still below threshold?  → classify why:
                    external info missing → question → needs_input (HALT)
                    otherwise             → carry into next round
  4. coverage   — domain-judge panel over the current set   (skip if set unchanged)
  5. author     — draft a requirement per open gap (Phase C), dedup against the set
  6. set-level  — overlaps / C10–C15 (authored requirements can collide)
  repeat while the round changed anything
```

Authored requirements are scored and refined at the top of round N+1, not inline
— it keeps each stage a single batched pass and makes the round boundary a clean
persistence point.

## 3. Termination

**Converged** when all four hold:
- every requirement `overall >= threshold`
- every requirement `judges_ok == judges_total`  *(Phase A; a partial score must
  not satisfy the exit test)*
- no open coverage gaps
- the round changed nothing (no text edits, no requirements authored)

**Bounds** — `MAX_ROUNDS` (default 6) and a no-progress guard: if a round closes
no gaps and raises no score, stop at `stalled`. Both must be provable in tests
with a stubbed LLM, not observed by eye.

### The three failure modes

| Mode | Cause | Guard |
|---|---|---|
| **Gap thrash** | authoring for gap G, next coverage still reports G | per-gap attempt cap; needs **gap identity** (§4) |
| **Oscillation** | refining X creates an overlap with Y; fixing Y reopens X | per-requirement edit cap; fingerprint the whole set per round and stop on repeat |
| **Plateau** | refinement cannot reach threshold without external facts | `needs_input` with a specific question (§5) |

## 4. Gap identity — the crux

**Coverage gaps have no stable id.** A real gap record is:

```json
{"title": "Data Retention, Archival, and Deletion Policies",
 "severity": "critical", "domain": "data",
 "detail": "...", "question": "...",
 "grounding": ["Data & information domain concerns: retention, archival, deletion", ...]}
```

`domain` is stable (16 catalog ids). `title`/`detail`/`question` are free text
generated per run by an LLM, so round 2's phrasing of the same gap will differ
from round 1's. Without identity the loop cannot cap attempts per gap, cannot
detect no-progress, and may author near-duplicates forever.

**Measured, not assumed:** of the 130 `grounding` strings in the NIST run, only
**6 (4 %) match a catalog concern exactly**. Grounding is LLM-paraphrased and
mixes three sources — domain concerns, *archetype* concerns ("Web / SaaS
application concerns: …"), and standards leaves ("Time behaviour
(iso-25010:2023)"). So grounding **cannot** be used as a key as it stands, and a
single gap often spans several sources, so one concern index would not represent
it anyway.

Three candidate mechanisms:

- **(a) Catalog-concern keying.** Requires changing `coverage_judge` to emit
  `(domain_id, concern_index)` *deliberately* as a structured field — it cannot
  be recovered from today's output (see the 4 % measurement above), and cannot be
  backfilled onto existing runs. Constrains judges to catalog concerns, so a
  legitimate gap outside the catalog becomes unkeyable, and multi-source gaps fit
  poorly.
- **(b) Embedding/rerank match.** Match round-N gaps to round-(N−1) gaps by
  similarity, reusing the reranker already used for dedup and overlap. No preset
  change; thresholds are another tunable, and it inherits reranker error.
- **(c) Explicit closure check.** Carry each open gap forward and ask a judge
  "does the current set now cover this gap?" instead of re-deriving gaps. Turns
  identity into a non-problem; costs one call per open gap per round (78 calls on
  the NIST project) and risks drift from what a fresh panel would say.

Recommendation (revised after the 4 % measurement): **(c) as the primary
mechanism.** Carrying each open gap forward and asking whether the current set now
covers it makes identity a non-problem — the gap object *is* its own identity,
minted once and tracked, never re-derived. A fresh full panel runs on the first
round (to discover gaps) and the final round (to confirm nothing new opened);
interior rounds only ask the closure question. Cost is one call per open gap per
round (78 on the NIST project), which is cheaper than a full 16-domain panel.

(a) stays available if we later want structural keys, but it needs a deliberate
preset change and buys less than it appeared to. (b) is the fallback for matching
a final-round fresh gap against a carried one.

## 5. `needs_input` detection

The P4 finding: proposals converge to parameterized text
(`[specify maximum response time]`) and plateau ~3.5 because the value is external
to the document. That is the loop working correctly — it must surface the question,
not keep burning attempts.

Detection options: a text heuristic for bracketed placeholders (brittle), or
**extend `incose_reviewer` to return a structured signal** —
`{blocked: true, question: "...", missing: "maximum response time"}` — when it
cannot improve a requirement without a fact it does not have. Preferred: it is the
component that already knows why it is stuck.

Answers are stored per project as **clarifications** and injected into subsequent
refinement prompts, so the fact is available to every requirement that needs it,
not just the one that asked.

## 6. Cost model — the binding constraint

Measured on the real NIST project (85 requirements):

- coverage produced **78 gaps** — 13 critical, 39 high, 25 medium, 1 low
- decision 3 (all severities) ⇒ up to **78 authored requirements**, growing the
  set from 85 to ~163 (**+92 %**)
- a full round then costs **163 × 9 = 1 467 judge calls**, plus 16 domain judges,
  plus refinement, plus authoring — on one serialized GPU

Wall-clock is **unmeasured**: run metadata records `finished_at` but no
`started_at`, so no duration exists in the store. Adding `started_at`/`duration`
is a prerequisite for planning this honestly.

Consequences, all load-bearing rather than optional:
- **incremental scoring** (`text_hash` → skip unchanged) is mandatory, not a nicety
- coverage re-runs only when the set changed
- the per-gap closure check (§4c) replaces full re-derivation on interior rounds
- round boundaries persist, so a long run survives restart (Phase G)

## 7. Persistence

Loop state (`round`, `state`, per-gap attempts, open questions, set fingerprint)
persists under the project each round boundary. A restart resumes at the last
completed round instead of restarting — today `JobManager.jobs` is an in-memory
dict and a restart loses everything, which is survivable for a 3-minute quality
run and not for a multi-round convergence run.

## 8. Fabrication risk

Decision 2 has the Analyst writing requirements **no stakeholder asked for**. A
plausible-sounding authored requirement is exactly the kind of thing that survives
review by looking reasonable. Mitigations:

- an authored requirement must cite the gap and the catalog grounding that
  motivated it — no free invention
- `provenance.origin = "analyst_authored"`, never a source document, page, or bbox
- `ratified: false` until a human accepts; unratified authored requirements block
  release (implemented in Phase A)
- the reissued document (Phase F) marks them as additions, never as source content
- they are visually distinct in any UI surface

## 9. Open decisions

1. Gap identity mechanism — (c) carried-forward closure checks is now the
   recommendation; (a) was weakened by the 4 % grounding measurement.
2. Whether all-severity authoring is really wanted given +92 % set growth, or
   whether medium/low gaps should raise *questions* to the human rather than
   authored requirements.
3. `needs_input` detection — structured reviewer signal vs heuristic.
4. `MAX_ROUNDS` default and whether `stalled` blocks release (it should).
