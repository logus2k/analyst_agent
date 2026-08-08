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
                client: AgentServerClient, source_context: str = "") -> dict:
    """Bounded improve→re-score loop for a single requirement.

    `source_context` (problem statement + the item's source section) lets the reviewer
    COMPOSE the obvious action for terse feature-label requirements ("Opening hours" →
    "The tenant shall configure the restaurant's opening hours") — the Analyst's defining
    prose→INCOSE capability. Returns {final_text, final_score, attempts, history, status}.
    """
    best_text, best_score = original_text, start_score
    history: list[dict] = []

    # Score the current text once to obtain BOTH its score and a rewrite proposal;
    # each later attempt reuses the proposal's own assessment, so it is one LLM
    # round per attempt rather than two.
    cur = assess_requirement(best_text, client=client, review=True, source_context=source_context)
    if cur.get("overall") is not None:
        best_score = cur["overall"]

    for attempt in range(1, MAX_ATTEMPTS + 1):
        if best_score is not None and best_score >= threshold:
            break
        proposal = _first_rewrite(cur)
        if not proposal or proposal.strip() == best_text.strip():
            break                                   # nothing new to try
        nxt = assess_requirement(proposal, client=client, review=True, source_context=source_context)
        new_score = nxt.get("overall")
        history.append({"attempt": attempt, "text": proposal,
                        "score_before": best_score, "score_after": new_score})
        if new_score is None or (best_score is not None and new_score <= best_score):
            break                                   # no improvement -> stop early, keep best
        best_text, best_score, cur = proposal, new_score, nxt

    passed = best_score is not None and best_score >= threshold
    # `cur` is the assessment of best_text — persist its C1–C9 breakdown so the DASHBOARD
    # (merged_scorecard) overlays the refined text/score. Without this the dashboard keeps
    # showing the pre-refine text even though final_text was updated.
    chars = cur.get("characteristics") or []
    return {
        "final_text": best_text,
        "final_score": best_score,
        "attempts": len(history),
        "history": history,
        "status": "accepted_refined" if passed else "needs_human",
        "characteristics": chars,
        "deterministic": cur.get("deterministic") or [],
        "judges_ok": sum(1 for c in chars if c.get("score") is not None),
        "judges_total": len(chars),
    }


