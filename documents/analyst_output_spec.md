# Analyst Agent — Output Specification

**Purpose of this document.** A precise technical description of *what the Analyst Agent
produces*, for reconstructing or re-implementing the stage on a different LLM/toolchain. Every
schema and field below was verified against a live output package (`Restaurant Menu Manager`,
project `185d83e8…`) and against the assembler source (`src/analyst_agent/package.py`). Where a
field is model-generated vs. deterministic is called out, because that is what matters for a
transition.

---

## 1. Role and interface

- **Service:** `analyst-agent`, HTTP on **`:7803`** (contract `1.0`). Job-oriented API
  (`ProjectRunManager` creates runs; socket.io streams progress). SDKs:
  `sdk/js/analyst-client.js` and `sdk/python/analyst_client/`. Authoritative interface contract:
  `sdk/how_to.md`.
- **Position in the pipeline:** first stage of **Analyst → Architect → Planner (→ Builder)**.
  `req_id` is the trace key that every downstream stage joins on.
- **Input:** raw stakeholder material (PDF / DOCX / Markdown / one-line request).
- **Output:** a single **validated requirements package** — a scored, classified, traceable
  requirement set plus a project glossary, tag taxonomy, requirement tree, coverage analysis, and
  a release manifest.

The Analyst's defining behaviour is **bounded autonomous refinement**: a requirement scoring
below the acceptance threshold is rewritten and re-scored (≤3 attempts, stop on no improvement,
keep the best); if it still cannot clear the bar it is escalated as `needs_human` rather than
silently dropped or "fixed". Original text is immutable; every attempt is persisted.

---

## 2. Output artifacts (on disk)

Written to the project repo under `requirements/`:

| File | Type | Content |
|---|---|---|
| `package.json` | object | The complete validated package (all of §3). The authoritative deliverable. |
| `glossary.json` | array | The project glossary (also embedded in `package.json.glossary`). |
| `tags.json` | array | The cross-cutting tag taxonomy (also embedded in `package.json.tags`). |
| `tree.json` | object | The requirement tree: branches, nodes, unassigned (also embedded). |

`package.json` is self-contained (it embeds glossary/tags/tree); the standalone files are
convenience copies.

---

## 3. `package.json` — top-level schema

Eleven keys:

| Key | Type | Meaning |
|---|---|---|
| `manifest` | object | Release gating + provenance (§4). |
| `requirements` | array | The requirement records (§5). One per requirement. |
| `set_level` | object | Set-wide analysis, notably `overlaps` (§6). |
| `aggregates` | object | Roll-up statistics over the set (§7). |
| `characteristic_names` | object | The 9 INCOSE quality dimensions `C1..C9` → name (§5.2). |
| `problem_statement` | object | `{version, ratified, updated_at, statement}` — the ratified problem framing the set derives from. |
| `coverage` | object | Domain coverage + gap analysis (§8). |
| `coverage_profile` | object | Inferred solution archetypes (`web-saas`, `api`, …) with confidence + rationale. |
| `glossary` | array | Canonical entities/terms (§9). |
| `tags` | array | Cross-cutting concern taxonomy (§9). |
| `tree` | object | Requirement tree by branch (§9). |

---

## 4. `manifest` — release gating & provenance

```json
{
  "contract_version": "1.0",
  "generated_by": "analyst_agent",
  "project_id": "...", "project_name": "...", "run_id": "...",
  "threshold": 4.0,
  "release_status": "validated",
  "released_at": "...", "released_by": "...", "release_note": "",
  "architect_ready": false,
  "can_release": false,
  "hard_blockers": ["42 open coverage gap(s) must be closed before release (6 critical, 36 high)"],
  "blockers": [ ... ],
  "counts": {
    "total": 60, "excluded_duplicates": 0, "scored": 60,
    "at_or_above_threshold": 60, "below_threshold": 0,
    "incompletely_judged": 0, "with_placeholders": 0,
    "analyst_authored": 0, "unratified_authored": 0, "unclassified": 0,
    "mean_score": 4.54
  }
}
```

- `threshold` (default **4.0**) is the INCOSE acceptance bar on the per-requirement score.
- `architect_ready` / `can_release` are the **gates**: a package with open critical/high coverage
  gaps is `validated` in quality but **not** release-ready (`hard_blockers` explain why). The
  Architect should not consume a package that is not `architect_ready`.
