# Analyst Agent — capabilities and interfaces

**Audience:** the Architect Agent (and any other consumer of validated requirements).
**Service:** `analyst-agent`, HTTP on **`:7803`**, version `0.1.0`.
**Status of this document:** generated from the live OpenAPI surface on 2026-07-18 and
verified against a running instance. Where something is designed-but-not-built it says so
explicitly — do not infer capability from silence.

**Clients:** `sdk/js/analyst-client.js` (browser, ES module, no build step) and
`sdk/python/analyst_client/` (httpx). **45 methods shared** under the same names
(camelCase in JS, snake_case in Python); the JS client additionally has
`streamJob` and `assess`, which are socket.io and therefore browser-only. Both
absorb the envelope inconsistencies described in §11.

---

## 1. What the Analyst does

It turns raw stakeholder material (PDF, DOCX, Markdown, or a one-line request) into a
**scored, classified, traceable requirement set**, and hands it over as a single package.

It is the first stage of the chain **Analyst → Architect → Planner**.

Its defining capability is not extraction but **bounded autonomous refinement**: a
requirement scoring below the acceptance threshold is rewritten and re-scored (≤3 attempts,
stop on no improvement, keep the best) and, if it still cannot clear the bar, escalated as
`needs_human` rather than silently dropped or silently "fixed". The original text is
immutable and every attempt is persisted, so semantic drift stays auditable.

### What it does NOT do
- It does **not** classify architecture. It labels requirements for routing (§4); turning
  those into `part def`/`action def`/`port def` is the Architect's job.
- It does **not** self-promote a set to released. A human signs off (§6).
- It does **not** parse documents itself — that is delegated to `ingestion-server:8700`.

---

## 2. The one endpoint you need

```
GET /projects/{pid}/package
```

| Query param | Default | Meaning |
|---|---|---|
| `run` | latest quality run | pin a specific run id |
| `format` | `json` | `json` = machine contract · `md` = human-readable companion |

```bash
curl -s http://localhost:7803/projects/<PID>/package -o requirements.json
```

Everything else in this document is optional context. This call returns the complete
handover document; nothing else needs to be assembled client-side.

### Response envelope

```jsonc
{
  "manifest":            { /* provenance of the package + readiness verdict, §6 */ },
  "requirements":        [ /* the payload, §3 */ ],
  "set_level":           { "overlaps": [], "set_assessment": [] },   // INCOSE C10–C15
  "aggregates":          { "per_characteristic_mean": {}, "per_rule_violation_count": {},
                           "score_distribution": {}, "total": 85 },
  "characteristic_names":{ "C1": "Necessary", "...": "..." },
  "problem_statement":   { /* versioned, may be null */ },
  "coverage":            { /* domain-gap analysis, may be null */ },
  "coverage_profile":    { /* which domains/standards apply, may be null */ }
}
```

**Design rule: the package delivers more, not less.** You can ignore a field you do not
need; you cannot recover one that was dropped. Expect ~550 KB for 85 requirements — most of
that is the full characteristic objects, including each judge's justification.

---

## 3. The requirement record

