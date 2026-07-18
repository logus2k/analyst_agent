# Analyst Agent — Implementation Specification

Companion to `technical_architecture.md`. Defines the concrete migration out of
`~/env/labs/requirements` (reqoach), the service surface, and the build order.

## 1. Repository Layout

```
analyst_agent/
  documents/            technical_architecture.md, implementation.md
  src/analyst/
    ingest/             ingestion-server client + markdown path + SourceItem model
    segment/            chunker, identify, assemble, dedup, gate, pipeline, prompts, verify
    score/              characteristics (C1–C9), deterministic rules, set-level
    refine/             the bounded refinement loop  (NEW)
    classify/           type + constraints extraction  (NEW)
    coverage/           domain-judge panel
    framing/            problem statement
    release/            gate + proposed→validated + Architect package  (NEW)
    reissue/            content-complete document reconstruction  (NEW)
    llm/                agent_server client + rerank client
    store/              filesystem state (projects, runs, analyses, review)
    api.py              REST + socket.io
  knowledge/
    incose/catalog.json     rule catalog (runtime)
    catalog/                coverage domains, project_types, standards
  Dockerfile  docker-compose.yml  requirements.txt  README.md
```

## 2. Migration Inventory (verified against the reqoach tree)

### 2.1 Moves to the Analyst
| From reqoach | Notes |
|---|---|
| `src/reqqa/segment/*` | chunker, identify, assemble, dedup, gate, pipeline, prompts, verify, model |
| `src/reqqa/score/*` | characteristics, deterministic, setlevel |
| `src/reqqa/assess.py` | single-requirement engine (live assessor + refinement scoring) |
| `src/reqqa/jobs.py` | pipeline orchestration (ingest→segment→gate→score→review) |
| `src/reqqa/framing.py`, `coverage.py` | problem statement + coverage panel |
| `src/reqqa/projects.py` | project/document/run/review state → becomes `store/` |
| `src/reqqa/llm/*` | agent_server client + `retrieval.rerank` |
| `src/reqqa/ingest/{model,markdown,dispatch}.py` | SourceItem model + Markdown path + dispatch |
| `incose/catalog.json`, `catalog/` | ~600 KB knowledge assets |
| `store/` | existing project data |

### 2.2 Stays in reqoach
All of `frontend/` (dashboard, Live Editor, coverage, overview, documents, projects, review)
plus a **thin BFF** server (static + proxy).

### 2.3 Split, not moved
`src/reqqa/orchestration_api.py` (763 lines) fuses three concerns:
- job manager + analysis REST + socket.io → **Analyst** `api.py`
- static serving + UI routes → **reqoach BFF**
- rules-metadata endpoint (`incose/catalog.json`) → **Analyst** (BFF proxies it)

### 2.4 Deleted / not migrated
- `src/reqqa/ingest/docling_adapter.py` → **donated to ingestion-server** as the `structural`
  strategy (§6); the Analyst keeps no Docling.
- `data/ecser` (773 MB), `data/req2019`, `data/srdataset`, `data/msr`, `data/incose` —
  **zero code references** (verified). Research leftovers; left in place, retrievable later.
- `models/docling` (1.2 GB) — belongs to the ingestion service, not the Analyst.
- `Dockerfile` (ingest service) + the `ingest` compose service — superseded by ingestion-server.

## 3. Dependencies (verified, exact)

**Python:** `fastapi`, `uvicorn[standard]`, `python-multipart`, `httpx`, `markdown-it-py`,
`python-socketio`. **No docling** (delegated over HTTP; existing imports are already lazy).

**Services (three):**
| Service | Default | Used by |
|---|---|---|
| agent_server | `:7701` | every LLM preset (judges, reviewer, framing, coverage, classify) |
| ingestion-server | `:8700` | document parsing (`structural` strategy) |
| embeddings / reranker | `:8601` | `segment.dedup`, `score.setlevel` overlap detection |

**Env:** `AGENT_SERVER_URL`, `INGESTION_URL`, `EMBEDDINGS_URL`, `RERANK_MODEL_NAME`,
`ANALYST_STORE`, `ANALYST_KNOWLEDGE`.

## 4. Service Surface

