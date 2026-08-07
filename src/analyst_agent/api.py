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
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse

from analyst_agent import __version__
from analyst_agent import config
from analyst_agent import authoring
from analyst_agent import gap_assessor as gap_assessor_mod
from analyst_agent import converge as converge_mod
from analyst_agent import coverage
from analyst_agent import framing
from analyst_agent import refine
from analyst_agent import classify as classify_mod
from analyst_agent import structure as structure_mod
from analyst_agent import package as package_mod
from analyst_agent import rescore as rescore_mod
from analyst_agent import reissue as reissue_mod
from analyst_agent import questions as questions_mod
from analyst_agent import overlap_merge as overlap_merge_mod
from analyst_agent import store as pj
from analyst_agent.assess import iter_assessment
from analyst_agent.ingest.dispatch import SUPPORTED_EXTENSIONS
from analyst_agent.jobs import JobOptions, iter_job, iter_project_job
from analyst_agent.llm.client import AgentServerClient

STORE = config.STORE

# What the "done/total" of each stage counts — used to label the progress readout.
_STAGE_UNIT = {"ingest": "documents", "score": "requirements", "review": "requirements",
               "judges": "domains", "refine": "requirements", "classify": "requirements",
               "author": "gaps", "converge": "rounds", "rescore": "requirements",
               "gap_assess": "gaps"}


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
        elif et in ("refined", "classified", "authored", "rescored", "gap_assessed"):  # one per item
            stage = {"refined": "refine", "classified": "classify", "authored": "author",
                     "rescored": "rescore", "gap_assessed": "gap_assess"}[et]
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

    # --- re-score reviewed requirements (the review-preserving "Re-Run") ---
    def _run_rescore(self, job: Job, quality_run: str,
                     changed_only: bool, set_level: bool) -> None:
        job.status = "running"
        job.started_at = time.time()
        self._emit(job, {"type": "stage", "stage": "queued", "status": "done"})
        try:
            for event in rescore_mod.iter_rescore_for_project(
                    job.project_id, quality_run, changed_only=changed_only,
                    set_level=set_level, should_cancel=job.cancel_event.is_set):
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

    def create_rescore_run(self, pid: str, quality_run: str,
                           changed_only: bool = True, set_level: bool = True) -> Job:
        job = Job(job_id=uuid.uuid4().hex, doc_id="", source_file="")
        job.project_id, job.run_id, job.kind = pid, job.job_id, "rescore"
        self.jobs[job.job_id] = job
        threading.Thread(target=self._run_rescore,
                         args=(job, quality_run, changed_only, set_level), daemon=True).start()
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

    def _run_structure(self, job: "Job") -> None:
        job.status = "running"; job.started_at = time.time()
        self._emit(job, {"type": "stage", "stage": "structure", "status": "start"})
        try:
            data = structure_mod.run(job.project_id)
            if data is None:
                raise RuntimeError("no requirements to structure (run quality first)")
            self._emit(job, {"type": "structure", "data": {
                "terms": len(data["vocabulary"]["glossary"]),
                "tags": len(data["vocabulary"]["tags"]),
                "branches": len(data["tree"]["branches"]),
                "unassigned": len(data["tree"]["unassigned"])}})
            job.status = "done"
            self._emit(job, {"type": "job_done", "project_id": job.project_id})
        except Exception as e:  # noqa: BLE001
            job.status = "error"; job.error = f"{type(e).__name__}: {e}"
            self._emit(job, {"type": "job_error", "message": job.error})

    def create_structure_run(self, pid: str) -> "Job":
        job = Job(job_id=uuid.uuid4().hex, doc_id="", source_file="")
        job.project_id, job.run_id, job.kind = pid, job.job_id, "structure"
        self.jobs[job.job_id] = job
        threading.Thread(target=self._run_structure, args=(job,), daemon=True).start()
        return job

    def create_classify_run(self, pid: str, quality_run: str) -> Job:
        job = Job(job_id=uuid.uuid4().hex, doc_id="", source_file="")
        job.project_id, job.run_id, job.kind = pid, job.job_id, "classify"
        self.jobs[job.job_id] = job
        threading.Thread(target=self._run_classify, args=(job, quality_run), daemon=True).start()
        return job

    # --- gap assessment (triage each coverage gap: author / needs_input / dismiss) ---
    def _run_gap_assess(self, job: Job) -> None:
        job.status = "running"
        job.started_at = time.time()
        self._emit(job, {"type": "stage", "stage": "queued", "status": "done"})
        try:
            for event in gap_assessor_mod.iter_assess_gaps(
                    job.project_id, should_cancel=job.cancel_event.is_set):
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

    def create_gap_assess_run(self, pid: str) -> Job:
        job = Job(job_id=uuid.uuid4().hex, doc_id="", source_file="")
        job.project_id, job.run_id, job.kind = pid, job.job_id, "gap_assess"
        self.jobs[job.job_id] = job
        threading.Thread(target=self._run_gap_assess, args=(job,), daemon=True).start()
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
    def _run_converge(self, job: Job, quality_run: str,
                      max_rounds: int = converge_mod.MAX_ROUNDS) -> None:
        job.status = "running"
        job.started_at = time.time()
        self._emit(job, {"type": "stage", "stage": "queued", "status": "done"})
        try:
            for event in converge_mod.iter_converge(
                    job.project_id, quality_run, should_cancel=job.cancel_event.is_set,
                    max_rounds=max_rounds):
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

    def create_converge_run(self, pid: str, quality_run: str,
                            max_rounds: int = converge_mod.MAX_ROUNDS) -> Job:
        job = Job(job_id=uuid.uuid4().hex, doc_id="", source_file="")
        job.project_id, job.run_id, job.kind = pid, job.job_id, "converge"
        self.jobs[job.job_id] = job
        threading.Thread(target=self._run_converge, args=(job, quality_run, max_rounds),
                         daemon=True).start()
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


