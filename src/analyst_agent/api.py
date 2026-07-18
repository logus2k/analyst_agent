"""reqoach — the single-container backend: static dashboard + editor + the
orchestration API + socket.io (job progress AND live single-requirement assess).

One FastAPI app serves the static frontend (dashboard, editor, vendored libs,
data/) and the REST API; one socket.io server carries both the job-progress
stream (`join` → rooms) and the live assessor (`assess` → per-client). This is
the same-origin backend nginx routes `/reqoach/` and `/reqoach/socket.io/` to.

Wraps `analyst_agent.jobs.iter_job` in a background worker and exposes:

  POST /documents                    upload → store → create job (returns job_id, doc_id)
  GET  /jobs/{job_id}                job status snapshot (stage, progress, error)
  GET  /jobs/{job_id}/events         buffered events so far (replay for late/polling clients)
  GET  /documents                    library listing
  GET  /documents/{doc_id}/scorecard the assembled scorecard JSON
  GET  /health

Live progress also streams over **socket.io**: a client emits
`join {job_id}` and receives the same events (`stage`, `requirement`,
`review_result`, `set_level`, `aggregates`, `scorecard`, `job_done`,
`job_error`) as the job runs. The job runs in a worker thread; its events are
marshalled onto the asyncio loop with `run_coroutine_threadsafe`.

Run:  PYTHONPATH=src uvicorn analyst_agent.orchestration_api:asgi --host 0.0.0.0 --port 7802
(or via the `reqoach` compose service, which is how it runs in production)

Persistence is the filesystem under REQQA_STORE (default <repo>/store):
  store/<doc_id>/source.<ext>, scorecard.json, meta.json
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field

import httpx
import socketio
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from analyst_agent import __version__
from analyst_agent import config
from analyst_agent import authoring
from analyst_agent import converge as converge_mod
from analyst_agent import coverage
from analyst_agent import framing
from analyst_agent import refine
from analyst_agent import classify as classify_mod
from analyst_agent import package as package_mod
from analyst_agent import questions as questions_mod
from analyst_agent import store as pj
from analyst_agent.assess import iter_assessment
from analyst_agent.ingest.dispatch import SUPPORTED_EXTENSIONS
from analyst_agent.jobs import JobOptions, iter_job, iter_project_job
from analyst_agent.llm.client import AgentServerClient

STORE = config.STORE

# What the "done/total" of each stage counts — used to label the progress readout.
_STAGE_UNIT = {"ingest": "documents", "score": "requirements", "review": "requirements",
               "judges": "domains", "refine": "requirements", "classify": "requirements",
               "author": "gaps", "converge": "rounds"}


@dataclass
class Job:
    job_id: str
    doc_id: str
    source_file: str
    status: str = "queued"          # queued | running | done | error
    stage: str | None = None
    progress: dict = field(default_factory=dict)   # {done, total} of the active stage
    error: str | None = None
    events: list[dict] = field(default_factory=list)
    project_id: str | None = None   # set for project (multi-doc) runs
    run_id: str | None = None       # == job_id for project runs
    kind: str = "document"          # document | quality (project)
    started_at: float | None = None  # wall-clock when the job began running
    cancel_event: threading.Event = field(default_factory=threading.Event)

    def snapshot(self) -> dict:
        return {"job_id": self.job_id, "doc_id": self.doc_id,
                "source_file": self.source_file, "status": self.status,
                "stage": self.stage, "progress": self.progress, "error": self.error,
                "project_id": self.project_id, "run_id": self.run_id, "kind": self.kind,
                "elapsed_s": (round(time.time() - self.started_at)
                              if self.started_at else None),
                "event_count": len(self.events)}


class JobManager:
    """Owns jobs, runs each in a worker thread, and fans events out to socket.io
    room == job_id (plus an in-memory buffer for replay)."""

    def __init__(self, sio: socketio.AsyncServer):
        self.sio = sio
        self.jobs: dict[str, Job] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def _emit(self, job: Job, event: dict) -> None:
        job.events.append(event)
        # Keep a light status snapshot in sync for POLLING clients (the pipeline
        # Overview polls /jobs/{id}; it does not use socket.io). `stage` events mark
        # phase boundaries; the fine-grained per-item events (`requirement`, `domain`,
        # `review_result`) carry the live counts that make a 30-min run legible, so
        # fold those into `job.progress` too — otherwise the snapshot looks frozen.
        et = event.get("type")
        if et == "stage":
            job.stage = event.get("stage")
            job.progress = {"stage": event.get("stage"), "done": event.get("done"),
                            "total": event.get("total"), "status": event.get("status"),
                            "message": event.get("message"), "pages": event.get("pages"),
                            "unit": _STAGE_UNIT.get(event.get("stage"))}
        elif et == "requirement":     # scoring: one per requirement finished
            job.stage = "score"
            job.progress = {"stage": "score", "done": event.get("scored"),
                            "total": event.get("total"), "status": "progress",
                            "unit": "requirements"}
        elif et == "review_result":   # reviewer pass over flagged requirements
            job.stage = "review"
            job.progress = {"stage": "review", "done": event.get("done"),
                            "total": event.get("total"), "status": "progress",
                            "unit": "requirements"}
        elif et in ("refined", "classified", "authored"):   # one per item finished
            stage = {"refined": "refine", "classified": "classify",
                     "authored": "author"}[et]
            job.stage = stage
            job.progress = {"stage": stage, "done": event.get("done"),
                            "total": event.get("total"), "status": "progress",
                            "unit": _STAGE_UNIT.get(stage, "requirements")}
        elif et == "round":           # convergence: one per loop round
            job.stage = "converge"
            job.progress = {"stage": "converge", "done": event.get("round"),
                            "total": event.get("total"), "status": "progress",
                            "unit": "rounds"}
        elif et == "domain":          # coverage: one per domain judge finished
            job.stage = "judges"
            job.progress = {"stage": "judges", "done": event.get("done"),
                            "total": event.get("total"), "status": "progress",
                            "unit": "domains"}
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(
                self.sio.emit(event["type"], {**event, "job_id": job.job_id},
                              room=job.job_id), self._loop)

    def _run(self, job: Job, path: str, options: JobOptions) -> None:
        job.status = "running"
        job.started_at = time.time()
        self._emit(job, {"type": "stage", "stage": "queued", "status": "done"})
        try:
            for event in iter_job(path, options=options, source_file=job.source_file,
                                  should_cancel=job.cancel_event.is_set):
                self._emit(job, event)
                if event.get("type") == "scorecard":
                    self._persist_scorecard(job, event["data"])
            if job.cancel_event.is_set():
                job.status = "cancelled"
                self._emit(job, {"type": "job_cancelled", "doc_id": job.doc_id})
            else:
                job.status = "done"
                self._emit(job, {"type": "job_done", "doc_id": job.doc_id})
        except Exception as e:  # noqa: BLE001 — surface any pipeline failure
            job.status = "error"
            job.error = f"{type(e).__name__}: {e}"
            self._emit(job, {"type": "job_error", "message": job.error})

    def _persist_scorecard(self, job: Job, scorecard: dict) -> None:
        doc_dir = os.path.join(STORE, job.doc_id)
        os.makedirs(doc_dir, exist_ok=True)
        with open(os.path.join(doc_dir, "scorecard.json"), "w", encoding="utf-8") as f:
            json.dump(scorecard, f, indent=1)
        meta = {"doc_id": job.doc_id, "job_id": job.job_id,
                "source_file": job.source_file,
                "total": scorecard.get("aggregates", {}).get("total"),
                "produced_in_s": scorecard.get("produced_in_s"),
                "per_characteristic_mean": scorecard.get("aggregates", {}).get("per_characteristic_mean")}
        with open(os.path.join(doc_dir, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=1)

    def create(self, path: str, source_file: str, doc_id: str,
               options: JobOptions) -> Job:
        job = Job(job_id=uuid.uuid4().hex, doc_id=doc_id, source_file=source_file)
        self.jobs[job.job_id] = job
        threading.Thread(target=self._run, args=(job, path, options),
                         daemon=True).start()
        return job

    # --- project (multi-document) quality runs ---
    def _run_project(self, job: Job, docs: list[dict], source_file: str,
                     options: JobOptions) -> None:
        job.status = "running"
        job.started_at = time.time()
        self._emit(job, {"type": "stage", "stage": "queued", "status": "done"})
        try:
            for event in iter_project_job(docs, source_file, options=options,
                                          should_cancel=job.cancel_event.is_set):
                self._emit(job, event)
                if event.get("type") == "scorecard":
                    self._persist_project_scorecard(job, event["data"])
            if job.cancel_event.is_set():
                job.status = "cancelled"
                self._emit(job, {"type": "job_cancelled", "project_id": job.project_id, "run_id": job.run_id})
            else:
                job.status = "done"
                self._emit(job, {"type": "job_done", "project_id": job.project_id, "run_id": job.run_id})
        except Exception as e:  # noqa: BLE001
            job.status = "error"
            job.error = f"{type(e).__name__}: {e}"
            self._emit(job, {"type": "job_error", "message": job.error})

    def _persist_project_scorecard(self, job: Job, scorecard: dict) -> None:
        meta = {"run_id": job.run_id, "project_id": job.project_id, "kind": "quality",
                "finished_at": pj._now(), "source_file": job.source_file,
                "total": scorecard.get("aggregates", {}).get("total"),
                "produced_in_s": scorecard.get("produced_in_s"),
                "documents": scorecard.get("documents", [])}
        pj.save_quality_run(job.project_id, job.run_id, scorecard, meta)

    def create_project_run(self, pid: str, docs: list[dict], source_file: str,
                           options: JobOptions) -> Job:
        job = Job(job_id=uuid.uuid4().hex, doc_id="", source_file=source_file)
        job.project_id, job.run_id, job.kind = pid, job.job_id, "quality"
        self.jobs[job.job_id] = job
        threading.Thread(target=self._run_project, args=(job, docs, source_file, options),
                         daemon=True).start()
        return job

    # --- coverage (domain-judge panel) runs ---
    def _run_coverage(self, job: Job) -> None:
        job.status = "running"
        job.started_at = time.time()
        self._emit(job, {"type": "stage", "stage": "queued", "status": "done"})
        try:
            for event in coverage.iter_coverage_for_project(
                    job.project_id, should_cancel=job.cancel_event.is_set):
                self._emit(job, event)
                if event.get("type") == "coverage":
                    meta = {"run_id": job.run_id, "project_id": job.project_id, "kind": "coverage",
                            "finished_at": pj._now(),
                            "requirement_count": event["data"].get("requirement_count"),
                            "gap_count": len(event["data"].get("gaps", []))}
                    pj.save_coverage_run(job.project_id, job.run_id, event["data"], meta)
            if job.cancel_event.is_set():
                job.status = "cancelled"
                self._emit(job, {"type": "job_cancelled", "project_id": job.project_id, "run_id": job.run_id})
            else:
                job.status = "done"
                self._emit(job, {"type": "job_done", "project_id": job.project_id, "run_id": job.run_id})
        except Exception as e:  # noqa: BLE001
            job.status = "error"
            job.error = f"{type(e).__name__}: {e}"
            self._emit(job, {"type": "job_error", "message": job.error})

    def cancel(self, job_id: str) -> bool:
        """Signal a running job to abort (cooperative — checked between items)."""
        job = self.jobs.get(job_id)
        if not job or job.status not in ("queued", "running"):
            return False
        job.cancel_event.set()
        return True

    def create_coverage_run(self, pid: str) -> Job:
        job = Job(job_id=uuid.uuid4().hex, doc_id="", source_file="")
        job.project_id, job.run_id, job.kind = pid, job.job_id, "coverage"
        self.jobs[job.job_id] = job
        threading.Thread(target=self._run_coverage, args=(job,), daemon=True).start()
        return job

    # --- refinement loop (the Analyst's defining capability) ---
    def _run_refine(self, job: Job, quality_run: str) -> None:
        job.status = "running"
        job.started_at = time.time()
        self._emit(job, {"type": "stage", "stage": "queued", "status": "done"})
        try:
            for event in refine.iter_refine_for_project(
                    job.project_id, quality_run, should_cancel=job.cancel_event.is_set):
                self._emit(job, event)
            if job.cancel_event.is_set():
                job.status = "cancelled"
                self._emit(job, {"type": "job_cancelled", "project_id": job.project_id})
            else:
                job.status = "done"
                self._emit(job, {"type": "job_done", "project_id": job.project_id})
        except Exception as e:  # noqa: BLE001
            job.status = "error"
            job.error = f"{type(e).__name__}: {e}"
            self._emit(job, {"type": "job_error", "message": job.error})

    def create_refine_run(self, pid: str, quality_run: str) -> Job:
        job = Job(job_id=uuid.uuid4().hex, doc_id="", source_file="")
        job.project_id, job.run_id, job.kind = pid, job.job_id, "refine"
        self.jobs[job.job_id] = job
        threading.Thread(target=self._run_refine, args=(job, quality_run), daemon=True).start()
        return job

    # --- classification (the Architect contract: classes[] + type + constraints[]) ---
    def _run_classify(self, job: Job, quality_run: str) -> None:
        job.status = "running"
        job.started_at = time.time()
        self._emit(job, {"type": "stage", "stage": "queued", "status": "done"})
        try:
            for event in classify_mod.iter_classify_for_project(
                    job.project_id, quality_run, should_cancel=job.cancel_event.is_set):
                self._emit(job, event)
            if job.cancel_event.is_set():
                job.status = "cancelled"
                self._emit(job, {"type": "job_cancelled", "project_id": job.project_id})
            else:
                job.status = "done"
                self._emit(job, {"type": "job_done", "project_id": job.project_id})
        except Exception as e:  # noqa: BLE001
            job.status = "error"
            job.error = f"{type(e).__name__}: {e}"
            self._emit(job, {"type": "job_error", "message": job.error})

    def create_classify_run(self, pid: str, quality_run: str) -> Job:
        job = Job(job_id=uuid.uuid4().hex, doc_id="", source_file="")
        job.project_id, job.run_id, job.kind = pid, job.job_id, "classify"
        self.jobs[job.job_id] = job
        threading.Thread(target=self._run_classify, args=(job, quality_run), daemon=True).start()
        return job

    # --- gap authoring (the Analyst closes coverage gaps, decisions 2 + 3) ---
    def _run_author(self, job: Job, quality_run: str) -> None:
        job.status = "running"
        job.started_at = time.time()
        self._emit(job, {"type": "stage", "stage": "queued", "status": "done"})
        try:
            for event in authoring.iter_author_for_project(
                    job.project_id, quality_run, should_cancel=job.cancel_event.is_set):
                self._emit(job, event)
            if job.cancel_event.is_set():
                job.status = "cancelled"
                self._emit(job, {"type": "job_cancelled", "project_id": job.project_id})
            else:
                job.status = "done"
                self._emit(job, {"type": "job_done", "project_id": job.project_id})
        except Exception as e:  # noqa: BLE001
            job.status = "error"
            job.error = f"{type(e).__name__}: {e}"
            self._emit(job, {"type": "job_error", "message": job.error})

    def create_author_run(self, pid: str, quality_run: str) -> Job:
        job = Job(job_id=uuid.uuid4().hex, doc_id="", source_file="")
        job.project_id, job.run_id, job.kind = pid, job.job_id, "author"
        self.jobs[job.job_id] = job
        threading.Thread(target=self._run_author, args=(job, quality_run), daemon=True).start()
        return job

    # --- convergence loop (Phase D: rounds until complete, stalled or capped) ---
    def _run_converge(self, job: Job, quality_run: str) -> None:
        job.status = "running"
        job.started_at = time.time()
        self._emit(job, {"type": "stage", "stage": "queued", "status": "done"})
        try:
            for event in converge_mod.iter_converge(
                    job.project_id, quality_run, should_cancel=job.cancel_event.is_set):
                self._emit(job, event)
            if job.cancel_event.is_set():
                job.status = "cancelled"
                self._emit(job, {"type": "job_cancelled", "project_id": job.project_id})
            else:
                job.status = "done"
                self._emit(job, {"type": "job_done", "project_id": job.project_id})
        except Exception as e:  # noqa: BLE001
            job.status = "error"
            job.error = f"{type(e).__name__}: {e}"
            self._emit(job, {"type": "job_error", "message": job.error})

    def create_converge_run(self, pid: str, quality_run: str) -> Job:
        job = Job(job_id=uuid.uuid4().hex, doc_id="", source_file="")
        job.project_id, job.run_id, job.kind = pid, job.job_id, "converge"
        self.jobs[job.job_id] = job
        threading.Thread(target=self._run_converge, args=(job, quality_run), daemon=True).start()
        return job

    # --- problem framing (streamed) runs ---
    def _run_framing(self, job: Job, user_request: str) -> None:
        job.status = "running"
        job.started_at = time.time()
        self._emit(job, {"type": "stage", "stage": "queued", "status": "done"})
        try:
            paths = [p for d in pj.list_documents(job.project_id)
                     if (p := pj.document_path(job.project_id, d["id"]))]
            for event in framing.iter_frame_problem(paths, user_request,
                                                    should_cancel=job.cancel_event.is_set):
                self._emit(job, event)
                if event.get("type") == "problem_statement":
                    st = event["data"]
                    pj.save_problem_statement(job.project_id, st, ratified=False)
                    if not pj.get_coverage_profile(job.project_id):   # seed from framing output
                        pj.save_coverage_profile(job.project_id,
                                                 {"archetypes": st.get("candidate_archetypes", []),
                                                  "salient_domains": st.get("salient_domains", []),
                                                  "domain_overrides": {}})
            if job.cancel_event.is_set():
                job.status = "cancelled"
                self._emit(job, {"type": "job_cancelled", "project_id": job.project_id, "run_id": job.run_id})
            else:
                job.status = "done"
                self._emit(job, {"type": "job_done", "project_id": job.project_id, "run_id": job.run_id})
        except Exception as e:  # noqa: BLE001
            job.status = "error"
            job.error = f"{type(e).__name__}: {e}"
            self._emit(job, {"type": "job_error", "message": job.error})

    def create_framing_run(self, pid: str, user_request: str = "") -> Job:
        job = Job(job_id=uuid.uuid4().hex, doc_id="", source_file="")
        job.project_id, job.run_id, job.kind = pid, job.job_id, "framing"
        self.jobs[job.job_id] = job
        threading.Thread(target=self._run_framing, args=(job, user_request), daemon=True).start()
        return job


sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins="*")
api = FastAPI(title="analyst-agent")
api.add_middleware(GZipMiddleware, minimum_size=1024)   # big scorecard JSON


@api.middleware("http")
async def _revalidate_assets(request, call_next):
    """Serve HTML/JS/CSS with `Cache-Control: no-cache` so browsers always revalidate
    (cheap 304 via the ETag) instead of silently serving a stale bundle after a rebuild."""
    resp = await call_next(request)
    path = request.url.path
    if path.endswith((".html", ".js", ".css")) or path.endswith("/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


jm = JobManager(sio)


@api.on_event("startup")
async def _startup() -> None:
    jm.bind_loop(asyncio.get_running_loop())
    os.makedirs(STORE, exist_ok=True)


@api.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "analyst-agent", "version": __version__,
            "store": STORE, "jobs": len(jm.jobs)}


@api.get("/version")
def version() -> dict:
    return {"version": __version__}


@api.get("/dependencies")
async def dependencies() -> dict:
    """Reachability of the three shared services. Reported, not enforced: the
    agent starts even when one is down, so the failure shows here rather than as
    a boot crash."""
    targets = {"agent_server": config.AGENT_SERVER_URL,
               "ingestion_server": config.INGESTION_URL,
               "embeddings": config.EMBEDDINGS_URL}
    out = {}
    async with httpx.AsyncClient(timeout=3.0) as client:
        for name, url in targets.items():
            try:
                r = await client.get(url + "/health")
                out[name] = {"url": url, "reachable": True, "status": r.status_code}
            except Exception as e:  # noqa: BLE001 — probe, never raises
                out[name] = {"url": url, "reachable": False, "error": type(e).__name__}
    return out


_RULES_META: dict | None = None


@api.get("/rules")
def rules_meta() -> dict:
    """INCOSE rule metadata for the frontend: id -> {name, category, detector,
    text (guidance), terms}. Lets the UI show rule names/guidance instead of bare
    ids, group by category, and label deterministic vs judge-flagged findings.
    Cached after first read; the catalog is static."""
    global _RULES_META
    if _RULES_META is None:
        path = os.path.join(config.KNOWLEDGE, "incose", "catalog.json")
        with open(path, encoding="utf-8") as f:
            cat = json.load(f)
        _RULES_META = {r["id"]: {"name": r.get("name"), "category": r.get("category"),
                                 "detector": r.get("detector"), "scope": r.get("scope"),
                                 "text": r.get("text"), "terms": r.get("terms", [])}
                       for r in cat.get("rules", [])}
    return _RULES_META


@api.post("/documents")
async def upload_document(file: UploadFile = File(...),
                          review: bool = True, set_level: bool = True) -> JSONResponse:
    filename = file.filename or "upload"
    ext = os.path.splitext(filename)[1].lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise HTTPException(415, f"unsupported extension {ext!r}; supported: {sorted(SUPPORTED_EXTENSIONS)}")

    doc_id = uuid.uuid4().hex
    doc_dir = os.path.join(STORE, doc_id)
    os.makedirs(doc_dir, exist_ok=True)
    src_path = os.path.join(doc_dir, f"source{ext}")
    with open(src_path, "wb") as f:
        f.write(await file.read())

    job = jm.create(src_path, filename, doc_id,
                    JobOptions(review=review, set_level=set_level))
    return JSONResponse(status_code=202,
                        content={"job_id": job.job_id, "doc_id": doc_id,
                                 "source_file": filename, "status": job.status})


@api.get("/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    job = jm.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "unknown job")
    return job.snapshot()


@api.get("/jobs/{job_id}/events")
def job_events(job_id: str) -> dict:
    job = jm.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "unknown job")
    return {"job_id": job_id, "status": job.status, "events": job.events}


@api.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    """Cooperatively abort a running job (quality or coverage). In-flight LLM calls
    finish; queued ones are dropped, so the run stops within ~one judge latency."""
    if job_id not in jm.jobs:
        raise HTTPException(404, "unknown job")
    ok = jm.cancel(job_id)
    return {"job_id": job_id, "cancelling": ok, "status": jm.jobs[job_id].status}


@api.get("/documents")
def list_documents() -> dict:
    docs = []
    if os.path.isdir(STORE):
        for doc_id in sorted(os.listdir(STORE)):
            meta_path = os.path.join(STORE, doc_id, "meta.json")
            if os.path.isfile(meta_path):
                with open(meta_path, encoding="utf-8") as f:
                    docs.append(json.load(f))
    return {"documents": docs}


@api.get("/documents/{doc_id}/scorecard")
def get_scorecard(doc_id: str) -> dict:
    path = os.path.join(STORE, doc_id, "scorecard.json")
    if not os.path.isfile(path):
        raise HTTPException(404, "no scorecard (job not finished or unknown doc)")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


@sio.event
async def join(sid, data):
    """Client subscribes to a job's live event stream, and gets a replay of
    events already emitted so it never misses the early stages."""
    job_id = (data or {}).get("job_id")
    job = jm.jobs.get(job_id)
    if not job:
        await sio.emit("job_error", {"message": "unknown job"}, to=sid)
        return
    await sio.enter_room(sid, job_id)
    for event in list(job.events):
        await sio.emit(event["type"], {**event, "job_id": job_id, "replay": True}, to=sid)


# --- Live single-requirement assessor, folded onto the SAME socket.io server ---
# The dashboard/monitor uses `join` (job rooms); the editor uses `assess` (per-sid).
# One server, one origin, one container.
_assess_gen: dict[str, int] = {}
_assess_client = AgentServerClient()


@sio.event
async def connect(sid, environ):
    _assess_gen[sid] = 0


@sio.event
async def disconnect(sid):
    _assess_gen.pop(sid, None)


@sio.event
async def assess(sid, data):
    """Stream a single requirement's live assessment; a newer `assess` from the
    same client supersedes the in-flight one (generation counter)."""
    text = ((data or {}).get("text") or "").strip()
    review = bool((data or {}).get("review", True))
    gen = _assess_gen.get(sid, 0) + 1
    _assess_gen[sid] = gen
    if len(text) < 12:                       # matches verify.MIN_TEXT_LEN
        await sio.emit("idle", {"reason": "too_short"}, to=sid)
        return
    await sio.emit("start", {"gen": gen}, to=sid)

    loop = asyncio.get_running_loop()
    gen_iter = iter_assessment(text, client=_assess_client, review=review)
    sentinel = object()

    def _next():
        try:
            return next(gen_iter)
        except StopIteration:
            return sentinel
        except Exception as e:               # a step that slipped past its own guard
            return {"type": "__genfail__", "error": f"{type(e).__name__}: {e}"}

    done_sent = False
    try:
        while True:
            if _assess_gen.get(sid) != gen:      # superseded
                gen_iter.close()
                return
            event = await loop.run_in_executor(None, _next)
            if event is sentinel:
                break
            if _assess_gen.get(sid) != gen:
                return
            if event.get("type") == "__genfail__":
                break                            # stop; the terminal 'done' below still fires
            if event.get("type") == "done":
                done_sent = True
            await sio.emit(event["type"], {**event, "gen": gen}, to=sid)
    finally:
        gen_iter.close()
    # ALWAYS deliver a terminal 'done' so the client's AVG SCORE never hangs on "—".
    if not done_sent and _assess_gen.get(sid) == gen:
        await sio.emit("done", {"gen": gen, "overall": None}, to=sid)


# --- Projects mode (see specs/projects_mode/): project workspace + project-scoped
# document upload. Uploading stores source + metadata ONLY — it triggers no analysis;
# Quality/Coverage runs are explicit (later phases). ---

@api.post("/projects")
async def create_project(payload: dict | None = None) -> dict:
    return pj.create_project((payload or {}).get("name", ""))


@api.get("/projects")
def list_projects() -> dict:
    return {"projects": pj.list_projects()}


@api.get("/projects/{pid}")
def get_project(pid: str) -> dict:
    proj = pj.get_project(pid)
    if not proj:
        raise HTTPException(404, "unknown project")
    return proj


@api.delete("/projects/{pid}")
def delete_project(pid: str) -> dict:
    """Delete a project and all of its data. Any in-flight job for it is signalled to abort."""
    if not pj.get_project(pid):
        raise HTTPException(404, "unknown project")
    for job in jm.jobs.values():
        if job.project_id == pid and job.status in ("queued", "running"):
            job.cancel_event.set()
    pj.delete_project(pid)
    return {"deleted": pid}


@api.post("/projects/{pid}/documents")
async def upload_project_documents(pid: str, files: list[UploadFile] = File(...)) -> JSONResponse:
    if not pj.get_project(pid):
        raise HTTPException(404, "unknown project")
    saved, errors = [], []
    for f in files:
        fn = f.filename or "upload"
        ext = os.path.splitext(fn)[1].lower()
        if ext not in SUPPORTED_EXTENSIONS:
            errors.append({"filename": fn, "error": f"unsupported extension {ext!r}"})
            continue
        saved.append(pj.add_document(pid, fn, ext, await f.read()))
    return JSONResponse(status_code=201, content={"documents": saved, "errors": errors})


@api.get("/projects/{pid}/documents")
def list_project_documents(pid: str) -> dict:
    if not pj.get_project(pid):
        raise HTTPException(404, "unknown project")
    return {"documents": pj.list_documents(pid)}


# --- Review & Reissue (quality/correction loop). Review state is one session per quality run. ---

@api.get("/projects/{pid}/reviews/{run}")
def get_project_review(pid: str, run: str) -> dict:
    """The review session for a quality run (seeded from its scorecard on first access)."""
    if not pj.get_project(pid):
        raise HTTPException(404, "unknown project")
    doc = pj.get_review(pid, run)
    if not doc:
        raise HTTPException(404, "no such quality run to review")
    return doc


@api.put("/projects/{pid}/reviews/{run}/requirements/{req_id}")
async def put_req_review(pid: str, run: str, req_id: str, payload: dict) -> dict:
    """Upsert one requirement's review: {status, final_text, note, overall_after}."""
    entry = pj.upsert_req_review(pid, run, req_id, payload or {})
    if entry is None:
        raise HTTPException(404, "unknown review session or requirement")
    return entry