### 4.1 REST (consumed by the reqoach BFF and the Architect)
```
POST   /projects                                  create
GET    /projects | /projects/{pid}                list / detail
POST   /projects/{pid}/documents                  upload (delegates parsing to :8700)
POST   /projects/{pid}/analysis:run               ingest→segment→gate→score  (async job)
POST   /projects/{pid}/refine:run                 the refinement loop        (async job)
POST   /projects/{pid}/coverage:run               coverage panel             (async job)
POST   /projects/{pid}/framing:run                problem statement          (async job)
GET    /projects/{pid}/requirements               current set (+ scores, status, history)
PUT    /projects/{pid}/requirements/{rid}         human correction / accept / escalate resolve
GET/PUT /projects/{pid}/threshold                 acceptance threshold
GET    /projects/{pid}/release                    gate status (quality, coverage, sign-off)
POST   /projects/{pid}/release:approve            human sign-off → validated
GET    /projects/{pid}/release/package            Architect contract package (§7 of arch doc)
GET    /projects/{pid}/reissue?format=md|pdf      corrected specification document
GET    /jobs/{id} | POST /jobs/{id}/cancel        progress / abort
GET    /rules                                     INCOSE rule metadata (for the UI)
```

### 4.2 socket.io
- `assess {text}` → `start / deterministic / characteristic / review / done` — live
  single-requirement assessment (Live Editor + the refinement loop's scoring).
- job progress events (`stage`, `requirement`, `domain`, `job_done`, `job_cancelled`).

**Hardening already applied upstream and to be carried over:** judges/reviewer must never raise,
and the handler must always emit a terminal `done` — otherwise a transient LLM error strands the
client with no score.

## 5. Refinement Loop (`refine/`)

```python
MAX_ATTEMPTS = 3
for req in requirements_below_threshold:
    best, best_score = req.text, req.score
    for _ in range(MAX_ATTEMPTS):
        proposal = reviewer(best, scores, findings)      # incose_reviewer preset
        new_score = score(proposal)                      # same 9 judges
        if new_score <= best_score:
            break                                        # no improvement → stop early
        best, best_score = proposal, new_score
        if best_score >= threshold:
            break
    persist(req, final_text=best, score=best_score,
            status="accepted_refined" if best_score >= threshold else "needs_human",
            history=[...])                               # original immutable + per-attempt diffs
```
- Threshold is a **per-requirement floor**; the run is complete when every requirement is at or
  above it, or is marked `needs_human`.
- Parallelised with a bounded worker pool; emits progress + supports abort like other jobs.
- **Never mutates `original_text`.** Every attempt is recorded (text, score, rationale) so a
  human can audit semantic drift.

## 6. Upstream Work Item — ingestion-server `structural` strategy

Add to `~/env/assets/ingestion_server`:
- `Chunking.strategy` gains `"structural"`.
- New `chunking/structural.py`: walk `DoclingDocument.iterate_items()`, map `DocItemLabel` →
  normalized `block_type`, emit un-merged per-item blocks carrying `page_no`, `bbox`, `regions`,
  `section_path`, `order`. (Port of reqoach's `ingest/docling_adapter.py`.)
- No change to existing retrieval behaviour; `pdf_docling` remains the default.

## 7. reqoach Changes (thin BFF)
- Keep `frontend/` unchanged as far as possible.
- Server reduces to: serve static + proxy `/api/*` and `/socket.io` to the Analyst.
- Delete all analysis modules, `incose/`, `catalog/`, `store/`, the `ingest` service.
- Frontend calls become `/api/...`; one origin, so no CORS and the socket path is unchanged.

## 8. Build Order (each step leaves both sides working)

| Phase | Outcome |
|---|---|
| **P0** | Scaffold `analyst_agent` (repo, Dockerfile, compose, health), no logic |
| **P1** | Add `structural` mode to ingestion-server; verify bbox/block_type on a real PDF |
| **P2** | Move engine (ingest client, segment, score, assess, jobs, framing, coverage, llm, store) + knowledge; reproduce a full analysis run headlessly |
| **P3** | Analyst API + socket.io; reqoach switched to thin BFF; **UI works end-to-end unchanged** |
| **P4** | Refinement loop + threshold + escalation |
| **P5** | Classification (`type`, `constraints[]`) |
| **P6** | Release gate + human sign-off + Architect package |
| **P7** | Reissue (content-complete document) + server-side PDF |
| **P8** | Decommission the old reqoach backend + `ingest` service |

Review & Reissue **M1** (review state + endpoints, currently half-built in reqoach) is absorbed:
its state/endpoints land in P3–P4 (Analyst), its `review.html` stays in reqoach.

## 9. Acceptance
- A full analysis of a real SRS reproduces the current reqoach results (same requirement count,
  comparable scores) through the new service.
- Every released requirement carries `id, text, type, constraints[]` + analysis + provenance.
- The Architect Agent can consume the package without transformation.
- reqoach holds no analysis data and still renders dashboard, Live Editor, coverage, and review.

## 10. Open / Deferred
- Auth on write endpoints (deferred by decision; currently ungated).
- Custom export template, DOCX (M5-class work).
- Whether the Architect pulls the package via API or reads a published directory.
