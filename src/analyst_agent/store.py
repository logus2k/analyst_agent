"""Project workspace storage (projects mode) — see specs/projects_mode/.

Greenfield, filesystem-backed. A Project owns 1..n uploaded Documents; uploading
a document only stores its source + metadata — it triggers NO analysis. Quality
and Coverage runs are explicit (later phases) and write under the project.

Layout (ANALYST_STORE, default <repo>/store):
  store/projects/<project_id>/
    meta.json                       # {id, name, created_at, documents:[doc-meta...]}
    documents/<document_id>/
      source.<ext>
      meta.json                     # {id, project_id, filename, ext, ingested_at, size}
"""

from __future__ import annotations

import datetime
import json
import os
import shutil
import uuid

from analyst_agent import config

STORE = config.STORE
PROJECTS_DIR = os.path.join(STORE, "projects")


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _project_dir(pid: str) -> str:
    return os.path.join(PROJECTS_DIR, pid)


def _read_json(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)                       # atomic within a dir


# ---- projects ----

def create_project(name: str) -> dict:
    pid = uuid.uuid4().hex
    meta = {"id": pid, "name": (name or "").strip() or "Untitled project",
            "created_at": _now(), "documents": []}
    os.makedirs(os.path.join(_project_dir(pid), "documents"), exist_ok=True)
    _write_json(os.path.join(_project_dir(pid), "meta.json"), meta)
    return meta


def list_projects() -> list[dict]:
    out: list[dict] = []
    if os.path.isdir(PROJECTS_DIR):
        for pid in os.listdir(PROJECTS_DIR):
            m = _read_json(os.path.join(_project_dir(pid), "meta.json"))
            if m:
                out.append({**m, "document_count": len(m.get("documents", []))})
    out.sort(key=lambda p: p.get("created_at", ""), reverse=True)
    return out


def get_project(pid: str) -> dict | None:
    return _read_json(os.path.join(_project_dir(pid), "meta.json"))


def delete_project(pid: str) -> bool:
    """Remove a project and everything under it (documents, quality/coverage runs,
    problem statement, profile). Returns False if the project doesn't exist."""
    d = _project_dir(pid)
    if not os.path.isdir(d):
        return False
    shutil.rmtree(d, ignore_errors=True)
    return True


# ---- documents ----

def add_document(pid: str, filename: str, ext: str, data: bytes) -> dict | None:
    proj = get_project(pid)
    if not proj:
        return None
    did = uuid.uuid4().hex
    ddir = os.path.join(_project_dir(pid), "documents", did)
    os.makedirs(ddir, exist_ok=True)
    with open(os.path.join(ddir, f"source{ext}"), "wb") as f:
        f.write(data)
    dmeta = {"id": did, "project_id": pid, "filename": filename, "ext": ext,
             "ingested_at": _now(), "size": len(data)}
    _write_json(os.path.join(ddir, "meta.json"), dmeta)
    proj.setdefault("documents", []).append(dmeta)
    _write_json(os.path.join(_project_dir(pid), "meta.json"), proj)
    return dmeta


def list_documents(pid: str) -> list[dict]:
    proj = get_project(pid)
    return proj.get("documents", []) if proj else []


def document_path(pid: str, did: str) -> str | None:
    ddir = os.path.join(_project_dir(pid), "documents", did)
    dmeta = _read_json(os.path.join(ddir, "meta.json"))
    if not dmeta:
        return None
    return os.path.join(ddir, f"source{dmeta['ext']}")


# ---- quality runs (explicit; written under the project) ----

def _quality_dir(pid: str, run_id: str) -> str:
    return os.path.join(_project_dir(pid), "quality", run_id)


def save_quality_run(pid: str, run_id: str, scorecard: dict, meta: dict) -> None:
    _write_json(os.path.join(_quality_dir(pid, run_id), "scorecard.json"), scorecard)
    _write_json(os.path.join(_quality_dir(pid, run_id), "meta.json"), meta)
    proj = get_project(pid)
    if proj is not None:
        runs = [r for r in proj.get("quality_runs", []) if r.get("run_id") != run_id]
        runs.append(meta)
        proj["quality_runs"] = runs
        _write_json(os.path.join(_project_dir(pid), "meta.json"), proj)


def list_quality_runs(pid: str) -> list[dict]:
    proj = get_project(pid)
    return proj.get("quality_runs", []) if proj else []


def get_quality_scorecard(pid: str, run_id: str | None = None) -> dict | None:
    runs = list_quality_runs(pid)
    if run_id is None:
        if not runs:
            return None
        run_id = sorted(runs, key=lambda r: r.get("finished_at") or "")[-1]["run_id"]
    return _read_json(os.path.join(_quality_dir(pid, run_id), "scorecard.json"))


# ---- review sessions (Review & Reissue — one per quality run) ----

_DEFAULT_THRESHOLD = {"mode": "avg_ge", "value": 4.3}