# --- Authorization (see config.py) ---------------------------------------------------------
# Browsing is public; mutations require a signed-in Google user, and per-project mutations
# require the OWNER or the ADMIN. Identity is the `X-Auth-Request-Email` header set by nginx
# from oauth2-proxy. These are FastAPI dependencies so an endpoint opts in by declaring one.

def caller_email(request: Request) -> str | None:
    """The authenticated caller's email, or the dev fallback, or None (anonymous)."""
    hdr = (request.headers.get("x-auth-request-email") or "").strip().lower()
    return hdr or config.DEV_EMAIL


def require_signed_in(request: Request) -> str:
    """Any signed-in Google user. Used for create-project and non-project mutations."""
    email = caller_email(request)
    if not email:
        raise HTTPException(401, "sign in required")
    return email


def can_manage(proj: dict | None, email: str | None) -> bool:
    """True if `email` may manage `proj`: the admin always; the owner of their own project.
    Legacy projects with no owner are admin-only (fail-closed)."""
    if not email:
        return False
    if email == config.ADMIN_EMAIL:
        return True
    owner = ((proj or {}).get("owner") or "").lower()
    return bool(owner) and owner == email


def _pid_from_path(path: str) -> str | None:
    parts = path.strip("/").split("/")
    return parts[1] if len(parts) >= 2 and parts[0] == "projects" else None


# ONE gate for every write. Browsing (GET/HEAD) is public; every mutating method must carry a
# signed-in identity, and a project-scoped mutation must come from the owner or the admin. A new
# write endpoint is covered automatically — there is no per-route opt-in to forget. (socket.io
# runs at the ASGI layer and never reaches this middleware; it is gated at nginx instead.)
@api.middleware("http")
async def _authz(request: Request, call_next):
    if request.method in ("POST", "PUT", "DELETE", "PATCH"):
        email = caller_email(request)
        if not email:
            return JSONResponse({"detail": "sign in required"}, status_code=401)
        pid = _pid_from_path(request.url.path)
        if pid:
            proj = pj.get_project(pid)
            if not proj:
                return JSONResponse({"detail": "unknown project"}, status_code=404)
            if not can_manage(proj, email):
                return JSONResponse(
                    {"detail": "only the project owner or the administrator can manage this project"},
                    status_code=403)
    return await call_next(request)


@api.on_event("startup")
async def _startup() -> None:
    jm.bind_loop(asyncio.get_running_loop())
    os.makedirs(STORE, exist_ok=True)
    # One-time, idempotent: lift any documents embedded under projects into the global KB.
    n = pj.migrate_legacy_documents()
    if n:
        print(f"[kb] migrated {n} legacy document(s) into the Knowledge Base")


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


# ---- Knowledge Base: global documents, many-to-many with projects -------------------------
# A document is uploaded once and attached to 0..n projects. Visibility is per-user: the admin
# sees everything; anyone else sees a document they own OR one attached to a project they own.
# Attaching/detaching and deleting need the admin or the document's owner. Reads are NOT gated
# by the write-middleware, so each handler filters by identity itself (anonymous -> nothing).

def _owned_pids(email: str | None) -> set[str]:
    if not email:
        return set()
    if email == config.ADMIN_EMAIL:
        return {p["id"] for p in pj.list_projects()}
    return {p["id"] for p in pj.list_projects() if (p.get("owner") or "").lower() == email}


def _kb_can_see(doc: dict, email: str | None, owned: set[str]) -> bool:
    if email and email == config.ADMIN_EMAIL:
        return True
    if email and (doc.get("owner") or "") == email:
        return True
    return any(p in owned for p in doc.get("projects", []))


def _kb_can_manage(doc: dict, email: str | None) -> bool:
    return bool(email) and (email == config.ADMIN_EMAIL or (doc.get("owner") or "") == email)


def _kb_view(doc: dict, email: str | None, owned: set[str], names: dict) -> dict:
    admin = email == config.ADMIN_EMAIL
    # Only surface associations the viewer is entitled to see (avoid leaking others' project names).
    vis = [p for p in doc.get("projects", []) if admin or p in owned]
    return {**doc, "projects": vis, "project_names": [names.get(p, p) for p in vis]}


@api.get("/kb/documents")
def kb_list(request: Request) -> dict:
    email = caller_email(request)
    owned = _owned_pids(email)
    names = {p["id"]: p.get("name") for p in pj.list_projects()}
    out = [_kb_view(d, email, owned, names)
           for d in pj.kb_list_documents() if _kb_can_see(d, email, owned)]
    return {"documents": out}


