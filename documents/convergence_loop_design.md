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
| **Gap thrash** | authoring for a gap the next coverage run still reports | the gap count stops dropping → `stalled` (§4) |
| **Oscillation** | refining X creates an overlap with Y; fixing Y reopens X | per-requirement edit cap; fingerprint the whole set per round and stop on repeat |
| **Plateau** | refinement cannot reach threshold without external facts | `needs_input` with a specific question (§5) |

## 4. Termination signal — the gap COUNT

**Simplest thing that works.** Each round re-runs coverage and reads one number:
how many gaps. That is the progress signal. Nothing is matched across rounds.

```
converged  when gaps == 0
stalled    when the count stops dropping
capped     when MAX_ROUNDS is reached          (safety backstop only)
```

Counting *rounds* alone is not enough — it tells you that you stopped, not that
you are done. Counting *gaps* answers "done"; the round cap only stops a bug from
spinning forever. Both are needed, and both are a constant plus a comparison.

**Noise.** Coverage is an LLM, so two identical runs will not return exactly the
same count. Require a real drop before calling it progress, and allow two flat
rounds before declaring `stalled`, so ordinary variance is not mistaken for a
plateau.

**What this gives up** (accepted, for now):
- *Rotation is invisible* — closing 5 gaps while 5 new ones open reads as a
  plateau. Stalling is the right response there anyway.
- *Repeat authoring* — a round may re-author for a gap it already failed at.
  Authoring's rerank dedup catches most (26 of 78 suppressed on the NIST run); a
  repeat costs a wasted draft + 9 judges.
- *No per-gap history* — you see the remaining gaps, not what was attempted.

What is **not** given up: termination, and gap → requirement traceability (each
authored requirement already carries `gap_id`/`gap_title` in its provenance,
independent of any ledger).

**Rejected: a carried gap ledger + closure judge.** Built and removed on
2026-07-18. It solved gap identity across rounds — real, but only needed for
per-gap attempt caps, which is a v2 refinement, not a v1 requirement. It also cost
*more*: one closure call per open gap (78) versus one coverage panel (16 domain
judges). Simpler and cheaper to re-run coverage and read the count.

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
- round boundaries persist, so a long run survives restart (Phase G)

## 7. Persistence

Loop state (`round`, `state`, gap counts per round, open questions, set fingerprint)
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

1. ~~Gap identity mechanism~~ — **resolved: not needed.** The loop terminates on
   the gap count; identity was solving a problem v1 does not have.
2. Whether all-severity authoring is really wanted given +92 % set growth, or
   whether medium/low gaps should raise *questions* to the human rather than
   authored requirements.
3. `needs_input` detection — structured reviewer signal vs heuristic.
4. `MAX_ROUNDS` default and whether `stalled` blocks release (it should).
