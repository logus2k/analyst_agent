# Analyst Agent — Migration Plan

Operational companion to `technical_architecture.md` / `implementation.md`.
Source: `~/env/labs/requirements` (reqoach). Target: `~/env/assets/analyst_agent` (:7803).

## Principles

1. **reqoach keeps working at every step.** Nothing is deleted from reqoach until the Analyst
   demonstrably replaces it (P8). P0–P2 don't touch reqoach at all.
2. **Every phase has a verification gate.** A phase is done only when its check passes against
   live services — not when the code "looks right".
3. **Copy, then delete.** Modules are copied into the Analyst and proven there; the reqoach
   originals are removed only in P8.
4. **No data loss.** `store/` is copied, not moved, until P8.
5. **Ports:** Analyst `:7803` (verified free). Shared: agent_server `:7701`,
   ingestion-server `:8700`, embeddings/reranker `:8601`.

## Phase Gates

| Phase | Goal | Verification gate |
|---|---|---|
| **P0** | Scaffold the service | container builds; `GET /health` → 200 |
| **P1** | `structural` mode in ingestion-server | real PDF → un-merged blocks w/ `block_type` + `page_no`/`bbox`; block count comparable to reqoach's adapter |
| **P2** | Engine migrated | full analysis of a real SRS reproduces reqoach's requirement count + comparable scores |
| **P3** | API + reqoach thin BFF | dashboard, Live Editor, coverage, review all work through the Analyst, UI unchanged |
| **P4** | Refinement loop | a below-threshold set converges or escalates; originals immutable; abort works |
| **P5** | Classification | every requirement carries `type` + `constraints[]` |
| **P6** | Release gate | `proposed → validated` only after quality+coverage+sign-off; package emitted |
| **P7** | Reissue | content-complete corrected doc + server-side PDF |
| **P8** | Decommission | reqoach holds no analysis code/data; old `ingest` service removed |

---

## P0 — Scaffold  *(no reqoach impact)*
1. Repo layout mirroring `ingestion_server`: `Dockerfile`, `compose/docker-compose.yml`,
   `requirements.txt`, `README.md`, `src/analyst_agent/`, `documents/`.
2. Minimal FastAPI app: `GET /health`, `GET /version`.
3. Deps: fastapi, uvicorn[standard], python-multipart, httpx, markdown-it-py, python-socketio.
4. **Gate:** `docker compose build && up -d` → `curl :7803/health` = 200.

## P1 — `structural` strategy in ingestion-server  *(other repo)*
1. `Chunking.strategy` gains `"structural"`.
2. `chunking/structural.py`: walk `DoclingDocument.iterate_items()`; map `DocItemLabel` →
   normalized `block_type`; emit **un-merged** per-item blocks with `page_no`, `bbox`, `regions`,
   `section_path`, `order`. (Port of reqoach `ingest/docling_adapter.py`; do not touch
   `pdf_docling`.)
3. **Gate:** parse a real SRS PDF via `:8700` with `strategy=structural`; assert
   (a) blocks are not merged (count ≫ retrieval-chunk count),
   (b) `block_type` distinguishes tables/headings,
   (c) `bbox` is `[x0,y0,x1,y1]` PDF-space and lands on the right text when highlighted.

## P2 — Engine migration  *(copy; reqoach untouched)*
Copy into `src/analyst_agent/`, rewriting imports `reqqa.*` → `analyst_agent.*`:
`ingest/{model,markdown,dispatch}` (+ new ingestion-server client, **no docling**),
`segment/*`, `score/*`, `assess.py`, `jobs.py`, `framing.py`, `coverage.py`,
`projects.py`→`store/`, `llm/*`. Knowledge: `incose/catalog.json`, `catalog/` → `knowledge/`.
- **Gate:** headless script runs ingest→segment→gate→score on a real SRS and produces a
  scorecard with a requirement count and per-characteristic means comparable to reqoach's
  existing run for the same document.

## P3 — API + reqoach becomes a thin BFF
1. Analyst `api.py`: REST (§4.1 of implementation.md) + socket.io (`assess`, job events),
   carrying over the terminal-`done` hardening.