@api.post("/kb/documents")
async def kb_upload(request: Request, files: list[UploadFile] = File(...),
                    projects: str = Form("")) -> JSONResponse:
    email = caller_email(request)                       # middleware already required sign-in
    owned = _owned_pids(email)
    pids = [p.strip() for p in (projects or "").split(",") if p.strip()]
    pids = [p for p in pids if p in owned]               # attach only to projects you manage
    saved, errors = [], []
    for f in files:
        fn = f.filename or "upload"
        ext = os.path.splitext(fn)[1].lower()
        if ext not in SUPPORTED_EXTENSIONS:
            errors.append({"filename": fn, "error": f"unsupported extension {ext!r}"})
            continue
        saved.append(pj.kb_add_document(fn, ext, await f.read(), owner=email, projects=pids))
    return JSONResponse(status_code=201, content={"documents": saved, "errors": errors})


@api.patch("/kb/documents/{did}")
async def kb_set_projects(did: str, request: Request) -> dict:
    email = caller_email(request)
    doc = pj.kb_get_document(did)
    if not doc:
        raise HTTPException(404, "unknown document")
    if not _kb_can_manage(doc, email):
        raise HTTPException(403, "only the document owner or the administrator can change this")
    owned = _owned_pids(email)
    admin = email == config.ADMIN_EMAIL
    body = await request.json()
    want = [p for p in (body.get("projects") or []) if isinstance(p, str)]
    # Keep associations to projects the caller can't manage; replace the manageable set.
    keep = [p for p in doc.get("projects", []) if not (admin or p in owned)]
    add = [p for p in want if admin or p in owned]
    return pj.kb_set_projects(did, keep + add) or {}


@api.delete("/kb/documents/{did}")
def kb_delete(did: str, request: Request) -> dict:
    email = caller_email(request)
    doc = pj.kb_get_document(did)
    if not doc:
        raise HTTPException(404, "unknown document")
    if not _kb_can_manage(doc, email):
        raise HTTPException(403, "only the document owner or the administrator can delete this")
    pj.kb_delete_document(did)
    return {"deleted": did}


@api.get("/kb/documents/{did}/source")
def kb_source(did: str, request: Request) -> FileResponse:
    email = caller_email(request)
    doc = pj.kb_get_document(did)
    if not doc:
        raise HTTPException(404, "unknown document")
    if not _kb_can_see(doc, email, _owned_pids(email)):
        raise HTTPException(403, "not allowed")
    path = pj.kb_document_path(did)
    if not path or not os.path.isfile(path):
        raise HTTPException(404, "source missing")
    return FileResponse(path, filename=doc.get("filename") or f"document{doc.get('ext', '')}")


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
async def create_project(payload: dict | None = None,
                         email: str = Depends(require_signed_in)) -> dict:
    """Any signed-in Google user may create a project; they become its owner."""
    return pj.create_project((payload or {}).get("name", ""), owner=email)


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


@api.delete("/projects/{pid}/reviews/{run}/requirements/{req_id}")
def delete_req(pid: str, run: str, req_id: str) -> dict:
    """Purge one requirement from the scorecard AND the review (e.g. a junk authored GAP-*)."""
    if not pj.get_review(pid, run):
        raise HTTPException(404, "no such review session")
    ok = pj.delete_requirement(pid, run, req_id)
    if not ok:
        raise HTTPException(404, "unknown requirement")
    return {"deleted": req_id}


@api.get("/projects/{pid}/threshold")
def get_project_threshold(pid: str) -> dict:
    """The project acceptance threshold (default 4.3). Every review seeds from it."""
    if not pj.get_project(pid):
        raise HTTPException(404, "unknown project")
    return pj.get_project_threshold(pid)


@api.put("/projects/{pid}/threshold")
def put_project_threshold(pid: str, payload: dict) -> dict:
    """Set the project threshold and push it onto every existing review session,
    so the change takes effect immediately rather than only on the next run."""
    t = pj.set_project_threshold(pid, payload or {})
    if t is None:
        raise HTTPException(404, "unknown project")
    return t


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
def project_quality_scorecard(pid: str, run: str | None = None, merged: bool = True) -> dict:
    """The scorecard the dashboard reads. By default it is the immutable run OVERLAID with
    reviewed (re-scored) requirements, so accepted revisions show up. `merged=false` returns
    the raw run untouched (what the Architect package and convergence loop read)."""
    sc = pj.merged_scorecard(pid, run) if merged else pj.get_quality_scorecard(pid, run)
    if not sc:
        raise HTTPException(404, "no quality scorecard yet (run not finished or none run)")
    return sc


@api.post("/projects/{pid}/reviews/{run}/requirements/{req_id}/rescore")
def rescore_one_requirement(pid: str, run: str, req_id: str) -> dict:
    """Re-assess ONE reviewed requirement's current text and persist the full C1–C9
    breakdown (the per-Accept path — also callable headless by the Analyst itself).
    Synchronous: a single requirement is ~seconds."""
    if not pj.get_review(pid, run):
        raise HTTPException(404, "no such review session")
    patch = rescore_mod.rescore_requirement(pid, run, req_id)
    if patch is None:
        raise HTTPException(404, "unknown requirement, or it has no text to score")
    return {"req_id": req_id, "overall": patch.get("overall_after"),
            "judges_ok": patch.get("judges_ok"), "judges_total": patch.get("judges_total")}


