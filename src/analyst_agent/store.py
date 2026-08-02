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

def create_project(name: str, owner: str | None = None) -> dict:
    pid = uuid.uuid4().hex
    meta = {"id": pid, "name": (name or "").strip() or "Untitled project",
            "created_at": _now(), "owner": (owner or "").strip().lower() or None,
            "documents": []}
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


def get_project_threshold(pid: str) -> dict:
    """The project's acceptance threshold. Every review of the project seeds from
    this; the built-in default is 4.3. Stored on the project so a new quality run
    does not silently reset a threshold the reviewer chose."""
    proj = get_project(pid) or {}
    t = proj.get("threshold")
    if isinstance(t, dict) and t.get("value") is not None:
        return t
    return dict(_DEFAULT_THRESHOLD)


def set_project_threshold(pid: str, threshold: dict, propagate: bool = True) -> dict | None:
    """Set the project threshold, and by default push it onto every existing review
    session — otherwise changing it would have no visible effect until the next run."""
    proj = get_project(pid)
    if not proj:
        return None
    t = {"mode": threshold.get("mode", "avg_ge"), "value": float(threshold.get("value", 4.3))}
    if "pct" in threshold:
        t["pct"] = float(threshold["pct"])
    proj["threshold"] = t
    _write_json(os.path.join(_project_dir(pid), "meta.json"), proj)
    if propagate:
        for run in list_quality_runs(pid):
            doc = get_review(pid, run.get("run_id"), seed=False)
            if doc:
                doc["threshold"] = dict(t)
                save_review(pid, run.get("run_id"), doc)
    return t


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
    doc = {"run_id": run_id, "project_id": pid, "threshold": get_project_threshold(pid),
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
    # `characteristics`/`deterministic_findings`/`judges_*`/`scored_text` are the
    # authoritative per-requirement re-assessment of the reviewed text (see rescore.py);
    # they are what lets the dashboard show reviewed scores WITH a consistent C1–C9 radar.
    for k in ("status", "final_text", "note", "overall_after", "refinement", "classification",
              "characteristics", "deterministic_findings", "judges_ok", "judges_total",
              "scored_text"):
        if k in patch:
            entry[k] = patch[k]
    # `reviewed_at` means "a human or the refinement loop touched this requirement".
    # A re-score (overall_after/characteristics without a status/text change) is MACHINE
    # metadata about unchanged intent, so it stamps `scored_at`, never `reviewed_at`.
    if any(k in patch for k in ("status", "final_text", "note", "refinement")):
        entry["reviewed_at"] = _now()
    if any(k in patch for k in ("overall_after", "characteristics", "scored_text")):
        entry["scored_at"] = _now()
    save_review(pid, run_id, doc)
    return entry


def set_release_status(pid: str, run_id: str, status: str,
                       approver: str | None = None, note: str | None = None) -> dict | None:
    """Record the human release decision on a review session. `validated` stamps who signed
    off and when; anything else (e.g. `draft` on revoke) clears that metadata. The Analyst
    never sets `validated` itself — only this endpoint, driven by a human, does."""
    doc = get_review(pid, run_id)
    if not doc:
        return None
    doc["release_status"] = status
    if status == "validated":
        doc["released_at"] = _now()
        doc["released_by"] = approver or "human"
        doc["release_note"] = note or ""
    else:
        doc["released_at"] = None
        doc["released_by"] = None
        doc["release_note"] = note or ""
    save_review(pid, run_id, doc)
    return {"release_status": status, "released_at": doc.get("released_at"),
            "released_by": doc.get("released_by"), "release_note": doc.get("release_note")}


def set_review_set_level(pid: str, run_id: str, set_level: dict) -> dict | None:
    """Persist a batch-recomputed set-level result (overlaps + C10–C15) onto the review
    session. Kept separate from the immutable quality run so the original stays intact."""
    doc = get_review(pid, run_id)
    if not doc:
        return None
    doc["set_level_current"] = set_level
    save_review(pid, run_id, doc)
    return set_level


def merged_scorecard(pid: str, run_id: str | None = None) -> dict | None:
    """The quality scorecard the DASHBOARD reads: the immutable run, overlaid with the
    reviewed (re-scored) state where it exists.

    A requirement is overlaid ONLY when the review carries an authoritative characteristic
    breakdown (`characteristics`) — a bare `overall_after` from the live editor is not
    enough, because it would move the headline number while the C1–C9 radar still showed
    the pre-review scores. So the dashboard flips to reviewed scores only after a re-score
    (Accept-with-breakdown or the batch Re-Run). When nothing is overlaid the raw scorecard
    is returned byte-for-byte, so un-reviewed projects are unaffected.
    """
    runs = list_quality_runs(pid)
    if not runs:
        return None
    if run_id is None:
        run_id = sorted(runs, key=lambda r: r.get("finished_at") or "")[-1]["run_id"]
    sc = _read_json(os.path.join(_quality_dir(pid, run_id), "scorecard.json"))
    if not sc:
        return None
    review = get_review(pid, run_id, seed=False)
    if not review:
        return sc
    entries = review.get("requirements") or {}

    char_keys = list((sc.get("characteristic_names") or {}).keys())
    overlaid = 0
    merged_reqs: list[dict] = []
    for r in sc.get("requirements", []):
        e = entries.get(r.get("req_id"))
        r2 = dict(r)
        if e and e.get("characteristics"):          # authoritative breakdown present
            overlaid += 1
            r2["text"] = e.get("final_text") or r2.get("text")
            r2["characteristics"] = e["characteristics"]
            if e.get("deterministic_findings") is not None:
                r2["deterministic_findings"] = e["deterministic_findings"]
            if e.get("overall_after") is not None:
                r2["overall"] = e["overall_after"]
            if e.get("judges_ok") is not None:
                r2["judges_ok"] = e["judges_ok"]
                r2["judges_total"] = e.get("judges_total")
            r2["review_status"] = e.get("status")
        merged_reqs.append(r2)

    if not overlaid:                                # nothing reviewed-and-scored → untouched
        return sc

    sc2 = dict(sc)
    sc2["requirements"] = merged_reqs
    sc2["aggregates"] = _recompute_aggregates(sc.get("aggregates") or {}, merged_reqs, char_keys)
    if review.get("set_level_current"):
        sc2["set_level"] = review["set_level_current"]
    sc2["_merged"] = {"run_id": run_id, "overlaid": overlaid,
                      "total": len(merged_reqs)}
    return sc2


def _recompute_aggregates(base: dict, reqs: list[dict], char_keys: list[str]) -> dict:
    """Recompute the aggregates that change when reviewed scores are overlaid: the
    per-characteristic means, the score distribution, and the headline `overall_health`
    (mean of per-requirement overall — what the dashboard shows). Other keys are preserved.
    """
    ag = dict(base)
    per_char: dict[str, float] = {}
    for c in char_keys:
        vs = [s for r in reqs
              if (s := (r.get("characteristics") or {}).get(c, {}).get("score")) is not None]
        if vs:
            per_char[c] = round(sum(vs) / len(vs), 2)
    ag["per_characteristic_mean"] = per_char
    overalls = [o for r in reqs if (o := r.get("overall")) is not None]
    ag["overall_health"] = round(sum(overalls) / len(overalls), 2) if overalls else None
    dist: dict[str, int] = {}
    for o in overalls:
        b = str(int(o)) if o is not None else "na"
        dist[b] = dist.get(b, 0) + 1
    ag["score_distribution"] = dist
    ag["total"] = len(reqs)
    return ag


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



# ---- convergence loop state (Phase D) ----
#
# The loop runs for many minutes across several rounds; persisting at each round
# boundary means a crash or restart leaves a readable record of where it got to
# rather than nothing. `JobManager.jobs` is in-memory and does not survive either.

def get_convergence(pid: str) -> dict | None:
    return _read_json(os.path.join(_project_dir(pid), "convergence.json"))


def save_convergence(pid: str, state: dict) -> dict | None:
    if not get_project(pid):
        return None
    state["updated_at"] = _now()
    _write_json(os.path.join(_project_dir(pid), "convergence.json"), state)
    return state


# ---- project vocabulary + requirement tree (glossary/tags/structure) ----

def save_structure(pid: str, data: dict) -> None:
    """Persist the project's vocabulary (glossary + tags) and requirement tree."""
    _write_json(os.path.join(_project_dir(pid), "structure.json"), data)


def get_structure(pid: str) -> dict | None:
    return _read_json(os.path.join(_project_dir(pid), "structure.json"))


# ---- gap assessment (the assessor's per-gap disposition) ----
#
# One assessment per project, recomputed when gaps change. Keyed on nothing but the
# project: it is a snapshot of "how to proceed on the current gaps", overwritten each
# run. Downstream (author/questions/dismiss) reads the dispositions from here.

def get_gap_assessment(pid: str) -> dict | None:
    return _read_json(os.path.join(_project_dir(pid), "gap_assessment.json"))


def save_gap_assessment(pid: str, data: dict) -> dict | None:
    if not get_project(pid):
        return None
    data["assessed_at"] = _now()
    _write_json(os.path.join(_project_dir(pid), "gap_assessment.json"), data)
    return data


# ---- dismissed gaps (out-of-scope, recorded, never silently dropped) ----
#
# A gap the human (or the assessor, confirmed) judges NOT applicable to this system.
# Keyed by `<domain>::<title>` (the gap has no id and its wording is stable enough
# within a coverage run). Release excludes dismissed gaps from the blocking count,
# but they stay recorded — with reason and who — so a dismissal is auditable, never
# a silent drop.

def get_dismissed_gaps(pid: str) -> dict:
    return _read_json(os.path.join(_project_dir(pid), "dismissed_gaps.json")) or {}


def dismiss_gap(pid: str, gap_key: str, reason: str, by: str | None = None,
                title: str = "", severity: str = "", domain: str = "",
                detail: str = "") -> dict | None:
    if not get_project(pid):
        return None
    d = get_dismissed_gaps(pid)
    d[gap_key] = {"gap_key": gap_key, "title": title, "severity": severity,
                  "domain": domain, "detail": detail, "reason": reason,
                  "by": by, "at": _now()}
    _write_json(os.path.join(_project_dir(pid), "dismissed_gaps.json"), d)
    return d[gap_key]


def undismiss_gap(pid: str, gap_key: str) -> bool:
    d = get_dismissed_gaps(pid)
    if gap_key not in d:
        return False
    del d[gap_key]
    _write_json(os.path.join(_project_dir(pid), "dismissed_gaps.json"), d)
    return True


# ---- ratification of analyst-authored requirements ----
#
# An authored gap-filler is born `provenance.ratified = False` and blocks release
# until a human accepts it (decision 2: analyst owns completeness, human owns truth).
# Ratifying flips that flag in the scorecard. It does NOT clear other gates — a
# ratified requirement still must clear threshold and carry no placeholder.

def ratify_requirement(pid: str, run_id: str, req_id: str, ratified: bool = True,
                       by: str | None = None) -> dict | None:
    sc = get_quality_scorecard(pid, run_id)
    if not sc:
        return None
    for r in sc.get("requirements", []):
        if r.get("req_id") == req_id:
            prov = r.setdefault("provenance", {})
            if prov.get("origin") != "analyst_authored":
                return None                          # only authored requirements are ratifiable
            prov["ratified"] = bool(ratified)
            prov["ratified_by"] = by
            prov["ratified_at"] = _now() if ratified else None
            meta = next((m for m in list_quality_runs(pid) if m.get("run_id") == run_id),
                        {"run_id": run_id, "project_id": pid})
            save_quality_run(pid, run_id, sc, meta)
            return {"req_id": req_id, "ratified": bool(ratified)}
    return None


def ratify_all_authored(pid: str, run_id: str, by: str | None = None) -> dict:
    """Ratify every analyst-authored requirement in one call — the batch a reviewer
    uses after eyeballing the generated set. Returns the ids ratified."""
    sc = get_quality_scorecard(pid, run_id) or {}
    ids = []
    for r in sc.get("requirements", []):
        if (r.get("provenance") or {}).get("origin") == "analyst_authored":
            r["provenance"]["ratified"] = True
            r["provenance"]["ratified_by"] = by
            r["provenance"]["ratified_at"] = _now()
            ids.append(r.get("req_id"))
    if ids:
        meta = next((m for m in list_quality_runs(pid) if m.get("run_id") == run_id),
                    {"run_id": run_id, "project_id": pid})
        save_quality_run(pid, run_id, sc, meta)
    return {"ratified": ids, "count": len(ids)}