@api.get("/projects/{pid}/reviews/{run}/threshold")
def get_review_threshold(pid: str, run: str) -> dict:
    doc = pj.get_review(pid, run)
    if not doc:
        raise HTTPException(404, "no review session")
    return doc["threshold"]


@api.put("/projects/{pid}/reviews/{run}/threshold")
async def put_review_threshold(pid: str, run: str, payload: dict) -> dict:
    t = pj.set_threshold(pid, run, payload or {})
    if t is None:
        raise HTTPException(404, "no review session")
    return t


@api.get("/projects/{pid}/documents/{did}/source")
def get_document_source(pid: str, did: str):
    """Serve a document's raw source file (e.g. the PDF) — used by the in-app PDF
    viewer to render the page a requirement came from and highlight its bbox."""
    import mimetypes
    path = pj.document_path(pid, did)
    if not path or not os.path.exists(path):
        raise HTTPException(404, "document not found")
    mt = mimetypes.guess_type(path)[0] or "application/octet-stream"
    return FileResponse(path, media_type=mt)


@api.post("/projects/{pid}/quality:run")
async def run_project_quality(pid: str, payload: dict | None = None) -> JSONResponse:
    """Explicit, user-triggered INCOSE quality run over the project's documents
    (all, or a `document_ids` subset). One scorecard; set-level across all docs;
    each requirement traceable to its source document."""
    proj = pj.get_project(pid)
    if not proj:
        raise HTTPException(404, "unknown project")
    wanted = set((payload or {}).get("document_ids") or [])
    docs = [d for d in pj.list_documents(pid) if not wanted or d["id"] in wanted]
    run_docs = []
    for d in docs:
        path = pj.document_path(pid, d["id"])
        if path:
            run_docs.append({"path": path, "source_file": d["filename"], "document_id": d["id"]})
    if not run_docs:
        raise HTTPException(400, "no documents to analyze")
    job = jm.create_project_run(pid, run_docs, proj.get("name") or "project", JobOptions())
    return JSONResponse(status_code=202,
                        content={"job_id": job.job_id, "run_id": job.run_id,
                                 "project_id": pid, "status": job.status,
                                 "document_count": len(run_docs)})