def _review_dir(pid: str, run_id: str) -> str:
    return os.path.join(_project_dir(pid), "reviews", run_id)


def get_review(pid: str, run_id: str, seed: bool = True) -> dict | None:
    """Read the review session for a quality run; if absent and `seed`, create it from
    that run's scorecard (one entry per requirement, status=unreviewed)."""
    path = os.path.join(_review_dir(pid, run_id), "review.json")
    doc = _read_json(path)
    if doc or not seed:
        return doc
    sc = get_quality_scorecard(pid, run_id)
    if not sc:
        return None
    reqs: dict = {}
    for r in sc.get("requirements", []):
        rid = r.get("req_id")
        if not rid:
            continue
        reqs[rid] = {"status": "unreviewed", "original_text": r.get("text", ""),
                     "final_text": r.get("text", ""), "note": "",
                     "overall_before": r.get("overall"), "overall_after": None,
                     "reviewed_at": None}
    doc = {"run_id": run_id, "project_id": pid, "threshold": dict(_DEFAULT_THRESHOLD),
           "updated_at": _now(), "requirements": reqs}
    _write_json(path, doc)
    return doc


def save_review(pid: str, run_id: str, doc: dict) -> dict:
    doc["updated_at"] = _now()
    _write_json(os.path.join(_review_dir(pid, run_id), "review.json"), doc)
    return doc


def upsert_req_review(pid: str, run_id: str, req_id: str, patch: dict) -> dict | None:
    doc = get_review(pid, run_id)
    if not doc or req_id not in doc.get("requirements", {}):
        return None
    entry = doc["requirements"][req_id]
    for k in ("status", "final_text", "note", "overall_after", "refinement", "classification"):
        if k in patch:
            entry[k] = patch[k]
    # `reviewed_at` means "a human or the refinement loop touched this requirement".
    # Classification is machine metadata about unchanged text, so it must not forge one.
    if any(k in patch for k in ("status", "final_text", "note", "overall_after", "refinement")):
        entry["reviewed_at"] = _now()
    save_review(pid, run_id, doc)
    return entry


def set_threshold(pid: str, run_id: str, threshold: dict) -> dict | None:
    doc = get_review(pid, run_id)
    if not doc:
        return None
    t = {"mode": threshold.get("mode", "avg_ge"), "value": float(threshold.get("value", 4.3))}
    if "pct" in threshold:
        t["pct"] = float(threshold["pct"])
    doc["threshold"] = t
    save_review(pid, run_id, doc)
    return t


# ---- problem statement & coverage profile (versioned, human-ratifiable) ----

def get_problem_statement(pid: str) -> dict | None:
    return _read_json(os.path.join(_project_dir(pid), "problem_statement.json"))


def save_problem_statement(pid: str, statement: dict, ratified: bool = False) -> dict:
    if not get_project(pid):
        return None
    cur = get_problem_statement(pid) or {"version": 0}
    doc = {"version": cur.get("version", 0) + 1, "ratified": bool(ratified),
           "updated_at": _now(), "statement": statement}
    _write_json(os.path.join(_project_dir(pid), "problem_statement.json"), doc)
    return doc


def get_coverage_profile(pid: str) -> dict | None:
    return _read_json(os.path.join(_project_dir(pid), "coverage_profile.json"))


def save_coverage_profile(pid: str, profile: dict) -> dict:
    if not get_project(pid):
        return None
    cur = get_coverage_profile(pid) or {"version": 0}
    doc = {"version": cur.get("version", 0) + 1, "updated_at": _now(), "profile": profile}
    _write_json(os.path.join(_project_dir(pid), "coverage_profile.json"), doc)
    return doc


# ---- coverage runs (domain-judge panel output) ----

def _coverage_dir(pid: str, run_id: str) -> str:
    return os.path.join(_project_dir(pid), "coverage", run_id)


def save_coverage_run(pid: str, run_id: str, coverage: dict, meta: dict) -> None:
    _write_json(os.path.join(_coverage_dir(pid, run_id), "coverage.json"), coverage)
    _write_json(os.path.join(_coverage_dir(pid, run_id), "meta.json"), meta)
    proj = get_project(pid)
    if proj is not None:
        runs = [r for r in proj.get("coverage_runs", []) if r.get("run_id") != run_id]
        runs.append(meta)
        proj["coverage_runs"] = runs
        _write_json(os.path.join(_project_dir(pid), "meta.json"), proj)


def list_coverage_runs(pid: str) -> list[dict]:
    proj = get_project(pid)
    return proj.get("coverage_runs", []) if proj else []


def get_coverage(pid: str, run_id: str | None = None) -> dict | None:
    runs = list_coverage_runs(pid)
    if run_id is None:
        if not runs:
            return None
        run_id = sorted(runs, key=lambda r: r.get("finished_at") or "")[-1]["run_id"]
    return _read_json(os.path.join(_coverage_dir(pid, run_id), "coverage.json"))