2. reqoach: strip analysis routes; keep static serving + proxy `/api/*` and `/socket.io` → :7803.
3. **Gate:** dashboard, Live Editor (live score appears), coverage, overview, review all work
   with the UI unchanged; reqoach process holds no analysis state.

## P4 — Refinement loop
`refine/`: ≤3 attempts, stop on no improvement, keep best, `needs_human` escalation; parallel
with progress + abort; originals immutable with per-attempt diffs.
- **Gate:** a set with known-bad requirements converges above threshold or escalates; no
  `original_text` mutated; abort mid-loop leaves consistent state.

## P5 — Classification
`classify/`: new agent_server preset → `type` + `constraints[]` per requirement.
- **Gate:** 100% of requirements in a real run carry a valid `type` and a `constraints[]` array.

## P6 — Release gate + Architect package
`release/`: gate (quality floor + coverage sign-off + human approval), state machine
`draft→refined→proposed→validated`, package emitter.
- **Gate:** release blocked while any requirement is below threshold or a critical coverage gap is
  unaccepted; after approval, `GET …/release/package` returns the contract shape.

## P7 — Reissue
`reissue/`: reconstruct the full document (headings, prose, tables in order) substituting
corrected text; server-side PDF (engine choice pending; WeasyPrint recommended).
- **Gate:** exported doc contains all source sections + corrected requirements; PDF downloads.

## P8 — Decommission
Delete analysis code/knowledge/store from reqoach; remove the `ingest` service + `models/docling`
from that repo; reqoach = frontend + BFF only.
- **Gate:** reqoach repo has no `src/reqqa` analysis modules; full UI still works.

---

## Risks
- **Segmentation regression from `structural`** — mitigated by the P1 gate comparing against the
  current adapter before any engine migration.
- **Score drift** (LLM non-determinism) — P2 compares *distributions*, not exact values.
- **Long-running jobs during cutover** — cut over when no run is active.
- **Auth** — write endpoints remain ungated (explicitly deferred by the user).

## Status log
- 2026-07-18 — plan written.
- 2026-07-18 — **P0 DONE (gate passed).** `analyst_agent` scaffolded on **:7803**; image
  `analyst-agent:0.1.0` builds; `GET /health` → 200; `GET /dependencies` reports all three shared
  services reachable (agent_server :7701, ingestion-server :8700, embeddings :8601). reqoach
  unaffected (:7802 → 200).
- 2026-07-18 — **P1 DONE (gate passed).** `structural` strategy added to ingestion-server
  (`chunking/structural.py`, registered in `get_chunker`, added to the `Chunking.strategy`
  Literal). Verified on `IEEE29148-srs_example.pdf`: **198 blocks, identical to reqoach's adapter**
  (same count, same kinds {heading 48, paragraph 126, table 1, list_item 23}, 198/198 with
  page+bbox, identical sample bbox `[106.7, 571.57, 547.40476, 681.73]` p1) and **2.75× the
  retrieval chunker's 72 chunks**, confirming un-merged. Image rebuilt; `/docs` → 200; strategy
  resolves from the rebuilt image. **Segmentation parity is proven — safe to migrate the engine.**
- 2026-07-18 — **P1 addendum: stateless parse endpoint.** Runs are corpus ingestion (a pipeline
  needs corpus+chunking+types+index+steps and `validate_semantics()` rejects a steps-less one),
  so they are the wrong tool for "parse this file into blocks". Added
  **`POST /v1/parse`** to ingestion-server: multipart upload + `strategy` (+`target_tokens`),
  returns `{name, strategy, count, blocks[]}`. Stateless — no corpus, no run, nothing persisted —
  and upload-based, so callers need no shared filesystem. Parsing runs via `asyncio.to_thread`
  (model-heavy, must not block the loop).
  **Verified over HTTP:** `POST /v1/parse?strategy=structural` on IEEE29148-srs_example.pdf →
  198 blocks, kinds {heading 48, paragraph 126, table 1, list_item 23}, 198/198 with page+bbox,
  bbox identical to reqoach's adapter, ~11 s.
  *Incident (resolved):* the first rebuild crashed the service — `UploadFile` needs
  `python-multipart`, and this image installs pins **inline in the Dockerfile**, not from
  `requirements.txt` (that file is not used by the build). Added the pin to the Dockerfile,
  reverted the misleading requirements.txt edit, rebuilt; `/v1/healthz` → 200.
