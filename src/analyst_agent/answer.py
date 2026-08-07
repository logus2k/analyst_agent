"""Human-in-the-loop resolution of Planner-surfaced gaps (`needs_input` / `flagged`).

The Planner's `needs_input` questions are usually MISSING requirements, not defects in the
requirement they trace to (the traced requirement typically already scores well). So answering
a question can:

  - REFINE the traced requirement (when it really is thin), and/or
  - AUTHOR one or more new requirements (when the answer describes something never written),

possibly BOTH for one question. The human's answer is run through the INCOSE refiner, scored,
and shown; the human then picks/edits the final text before anything is applied. `preview` is
pure (commits nothing); `apply` writes the chosen text(s).

Reuses the existing machinery: `refine._refine_one` (bounded improve→re-score, non-committing),
`assess.assess_requirement` (single-req C1–C9 + rewrite), `rescore.rescore_requirement` (persist
the breakdown for a reviewed text), and `authoring` (`_scorecard_record`/`_persist`/`_next_gap_seq`)
to insert an `origin:"analyst_authored"` requirement. See documents/planner_gap_feedback_loop.md.
"""

from __future__ import annotations

from analyst_agent import authoring, store as pj
from analyst_agent.assess import assess_requirement
from analyst_agent.llm.client import AgentServerClient
from analyst_agent.refine import _refine_one


def _threshold(pid: str, run_id: str) -> float:
    review = pj.get_review(pid, run_id) or {}
    return float((review.get("threshold") or {}).get("value", 4.3))


def _entry(pid: str, run_id: str, req_id: str) -> dict:
    review = pj.get_review(pid, run_id) or {}
    return (review.get("requirements") or {}).get(req_id) or {}


def _current_text_score(e: dict) -> tuple[str, float | None]:
    text = e.get("final_text") or e.get("original_text") or ""
    score = e.get("overall_after") if e.get("overall_after") is not None else e.get("overall_before")
    return text, score


def preview(pid: str, run_id: str, req_id: str, question: str, answer: str,
            client: AgentServerClient | None = None) -> dict:
    """Non-committing. Produce BOTH candidate resolutions for one answered question:

      refine  — the traced requirement with the answer folded in, refined + re-scored
      author  — the answer as a NEW standalone requirement, refined + scored

    Returns the candidate texts and their INCOSE scores so the human can choose/edit before
    applying. Nothing is written.
    """
    client = client or AgentServerClient()
    answer = (answer or "").strip()
    thr = _threshold(pid, run_id)
    cur_text, cur_score = _current_text_score(_entry(pid, run_id, req_id))

    # REFINE candidate: fold the answer into the existing requirement text.
    refine_seed = f"{cur_text} {answer}".strip() if cur_text else answer
    rf = _refine_one(refine_seed, cur_score, thr, client)
    # AUTHOR candidate: the answer as its own requirement.
    au = _refine_one(answer, None, thr, client)

    return {
        "req_id": req_id,
        "question": question,
        "answer": answer,
        "threshold": thr,
        "refine": {"original_text": cur_text, "score_before": cur_score,
                   "candidate_text": rf["final_text"], "score_after": rf["final_score"]},
        "author": {"candidate_text": au["final_text"], "score": au["final_score"]},
    }


def _author_insert(pid: str, run_id: str, text: str, question: str,
                   client: AgentServerClient) -> str | None:
    """Score `text` and append it to the run's scorecard + review as an authored requirement
    (`GAP-nnnn`, `origin:"analyst_authored"`). Returns the new req_id, or None if the run is
    missing. Reuses the coverage-authoring persist path so authored reqs behave identically."""
    scorecard = pj.get_quality_scorecard(pid, run_id)
    if not scorecard:
        return None
    seq = authoring._next_gap_seq(scorecard) + 1
    req_id = f"GAP-{seq:04d}"
    assessment = assess_requirement(text.strip(), client=client, review=True)
    gap = {"title": (question or "")[:200], "severity": "", "domain": "",
           "domain_name": "", "grounding": []}
    drafted = {"rationale": "authored from a Planner gap answer", "assumptions": [],
               "needs_input": False, "question": ""}
    rec = authoring._scorecard_record(req_id, assessment, gap, drafted, refinement=None)
    authoring._persist(pid, run_id, scorecard, [rec])
    return req_id


def apply(pid: str, run_id: str, req_id: str, refine_text: str | None = None,
          author_texts: list[str] | None = None, question: str = "", answer: str = "",
          client: AgentServerClient | None = None) -> dict:
    """Commit the human's chosen resolution(s). `refine_text` updates the traced requirement's
    `final_text` (and re-scores it); each `author_texts` entry becomes a new authored requirement.
    ALSO records the answered gap on the ORIGIN requirement so the Planner sees the question as
    resolved on re-plan — otherwise authoring a sibling requirement leaves the origin unchanged
    and the auto-loop re-asks the same question forever. Returns the changed req ids."""
    from analyst_agent import rescore as rescore_mod
    client = client or AgentServerClient()
    affected: list[str] = []
    authored: list[str] = []

    if refine_text and refine_text.strip():
        patch = {"final_text": refine_text.strip(), "status": "gap_answered"}
        if pj.upsert_req_review(pid, run_id, req_id, patch) is not None:
            rescore_mod.rescore_requirement(pid, run_id, req_id)   # persist C1–C9 for the new text
            affected.append(req_id)

    for txt in (author_texts or []):
        new_id = _author_insert(pid, run_id, txt, question, client) if (txt or "").strip() else None
        if new_id:
            authored.append(new_id)
            affected.append(new_id)

    # Record the answered gap on the origin requirement (close the loop). The resolution the
    # Planner needs is the concrete content the human supplied — the answer plus any authored
    # requirement text — which the decomposer/gate will treat as specified for this requirement.
    resolution = " ".join(t.strip() for t in ([refine_text] + list(author_texts or [])) if t and t.strip())
    e = _entry(pid, run_id, req_id)
    prior = list(e.get("answered_gaps") or [])
    prior.append({"question": (question or "").strip(),
                  "answer": (answer or "").strip(),
                  "resolution": resolution.strip()})
    pj.upsert_req_review(pid, run_id, req_id, {"answered_gaps": prior})
    affected.append(req_id)     # the origin's context changed -> re-plan it

    return {"req_id": req_id, "affected_req_ids": sorted(set(affected)),
            "authored_req_ids": authored}