```jsonc
{
  "req_id": "REQ-0008",                         // ← THE TRACE KEY. Use verbatim.
  "text": "The VDS must provide a near-real-time stream of most current value for each data item",

  // --- routing contract (§4) ---
  "classes": ["functional", "interface"],       // MULTI-label
  "type": "performance",                        // single primary label, reporting only
  "constraints": ["latency", "throughput"],     // closed vocabulary
  "classification_rationale": "…one sentence…",

  // --- quality evidence (§5) ---
  "analysis": {
    "score": 3,                                 // mean of C1–C9, 1–5
    "score_before_refinement": 3,
    "characteristics": { "C1": {"id":"C1","score":4,"rules_triggered":[],
                                "evidence":"","justification":"…"}, "…": {} },
    "characteristic_scores": { "C1": 4, "C2": 3, "…": 0 },   // flattened convenience
    "rules_triggered": ["R7"],
    "deterministic_findings": [ { "rule_id": "R7", "…": "…" } ],
    "review": null,                             // reviewer rewrites/advisories, if any
    "status": "unreviewed",                     // see §6 status values
    "original_text": "…",                       // immutable
    "text_changed": false,
    "refinement": null                          // {attempts, history[]} once refined
  },

  "lineage":    { "origin": "derived", "was_compound": true,
                  "derived_from": null, "duplicate_of": null },
  "provenance": { "source_document": "NIST SRS….pdf", "source_file": "source.pdf",
                  "source_document_id": "fe30…", "page": 9,
                  "section_path": "2.1.1 Volatile Data Stream",
                  "bbox": [90.0, 193.9, 558.0, 247.7], "char_span": null }
}
```

### Guarantees
- `req_id` is present and unique. **The key is `req_id`, not `id`** — trace links must use it
  verbatim so the architecture stays traceable to the source document.
- `classes[]` is never empty (falls back to `["functional"]`).
- `constraints[]` is always an array (often empty — that is meaningful, see §4).
- `provenance` and `lineage` are always present.
- Duplicates are **excluded** from the package (`lineage.duplicate_of` is filtered out);
  `manifest.counts.excluded_duplicates` reports how many.

### Fields worth not ignoring
- **`lineage.was_compound: true`** — this requirement was split out of a compound statement.
  It is a derived fragment, not verbatim source text.
- **`provenance.page` + `bbox`** — enough to highlight the exact region in the source PDF.
  Carry these into the ADD traceability section rather than discarding them.
- **`analysis.score` with low `C4`/`C7`** — the warning label. Example above scored C7=2
  because *"near-real-time"* is unverifiable: there is **no number in that requirement**.
  Modelling a latency constraint from it will invent a value the stakeholder never gave.

---

## 4. Classification — the routing contract

Two labels are emitted deliberately, because routing is inherently multi-label while
reporting needs a single one.

### `classes[]` — multi-label, this is the one that drives routing

| class | the requirement states… | SysML v2 |
|---|---|---|
| `functional` | a capability/action the system performs | `action def` |
| `structural` | a component/part the system must contain | `part def` |
| `interface` | an exchange with an external actor/system | `port def` / `interface def` |
| `behavioral` | a state, mode, transition, event-ordered response | `state def` |
| `constraint` | a bound/limit/budget: time, size, rate, accuracy, standard | `constraint def` |
| `allocation` | a function assigned to a specific component/resource | `allocate` |

A requirement routes to **every** module whose class it carries. *"The system shall allocate
GPUs fairly within 100 ms"* is `functional` **and** `constraint`; taking only the first label
silently drops the timing budget. In a real 85-requirement run, **28 carried more than one
class**.

> **This supersedes `architect_agent/documents/implementation.md` §2.1.1**, which states the
> Analyst supplies no `type`/`constraints` and that the Architect must classify requirements
> itself. That is no longer true — classification is done upstream, and the Architect's own
> classifier step can be removed. That document has not yet been updated.

### `type` — single label, reporting/UI only
One of: `functional | performance | safety | security | usability | reliability | interface |
data | operational | constraint | other`. **Do not route on this field.**

### `constraints[]` — closed vocabulary
`performance, latency, throughput, capacity, scalability, availability, reliability, safety,
security, privacy, compliance, usability, accessibility, portability, maintainability,
interoperability, resource, cost, schedule, fairness, environmental, data_retention,
traceability`

A term appears only if the requirement actually **bounds** it. An empty array means "no
attribute bounded" — it is a real signal, not missing data.

### ⚠️ Stability caveat
Classification runs at `temperature: 0.0` but is **not bit-reproducible**: re-running over the
same 85 requirements moved ~7 labels (`classes[]` was the more stable half). **Freeze the
labels you receive with your model; do not re-fetch and diff, or you will see drift with no
input change.**