@api.get("/projects/{pid}/quality")
def project_quality_runs(pid: str) -> dict:
    if not pj.get_project(pid):
        raise HTTPException(404, "unknown project")
    return {"runs": pj.list_quality_runs(pid)}


@api.get("/projects/{pid}/quality/scorecard")
def project_quality_scorecard(pid: str, run: str | None = None) -> dict:
    sc = pj.get_quality_scorecard(pid, run)
    if not sc:
        raise HTTPException(404, "no quality scorecard yet (run not finished or none run)")
    return sc


# --- Requirements Coverage — Problem Framing (stage 0). Explicit, user-triggered. ---

@api.post("/projects/{pid}/problem-statement:generate")
def generate_problem_statement(pid: str, payload: dict | None = None) -> dict:
    """Distil a structured, provenance-graded problem statement from the project's
    documents (+ optional free-text request). Sync `def` → runs in the threadpool so
    the ~20s LLM call doesn't block the event loop. Saved as an unratified draft."""
    if not pj.get_project(pid):
        raise HTTPException(404, "unknown project")
    paths = [p for d in pj.list_documents(pid) if (p := pj.document_path(pid, d["id"]))]
    try:
        statement = framing.frame_problem(paths, (payload or {}).get("user_request", ""))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"framing failed: {type(e).__name__}: {e}")
    doc = pj.save_problem_statement(pid, statement, ratified=False)
    if not pj.get_coverage_profile(pid):        # seed the profile from the framing output
        pj.save_coverage_profile(pid, {"archetypes": statement.get("candidate_archetypes", []),
                                       "salient_domains": statement.get("salient_domains", []),
                                       "domain_overrides": {}})
    return doc


