"""Analyst Agent — Python client.

Mirrors `sdk/js/analyst-client.js` method-for-method so the two SDKs stay
describable by one document. For the Architect Agent and any scripted consumer;
a browser review UI wants the JS one.

    from analyst_client import AnalystClient

    with AnalystClient("http://localhost:7803") as analyst:
        pkg = analyst.get_package(pid)
        if pkg["manifest"]["architect_ready"]:
            ...

LONG RUNS. Analysis is slow — a 386-requirement quality run takes tens of minutes,
a 78-gap authoring pass ~9. Every `run_*` returns immediately with `{"job_id":...}`;
follow it with `wait_for_job`, or use `run_and_wait`.
"""

from __future__ import annotations

import os
import time
from typing import Any, Callable, Iterable

import httpx

JOB_TERMINAL = frozenset({"done", "error", "cancelled"})


class AnalystError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


def _unwrap(body: Any, key: str) -> list:
    """The API wraps lists differently per endpoint — `{projects:[]}`, `{runs:[]}`,
    `{domains:[]}` — and sometimes returns a bare array. Callers always get a list."""
    if isinstance(body, list):
        return body
    if isinstance(body, dict) and isinstance(body.get(key), list):
        return body[key]
    return []


class AnalystClient:
    def __init__(self, base_url: str | None = None, *, timeout: float = 60.0,
                 client: httpx.Client | None = None):
        self.base_url = (base_url or os.environ.get("ANALYST_URL", "http://localhost:7803")).rstrip("/")
        self._client = client or httpx.Client(timeout=timeout)
        self._owns_client = client is None

    def __enter__(self) -> "AnalystClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # ---- transport ---------------------------------------------------------

    def _request(self, method: str, path: str, *, params: dict | None = None,
                 json: Any = None, files: Any = None, raw: bool = False) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"
        params = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            r = self._client.request(method, url, params=params or None, json=json, files=files)
        except httpx.HTTPError as e:
            raise AnalystError(f"{method} {path} failed: {e}") from e
        if r.status_code >= 400:
            detail = ""
            try:
                detail = r.json().get("detail", "")
            except Exception:                             # noqa: BLE001 — not json
                detail = r.text[:200]
            raise AnalystError(f"{method} {path} -> {r.status_code}: {detail}",
                               status=r.status_code, body=detail)
        return r if raw else r.json()

    # ---- meta --------------------------------------------------------------

    def health(self) -> dict:        return self._request("GET", "health")
    def version(self) -> dict:       return self._request("GET", "version")
    def dependencies(self) -> dict:  return self._request("GET", "dependencies")

    # ---- projects ----------------------------------------------------------

    def list_projects(self) -> list:            return _unwrap(self._request("GET", "projects"), "projects")
    def create_project(self, name: str) -> dict: return self._request("POST", "projects", json={"name": name})
    def get_project(self, pid: str) -> dict:    return self._request("GET", f"projects/{pid}")
    def delete_project(self, pid: str) -> dict: return self._request("DELETE", f"projects/{pid}")
    def list_documents(self, pid: str) -> list: return _unwrap(self._request("GET", f"projects/{pid}/documents"), "documents")

    def upload_documents(self, pid: str, paths: Iterable[str]) -> dict:
        """Upload one or more files. The multipart field is `files` (plural)."""
        handles = [open(p, "rb") for p in paths]
        try:
            files = [("files", (os.path.basename(h.name), h)) for h in handles]
            return self._request("POST", f"projects/{pid}/documents", files=files)
        finally:
            for h in handles:
                h.close()

    def document_source_url(self, pid: str, did: str) -> str:
        return f"{self.base_url}/projects/{pid}/documents/{did}/source"

    # ---- runs (each returns 202 {"job_id": ...}) ---------------------------

    def run_quality(self, pid: str, **opts) -> dict:  return self._request("POST", f"projects/{pid}/quality:run", json=opts or {})
    def run_refine(self, pid: str, run: str | None = None) -> dict:   return self._request("POST", f"projects/{pid}/refine:run", json={"run": run})
    def run_classify(self, pid: str, run: str | None = None) -> dict: return self._request("POST", f"projects/{pid}/classify:run", json={"run": run})
    def run_coverage(self, pid: str) -> dict:         return self._request("POST", f"projects/{pid}/coverage:run")
    def run_framing(self, pid: str, user_request: str = "") -> dict:  return self._request("POST", f"projects/{pid}/framing:run", json={"user_request": user_request})
    def run_author(self, pid: str, run: str | None = None) -> dict:   return self._request("POST", f"projects/{pid}/author:run", json={"run": run})
    def run_converge(self, pid: str, run: str | None = None) -> dict: return self._request("POST", f"projects/{pid}/converge:run", json={"run": run})
    def generate_problem_statement(self, pid: str, user_request: str = "") -> dict:
        return self._request("POST", f"projects/{pid}/problem-statement:generate", json={"user_request": user_request})

    # ---- results -----------------------------------------------------------

    def get_package(self, pid: str, run: str | None = None, fmt: str = "json"):
        """The handover package. `fmt='md'` returns text, not a dict."""
        if fmt == "md":
            return self._request("GET", f"projects/{pid}/package",
                                 params={"run": run, "format": "md"}, raw=True).text
        return self._request("GET", f"projects/{pid}/package", params={"run": run})

    def list_quality_runs(self, pid: str) -> list: return _unwrap(self._request("GET", f"projects/{pid}/quality"), "runs")
    def get_scorecard(self, pid: str, run: str | None = None) -> dict: return self._request("GET", f"projects/{pid}/quality/scorecard", params={"run": run})
    def get_coverage(self, pid: str, run: str | None = None) -> dict:  return self._request("GET", f"projects/{pid}/coverage", params={"run": run})
    def get_questions(self, pid: str, run: str | None = None) -> dict: return self._request("GET", f"projects/{pid}/questions", params={"run": run})
    def get_convergence(self, pid: str) -> dict:  return self._request("GET", f"projects/{pid}/convergence")
    def get_problem_statement(self, pid: str) -> dict: return self._request("GET", f"projects/{pid}/problem-statement")
    def put_problem_statement(self, pid: str, doc: dict) -> dict: return self._request("PUT", f"projects/{pid}/problem-statement", json=doc)
    def get_coverage_profile(self, pid: str) -> dict: return self._request("GET", f"projects/{pid}/coverage-profile")
    def put_coverage_profile(self, pid: str, p: dict) -> dict: return self._request("PUT", f"projects/{pid}/coverage-profile", json=p)

    # ---- review state ------------------------------------------------------

    def get_review(self, pid: str, run: str) -> dict: return self._request("GET", f"projects/{pid}/reviews/{run}")
    def update_requirement(self, pid: str, run: str, req_id: str, patch: dict) -> dict:
        return self._request("PUT", f"projects/{pid}/reviews/{run}/requirements/{req_id}", json=patch)
    def get_threshold(self, pid: str, run: str) -> dict: return self._request("GET", f"projects/{pid}/reviews/{run}/threshold")
    def set_threshold(self, pid: str, run: str, threshold: dict) -> dict:
        return self._request("PUT", f"projects/{pid}/reviews/{run}/threshold", json=threshold)

    # ---- jobs --------------------------------------------------------------

    def get_job(self, job_id: str) -> dict:        return self._request("GET", f"jobs/{job_id}")
    def get_job_events(self, job_id: str) -> Any:  return self._request("GET", f"jobs/{job_id}/events")
    def cancel_job(self, job_id: str) -> dict:     return self._request("POST", f"jobs/{job_id}/cancel")
    def get_active_job(self, pid: str) -> dict:    return self._request("GET", f"projects/{pid}/active-job")

    # ---- reference data ----------------------------------------------------

    def get_rules(self) -> dict:
        """NOT a list: a lookup map keyed by rule id — `{"R1": {...}, "R7": {...}}`."""
        return self._request("GET", "rules")

    def get_domains(self) -> list:    return _unwrap(self._request("GET", "catalog/domains"), "domains")
    def get_archetypes(self) -> list: return _unwrap(self._request("GET", "catalog/archetypes"), "archetypes")
    def get_standards(self) -> list:  return _unwrap(self._request("GET", "catalog/standards"), "standards")

    # ---- helpers -----------------------------------------------------------

    def wait_for_job(self, job_id: str, *, on_progress: Callable[[dict], None] | None = None,
                     interval_s: float = 2.0, timeout_s: float | None = None) -> dict:
        """Poll a job to completion. Returns the final snapshot; does NOT raise on
        `status == 'error'` — a cancelled run is a normal outcome, so inspect it."""
        started = time.monotonic()
        while True:
            job = self.get_job(job_id)
            if on_progress:
                on_progress(job)
            if job.get("status") in JOB_TERMINAL:
                return job
            if timeout_s is not None and time.monotonic() - started > timeout_s:
                raise AnalystError(f"job {job_id} did not finish within {timeout_s}s")
            time.sleep(interval_s)

    def run_and_wait(self, started: dict, **kw) -> dict:
        """`client.run_and_wait(client.run_quality(pid))`"""
        return self.wait_for_job(started["job_id"], **kw)

    def load_review(self, pid: str, run: str | None = None) -> dict:
        """Scorecard × review joined into the per-requirement view a reviewer needs,
        with the flags that must not be missed: below-threshold (blocks release),
        generated (analyst-authored, needs ratifying), text_changed (drift audit).

        Mirrors `loadReview` in the JS client.
        """
        if not run:
            runs = self.list_quality_runs(pid)
            if not runs:
                raise AnalystError("no quality run for this project")
            run = sorted(runs, key=lambda r: r.get("finished_at") or "")[-1]["run_id"]
        scorecard = self.get_scorecard(pid, run)
        review = self.get_review(pid, run) or {}
        entries = review.get("requirements") or {}
        threshold = float((review.get("threshold") or {}).get("value", 4.3))

        out = []
        for r in scorecard.get("requirements", []):
            if (r.get("lineage") or {}).get("duplicate_of"):
                continue
            e = entries.get(r.get("req_id")) or {}
            prov = r.get("provenance") or {}
            score = e.get("overall_after") if e.get("overall_after") is not None else r.get("overall")
            text = e.get("final_text") or r.get("text", "")
            original = e.get("original_text") or r.get("text", "")
            ok, want = r.get("judges_ok"), r.get("judges_total")
            out.append({
                "req_id": r.get("req_id"), "text": text, "original_text": original,
                "text_changed": text.strip() != original.strip(),
                "score": score, "score_before": e.get("overall_before"),
                "below_threshold": score is None or score < threshold,
                "judges_ok": ok, "judges_total": want,
                "incompletely_judged": ok is not None and want is not None and ok < want,
                "status": e.get("status") or "unreviewed", "note": e.get("note") or "",
                "characteristics": r.get("characteristics") or {},
                "deterministic_findings": r.get("deterministic_findings") or [],
                "review": r.get("review"), "refinement": e.get("refinement"),
                "classification": e.get("classification"),
                "provenance": prov,
                "generated": prov.get("origin") == "analyst_authored",
                "ratified": prov.get("ratified") is True,
                "gap_title": prov.get("gap_title"),
                "open_question": prov.get("open_question", ""),
                "raw": r,
            })
        counts = {
            "total": len(out),
            "below_threshold": sum(1 for r in out if r["below_threshold"]),
            "generated": sum(1 for r in out if r["generated"]),
            "unratified_generated": sum(1 for r in out if r["generated"] and not r["ratified"]),
            "incompletely_judged": sum(1 for r in out if r["incompletely_judged"]),
            "needs_human": sum(1 for r in out if r["status"] == "needs_human"),
        }
        return {"run": run, "threshold": threshold, "requirements": out, "counts": counts}

    def accept_requirement(self, pid: str, run: str, req_id: str, note: str = "") -> dict:
        return self.update_requirement(pid, run, req_id, {"status": "accepted", "note": note})

    def correct_requirement(self, pid: str, run: str, req_id: str, text: str, note: str = "") -> dict:
        """Submit a human correction. The new text is NOT re-scored automatically —
        run refine/quality afterwards if an updated score is needed."""
        return self.update_requirement(pid, run, req_id,
                                       {"status": "accepted", "final_text": text, "note": note})