- 2026-07-18 — **P2 DONE (gate passed).** Engine copied into `src/analyst_agent/` with
  `reqqa.*`→`analyst_agent.*` rewritten (segment/, score/, llm/, ingest/{model,markdown,dispatch},
  assess.py, jobs.py, framing.py, coverage.py, projects.py→**store.py**). NOT copied:
  `docling_adapter.py` (replaced), `orchestration_api.py`/`realtime.py`/`api.py` (P3).
  Knowledge (336 KB) → `knowledge/{incose/catalog.json, catalog/}`; path constants rewired to
  `config.KNOWLEDGE` / `config.STORE` (no `REQQA_*` left). New
  `ingest/ingestion_client.py` → `POST :8700/v1/parse?strategy=structural`, mapping
  `blocks[]`→`SourceItem` (`kind`→`block_type` — same vocabulary, no table needed;
  `page_no`→`page`, `index`→`order`); `INGEST_URL`/`:5601` gone.
  **Gates:** (a) engine imports in-container, knowledge loads (16 domains / 20 archetypes /
  6 standards); (b) `_ingest` on a real PDF via ingestion-server → **198 items, block types and
  bbox identical to reqoach**; (c) full pipeline (ingest→segment→gate→score→review→set-level) on
  the same doc in **both** engines → identical stages, 3 requirements, identical score
  distribution `{4:3}` and sample score; per-characteristic means identical except C2/C8
  (4.33 vs 4.0) — single-judge LLM non-determinism, within expected variance.
  reqoach untouched and still healthy (:7802 → 200).
- 2026-07-18 — **P3a DONE (Analyst API live).** `orchestration_api.py` ported to
  `analyst_agent/api.py`: static-serving layer stripped (`StaticFiles`, `_FRONTEND`, the `/` mount),
  paths rewired to `config.STORE`/`config.KNOWLEDGE`, `projects`→`store as pj`, `__version__`/
  `config`/`httpx` imports added, entrypoint switched to `analyst_agent.api:asgi` (socket.io ASGI).
  **34 analysis routes + socket.io** now serve on :7803.
  **Verified:** all endpoints 200 incl. `/socket.io/?EIO=4&transport=polling`; `/rules` → 42 rules;
  `/catalog/domains` → 16; and a **full project flow through the API** — create → upload →
  `quality:run` → job progressed `segment → score(0/27→2/3) → review → set_level → done` →
  scorecard (3 requirements, dist `{4:3}`, per-char means matching reqoach). Smoke project deleted.
  reqoach untouched (:7802 → 200, its 3 real projects intact).
  *Two boot failures found and fixed en route: `from analyst_agent import projects as pj` (module is
  `store.py`) and a missing `config` import — both from combined-import assumptions in the rewrite.*
- 2026-07-18 — **P3b PENDING (reqoach → thin BFF).** Blocked on one decision: how the browser
  reaches the analyst's **socket.io** once reqoach stops serving it. The frontend uses
  `io({path: _base + "socket.io"})` (same-origin), so HTTP paths proxy trivially but the
  **WebSocket upgrade does not** proxy through a plain httpx forwarder. See §Open decision below.

- 2026-07-18 — **All migrated analyst paths exercised** (not merely importable): socket.io `assess`
  streams (9 characteristics fast-lane, review, `done overall 2.78`); `framing:run`; `quality:run`;
  review seed/upsert/threshold; `documents/{did}/source`; **`coverage:run` → 16 domains, 67 gaps,
  31 enrichments, synthesis `{partial, medium}`** (also proves the long-unverified Phase-5
  synthesis). ingestion-server regression-checked: healthz/layers/runs/openapi all 200.