@api.post("/projects/{pid}/framing:run")
def run_project_framing(pid: str, payload: dict | None = None) -> JSONResponse:
    """Streamed Problem Framing (async job): reads the documents (with page counts) then
    distils the problem statement, saved as an unratified draft. Progress + abort via /jobs."""
    if not pj.get_project(pid):
        raise HTTPException(404, "unknown project")
    if not pj.list_documents(pid):
        raise HTTPException(400, "no documents to frame")
    job = jm.create_framing_run(pid, (payload or {}).get("user_request", ""))
    return JSONResponse(status_code=202,
                        content={"job_id": job.job_id, "run_id": job.run_id,
                                 "project_id": pid, "status": job.status})


@api.get("/projects/{pid}/problem-statement")
def get_problem_statement(pid: str) -> dict:
    if not pj.get_project(pid):
        raise HTTPException(404, "unknown project")
    return pj.get_problem_statement(pid) or {"version": 0, "statement": None}


@api.put("/projects/{pid}/problem-statement")
async def put_problem_statement(pid: str, payload: dict) -> dict:
    if not pj.get_project(pid):
        raise HTTPException(404, "unknown project")
    st = (payload or {}).get("statement")
    if st is None:
        raise HTTPException(400, "missing 'statement'")
    return pj.save_problem_statement(pid, st, ratified=bool((payload or {}).get("ratified", False)))