@api.post("/projects/{pid}/reviews/{run}/rescore:run")
def run_review_rescore(pid: str, run: str, payload: dict | None = None) -> JSONResponse:
    """Batch re-assessment of a review session's revised requirements — the review-preserving
    "Re-Run". Re-scores every requirement whose text changed since it was last scored
    (`changed_only`, default true), then recomputes set-level once. Streamed + abortable.

    This does NOT re-ingest the source document (that would discard the human review); it
    scores the reviewed `final_text`. Writes only into the review session.
    """
    if not pj.get_project(pid):
        raise HTTPException(404, "unknown project")
    if not pj.get_review(pid, run):
        raise HTTPException(404, "no such quality run to re-score")
    p = payload or {}
    job = jm.create_rescore_run(pid, run,
                                changed_only=bool(p.get("changed_only", True)),
                                set_level=bool(p.get("set_level", True)))
    return JSONResponse(status_code=202,
                        content={"job_id": job.job_id, "project_id": pid,
                                 "quality_run": run, "status": job.status})


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


# --- Planner->Analyst gap-resolution loop (lightweight pollable job; NOT the socket.io JM) ---
from analyst_agent import gap_resolver as gap_mod  # noqa: E402


@dataclass
class GapJob:
    job_id: str
    project_id: str
    status: str = "queued"
    stage: str | None = None
    progress: dict = field(default_factory=dict)
    error: str | None = None
    result: dict | None = None
    started_at: float | None = None

    def snapshot(self) -> dict:
        return {"job_id": self.job_id, "project_id": self.project_id, "kind": "gap_resolve",
                "status": self.status, "stage": self.stage, "progress": self.progress,
                "error": self.error, "result": self.result,
                "elapsed_s": round(time.time() - self.started_at) if self.started_at else None}


_gap_jobs: dict[str, GapJob] = {}


def _run_gap_resolve(job: GapJob, gaps: list, apply: bool) -> None:
    job.status = "running"
    job.started_at = time.time()
    job.stage = "resolve"
    try:
        def prog(i, total, rec):
            job.progress = {"stage": "resolve", "status": "progress", "done": i, "total": total,
                            "last": f"{rec.get('req_id')}: {rec.get('disposition')}"}
        res = gap_mod.resolve_planner_gaps(job.project_id, gaps, apply=apply, progress=prog)
        job.result = res
        job.status = "done"
        job.stage = "done"
        job.progress = {"stage": "done", "status": "done", "total": res.get("total"),
                        "counts": res.get("counts"), "affected": len(res.get("affected_req_ids", []))}
    except Exception as e:  # noqa: BLE001
        job.status = "error"
        job.error = f"{type(e).__name__}: {e}"


@api.post("/projects/{pid}/planner-gaps:resolve")
def resolve_planner_gaps_endpoint(pid: str, payload: dict | None = None) -> JSONResponse:
    """Route Planner-surfaced requirement gaps through the analyst_gap_resolver. Body:
    {gaps:[{req_id, gap, question, requirement_text?}], apply?}. `apply` defaults to the
    project's gap_loop.apply setting (auto). Runs as a pollable job (GET /gap-jobs/{id})."""
    if not pj.get_project(pid):
        raise HTTPException(404, "unknown project")
    payload = payload or {}
    gaps = payload.get("gaps") or []
    if not gaps:
        raise HTTPException(400, "no gaps provided")
    apply = payload.get("apply")
    if apply is None:
        apply = pj.get_settings(pid)["gap_loop"]["apply"] == "auto"
    job = GapJob(job_id=uuid.uuid4().hex, project_id=pid)
    _gap_jobs[job.job_id] = job
    threading.Thread(target=_run_gap_resolve, args=(job, gaps, bool(apply)), daemon=True).start()
    return JSONResponse(status_code=202,
                        content={"job_id": job.job_id, "project_id": pid,
                                 "status": job.status, "apply": bool(apply)})


@api.get("/gap-jobs/{job_id}")
def gap_job_status(job_id: str) -> dict:
    job = _gap_jobs.get(job_id)
    if not job:
        raise HTTPException(404, "unknown gap job")
    return job.snapshot()


@api.get("/projects/{pid}/planner-gaps")
def get_planner_gaps(pid: str) -> dict:
    return pj.get_planner_gap_resolution(pid) or {"records": [], "affected_req_ids": [],
                                                  "counts": {}, "total": 0}


@api.get("/projects/{pid}/settings")
def get_project_settings(pid: str) -> dict:
    if not pj.get_project(pid):
        raise HTTPException(404, "unknown project")
    return pj.get_settings(pid)


@api.put("/projects/{pid}/settings")
def put_project_settings(pid: str, payload: dict | None = None) -> dict:
    s = pj.set_settings(pid, payload or {})
    if s is None:
        raise HTTPException(404, "unknown project")
    return s