---

## 5. Quality scoring — how to read `analysis`

- **C1–C9** are the INCOSE GtWR characteristics, scored **1–5** by nine independent LLM judges
  (one preset each, batch size 1, run 9-wide in parallel). `score` is their mean.
  Names: Necessary, Appropriate, Unambiguous, Complete, Singular, Feasible, Verifiable,
  Correct, Conforming.
- **`deterministic_findings`** come from 14 pattern-checkable rules out of a 42-rule INCOSE
  catalog (vague terms, escape clauses, pronouns, combinators…). Rule ids look like `R7`.
- **`set_level`** (envelope, not per-requirement) holds C10–C15 — the set-as-a-whole
  characteristics — plus reranker-detected `overlaps` between requirement pairs.

A score is evidence, not a verdict. `characteristics.C7.justification` tells you *why* a
requirement is unverifiable, which is usually more actionable than the number.

---

## 6. Release status — read this before consuming

`manifest` reports readiness; it never grants it.

```jsonc
"manifest": {
  "contract_version": "1.0",
  "project_id": "…", "project_name": "…", "run_id": "…",
  "threshold": 4.3,
  "release_status": "draft",
  "architect_ready": false,
  "blockers": ["75 requirement(s) below threshold 4.3",
               "8 requirement(s) contain unfilled placeholders (e.g. [VALUE], TBD)",
               "no human sign-off (release_status is not 'validated')"],
  "counts": { "total": 85, "excluded_duplicates": 0, "scored": 85,
              "at_or_above_threshold": 10, "below_threshold": 75,
              "incompletely_judged": 0, "with_placeholders": 8,
              "analyst_authored": 0, "unratified_authored": 0,
              "unclassified": 0, "mean_score": 3.65 },
  "below_threshold_ids": ["REQ-0002", "…"],
  "source_documents": [ … ]
}
```

### Blocker types (all must be empty to release)
| Blocker | Why |
|---|---|
| below threshold | **absolute floor — there is no human-override path** |
| scored on fewer than all judges | a mean over 6 of 9 judges is not comparable to a complete one |
| unfilled placeholders | measured: `"...latency of less than [LATENCY_VALUE]"` scored **4.56**, above threshold. The judges rate the FORM of a statement; a parameterized statement is well-formed. Checked deterministically, for every requirement. |
| analyst-authored, not ratified | the Analyst writes requirements to fill coverage gaps; a human must accept them |
| missing routing classes | the Architect cannot route without `classes[]` |
| no human sign-off | the Analyst never self-promotes |

**Branch on `manifest.architect_ready`, not on the presence of data.** A `draft` package is
perfectly valid input for development and testing; it must not be treated as approved.

Intended state machine: `draft → refined → proposed → validated`
(`validated` = architect-consumable). Per-requirement `analysis.status` values:
`unreviewed | accepted_refined | needs_human | skipped`.

> **NOT YET BUILT (as of 2026-07-18):** nothing writes `release_status`. The sign-off endpoint
> and the state machine do not exist, so **every package today reports `draft` /
> `architect_ready: false`**. `package.py` already reads the field, so this will start working
> without any contract change on your side.
>
> Also true today: **no project has completed refinement.** On the reference run, 75 of 85
> requirements sit below the 4.3 threshold (mean 3.65), all `unreviewed`. Refinement is known
> to plateau near ~3.5 because rewrites become parameterized (*"[specify maximum response
> time]"*) — the missing value is external to the document. Escalation to a human is the
> expected outcome, not an error.

---

## 7. Full API surface (43 operations / 36 paths, live)

You only need §2. The rest is listed so nothing looks hidden.

**Health / meta**
```
GET  /health          GET  /version          GET  /dependencies
```