- `counts` is the scoreboard the release decision is made from.

---

## 5. `requirements[]` — the requirement record

Produced by `package._requirement_record`. One object per requirement:

```json
{
  "req_id": "REQ-0001",
  "text": "The system shall generate a food description from a food image.",
  "classes": ["functional"],
  "type": "functional",
  "constraints": [],
  "classification_rationale": "…why it was classified this way…",
  "analysis": { … see §5.1 … },
  "answered_gaps": [],
  "lineage": null,
  "provenance": null
}
```

- `req_id` — stable trace key across the whole pipeline.
- `text` — the **current** (possibly refined) requirement statement.
- `classes` / `type` — classification (`functional`, `non-functional`, `constraint`, …) with
  `classification_rationale`.
- `constraints` — extracted constraint clauses, if any.
- `answered_gaps` — coverage gaps that were closed by authoring/answering this requirement.
- `lineage` / `provenance` — origin trace (source document, authored-vs-extracted).

### 5.1 `analysis` — the quality assessment (per requirement)

```json
{
  "score": 4.33,
  "score_before_refinement": 2.33,
  "characteristics": { "C1": {…}, … "C9": {…} },
  "characteristic_scores": { "C1": 3, "C2": 2, … },
  "rules_triggered": ["R31", "R7", …],
  "deterministic_findings": [ { "rule_id": "R7", … }, … ],
  "review": { …reviewer rewrites/advisories… },
  "judges_ok": 4, "judges_total": 5,
  "status": "accepted",
  "original_text": "…immutable first statement…",
  "text_changed": true,
  "refinement": { …per-attempt history if refined… }
}
```

- `score` — overall INCOSE quality (1–5), mean of the 9 characteristic scores; `score_before_refinement`
  shows the lift from autonomous refinement.
- `characteristics` — full objects (see §5.2); `characteristic_scores` is the flattened `{Cn: score}` view.
- `rules_triggered` / `deterministic_findings` — **deterministic** rule engine results (rule IDs `R1..R39`),
  independent of the LLM judges.