# --- Human resolution of Planner gaps: answer a needs_input/flagged question -> refine the
#     traced requirement and/or author new one(s). Human-in-the-loop: preview (scored, commits
#     nothing) then apply the chosen text. See analyst_agent/answer.py. ---
from analyst_agent import answer as answer_mod  # noqa: E402


@api.post("/projects/{pid}/reviews/{run}/requirements/{req_id}/answer:preview")
def answer_preview(pid: str, run: str, req_id: str, payload: dict | None = None) -> JSONResponse:
    """Run the human's answer through the INCOSE refiner and return BOTH candidates —
    `refine` (traced requirement + answer, re-scored) and `author` (answer as a new
    requirement, scored) — with their scores. Commits nothing. Synchronous (~seconds)."""
    if not pj.get_review(pid, run):
        raise HTTPException(404, "no such review session")
    p = payload or {}
    ans = (p.get("answer") or "").strip()
    if not ans:
        raise HTTPException(400, "no answer provided")
    out = answer_mod.preview(pid, run, req_id, p.get("question", ""), ans)
    return JSONResponse(out)


@api.post("/projects/{pid}/reviews/{run}/requirements/{req_id}/answer:apply")
def answer_apply(pid: str, run: str, req_id: str, payload: dict | None = None) -> JSONResponse:
    """Commit the chosen resolution(s): `refine_text` updates the traced requirement (re-scored);
    each `author_texts` entry becomes a new authored requirement. Returns `affected_req_ids` so
    the Planner can re-plan only those. Both/either/neither may be supplied."""
    if not pj.get_review(pid, run):
        raise HTTPException(404, "no such review session")
    p = payload or {}
    refine_text = (p.get("refine_text") or "").strip() or None
    author_texts = [t for t in (p.get("author_texts") or []) if (t or "").strip()]
    if not refine_text and not author_texts:
        raise HTTPException(400, "nothing to apply (need refine_text and/or author_texts)")
    out = answer_mod.apply(pid, run, req_id, refine_text=refine_text,
                           author_texts=author_texts, question=p.get("question", ""),
                           answer=p.get("answer", ""))
    return JSONResponse(out)


# --- Semantic-plausibility check (analyst_sense_judge): catches WELL-FORMED nonsense that
#     INCOSE scoring (which grades form, not domain sense) passes. See analyst_agent/sense.py. ---
from analyst_agent import sense as sense_mod  # noqa: E402


@dataclass
class SenseJob:
    job_id: str
    project_id: str
    status: str = "queued"
    progress: dict = field(default_factory=dict)
    error: str | None = None
    result: dict | None = None
    started_at: float | None = None

    def snapshot(self) -> dict:
        return {"job_id": self.job_id, "project_id": self.project_id, "kind": "sense",
                "status": self.status, "progress": self.progress, "error": self.error,
                "result": self.result,
                "elapsed_s": round(time.time() - self.started_at) if self.started_at else None}


_sense_jobs: dict[str, SenseJob] = {}


def _run_sense(job: SenseJob) -> None:
    job.status = "running"
    job.started_at = time.time()
    try:
        def prog(i, total, rid, verdict):
            job.progress = {"status": "progress", "done": i, "total": total,
                            "last": f"{rid}: {'implausible' if not verdict['plausible'] else 'ok'}"}
        res = sense_mod.run(job.project_id, progress=prog)
        job.result = {"total": res.get("total"), "implausible": res.get("implausible", []),
                      "implausible_count": len(res.get("implausible", []))}
        job.status = "done"
        job.progress = {"status": "done", **job.result}
    except Exception as e:  # noqa: BLE001
        job.status = "error"
        job.error = f"{type(e).__name__}: {e}"


@api.post("/projects/{pid}/sense:run")
def run_sense(pid: str) -> JSONResponse:
    """Judge every requirement's SEMANTIC plausibility against the problem statement — the guard
    INCOSE can't provide. Pollable (GET /sense-jobs/{id}); results at GET /projects/{pid}/sense."""
    if not pj.get_project(pid):
        raise HTTPException(404, "unknown project")
    job = SenseJob(job_id=uuid.uuid4().hex, project_id=pid)
    _sense_jobs[job.job_id] = job
    threading.Thread(target=_run_sense, args=(job,), daemon=True).start()
    return JSONResponse(status_code=202, content={"job_id": job.job_id, "project_id": pid,
                                                  "status": job.status})


@api.get("/sense-jobs/{job_id}")
def sense_job_status(job_id: str) -> dict:
    job = _sense_jobs.get(job_id)
    if not job:
        raise HTTPException(404, "unknown sense job")
    return job.snapshot()


@api.get("/projects/{pid}/sense")
def get_sense_results(pid: str) -> dict:
    return pj.get_sense(pid) or {"results": {}, "implausible": [], "total": 0}


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


# --- release gate (human sign-off: draft -> validated) ---

@api.get("/projects/{pid}/release")
def get_release_status(pid: str, run: str | None = None) -> dict:
    """The release-gate verdict for the pipeline's Release panel: `release_status`,
    `can_release` (all hard blockers cleared), `hard_blockers`, `blockers`, `architect_ready`,
    counts, and who signed off. Light — no requirement payload."""
    m = package_mod.readiness(pid, run)
    if m is None:
        raise HTTPException(404, "unknown project, or no quality run yet")
    return m


