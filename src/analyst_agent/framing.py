"""Problem Framing (Coverage stage 0) — see specs/projects_mode/.

Distils a structured, provenance-graded PROBLEM STATEMENT from whatever project
material exists (from a full spec down to a one-line request), plus a soft list of
candidate archetypes and salient coverage domains that later weight the domain-judge
panel. The LLM work is the `problem_framing` preset on agent_server; this module
gathers the input (project doc text + the catalog menus) and calls it.
"""

from __future__ import annotations

import glob
import json
import os

from analyst_agent.jobs import _ingest
from analyst_agent.llm.client import AgentServerClient

from analyst_agent import config

# Coverage knowledge (domains, archetypes, standards) ships with the agent.
CATALOG = os.path.join(config.KNOWLEDGE, "catalog")


def archetype_menu() -> str:
    items = []
    for f in sorted(glob.glob(os.path.join(CATALOG, "project_types", "*.json"))):
        try:
            a = json.load(open(f, encoding="utf-8"))
            items.append(f"- {a['id']}: {a['name']} — {a.get('summary', '')}")
        except (OSError, ValueError, KeyError):
            continue
    return "\n".join(items)


def domain_menu() -> str:
    try:
        d = json.load(open(os.path.join(CATALOG, "domains.json"), encoding="utf-8"))
        return "\n".join(f"- {x['id']}: {x['name']}" for x in d.get("domains", []))
    except (OSError, ValueError):
        return ""


def gather_text(doc_paths: list[str], limit: int = 24000) -> str:
    """Ingest each document to plain text (markdown in-process, binary via the
    ingest service), joined and length-bounded to fit the framing context."""
    parts = []
    for p in doc_paths:
        try:
            parts.append("\n".join(it.text for it in _ingest(p)))
        except Exception:  # noqa: BLE001 — a bad doc shouldn't sink framing
            parts.append("")
    return ("\n\n---\n\n".join(x for x in parts if x))[:limit]


def iter_frame_problem(doc_paths: list[str], user_request: str = "",
                       client: AgentServerClient | None = None,
                       should_cancel=None, limit: int = 24000):
    """Streamed framing: yields stage events (reading documents — with page counts —
    then distilling) and a final `problem_statement` event. `should_cancel` is polled
    between documents / before the LLM call for abort. `frame_problem` wraps this for
    synchronous callers."""
    client = client or AgentServerClient()
    cancelled = lambda: bool(should_cancel and should_cancel())  # noqa: E731

    yield {"type": "stage", "stage": "ingest", "status": "start", "total": len(doc_paths)}
    parts, total_pages = [], 0
    for di, p in enumerate(doc_paths):
        if cancelled():
            yield {"type": "cancelled", "stage": "ingest"}
            return
        try:
            items = _ingest(p)
            pages = max((it.page or 0 for it in items), default=0)
            parts.append("\n".join(it.text for it in items))
        except Exception:  # noqa: BLE001 — a bad doc shouldn't sink framing
            pages = 0
            parts.append("")
        total_pages += pages
        yield {"type": "stage", "stage": "ingest", "status": "progress",
               "done": di + 1, "total": len(doc_paths), "pages": total_pages,
               "message": f"{os.path.basename(p)}: {pages} page(s)"}
    yield {"type": "stage", "stage": "ingest", "status": "done",
           "done": len(doc_paths), "total": len(doc_paths), "pages": total_pages,
           "message": f"{len(doc_paths)} document(s), {total_pages} page(s)"}

    if cancelled():
        yield {"type": "cancelled", "stage": "framing"}
        return
    text = ("\n\n---\n\n".join(x for x in parts if x))[:limit]
    yield {"type": "stage", "stage": "framing", "status": "start"}
    body = (
        f"ARCHETYPES:\n{archetype_menu()}\n\n"
        f"COVERAGE DOMAINS:\n{domain_menu()}\n\n"
        f"INPUT DOCUMENTS:\n{text or '(none)'}\n\n"
        f"USER REQUEST:\n{user_request or '(none)'}"
    )
    statement = client.complete_json("problem_framing", body)
    yield {"type": "stage", "stage": "framing", "status": "done"}
    yield {"type": "problem_statement", "data": statement}


def frame_problem(doc_paths: list[str], user_request: str = "",
                  client: AgentServerClient | None = None) -> dict:
    """Return the structured problem statement (the raw preset output). Synchronous
    wrapper over `iter_frame_problem` — used by the sync `:generate` endpoint."""
    statement: dict = {}
    for ev in iter_frame_problem(doc_paths, user_request, client=client):
        if ev.get("type") == "problem_statement":
            statement = ev["data"]
    return statement


# ---- catalog reads (for the API / UI) ----

def load_domains() -> dict:
    return json.load(open(os.path.join(CATALOG, "domains.json"), encoding="utf-8"))


def load_archetypes() -> list[dict]:
    out = []
    for f in sorted(glob.glob(os.path.join(CATALOG, "project_types", "*.json"))):
        try:
            out.append(json.load(open(f, encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out


def load_standards() -> list[dict]:
    out = []
    for f in sorted(glob.glob(os.path.join(CATALOG, "standards", "*.json"))):
        try:
            out.append(json.load(open(f, encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out
