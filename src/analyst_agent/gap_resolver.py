"""Resolve Planner-surfaced requirement gaps (the Planner->Analyst feedback loop).

The Planner, decomposing a requirement into buildable tasks, hits gaps it cannot fill on its
own (a schema's fields, a business/authorization rule). Rather than dead-ending them as plan
questions, each is routed back here: the `analyst_gap_resolver` agent decides a disposition —

  refine       rewrite the requirement to INCLUDE the missing detail (derivable, intent-preserving)
  author       a new, distinct derived requirement is needed
  needs_input  genuine product content only a human can supply -> one consolidated question
  dismiss      out of scope for this system
  wont_do      never requested -> the feature is intentionally absent; no block, no question

When `apply` is set (auto by default), a `refine` updates the requirement's `final_text` in the
run's review (which flows into the package the Planner reads), so the Planner can re-plan ONLY the
affected requirements. Nothing is fabricated: needs_input asks, wont_do/dismiss just record.
See documents/planner_gap_feedback_loop.md.
"""

from __future__ import annotations

from collections import Counter

from analyst_agent import store as pj
from analyst_agent.coverage import compact_problem_statement
from analyst_agent.llm.client import AgentServerClient

RESOLVER_AGENT = "analyst_gap_resolver"
DISPOSITIONS = {"refine", "author", "needs_input", "dismiss", "wont_do"}


def _latest_run_id(pid: str) -> str | None:
    runs = pj.list_quality_runs(pid) or []
    if not runs:
        return None
    runs = sorted(runs, key=lambda r: r.get("finished_at", ""))
    return runs[-1].get("run_id")


def _requirement_text(pid: str, run_id: str | None, req_id: str) -> str:
    if not run_id:
        return ""
    review = pj.get_review(pid, run_id) or {}
    e = (review.get("requirements") or {}).get(req_id) or {}
    return e.get("final_text") or e.get("original_text") or ""


def resolve_gap(gap: dict, problem_statement: str, client: AgentServerClient) -> dict:
    """Decide a disposition for one gap. Pure — applies nothing."""
    user = (f"REQUIREMENT ({gap.get('req_id', '')}): {gap.get('requirement_text', '')}\n"
            f"PROBLEM STATEMENT: {problem_statement}\n"
            f"GAP: {gap.get('gap', '')}\n"
            f"QUESTION: {gap.get('question', '')}")
    out = client.complete_json(RESOLVER_AGENT, user) or {}
    disp = (out.get("disposition") or "").strip().lower()
    if disp not in DISPOSITIONS:
        disp = "needs_input"
    return {
        "req_id": gap.get("req_id"),
        "disposition": disp,
        "rationale": out.get("rationale", ""),
        "refined_text": (out.get("refined_text") or "").strip(),
        "authored_requirement": (out.get("authored_requirement") or "").strip(),
        "question": (out.get("question") or "").strip(),
        "source_question": gap.get("question", ""),
        "gap": gap.get("gap", ""),
    }


def resolve_planner_gaps(pid: str, gaps: list[dict], apply: bool = True,
                         client: AgentServerClient | None = None, progress=None) -> dict:
    """Resolve every gap; when `apply`, mutate the requirement set (refine updates `final_text`).
    Returns {run_id, total, records[], affected_req_ids[], counts{}} and persists it."""
    client = client or AgentServerClient()
    run_id = _latest_run_id(pid)
    ps_text = compact_problem_statement(pj.get_problem_statement(pid))

    records: list[dict] = []
    affected: set[str] = set()
    total = len(gaps)
    for i, g in enumerate(gaps, 1):
        if not g.get("requirement_text") and run_id:
            g["requirement_text"] = _requirement_text(pid, run_id, g.get("req_id"))
        rec = resolve_gap(g, ps_text, client)
        rec["applied"] = False
        if apply and run_id:
            if rec["disposition"] == "refine" and rec["refined_text"]:
                pj.upsert_req_review(pid, run_id, rec["req_id"],
                                     {"final_text": rec["refined_text"], "status": "gap_refined"})
                rec["applied"] = True
                affected.add(rec["req_id"])
            elif rec["disposition"] == "author" and rec["authored_requirement"]:
                # Recorded now; inserting a NEW requirement into the scorecard/review is the
                # heavier authoring path (author:run) — surfaced for review, applied separately.
                affected.add(rec["req_id"])
        records.append(rec)
        if progress:
            progress(i, total, rec)

    result = {
        "run_id": run_id, "total": total, "applied": bool(apply),
        "records": records,
        "affected_req_ids": sorted(affected),
        "counts": dict(Counter(r["disposition"] for r in records)),
    }
    pj.save_planner_gap_resolution(pid, result)
    return result