- 2026-07-18 — **P4 DONE (gate passed).** `refine.py` + `_run_refine`/`create_refine_run` +
  `POST /projects/{pid}/refine:run`; `store.upsert_req_review` persists `refinement`.
  Gate on 3 weak requirements @ threshold 4.3: **1.11→3.56, 2.0→3.44, 3.11→3.67**, all escalated
  `needs_human`; originals immutable, per-attempt history persisted, stop-on-no-improvement fired.
  **Finding:** proposals converge to *parameterized* text (`[specify maximum response time]`) and
  plateau ~3.5 because the missing value is external to the document — escalation is the loop
  correctly demanding human input, validating the cap + `needs_human` design.

- 2026-07-18 — **P3b DONE (gate passed) — cutover live.** Decision: **option C**.
  Store migrated (3 real projects, verified intact on the Analyst). New `src/reqqa/bff.py` serves
  the frontend and proxies `projects|jobs|rules|catalog|documents|socket.io` → :7803 (prefix routes
  registered before the `/` static mount). compose `command:` switched to `reqqa.bff:app` —
  **rollback = remove that line**, the old app is still in the image. nginx
  `/reqoach/socket.io/` → **:7803** (real WebSocket deployed; polling proxied locally), so the
  frontend is unchanged in both environments.
  **Two bugs only a real browser caught:** forwarding `content-encoding: gzip` while httpx had
  already decompressed → `ERR_CONTENT_DECODING_FAILED` broke every API call; and the WS upgrade
  attempt against the BFF returned 500. Fixed by dropping `content-encoding` from proxied response
  headers and rewriting the handshake to advertise `"upgrades":[]`.
  **Gate:** Live Editor 2.67 + 9/9 characteristics + connected; 3 projects listed; dashboard 386
  rows / 3.69 / 88%; overview 4/4 Done; **no console errors**. Public site 200, public socket.io
  401 (expected auth gate).

- **P5 — classification** ✅ DONE (2026-07-18). `incose_classifier` preset (prompt +
  `.agent.json` in `agent_server/data/`, registered after a container restart) +
  `src/analyst_agent/classify.py` + `_run_classify`/`create_classify_run` +
  `POST /projects/{pid}/classify:run` (analyst now 31 routes). `store.upsert_req_review` accepts
  `classification` — and no longer forges `reviewed_at` for machine-only patches.
  **Contract conflict resolved by emitting BOTH labels — see below.**
  **Gate (NIST SRS run, 85 real requirements):** 85/85 classified · 0 missing · 0 invalid `type` ·
  0 invalid `classes` · 0 out-of-vocabulary `constraints` · 0 LLM errors · 0 forged `reviewed_at` ·
  requirement text unchanged. 28/85 carry >1 routing class. socket.io stream verified end-to-end:
  85 `classified` events + `classify` start/done stages + `classify_summary` + `job_done`.
  ⚠️ **Not bit-reproducible despite `temperature: 0.0`** — a second run over the same 85
  requirements moved ~7 labels (`functional` 30→29, `other` 4→6, `data` 3→2, one `safety`→
  `reliability`). Routing `classes[]` was the more stable of the two. Re-running classification
  will therefore perturb labels; if reproducibility is required for release, freeze the
  classification with the release package rather than recomputing it.

#### P5 decision — `classes[]` AND `type` (two contract docs disagreed)
`analyst_agent/.../technical_architecture.md` §7 specifies a **single-label** `type` (11 values).
`architect_agent/.../implementation.md` §2.1.1 states the Analyst supplies **no** `type`, that the
Architect classifies itself, and that routing is **multi-label** over 6 classes — arguing correctly
that a single label "silently drops the timing budget" (e.g. *allocate GPUs fairly within 100 ms*
is `functional` **and** `constraint`). A single label cannot drive Architect routing; the routing
labels are too coarse for reporting. **Both are emitted:** `classes[]` = machine/routing contract,
`type` = reporting/UI, `constraints[]` = closed vocabulary (23 terms; out-of-vocab discarded).
The Architect can now delete its own classification step — §2.1.1 needs updating to match.

