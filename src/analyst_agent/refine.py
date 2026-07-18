"""The refinement loop — the Analyst's defining capability.

Every requirement below the acceptance threshold is improved in a **bounded** loop:
ask the INCOSE reviewer for a rewrite, re-score it with the same nine judges, keep
the best-scoring version, and stop early the moment a pass fails to improve. A
requirement that still cannot clear the bar is **escalated** as `needs_human` — it
is never silently dropped, and never left mid-loop.

MEANING PRESERVATION (why this is not just "auto-fix"):
`original_text` is immutable and every attempt is recorded (text, score, delta) so a
human can audit semantic drift before release. A requirement can score 5/5 and no
longer mean what the stakeholder wrote; raising an INCOSE score is not the same as
preserving intent. That is precisely why release requires human sign-off — see
documents/technical_architecture.md §6 and §8.

Threshold is a PER-REQUIREMENT FLOOR: every requirement must clear it, not the
project average.
"""
from __future__ import annotations

from typing import Iterator

from analyst_agent import store as pj
from analyst_agent.assess import assess_requirement
from analyst_agent.llm.client import AgentServerClient

MAX_ATTEMPTS = 3


def _first_rewrite(assessment: dict) -> str | None:
    """The reviewer's top proposal for this text, if it offered one."""
    rewrites = ((assessment.get("review") or {}).get("rewrites")) or []
    for w in rewrites:
        t = (w.get("text") or "").strip()
        if t:
            return t
    return None


def _refine_one(original_text: str, start_score: float | None, threshold: float,
                client: AgentServerClient) -> dict:
    """Bounded improve→re-score loop for a single requirement.

    Returns {final_text, final_score, attempts, history, status}. `history` holds one
    entry per attempt so semantic drift is auditable.
    """
    best_text, best_score = original_text, start_score
    history: list[dict] = []

    # Score the current text once to obtain BOTH its score and a rewrite proposal;
    # each later attempt reuses the proposal's own assessment, so it is one LLM
    # round per attempt rather than two.
    cur = assess_requirement(best_text, client=client, review=True)
    if cur.get("overall") is not None:
        best_score = cur["overall"]

    for attempt in range(1, MAX_ATTEMPTS + 1):
        if best_score is not None and best_score >= threshold:
            break
        proposal = _first_rewrite(cur)
        if not proposal or proposal.strip() == best_text.strip():
            break                                   # nothing new to try
        nxt = assess_requirement(proposal, client=client, review=True)
        new_score = nxt.get("overall")
        history.append({"attempt": attempt, "text": proposal,
                        "score_before": best_score, "score_after": new_score})
        if new_score is None or (best_score is not None and new_score <= best_score):
            break                                   # no improvement -> stop early, keep best
        best_text, best_score, cur = proposal, new_score, nxt

    passed = best_score is not None and best_score >= threshold
    return {
        "final_text": best_text,
        "final_score": best_score,
        "attempts": len(history),
        "history": history,
        "status": "accepted_refined" if passed else "needs_human",
    }


def iter_refine_for_project(pid: str, run_id: str,
                            client: AgentServerClient | None = None,
                            should_cancel=None) -> Iterator[dict]:
    """Refine every below-threshold requirement of a run; yield progress events.

    Writes results into the run's review state: `final_text`, `overall_after`,
    `status` (accepted_refined | needs_human) and the attempt `history`.
    `original_text` is left untouched.
    """
    client = client or AgentServerClient()
    cancelled = lambda: bool(should_cancel and should_cancel())  # noqa: E731

    review = pj.get_review(pid, run_id)
    if not review:
        yield {"type": "error", "stage": "refine", "message": "no review session for this run"}
        return

    threshold = float((review.get("threshold") or {}).get("value", 4.3))
    reqs = review.get("requirements") or {}

    def _current_score(e: dict) -> float | None:
        return e.get("overall_after") if e.get("overall_after") is not None else e.get("overall_before")

    todo = [rid for rid, e in reqs.items()
            if e.get("status") != "skipped" and (
                _current_score(e) is None or _current_score(e) < threshold)]

    yield {"type": "stage", "stage": "refine", "status": "start", "done": 0,
           "total": len(todo), "unit": "requirements",
           "message": f"{len(todo)} of {len(reqs)} below {threshold}"}

    refined = escalated = 0
    for i, rid in enumerate(todo, 1):
        if cancelled():
            yield {"type": "cancelled", "stage": "refine"}
            return
        e = reqs[rid]
        text = e.get("final_text") or e.get("original_text") or ""
        if not text.strip():
            continue
        out = _refine_one(text, _current_score(e), threshold, client)
        pj.upsert_req_review(pid, run_id, rid, {
            "status": out["status"],
            "final_text": out["final_text"],
            "overall_after": out["final_score"],
            "refinement": {"attempts": out["attempts"], "history": out["history"]},
        })
        if out["status"] == "accepted_refined":
            refined += 1
        else:
            escalated += 1
        yield {"type": "refined", "req_id": rid, "done": i, "total": len(todo),
               "score_before": _current_score(e), "score_after": out["final_score"],
               "attempts": out["attempts"], "status": out["status"]}

    yield {"type": "stage", "stage": "refine", "status": "done",
           "done": len(todo), "total": len(todo),
           "message": f"{refined} refined, {escalated} escalated to needs_human"}
    yield {"type": "refine_summary", "data": {
        "threshold": threshold, "considered": len(todo),
        "refined": refined, "needs_human": escalated}}