@api.get("/projects/{pid}/coverage-profile")
def get_coverage_profile(pid: str) -> dict:
    if not pj.get_project(pid):
        raise HTTPException(404, "unknown project")
    return pj.get_coverage_profile(pid) or {"version": 0, "profile": None}


@api.put("/projects/{pid}/coverage-profile")
async def put_coverage_profile(pid: str, payload: dict) -> dict:
    if not pj.get_project(pid):
        raise HTTPException(404, "unknown project")
    prof = (payload or {}).get("profile")
    if prof is None:
        raise HTTPException(400, "missing 'profile'")
    return pj.save_coverage_profile(pid, prof)


# --- Coverage catalog (read-only, for the UI) ---

@api.post("/projects/{pid}/refine:run")
def run_project_refine(pid: str, payload: dict | None = None) -> JSONResponse:
    """Run the refinement loop over a quality run's below-threshold requirements.

    Bounded (<=3 attempts each, stop on no improvement, keep best); anything still
    below the threshold is escalated as `needs_human`. Writes into the run's review
    state; `original_text` is never mutated. Streamed + abortable like other jobs.
    """
    if not pj.get_project(pid):
        raise HTTPException(404, "unknown project")
    run = (payload or {}).get("run")
    if not run:
        runs = pj.list_quality_runs(pid)
        if not runs:
            raise HTTPException(400, "no quality run to refine")
        run = sorted(runs, key=lambda r: r.get("finished_at") or "")[-1]["run_id"]
    if not pj.get_review(pid, run):
        raise HTTPException(404, "no such quality run")
    job = jm.create_refine_run(pid, run)
    return JSONResponse(status_code=202,
                        content={"job_id": job.job_id, "project_id": pid,
                                 "quality_run": run, "status": job.status})