- **P8 — decommission** ✅ DONE (2026-07-18), pulled ahead of P6/P7 at the user's request.
  Deleted from reqoach: the whole old backend (`orchestration_api.py`, `api.py`, `assess.py`,
  `jobs.py`, `projects.py`, `realtime.py`, `coverage.py`, `framing.py`, `score/`, `segment/`,
  `llm/`, `ingest/` — 33 files, ~4 059 lines); `src/reqqa/` now holds **only** `bff.py` +
  `__init__.py`. `Dockerfile.orchestration` slimmed (deps: fastapi, uvicorn, httpx — dropped
  python-socketio, python-multipart, markdown-it-py; no longer COPYs `incose/`; CMD is now the
  BFF, so the rollback escape hatch is **gone by design**). `compose.yaml`: the `ingest` service
  and the `store`/`catalog` mounts removed; only `frontend/data` remains.
  **The stale store copy is deleted** — proven a strict subset first (`diff -rq`: nothing only in
  reqoach, 0 differing files, analyst strictly ahead by the `reviews/` dir); backed up anyway to
  `scratchpad/reqoach_store_backup_20260718.tgz` (7.0 MB, 92 entries, verified readable).
  Root-owned files needed `docker run --rm -v …:/w alpine rm -rf /w/store`.
  Old `requirements-ingest-1` (:5601) container removed; nothing referenced `INGEST_URL`/5601.
  **Gate (real browser, not curl):** dashboard 386 rows + score rendered, project switcher lists
  all 3 projects, editor loads, `200` on `/rules` + `/quality` + `/quality/scorecard`,
  **0 console errors, 0 failed requests**. Image 177 MB.