def _adjudicate(text: str, source_context: str, client: AgentServerClient) -> dict:
    """Common-sense SECOND pass for a requirement still below threshold: decide reject | revise
    (compose / supply a reasonable default value) | keep. Fails SAFE to 'keep'."""
    user = f"REQUIREMENT: {text}\n\n{source_context}"
    out = client.complete_json("requirement_adjudicator", user) or {}
    dec = (out.get("decision") or "").strip().lower()
    if dec not in ("reject", "revise", "keep"):
        dec = "keep"
    return {"decision": dec, "text": (out.get("text") or "").strip(),
            "assumption": (out.get("assumption") or "").strip(),
            "rationale": (out.get("rationale") or "").strip()}


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

    # Context so the reviewer can COMPOSE the obvious action for terse feature-labels: the
    # problem statement + each requirement's source SECTION and the SURROUNDING source text
    # (neighbouring bullets) — a section name alone is too thin to compose e.g. "AI integration".
    from analyst_agent.coverage import compact_problem_statement
    ps_ctx = compact_problem_statement(pj.get_problem_statement(pid))
    sc = pj.get_quality_scorecard(pid, run_id) or {}
    prov_by_id = {r.get("req_id"): (r.get("provenance") or {}) for r in sc.get("requirements", [])}
    _docs = pj.list_documents(pid) or []
    _doc_text = ""
    if _docs:
        try:
            _p = pj.document_path(pid, _docs[0]["id"])
            _doc_text = open(_p, encoding="utf-8").read() if _p else ""
        except OSError:
            _doc_text = ""

    # The reviewer/adjudicator need enough of the SRS to infer intent. The model slot is 32K, so
    # for a typical SRS (a few KB) we send the WHOLE document; only when it would crowd the slot
    # do we fall back to a generous window around this requirement. (Summarising an oversized SRS
    # while keeping the relevant section verbatim is the next step for very large inputs.)
    _MAX_DOC_CTX = 16000

    def _source_context(rid: str) -> str:
        prov = prov_by_id.get(rid) or {}
        parts = []
        if ps_ctx:
            parts.append(f"PROBLEM STATEMENT: {ps_ctx}")
        if prov.get("section_path"):
            parts.append(f"THIS REQUIREMENT'S SECTION: {prov['section_path']}")
        if _doc_text:
            if len(_doc_text) <= _MAX_DOC_CTX:
                parts.append("FULL SOURCE DOCUMENT (SRS):\n" + _doc_text)
            else:
                span = prov.get("char_span")
                if span and isinstance(span, (list, tuple)) and len(span) == 2:
                    a, b = max(0, span[0] - 900), min(len(_doc_text), span[1] + 900)
                    parts.append("SOURCE EXCERPT (SRS too large to include in full):\n" + _doc_text[a:b])
        return "\n".join(parts)

    def _current_score(e: dict) -> float | None:
        return e.get("overall_after") if e.get("overall_after") is not None else e.get("overall_before")

    todo = [rid for rid, e in reqs.items()
            if e.get("status") != "skipped" and (
                _current_score(e) is None or _current_score(e) < threshold)]

    yield {"type": "stage", "stage": "refine", "status": "start", "done": 0,
           "total": len(todo), "unit": "requirements",
           "message": f"{len(todo)} of {len(reqs)} below {threshold}"}

    refined = escalated = rejected = 0
    for i, rid in enumerate(todo, 1):
        if cancelled():
            yield {"type": "cancelled", "stage": "refine"}
            return
        e = reqs[rid]
        text = e.get("final_text") or e.get("original_text") or ""
        if not text.strip():
            continue
        out = _refine_one(text, _current_score(e), threshold, client, _source_context(rid))
        # COMMON-SENSE SECOND PASS: anything still below threshold is adjudicated — rejected as a
        # non-requirement, revised (composed / given a reasonable default value), or kept.
        adj = None
        if out["status"] == "needs_human":
            adj = _adjudicate(out["final_text"], _source_context(rid), client)
            if adj["decision"] == "reject":
                out["status"] = "rejected"
            elif adj["decision"] == "revise" and adj["text"]:
                a = assess_requirement(adj["text"], client=client, review=False,
                                       source_context=_source_context(rid))
                chars = a.get("characteristics") or []
                out.update({
                    "final_text": adj["text"], "final_score": a.get("overall"),
                    "characteristics": chars, "deterministic": a.get("deterministic") or [],
                    "judges_ok": sum(1 for c in chars if c.get("score") is not None),
                    "judges_total": len(chars),
                    "status": "accepted_refined" if (a.get("overall") or 0) >= threshold else "needs_human",
                })
        # The scorecard/merged_scorecard store characteristics as a DICT keyed C1–C9 (same shape
        # rescore.py writes); assess_requirement returns a LIST — convert before saving.
        chars_dict = {c["id"]: c for c in (out["characteristics"] or [])
                      if isinstance(c, dict) and c.get("id")}
        pj.upsert_req_review(pid, run_id, rid, {
            "status": out["status"],
            "final_text": out["final_text"],
            "overall_after": out["final_score"],
            "refinement": {"attempts": out["attempts"], "history": out["history"],
                           "adjudication": adj},
            # persist the breakdown so the dashboard overlays the refined text/score
            "characteristics": chars_dict,
            "deterministic_findings": out["deterministic"],
            "scored_text": out["final_text"],
            "judges_ok": out["judges_ok"],
            "judges_total": out["judges_total"],
        })
        if out["status"] == "accepted_refined":
            refined += 1
        elif out["status"] == "rejected":
            rejected += 1
        else:
            escalated += 1
        yield {"type": "refined", "req_id": rid, "done": i, "total": len(todo),
               "score_before": _current_score(e), "score_after": out["final_score"],
               "attempts": out["attempts"], "status": out["status"]}

    yield {"type": "stage", "stage": "refine", "status": "done",
           "done": len(todo), "total": len(todo),
           "message": f"{refined} refined, {rejected} rejected, {escalated} escalated to needs_human"}
    yield {"type": "refine_summary", "data": {
        "rejected": rejected,
        "threshold": threshold, "considered": len(todo),
        "refined": refined, "needs_human": escalated}}