@api.get("/projects/{pid}/package")
def get_project_package(pid: str, run: str | None = None, format: str = "json"):
    """The Architect handover package for a quality run (latest run by default).

    Joins the scorecard with the review/classification state and reports readiness in
    `manifest.blockers` — it never grants release. `format=md` renders the human-readable
    companion; `format=json` (default) is the machine contract.
    """
    pkg = package_mod.build_package(pid, run)
    if pkg is None:
        raise HTTPException(404, "unknown project, or no quality run to package")
    if format == "md":
        return PlainTextResponse(package_mod.render_markdown(pkg),
                                 media_type="text/markdown; charset=utf-8")
    return JSONResponse(content=pkg)


@api.post("/projects/{pid}/classify:run")
def run_project_classify(pid: str, payload: dict | None = None) -> JSONResponse:
    """Classify every requirement of a quality run: `classes[]` (Architect routing),
    `type` (reporting) and `constraints[]`.

    Run this AFTER refinement — it labels the text that will actually be released
    (`final_text` when refinement improved it). Streamed + abortable like other jobs.
    """
    if not pj.get_project(pid):
        raise HTTPException(404, "unknown project")
    run = (payload or {}).get("run")
    if not run:
        runs = pj.list_quality_runs(pid)
        if not runs:
            raise HTTPException(400, "no quality run to classify")
        run = sorted(runs, key=lambda r: r.get("finished_at") or "")[-1]["run_id"]
    if not pj.get_review(pid, run):
        raise HTTPException(404, "no such quality run")
    job = jm.create_classify_run(pid, run)
    return JSONResponse(status_code=202,
                        content={"job_id": job.job_id, "project_id": pid,
                                 "quality_run": run, "status": job.status})