**Projects**
```
GET    /projects                      POST   /projects
GET    /projects/{pid}                DELETE /projects/{pid}
GET    /projects/{pid}/documents      POST   /projects/{pid}/documents      (multipart upload)
GET    /projects/{pid}/documents/{did}/source        (original file bytes)
```

**Analysis runs** — all return `202 {job_id}` and stream progress (§8)
```
POST /projects/{pid}/quality:run     ingest → segment → gate → score
POST /projects/{pid}/refine:run      bounded refinement loop over below-threshold reqs
POST /projects/{pid}/classify:run    classes[] + type + constraints[]
POST /projects/{pid}/coverage:run    domain-judge panel: what is MISSING
POST /projects/{pid}/framing:run     derive a problem statement from the documents
POST /projects/{pid}/author:run      author a requirement per open coverage gap
POST /projects/{pid}/converge:run    the convergence loop (refine → coverage → author, repeated)
POST /projects/{pid}/problem-statement:generate
```
These are **independent operations, not a single pipeline**. Quality and coverage are
unrelated assessments (*quality = correction, coverage = completion*). Run order for a
complete package: `quality → refine → classify` (+ `framing`/`coverage` as needed) `→ package`.

**Results**
```
GET /projects/{pid}/package                 ← the handover (§2)
GET /projects/{pid}/quality                 list quality runs
GET /projects/{pid}/quality/scorecard?run=  raw scorecard
GET /projects/{pid}/coverage?run=           coverage result
GET /projects/{pid}/questions               what a human must answer (no LLM call)
GET /projects/{pid}/convergence             loop state: round, gap counts, outcome
GET /documents/{doc_id}/scorecard           single-document run (legacy path)
GET /projects/{pid}/problem-statement       PUT to ratify · POST …:generate
GET /projects/{pid}/coverage-profile        PUT to set
GET /projects/{pid}/reviews/{run}           review session (per-req state + threshold)
PUT /projects/{pid}/reviews/{run}/requirements/{req_id}
GET/PUT /projects/{pid}/reviews/{run}/threshold
```

**Jobs**
```
GET  /jobs/{job_id}            status + progress snapshot (poll this)
GET  /jobs/{job_id}/events     full event log
POST /jobs/{job_id}/cancel     cooperative abort
GET  /projects/{pid}/active-job   reattach after a page reload
```

**Reference data**
```
GET /rules               42-rule INCOSE catalog
GET /catalog/domains     GET /catalog/archetypes     GET /catalog/standards
```

**Single-requirement assessment (no project needed)** — socket.io: emit `assess {text}`,
receive `characteristic` events then `done`. Useful for interactive tooling.

---

## 8. Long-running work

Analysis is **slow** — a 386-requirement quality run takes tens of minutes. Every run returns
`202 {"job_id": …}` immediately. Two ways to follow it:

- **Poll** `GET /jobs/{job_id}` → `{status, stage, progress:{done,total,unit,message}, elapsed_s}`.
  `status` ∈ `queued | running | done | error | cancelled`. Simplest, and sufficient.
- **Stream** via socket.io on the same origin: connect, `emit("join", {job_id})`. Event types:
  `stage`, `requirement`, `characteristic`, `deterministic`, `review_result`, `refined`,
  `refine_summary`, `classified`, `classify_summary`, `domain`, `coverage`, `scorecard`,
  `set_level`, `aggregates`, `job_done`, `job_cancelled`, `job_error`, `cancelled`, `error`.

`POST /jobs/{job_id}/cancel` aborts cooperatively — checked between work items, so an
in-flight LLM call still completes first.

---

## 9. Operational notes

- **Dependencies** (all shared, all must be up): `agent_server:7701` (every LLM preset),
  `ingestion-server:8700` (document parsing), `embeddings/reranker:8601` (dedup + overlaps).
  `GET /dependencies` reports reachability. Note it returns `status: 404` for agent_server and
  ingestion-server — those services simply have no `/health` route; `reachable: true` is the
  field that matters.
