# Analyst Agent Technical Architecture

## 1. Purpose
The Analyst Agent turns raw stakeholder material (specifications, notes, a one-line request)
into a **validated, INCOSE-compliant requirements set** for a given problem. It ingests source
documents, extracts discrete requirements, scores them against the INCOSE characteristics and
writing rules, **autonomously refines** those below the acceptance threshold, checks the set for
coverage gaps, classifies each requirement, and — after human sign-off — releases the set for the
Architect Agent to design against.

It is the first stage of the agent chain: **Analyst → Architect → Planner**.

## 2. Inputs
- Source documents (PDF, DOCX, Markdown) for a project
- Optional free-text problem/user request
- Project configuration: acceptance threshold, coverage profile

## 3. Outputs
- **Validated requirements document** (structured JSON, plus Markdown rendering) — the contract
  consumed by the Architect Agent (§7)
- Per-requirement analysis record: INCOSE scores, rule findings, rationale, provenance, refinement
  history (original → final, with diffs)
- Problem statement (structured, provenance-graded)
- Coverage report: gaps, questions, confidence
- Reissued corrected specification document (content-complete replacement of the source)

## 4. Core Responsibilities
1. Ingest source documents to a structured item stream (text, headings, tables, page/bbox).
2. Segment the stream into discrete candidate requirements; gate out non-requirements.
3. Frame the problem (structured problem statement) from the material.
4. Score every requirement: 9 INCOSE characteristics (C1–C9) + deterministic writing rules.
5. **Refine** requirements below the acceptance threshold in a bounded loop until they pass
   or are escalated (§6) — the agent's defining capability.
6. Analyse **coverage**: is the set *enough* for the framed problem (gaps, not a % score).
7. **Classify** each requirement: `type` + `constraints[]` (Architect contract).
8. Enforce the **release gate** and publish the validated set (§8).
9. Serve all of the above over an API consumed by reqoach (UI) and the Architect Agent.

## 5. Architecture Overview

### 5.1 High-Level Components
- **Ingestion Client** — the Analyst does **not** parse documents itself. It calls the shared
  **ingestion-server** (`:8700`) using its **`structural`** strategy, which returns an ordered
  per-item block stream (`text`, `block_type`, `section_path`, `page_no`, `bbox`, `regions`,
  `order`) rather than retrieval chunks. The full stream is retained (needed for reissue).
  *Prerequisite: `structural` mode must be added to ingestion-server — see §9.2.*
- **Segmentation Module** — chunk → identify → assemble → dedup → **gate** (accept/reject).
- **Problem Framing Module** — structured, provenance-graded problem statement.
- **Scoring Module** — per-requirement C1–C9 judges (batch=1) + deterministic rule checker
  + set-level checks (C10–C15, overlaps).
- **Refinement Engine** — the bounded improve→re-score loop (§6).
- **Coverage Module** — domain-judge panel over a catalog of archetypes/standards → gaps.
- **Classification Module** — assigns `type` and extracts `constraints[]`.
- **Release Manager** — evaluates the gate, manages `proposed → validated` state, emits the
  Architect contract package.
- **Reissue Module** — reconstructs a content-complete corrected specification document.
- **API + Streaming Layer** — REST + socket.io (progress, live single-requirement assessment).
- **Store** — filesystem-backed project/run/analysis state (authoritative; reqoach holds none).

### 5.2 Data Flow
1. Documents → Ingestion → Segmentation → candidate requirements
2. Framing → problem statement
3. Scoring → per-requirement INCOSE scores + rule findings
4. **Refinement loop** → requirements at/above threshold, or escalated
5. Coverage → gaps against the framed problem
6. Classification → `type`, `constraints[]`
7. Release gate + human sign-off → **validated set** → **Architect Agent**

## 6. The Refinement Loop (defining capability)

For every requirement scoring below the **acceptance threshold**:

```
attempt = 0
while score < threshold and attempt < MAX_ATTEMPTS:      # MAX_ATTEMPTS default 3
    proposal = reviewer(requirement, scores, rule_findings)   # rewrite + rationale
    new_score = score(proposal)
    if new_score <= best_score:        # no improvement -> stop early
        break
    best = proposal; best_score = new_score
    attempt += 1
```

**Rules**
- **Threshold is a per-requirement floor** — *every* requirement must clear it (not an average).
- **Bounded**: at most `MAX_ATTEMPTS` passes; stop early when a pass fails to improve.
- **Keep best**: the highest-scoring version is retained even if the threshold isn't reached.
- **Escalate**: a requirement still below threshold is marked `needs_human` and surfaced in
  reqoach for manual correction. Nothing is silently dropped.