@api.post("/projects/{pid}/author:run")
def run_project_author(pid: str, payload: dict | None = None) -> JSONResponse:
    """Author a requirement for every open coverage gap, score it and refine it to
    threshold (remaining_work.md decisions 2 + 3 — all severities are in scope).

    Needs both a quality run and a coverage run. Every authored requirement is
    flagged `provenance.origin = "analyst_authored"`, carries no source document,
    and is `ratified: false` until a human accepts it — unratified ones block
    release. Streamed + abortable like other jobs.
    """
    if not pj.get_project(pid):
        raise HTTPException(404, "unknown project")
    run = (payload or {}).get("run")
    if not run:
        runs = pj.list_quality_runs(pid)
        if not runs:
            raise HTTPException(400, "no quality run to author against")
        run = sorted(runs, key=lambda r: r.get("finished_at") or "")[-1]["run_id"]
    if not pj.get_review(pid, run):
        raise HTTPException(404, "no such quality run")
    if not pj.get_coverage(pid):
        raise HTTPException(400, "no coverage run — gaps must be identified first")
    job = jm.create_author_run(pid, run)
    return JSONResponse(status_code=202,
                        content={"job_id": job.job_id, "project_id": pid,
                                 "quality_run": run, "status": job.status})


@api.post("/projects/{pid}/converge:run")
def run_project_converge(pid: str, payload: dict | None = None) -> JSONResponse:
    """Drive the set to complete and at/above threshold.

    Each round: refine below-threshold requirements → run coverage → author a
    requirement per open gap. Terminates on the gap COUNT — zero gaps with clean
    quality is `converged`; a count that stops dropping is `stalled` and needs a
    human; `MAX_ROUNDS` is a safety backstop, never the completion test.
    Long-running: state persists at each round boundary. Streamed + abortable.
    """
    if not pj.get_project(pid):
        raise HTTPException(404, "unknown project")
    run = (payload or {}).get("run")
    if not run:
        runs = pj.list_quality_runs(pid)
        if not runs:
            raise HTTPException(400, "no quality run to converge")
        run = sorted(runs, key=lambda r: r.get("finished_at") or "")[-1]["run_id"]
    if not pj.get_review(pid, run):
        raise HTTPException(404, "no such quality run")
    job = jm.create_converge_run(pid, run)
    return JSONResponse(status_code=202,
                        content={"job_id": job.job_id, "project_id": pid,
                                 "quality_run": run, "status": job.status})


