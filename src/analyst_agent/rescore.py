"""Re-assessment of reviewed requirements — recompute quality after a human revises.

A review changes a requirement's TEXT (`final_text`); its quality must be recomputed
from that text, not carried over from the original document scoring. This module does
exactly that, and it is the engine behind both triggers the product exposes:

  • per-Accept (incremental)  — the caller re-scores one requirement as it is accepted;
  • batch "Re-Run" (on demand) — re-score every requirement whose text changed since it
    was last scored, then recompute the set-level (duplicates / consistency) once.

EFFICIENCY: `changed_only` (default) re-scores only requirements whose `final_text`
differs from the text last scored (`scored_text`). Unchanged requirements keep their
valid scores; nothing is wasted.

SET-LEVEL is deliberately a batch-only step. Per-requirement C1–C9 is what the dashboard
headline averages, so an incremental accept fixes that number immediately. C10–C15
(duplication, consistency) are properties of the whole set and need the reranker over all
pairs — expensive, and not part of the headline — so they are recomputed once per batch,
never per accept.

NON-DESTRUCTIVE: the immutable quality run is never touched. Results are written into the
review session (`characteristics`, `overall_after`, `scored_text`, …) and surfaced by
`store.merged_scorecard`. `original_text` and the original scorecard are preserved.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Iterator

from analyst_agent import config
from analyst_agent import store as pj
from analyst_agent.assess import assess_requirement
from analyst_agent.llm.client import AgentServerClient
from analyst_agent.score.setlevel import assess_set

WORKERS = config.LLM_CONCURRENCY


def _score_one(text: str, client: AgentServerClient) -> dict:
    """Assess one requirement's text and shape the result the way the scorecard stores
    it: characteristics as a dict keyed C1–C9, plus judge-health counters."""
    res = assess_requirement(text, client=client, review=False)
    chars_list = res.get("characteristics") or []
    chars = {c["id"]: c for c in chars_list if c.get("id")}
    ok = sum(1 for c in chars_list if c.get("score") is not None)
    return {
        "characteristics": chars,
        "overall_after": res.get("overall"),
        "deterministic_findings": res.get("deterministic") or [],
        "judges_ok": ok,
        "judges_total": len(chars_list),
        "scored_text": text,
    }


def rescore_requirement(pid: str, run_id: str, req_id: str,
                        client: AgentServerClient | None = None) -> dict | None:
    """Re-score a single reviewed requirement's current text and persist the full
    breakdown. This is the per-Accept path; returns the stored patch (or None if the
    requirement has no text yet)."""
    client = client or AgentServerClient()
    review = pj.get_review(pid, run_id)
    if not review:
        return None
    e = (review.get("requirements") or {}).get(req_id)
    if not e:
        return None
    text = (e.get("final_text") or e.get("original_text") or "").strip()
    if not text:
        return None
    patch = _score_one(text, client)
    pj.upsert_req_review(pid, run_id, req_id, patch)
    return patch


def _is_stale(e: dict) -> bool:
    """True when this requirement's current text has not been scored yet."""
    text = (e.get("final_text") or e.get("original_text") or "").strip()
    if not text:
        return False
    if not e.get("characteristics"):
        return True                                 # never re-scored
    return (e.get("scored_text") or "").strip() != text


def iter_rescore_for_project(pid: str, run_id: str,
                             changed_only: bool = True, set_level: bool = True,
                             client: AgentServerClient | None = None,
                             should_cancel=None) -> Iterator[dict]:
    """Re-score reviewed requirements and (optionally) recompute set-level. Yields
    progress events shaped like the other jobs (`stage`, `rescored`, `rescore_summary`)."""
    client = client or AgentServerClient()
    cancelled = lambda: bool(should_cancel and should_cancel())  # noqa: E731

    review = pj.get_review(pid, run_id)
    if not review:
        yield {"type": "error", "stage": "rescore", "message": "no review session for this run"}
        return

    reqs = review.get("requirements") or {}
    todo = [rid for rid, e in reqs.items()
            if (e.get("final_text") or e.get("original_text") or "").strip()
            and (not changed_only or _is_stale(e))]

    yield {"type": "stage", "stage": "rescore", "status": "start", "done": 0,
           "total": len(todo), "unit": "requirements",
           "message": f"re-scoring {len(todo)} changed requirement(s)"
                      if changed_only else f"re-scoring all {len(todo)} requirements"}

    done = 0
    ex = ThreadPoolExecutor(max_workers=WORKERS)
    try:
        for start in range(0, len(todo), WORKERS):
            if cancelled():
                yield {"type": "cancelled", "stage": "rescore"}
                return
            wave = todo[start:start + WORKERS]
            texts = {rid: (reqs[rid].get("final_text")
                           or reqs[rid].get("original_text") or "").strip() for rid in wave}
            futures = [(rid, ex.submit(_score_one, texts[rid], client)) for rid in wave]
            for rid, fut in futures:
                patch = fut.result()
                pj.upsert_req_review(pid, run_id, rid, patch)
                done += 1
                yield {"type": "rescored", "req_id": rid, "done": done, "total": len(todo),
                       "overall": patch.get("overall_after")}
    finally:
        ex.shutdown(wait=False, cancel_futures=True)

    yield {"type": "stage", "stage": "rescore", "status": "done",
           "done": done, "total": len(todo), "message": f"{done} re-scored"}

    # Set-level over the CURRENT text of the whole set (not just the changed ones) —
    # editing one requirement can create or remove a duplicate with any other.
    if set_level and not cancelled():
        yield {"type": "stage", "stage": "setlevel", "status": "start",
               "message": "recomputing duplicates / consistency"}
        sc = pj.get_quality_scorecard(pid, run_id) or {}
        cur = pj.get_review(pid, run_id) or {}
        rv = cur.get("requirements") or {}
        set_reqs = []
        for r in sc.get("requirements", []):
            rid = r.get("req_id")
            e = rv.get(rid) or {}
            text = (e.get("final_text") or r.get("text") or "").strip()
            if text:
                set_reqs.append({"id": rid, "text": text})
        try:
            sl = assess_set(set_reqs, client=client)
            pj.set_review_set_level(pid, run_id, sl)
            yield {"type": "stage", "stage": "setlevel", "status": "done",
                   "message": f"{len(sl.get('overlaps') or [])} overlap(s) confirmed"}
        except Exception as e:  # noqa: BLE001 — set-level must not fail the whole re-score
            yield {"type": "stage", "stage": "setlevel", "status": "done",
                   "message": f"set-level skipped: {type(e).__name__}: {e}"}

    merged = pj.merged_scorecard(pid, run_id) or {}
    yield {"type": "rescore_summary", "data": {
        "rescored": done,
        "overall_health": (merged.get("aggregates") or {}).get("overall_health"),
        "total": len((merged.get("requirements") or []))}}