@api.post("/projects/{pid}/reviews/{run}/release")
def set_release(pid: str, run: str, request: Request,
                payload: dict | None = None) -> JSONResponse:
    """Human release decision. `{"action":"approve"}` promotes the set to `validated` — but
    only when every HARD blocker is cleared (quality floor, classification, ratified framing,
    coverage, no placeholders); otherwise 409 with the offending blockers. `{"action":"revoke"}`
    returns it to `draft`. The Analyst never self-promotes; this endpoint is the only writer.

    The signer recorded in `released_by` is the AUTHENTICATED caller (the identity the auth
    layer already established), never a client-supplied name."""
    if not pj.get_project(pid):
        raise HTTPException(404, "unknown project")
    if not pj.get_review(pid, run):
        raise HTTPException(404, "no such review session")
    p = payload or {}
    signer = caller_email(request)               # trusted identity, not a typed name
    action = (p.get("action") or "approve").lower()
    if action == "revoke":
        pj.set_release_status(pid, run, "draft", note=p.get("note"))
        return JSONResponse(content=package_mod.readiness(pid, run))
    if action != "approve":
        raise HTTPException(400, "action must be 'approve' or 'revoke'")
    m = package_mod.readiness(pid, run)
    if m is None:
        raise HTTPException(404, "no quality run to release")
    if not m.get("can_release"):
        raise HTTPException(409, {"detail": "cannot release: hard blockers remain",
                                  "hard_blockers": m.get("hard_blockers", [])})
    pj.set_release_status(pid, run, "validated", approver=signer or "human", note=p.get("note"))
    return JSONResponse(content=package_mod.readiness(pid, run))


# --- reissue: the corrected, content-complete specification (md / html / pdf) ---

@api.post("/projects/{pid}/reviews/{run}/requirements/{req_id}/ratify")
def ratify_requirement(pid: str, run: str, req_id: str, request: Request,
                       payload: dict | None = None) -> JSONResponse:
    """Human ratifies one analyst-authored requirement (or un-ratifies with
    `{"ratified": false}`). Only authored requirements are ratifiable; ratifying does
    NOT clear the threshold/placeholder gates."""
    if not pj.get_review(pid, run):
        raise HTTPException(404, "no such review session")
    ratified = bool((payload or {}).get("ratified", True))
    out = pj.ratify_requirement(pid, run, req_id, ratified=ratified, by=caller_email(request))
    if out is None:
        raise HTTPException(404, "unknown requirement, or it is not analyst-authored")
    return JSONResponse(out)


@api.post("/projects/{pid}/reviews/{run}/ratify:all")
def ratify_all(pid: str, run: str, request: Request) -> JSONResponse:
    """Ratify every analyst-authored requirement in the run at once."""
    if not pj.get_review(pid, run):
        raise HTTPException(404, "no such review session")
    return JSONResponse(pj.ratify_all_authored(pid, run, by=caller_email(request)))


@api.get("/projects/{pid}/reissue")
def get_reissue(pid: str, run: str | None = None, format: str = "pdf"):
    """The reissued specification — requirements grouped by source section, using the
    corrected (`final_text`) wording. `format=pdf` (default, WeasyPrint), `md`, or `html`."""
    if not pj.get_project(pid):
        raise HTTPException(404, "unknown project")
    name = (pj.get_project(pid).get("name") or "requirements").replace("/", "-")
    if format == "md":
        md = reissue_mod.build_markdown(pid, run)
        if md is None:
            raise HTTPException(404, "no quality run to reissue")
        return PlainTextResponse(md, media_type="text/markdown; charset=utf-8")
    if format == "html":
        h = reissue_mod.build_html(pid, run)
        if h is None:
            raise HTTPException(404, "no quality run to reissue")
        return HTMLResponse(h)
    try:
        pdf = reissue_mod.build_pdf(pid, run)
    except Exception as e:  # noqa: BLE001 — the PDF stack is optional; report cleanly
        raise HTTPException(503, f"PDF rendering unavailable: {type(e).__name__}: {e}")
    if pdf is None:
        raise HTTPException(404, "no quality run to reissue")
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{name}_reissued.pdf"'})


@api.post("/projects/{pid}/structure:run")
def run_project_structure(pid: str, payload: dict | None = None) -> JSONResponse:
    """Build the project's vocabulary (glossary + tags) and requirement tree
    (single-parent feature branches + per-node cross-cutting tags). Consumed by the
    Architect for per-aspect design and glossary-anchored naming. Streamed + abortable."""
    if not pj.get_project(pid):
        raise HTTPException(404, "unknown project")
    if not pj.get_quality_scorecard(pid):
        raise HTTPException(400, "no quality run to structure")
    job = jm.create_structure_run(pid)
    return JSONResponse(status_code=202,
                        content={"job_id": job.job_id, "project_id": pid, "status": job.status})


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