- **Meaning preservation (critical)**: the original text is immutable; every rewrite is recorded
  as a diff with the rationale and the score before/after. A requirement can score well and still
  have drifted in meaning — this is why human sign-off is mandatory (§8).

## 7. Output Contract (Architect Agent)

Per requirement (the Architect's documented input shape, plus analyst provenance):

```json
{
  "id": "REQ-0005",
  "text": "The system shall allocate GPUs fairly across concurrent sessions.",
  "type": "functional",
  "constraints": ["fairness", "resource"],
  "analysis": {
    "score": 4.6,
    "characteristics": { "C1": 5, "C3": 4, "...": 0 },
    "rules_triggered": ["R5"],
    "status": "accepted_refined",
    "original_text": "GPUs must be shared fairly.",
    "refinement": { "attempts": 2, "score_before": 2.4, "score_after": 4.6 }
  },
  "provenance": { "source_document": "srs.pdf", "section_path": "3) Resources", "page": 9 }
}
```

Package: `requirements.json` (above, as a list) + `requirements.md` (rendered) +
`problem_statement.json` + `coverage.json` + `manifest.json` (project, run, threshold,
release status, timestamps, counts).

`type` ∈ `functional | performance | safety | security | usability | reliability |
interface | data | operational | constraint | other`.

## 8. Release Gate

A requirement set is released to the Architect only when **all** hold:
1. **Quality** — every requirement is at/above the acceptance threshold, *or* explicitly
   accepted by a human despite being below (escalations resolved).
2. **Coverage** — no unaccepted critical coverage gaps (the human may explicitly accept gaps).
3. **Human sign-off** — a reviewer approves the set in reqoach, having seen the rewrite diffs
   and the escalated cases.

State machine: `draft → refined → proposed → validated` (`validated` = architect-consumable).
The Analyst never self-promotes to `validated`.

## 9. Integration

### 9.1 reqoach (UI) — thin BFF
reqoach keeps only the frontend plus a thin server that serves the static pages and proxies
`/api/*` and `/socket.io` to the Analyst. One origin (no CORS); the Analyst stays an internal
service. reqoach holds **no analysis data** — it is a consumer, like the Architect.

### 9.2 Architect Agent
Consumes the released package (§7). Contract is versioned; the Analyst guarantees `id`, `text`,
`type`, `constraints[]` for every released requirement.

### 9.2 ingestion-server (shared, `:8700`) — **requires a new `structural` strategy**
The Analyst is a *client* of the shared ingestion agent; it ships no Docling of its own.

ingestion-server today chunks for **retrieval** (Docling `HybridChunker(max_tokens)`, which
merges adjacent items across page breaks and bags whole lists into one chunk). That is the
**opposite** of what requirement segmentation needs: a retrieval chunk destroys the 1:1 block
structure that discrete-requirement identification, table-aware routing, and the content-complete
reissue all depend on.

**Required addition** (small, and it makes the shared agent more generic — structure *and*
retrieval): a `structural` strategy alongside `pdf_docling | markdown_render | plain_text` that
walks `DoclingDocument.iterate_items()` and emits **un-merged per-item blocks** with a normalized
`block_type` (from `DocItemLabel`) plus `page_no`/`bbox`/`regions`. This is the existing reqoach
Docling adapter, donated upstream. Its `bbox` convention (`[x0,y0,x1,y1]`, PDF space, bottom-left
origin) already matches what the Analyst and the reqoach PDF highlight view expect.

### 9.3 agent_server (shared, `:7701`)
All LLM work runs through the shared agent_server via named presets (`incose_*`,
`incose_reviewer`, `problem_framing`, `coverage_judge`, …). The Analyst adds a **classification**
preset (`type` + `constraints[]`). No external network calls; on-prem only.

### 9.4 embeddings / reranker (shared)
`EMBEDDINGS_URL` (default `:8601`) is a **live** dependency, not optional: `segment/dedup.py`
reranks summary-vs-detail candidates and `score/setlevel.py` performs reranker-based overlap
detection.

## 10. Non-Goals
- No UI. The Analyst is a headless service.
- No architecture/design output — that is the Architect's role.
- No in-place editing of the original source PDF; the reissue is a regenerated document
  (content-complete, formatting not replicated).
- No autonomous release: human sign-off is mandatory.