@api.get("/projects/{pid}/questions")
def get_open_questions(pid: str, run: str | None = None) -> JSONResponse:
    """What the Analyst needs a human to answer before the set can converge.

    Aggregated from data already stored — unfilled placeholders, the INCOSE
    reviewer's advisories on still-blocked requirements, and questions the gap
    author recorded. No LLM call. Questions asking the same thing are merged so one
    answer unblocks every requirement waiting on it.
    """
    if not pj.get_project(pid):
        raise HTTPException(404, "unknown project")
    qs = questions_mod.collect_questions(pid, run)
    return JSONResponse({"project_id": pid, "summary": questions_mod.summarize(qs),
                         "questions": qs})


@api.get("/projects/{pid}/convergence")
def get_convergence_state(pid: str) -> JSONResponse:
    """Where the convergence loop got to: round, per-round gap counts, outcome."""
    if not pj.get_project(pid):
        raise HTTPException(404, "unknown project")
    state = pj.get_convergence(pid)
    if not state:
        raise HTTPException(404, "no convergence run for this project")
    return JSONResponse(state)


@api.post("/projects/{pid}/coverage:run")
def run_project_coverage(pid: str) -> JSONResponse:
    """Explicit, user-triggered coverage run: the domain-judge panel over the project's
    requirement set + problem statement. Streamed via socket.io (join {run_id})."""
    if not pj.get_project(pid):
        raise HTTPException(404, "unknown project")
    job = jm.create_coverage_run(pid)
    return JSONResponse(status_code=202,
                        content={"job_id": job.job_id, "run_id": job.run_id,
                                 "project_id": pid, "status": job.status})


@api.get("/projects/{pid}/active-job")
def project_active_job(pid: str) -> dict:
    """The newest still-running (queued/running) job for this project, if any — so a
    reloaded Overview can reattach to a run in progress instead of looking idle."""
    for job in reversed(list(jm.jobs.values())):
        if job.project_id == pid and job.status in ("queued", "running"):
            return {"job_id": job.job_id, "kind": job.kind, "status": job.status}
    return {"job_id": None}


@api.get("/projects/{pid}/coverage")
def get_project_coverage(pid: str, run: str | None = None) -> dict:
    cov = pj.get_coverage(pid, run)
    if not cov:
        raise HTTPException(404, "no coverage run yet")
    return cov


@api.get("/catalog/domains")
def catalog_domains() -> dict:
    return framing.load_domains()


@api.get("/catalog/archetypes")
def catalog_archetypes() -> dict:
    return {"archetypes": framing.load_archetypes()}


@api.get("/catalog/standards")
def catalog_standards() -> dict:
    return {"standards": framing.load_standards()}



asgi = socketio.ASGIApp(sio, other_asgi_app=api)