@api.get("/projects/{pid}/classification")
def get_classification_status(pid: str, run: str | None = None) -> dict:
    """Lightweight classification status for the pipeline UI: how many requirements of a
    quality run carry routing labels, plus the type/class distribution. `done` is true when
    every requirement is classified."""
    if not pj.get_project(pid):
        raise HTTPException(404, "unknown project")
    runs = pj.list_quality_runs(pid)
    if not runs:
        return {"run_id": None, "total": 0, "classified": 0, "unclassified": 0,
                "done": False, "by_type": {}, "by_class": {}}
    if not run:
        run = sorted(runs, key=lambda r: r.get("finished_at") or "")[-1]["run_id"]
    review = pj.get_review(pid, run, seed=False) or {}
    reqs = review.get("requirements") or {}
    classified = 0
    by_type: dict[str, int] = {}
    by_class: dict[str, int] = {}
    for e in reqs.values():
        c = e.get("classification")
        if c and c.get("classes"):
            classified += 1
            by_type[c.get("type")] = by_type.get(c.get("type"), 0) + 1
            for cl in c["classes"]:
                by_class[cl] = by_class.get(cl, 0) + 1
    total = len(reqs)
    return {"run_id": run, "total": total, "classified": classified,
            "unclassified": total - classified, "done": total > 0 and classified == total,
            "by_type": dict(sorted(by_type.items(), key=lambda kv: -kv[1])),
            "by_class": dict(sorted(by_class.items(), key=lambda kv: -kv[1]))}


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
    max_rounds = int((payload or {}).get("max_rounds") or converge_mod.MAX_ROUNDS)
    if not 1 <= max_rounds <= 20:
        raise HTTPException(400, "max_rounds must be between 1 and 20")
    job = jm.create_converge_run(pid, run, max_rounds)
    return JSONResponse(status_code=202,
                        content={"job_id": job.job_id, "project_id": pid,
                                 "quality_run": run, "max_rounds": max_rounds,
                                 "status": job.status})


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


@api.post("/projects/{pid}/gaps:assess")
def run_gap_assess(pid: str) -> JSONResponse:
    """Triage every open coverage gap into a disposition: `author` (draft a requirement),
    `needs_input` (needs a human-supplied value), or `dismiss` (out of scope, recorded).
    Needs a coverage run. Streamed + abortable; persists the assessment."""
    if not pj.get_project(pid):
        raise HTTPException(404, "unknown project")
    if not pj.get_coverage(pid):
        raise HTTPException(400, "no coverage run \u2014 gaps must be identified first")
    job = jm.create_gap_assess_run(pid)
    return JSONResponse(status_code=202,
                        content={"job_id": job.job_id, "project_id": pid, "status": job.status})


@api.get("/projects/{pid}/gaps/assessment")
def get_gap_assessment(pid: str) -> JSONResponse:
    """The assessor's per-gap dispositions and rationale, plus a by-disposition count."""
    if not pj.get_project(pid):
        raise HTTPException(404, "unknown project")
    a = pj.get_gap_assessment(pid)
    if not a:
        raise HTTPException(404, "no gap assessment \u2014 run gaps:assess first")
    # Flag staleness (adjustment 2): a coverage run newer than the one assessed means
    # the dispositions may no longer match the current gaps.
    runs = pj.list_coverage_runs(pid)
    latest = sorted(runs, key=lambda r: r.get("finished_at") or "")[-1]["run_id"] if runs else None
    a = {**a, "is_stale": bool(latest and a.get("coverage_run") and latest != a["coverage_run"]),
         "current_coverage_run": latest}
    a["dismissed"] = pj.get_dismissed_gaps(pid)
    return JSONResponse(a)


@api.put("/projects/{pid}/gaps/disposition")
def override_gap_disposition(pid: str, payload: dict) -> JSONResponse:
    """Human override of the assessor's verdict for one gap (adjustment 1):
    `{"gap_key": "...", "disposition": "author"|"needs_input"|"dismiss", "reason": "..."}`.
    gap_key is in the BODY, not the path — gap titles contain '/' which breaks path params.
    Updates the persisted assessment; a `dismiss` also records the dismissal."""
    if not pj.get_project(pid):
        raise HTTPException(404, "unknown project")
    gap_key = (payload or {}).get("gap_key")
    if not gap_key:
        raise HTTPException(400, "gap_key is required in the body")
    a = pj.get_gap_assessment(pid)
    if not a:
        raise HTTPException(404, "no gap assessment")
    disp = str((payload or {}).get("disposition") or "").strip().lower()
    if disp not in ("author", "needs_input", "dismiss"):
        raise HTTPException(400, "disposition must be author, needs_input or dismiss")
    hit = None
    for g in a.get("gaps", []):
        if g.get("gap_key") == gap_key:
            g["disposition"] = disp
            g["overridden_by_human"] = True
            if (payload or {}).get("reason"):
                g["rationale"] = str(payload["reason"])[:500]
            hit = g
            break
    if not hit:
        raise HTTPException(404, "unknown gap_key")
    import collections
    a["by_disposition"] = dict(collections.Counter(x["disposition"] for x in a["gaps"]))
    pj.save_gap_assessment(pid, a)
    if disp == "dismiss":
        pj.dismiss_gap(pid, gap_key, hit.get("rationale") or "human override", by="human",
                       title=hit.get("title", ""), severity=hit.get("severity", ""),
                       domain=hit.get("domain", ""), detail=hit.get("detail", ""))
    return JSONResponse(hit)