- **P6a — Architect handover package** ✅ DONE (2026-07-18). `src/analyst_agent/package.py` +
  `GET /projects/{pid}/package?run=&format=json|md` (analyst now 32 routes). Joins scorecard ×
  review/classification into one document: `manifest` (contract_version, release_status,
  `architect_ready`, `blockers`, counts, below_threshold_ids, source_documents) + `requirements[]`
  + `set_level` + `aggregates` + `characteristic_names` + `problem_statement` + `coverage` +
  `coverage_profile`. Per requirement: `req_id`, `text`, `classes[]`, `type`, `constraints[]`,
  `classification_rationale`, `analysis{score, score_before_refinement, characteristics (FULL
  objects), characteristic_scores, rules_triggered, deterministic_findings, review, status,
  original_text, text_changed, refinement}`, `lineage`, `provenance`.
  Design rule agreed with the user: **deliver more, not less** — the Architect can ignore a field,
  it cannot invent a dropped one. Duplicates (`lineage.duplicate_of`) are excluded.
  **Key is `req_id`** (Architect's documented trace key); the Analyst's §7 `id` example is wrong
  and needs correcting.
  **Verified (NIST run, 85 reqs, 540 KB):** all 85 carry req_id/classes/type/constraints/
  provenance/lineage/score; problem_statement + coverage present; `format=md` renders;
  unknown project → 404; reachable through the BFF → 200.
  Reports readiness, never grants it: `architect_ready:false`, blockers = *75 below threshold 4.3*
  + *no human sign-off*.

- **P6b — release gate: NOT DONE.** Still missing: the `draft→refined→proposed→validated` state
  machine and the human sign-off endpoint that flips `release_status`. `package.py` already reads
  `review["release_status"]` and defaults to `draft`, so the gate only needs to write it.

- **Re-scoring after review** ✅ DONE (2026-07-29). A human review changes a requirement's
  TEXT; its quality must be recomputed from that text. New `rescore.py` +
  `POST /projects/{pid}/reviews/{run}/rescore:run` (batch, changed-only, streamed) +
  `POST …/requirements/{req_id}/rescore` (single, per-Accept, headless-callable). The dashboard
  endpoint `GET …/quality/scorecard` now returns a **merged** view by default (`merged=false`
  for raw): the immutable run overlaid with reviewed scores, but ONLY for requirements that carry
  an authoritative C1–C9 breakdown — a bare `overall_after` from the live editor is not enough,
  so the headline never moves while the radar still shows pre-review scores. `store.merged_scorecard`
  + `_recompute_aggregates` (per-char means + `overall_health` = mean of per-req overall) +
  `set_review_set_level`. Set-level (C10–C15) recomputes in the batch only (expensive, not in the
  headline). reqoach: Overview **Step 3 "Re-run assessment"** → rescore (reviews preserved),
  destructive re-extraction demoted to a confirmed **"Re-import from documents ⚠"**; review page
  Accept/Save now persists the full breakdown the live editor already computed (no extra LLM call).
  **Verified on a throwaway copy of the Restaurant project (60 reqs):** merged 2.14 → **4.54**,
  per-char means all ~4.5 (consistent), 26 set-level overlaps, raw run still 2.145 (immutable),
  all 60 reviews' `final_text`+`original_text` preserved. Backup of the real store before any
  change: `scratchpad/store_backup_20260729_reviews.tgz` (verified 60 reviews, mean 4.55).

- **P6b — release gate (human sign-off)** ✅ DONE (2026-07-29). `store.set_release_status` +
  `package.readiness()` (manifest with `hard_blockers` + `can_release` = all hard blockers
  cleared, sign-off excluded) + `GET /projects/{pid}/release` + `POST …/reviews/{run}/release`
  (`action: approve|revoke`). Approve promotes `draft→validated` ONLY when `can_release`
  (quality floor, classification, ratified framing, coverage, no placeholders); else **409** with
  the offending blockers. Analyst never self-promotes. reqoach: new **Release** node (6th) in the
  Overview — shows Ready/Blocked/Validated, Approve-for-Architect / Revoke, and the downloads.
  **Verified:** Restaurant approve → `validated`+`architect_ready:true`+`released_by`; revoke →
  `draft`; NIST (unclassified, below-threshold) approve → **409** with hard_blockers. Restaurant
  left at **draft** for the human to sign off in the UI.
- **P7 — reissue** ✅ DONE (2026-07-29). `reissue.py` reconstructs a content-complete corrected
  spec (requirements grouped by `provenance.section_path`, document order, `final_text` wording,
  with classes/type/constraints). `GET /projects/{pid}/reissue?format=pdf|md|html`. **PDF is a
  real WeasyPrint PDF** (system libs pango/fontconfig/dejavu added to the analyst Dockerfile;
  `weasyprint>=61` in requirements). Downloads wired into the Release node.
  **Verified on Restaurant:** md structured correctly, html 16.9 KB, **pdf = valid PDF v1.7,
  24 KB, `application/pdf`**. Analyst now **43 routes**.

### RESUME POINT (as of 2026-07-18)
**P0–P5, P6a and P8 done and gate-verified. Next work item is P6b (the release gate).**

Live: reqoach BFF :7802 (UI only — no engine, no store) · analyst :7803 (authoritative store,
31 routes + socket.io) · ingestion-server :8700 · agent_server :7701 · embeddings :8601.

**P6 — release gate + Architect package:**
1. Gate = quality floor (every req ≥ threshold **or** human-accepted) + coverage sign-off
   (no unaccepted critical gaps) + explicit human approval.
2. State machine `draft → refined → proposed → validated`; the Analyst never self-promotes.
3. Emit the package: `requirements.json` (id, text, classes, type, constraints, analysis,
   provenance) + `requirements.md` + `problem_statement.json` + `coverage.json` + `manifest.json`.
4. **Gate:** a real project reaches `validated` only via human approval, and the emitted
   `requirements.json` validates against the Architect's documented input shape.

Open hazards to carry forward: two store copies (analyst authoritative, reqoach stale until P8);
deployed WebSocket path unverified (only local polling proven); multipart upload + large PDF
download through the BFF not exercised post-exchange; refine abort path untested; **classify
abort path untested** (cancellation is checked per wave of 8); **no UI surfaces classification
yet** — it is API/store-only.

## Decision record — socket.io routing (RESOLVED: C)
| Option | How | Trade-off |
|---|---|---|
| A. HTTP-only proxy | polling only; WS upgrade fails | simplest; polling overhead |
| B. Real WS relay | bidirectional relay in the BFF | full transport; new failure mode |
| **C. Route at nginx** ✅ | `/reqoach/socket.io/` → :7803 | full WS deployed; BFF proxies polling locally, so the frontend still needs no change |