- **No auth.** Deliberate for now; the service is internal.
- **Store** is plain filesystem JSON under `/app/store`, atomically written.
- **Alternate origin:** `reqoach:7802` (the UI's BFF) proxies `/projects`, `/jobs`, `/rules`,
  `/catalog`, `/documents` and socket.io to this service, if you prefer one origin.
- **Errors:** `404` unknown project / no quality run to package · `400` missing prerequisite
  (e.g. no quality run to refine) · `202` accepted, work started.

---

## 10. Known gaps

| Gap | Impact on you |
|---|---|
| Release gate not built | every package is `draft` / `architect_ready:false` |
| No project has completed refinement | reference data is genuinely low-quality (mean 3.65) |
| Classification not bit-reproducible | freeze labels; don't re-fetch and diff |
| `architect_agent` §2.1.1 contradicts §4 above | its classifier step is now redundant |
| Analyst `technical_architecture.md` §7 says `id` | the real key is **`req_id`** — that doc is wrong |
| Document reissue (corrected doc + PDF) not built | no corrected source document to reference |

---

## 11. Using the clients

Two clients. **45 methods are shared** under the same names — camelCase in JS,
snake_case in Python — so one document describes both. `streamJob` and `assess`
exist only in JS (socket.io); `close` only in Python (connection lifetime).

| | |
|---|---|
| `sdk/js/analyst-client.js` | browser, ES module, **no build step, no dependencies** |
| `sdk/python/analyst_client/` | httpx; for the Architect and scripted consumers |

```js
import { AnalystClient } from './analyst-client.js';
const analyst = new AnalystClient();          // relative to the current page
const { requirements, counts, threshold } = await analyst.loadReview(pid);
```
```python
from analyst_client import AnalystClient
with AnalystClient("http://localhost:7803") as analyst:
    review = analyst.load_review(pid)
```

### Why not just call `fetch` directly

**Envelopes are inconsistent per endpoint** — the clients absorb this so every
list method returns a list:

| endpoint | raw shape |
|---|---|
| `/projects` | `{"projects": [...]}` |
| `/projects/{pid}/quality` | `{"runs": [...]}` |
| `/catalog/domains` | `{"_about", "version", "domains": [...]}` |
| `/rules` | **a map keyed by rule id**, `{"R1": {...}} ` — deliberately left as-is, since that is what a UI wants when rendering `rules_triggered` |

**The JS default base URL is page-relative**, matching reqoach's existing frontend,
so the same code works at `/` locally and `/reqoach/` behind nginx. Pass `baseUrl`
only for cross-origin use.

**Uploads use the field name `files` (plural).** `file` returns 422.

### The review helper

`loadReview(pid, run)` / `load_review(...)` joins the two files nobody joins
server-side — the run's `scorecard.json` and its `review.json` — into the
per-requirement view a review UI needs. (`/package` performs the same join but is
shaped for the Architect, not for editing.) Each requirement carries the flags a
reviewer must not miss:

| flag | meaning |
|---|---|
| `belowThreshold` | fails the absolute quality floor — blocks release |
| `generated` | **analyst-authored to fill a coverage gap — no stakeholder wrote it**; `ratified` must become true |
| `incompletelyJudged` | scored on fewer than all nine judges |
| `textChanged` | refinement rewrote it; `originalText` is retained for drift audit |

Plus `counts` for a header summary.

### Following long runs

```js
const { job_id } = await analyst.runQuality(pid);
const job = await analyst.waitForJob(job_id, { onProgress: j => render(j.progress) });
```
`waitForJob` polls and always works. `streamJob(jobId, handlers)` uses socket.io
for per-item events when the socket.io client is on the page; `assess(text, handlers)`
streams a live single-requirement score for an editor pane.

Neither raises on `status: "error"` or `"cancelled"` — those are outcomes to render,
not exceptions.