@api.post("/projects/{pid}/gaps/dismiss")
def dismiss_one_gap(pid: str, payload: dict) -> JSONResponse:
    """Dismiss one gap as out of scope, with a recorded reason. Body:
    `{"gap_key": "...", "reason": "..."}` — gap_key is in the body (titles contain '/')."""
    if not pj.get_project(pid):
        raise HTTPException(404, "unknown project")
    gap_key = (payload or {}).get("gap_key")
    if not gap_key:
        raise HTTPException(400, "gap_key is required in the body")
    # carry the gap's title/severity/domain from the assessment if present, for the record
    a = pj.get_gap_assessment(pid) or {}
    meta = next((g for g in a.get("gaps", []) if g.get("gap_key") == gap_key), {})
    d = pj.dismiss_gap(pid, gap_key, (payload or {}).get("reason") or "out of scope", by="human",
                       title=meta.get("title", ""), severity=meta.get("severity", ""),
                       domain=meta.get("domain", ""), detail=meta.get("detail", ""))
    if d is None:
        raise HTTPException(404, "unknown project")
    return JSONResponse(d)


@api.delete("/projects/{pid}/gaps/dismiss")
def undismiss_one_gap(pid: str, gap_key: str) -> JSONResponse:
    """Un-dismiss a gap (it blocks release again). gap_key is a query parameter."""
    if not pj.get_project(pid):
        raise HTTPException(404, "unknown project")
    ok = pj.undismiss_gap(pid, gap_key)
    if not ok:
        raise HTTPException(404, "gap was not dismissed")
    return JSONResponse({"undismissed": gap_key})


@api.post("/projects/{pid}/gaps:apply")
def apply_gap_assessment(pid: str) -> JSONResponse:
    """Act on the assessment: DISMISS the dismiss-disposition gaps immediately (recorded),
    and start an AUTHORING job for the author + needs_input gaps (a needs_input gap gets a
    draft that carries its open question). Returns the dismiss result + the author job id."""
    if not pj.get_project(pid):
        raise HTTPException(404, "unknown project")
    a = pj.get_gap_assessment(pid)
    if not a:
        raise HTTPException(400, "no gap assessment \u2014 run gaps:assess first")
    dismissed = 0
    for g in a.get("gaps", []):
        if g.get("disposition") == "dismiss":
            pj.dismiss_gap(pid, g.get("gap_key"), g.get("rationale") or "assessor: out of scope",
                           by="assessor", title=g.get("title", ""),
                           severity=g.get("severity", ""), domain=g.get("domain", ""),
                           detail=g.get("detail", ""))
            dismissed += 1
    to_author = sum(1 for g in a.get("gaps", []) if g.get("disposition") in ("author", "needs_input"))
    job = None
    if to_author:
        runs = pj.list_quality_runs(pid)
        run = sorted(runs, key=lambda r: r.get("finished_at") or "")[-1]["run_id"] if runs else None
        if run:
            job = jm.create_author_run(pid, run)
    return JSONResponse(status_code=202, content={
        "dismissed": dismissed, "to_author": to_author,
        "author_job_id": job.job_id if job else None,
        "note": "authoring skips dismissed/dismiss-disposition gaps automatically"})


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


@api.post("/projects/{pid}/coverage:dedup")
def dedup_project_coverage(pid: str) -> dict:
    """Collapse cross-domain duplicate gaps in the latest coverage run via the reranker, and
    re-save — so the visible gap count reflects DISTINCT concerns, not the same one counted in
    several domains. Idempotent (re-running merges nothing new)."""
    runs = pj.list_coverage_runs(pid)
    if not runs:
        raise HTTPException(404, "no coverage run")
    run_id = sorted(runs, key=lambda r: r.get("finished_at") or "")[-1]["run_id"]
    cov = pj.get_coverage(pid, run_id)
    if not cov:
        raise HTTPException(404, "no coverage run")
    before = len(cov.get("gaps", []))
    cov["gaps"] = coverage.dedup_gaps(cov.get("gaps", []))
    meta = next((m for m in runs if m.get("run_id") == run_id),
                {"run_id": run_id, "project_id": pid, "kind": "coverage"})
    pj.save_coverage_run(pid, run_id, cov, meta)
    return {"before": before, "after": len(cov["gaps"]), "merged": before - len(cov["gaps"])}


@api.post("/projects/{pid}/overlaps:merge")
def merge_project_overlaps(pid: str, apply: bool = True) -> dict:
    """Auto-resolve the Overlaps tab: cluster the confirmed duplicate/overlap pairs and merge
    each cluster into ONE requirement (survivor keeps the merged text, absorbed ones removed).
    `apply=false` previews the proposed merges without changing anything. Either way an
    `overlap_resolutions` record is written on the review for human review/undo."""
    if not pj.get_project(pid):
        raise HTTPException(404, "unknown project")
    res = overlap_merge_mod.resolve_overlaps(pid, apply=apply)
    if res.get("error"):
        raise HTTPException(404, res["error"])
    return res


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