- `judges_ok` / `judges_total` — how many independent LLM judges accepted it (multi-judge consensus).
- `status` — `accepted` | `needs_human` | `rejected` | `unreviewed`.
- `original_text` + `text_changed` + `refinement` — the immutable original, whether it was rewritten,
  and the attempt history (each attempt's text + score), so drift is auditable.

### 5.2 The 9 INCOSE characteristics (`characteristic_names`)

Each `characteristics.Cn` is `{id, score (1–5), rules_triggered, evidence, justification}`.

| Id | Name | | Id | Name | | Id | Name |
|---|---|---|---|---|---|---|---|
| C1 | Necessary | | C4 | Complete | | C7 | Verifiable |
| C2 | Appropriate | | C5 | Singular | | C8 | Correct |
| C3 | Unambiguous | | C6 | Feasible | | C9 | Conforming |

`score` per characteristic is LLM-judge-produced; `rules_triggered` links it to the deterministic
rule findings that justify a low score. `justification` is the natural-language rationale.

---

## 6. `set_level` — cross-requirement analysis

```json
{ "overlaps": [ {"a_id": "REQ-0003", "b_id": "REQ-0044", "score": 1.0}, … ], … }
```

- `overlaps` — pairs of requirements found to be duplicates/near-duplicates, with a similarity
  `score` (reranker-based, sigmoid-scaled; `1.0` = exact duplicate). This is where the set-level
  deduplication signal lives. **Note:** overlaps are *reported*; they are not necessarily removed
  (`counts.excluded_duplicates` records how many were excluded).

---

## 7. `aggregates` — roll-up statistics

```json
{
  "per_characteristic_mean": { "C1": 2.83, … "C9": 1 },
  "per_rule_violation_count": { "R39": 60, "R1": 60, "R7": 58, … },
  "score_distribution": { "2": 49, "3": 10, "4": 1 }
}
```

- `per_characteristic_mean` — set-wide weakness profile (which INCOSE dimensions the set is weakest on).
- `per_rule_violation_count` — how many requirements each deterministic rule flagged.
- `score_distribution` — histogram of pre-/post-refinement scores.

---

## 8. `coverage` and `coverage_profile`

`coverage` = domain coverage + **gap analysis** against the problem statement:

```json
{
  "problem_statement_version": 5,
  "requirement_count": 60,
  "domains": [
    {
      "id": "...", "name": "...",
      "coverage": "partial",                    // full | partial | none
      "addressed": ["Public browsing capabilities", …],
      "gaps": [ {"title": "Error/Exception Contract for Operations",
                 "severity": "high",            // critical | high | medium | low
                 "detail": "…what is missing…"}, … ],
      "enrichments": [ … ], "confidence": "..."
    }, …
  ],
  "gaps": [ … set-level gaps … ],
  "summary": { … }, "enrichments": [ … ], "synthesis": { … }
}
```

- Gaps are the **missing requirements** the Analyst detected (e.g. unspecified error contracts,
  missing auth flows). Critical/high gaps are the `manifest.hard_blockers` that keep a package from
  becoming `architect_ready`.

`coverage_profile` classifies the *kind of system* being built:

```json
{ "version": 4, "updated_at": "...",
  "profile": { "archetypes": [ {"id": "web-saas", "confidence": "high", "why": "…"}, {"id": "api", …} ] } }
```

---

## 9. Glossary, tags, tree

**`glossary`** — canonical entities/terms of the domain. Each entry:

```json
{ "name": "AIOperation", "definition": "…", "aliases": [], "req_ids": ["REQ-0020"] }
```

Fields: `name`, `definition`, `aliases[]`, `req_ids[]` (requirements that reference the term).
**There is no `kind` field** (no actor/entity/value tagging in the produced glossary) — consumers
that assume one must infer it or add it.

**`tags`** — cross-cutting concern taxonomy. Each entry:

```json
{ "name": "ai", "description": "", "req_ids": ["REQ-0001", "REQ-0020", …] }
```

**`tree`** — requirement grouping by feature branch:

```json
{
  "branches": [ … ],
  "nodes": [ { "req_id": "REQ-0001", "branch": "AI Content Generation", "tags": ["ai","vision"] }, … ],
  "unassigned": [ … ]
}
```

`nodes[].branch` is the aspect/feature grouping the **Architect** uses to design per-aspect;
`nodes[].tags` join to the tag taxonomy. Requirements not placed in a branch are in `unassigned`.

---

## 10. Deterministic vs. model-generated (important for a transition)

| Element | Source |
|---|---|
| `req_id`, `text` extraction, `original_text`, `refinement` history, `manifest`, `aggregates`, `counts`, `set_level.overlaps` scoring | **Deterministic** (code + reranker) |
| `deterministic_findings` / `rules_triggered` (`R1..R39`) | **Deterministic** rule engine |
| `characteristics[].score` and `justification`, `classification`, `coverage.gaps`, `coverage_profile`, refined `text`, glossary/tag naming | **LLM-generated** (judges/authoring) |

For a model swap: the **structure, scoring math, rule engine, dedup, and release gating are
deterministic** and portable unchanged. What is model-dependent is the *quality of judgement*
(characteristic scores, gap detection, refinement rewrites, classification, glossary/branch
naming) — i.e. the substance the new model must reproduce. The output **contract** (§3–§9) is what
the Architect consumes and must be preserved exactly (`contract_version` `1.0`); the join key is
`req_id`, and `tree.nodes[].branch` is the unit the Architect designs against.

---

## 11. Consumption contract (what the Architect reads)

The Architect (`Analyst → Architect`) consumes, per the chain:
- `requirements[]` (`req_id`, `text`, `type`, `constraints`) — the statements to realize.
- `tree.nodes[].branch` — the aspect grouping it designs per-branch.
- `glossary` — canonical entity names it must use verbatim as components.
- `tags` — cross-cutting concerns.
- `manifest.architect_ready` — the gate; do not design from a non-ready package.

A package that is `release_status: "validated"` but `architect_ready: false` has open coverage
gaps (`hard_blockers`) and is **quality-scored but not complete** — the coverage gaps are the known
missing requirements, and they propagate as missing capability downstream if the package is used
as-is.
