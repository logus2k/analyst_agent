"""The convergence loop — drive a requirement set to complete and at/above threshold.

The Analyst owns delivering a complete set; it does not report problems and hand
them over. Quality and coverage interact — authored requirements change the set,
which changes coverage — so this runs rounds to a fixpoint instead of one pass.

A round sequences pieces that already work on their own:

    refine  →  coverage  →  author
    (below threshold)  (gap count)  (one requirement per gap)

TERMINATION IS ONE NUMBER: the gap count from coverage.

    gaps == 0 and quality clean  →  converged
    count stops dropping         →  stalled   (ask the human)
    MAX_ROUNDS reached           →  capped    (safety backstop only)

Counting *rounds* alone would say we stopped, not that we are done — so the round
cap is a backstop, never the completion test. Nothing is matched between rounds:
an earlier design carried a gap ledger and asked a judge per gap whether it was
closed; that solved gap identity, which only buys per-gap attempt caps, and cost
78 calls a round against coverage's 16. Removed. If repeat authoring or spurious
stalls show up in practice, that is when identity earns its place.

NOISE. Coverage is an LLM: two identical runs will not return the same count. So
progress requires a real drop (`MIN_DROP`), and `FLAT_ROUNDS_BEFORE_STALL`
consecutive non-improving rounds are tolerated before declaring a plateau.
"""

from __future__ import annotations

from typing import Iterator

from analyst_agent import (authoring, coverage as coverage_mod, questions as questions_mod,
                           refine, store as pj)
from analyst_agent.llm.client import AgentServerClient

MAX_ROUNDS = 6
# Consecutive rounds without a real drop before we call it a plateau. >1 because
# coverage's count wobbles run to run and one flat round is not evidence.
FLAT_ROUNDS_BEFORE_STALL = 2
# How much the gap count must fall to count as progress rather than variance.
MIN_DROP = 1

CONVERGED = "converged"
STALLED = "stalled"
CAPPED = "capped"
CANCELLED = "cancelled"


def _quality_state(pid: str, run_id: str) -> dict:
    """Below-threshold / incompletely-judged / placeholder counts for the set.

    Mirrors the release blockers so the loop's exit test and the gate agree —
    converging on a set the gate would then reject is the failure mode here.
    """
    scorecard = pj.get_quality_scorecard(pid, run_id) or {}
    review = pj.get_review(pid, run_id) or {}
    threshold = float((review.get("threshold") or {}).get("value", 4.3))
    entries = review.get("requirements") or {}

    below = incomplete = placeholders = 0
    total = 0
    for r in scorecard.get("requirements", []):
        if (r.get("lineage") or {}).get("duplicate_of"):
            continue
        total += 1
        e = entries.get(r.get("req_id")) or {}
        score = e.get("overall_after") if e.get("overall_after") is not None else r.get("overall")
        if score is None or score < threshold:
            below += 1
        ok, want = r.get("judges_ok"), r.get("judges_total")
        if ok is not None and want is not None and ok < want:
            incomplete += 1
        if authoring.unresolved_placeholders(e.get("final_text") or r.get("text", "")):
            placeholders += 1
    return {"total": total, "threshold": threshold, "below_threshold": below,
            "incompletely_judged": incomplete, "with_placeholders": placeholders}


def _quality_clean(q: dict) -> bool:
    return not (q["below_threshold"] or q["incompletely_judged"] or q["with_placeholders"])


def get_state(pid: str) -> dict | None:
    return pj.get_convergence(pid)


def iter_converge(pid: str, run_id: str, client: AgentServerClient | None = None,
                  should_cancel=None, max_rounds: int = MAX_ROUNDS) -> Iterator[dict]:
    """Run rounds until the set converges, plateaus, or hits the cap.

    Persists loop state at every round boundary, so a crash or restart leaves a
    readable record of where it got to rather than nothing.
    """
    client = client or AgentServerClient()
    cancelled = lambda: bool(should_cancel and should_cancel())  # noqa: E731

    if not pj.get_quality_scorecard(pid, run_id):
        yield {"type": "error", "stage": "converge", "message": "no quality run to converge"}
        return

    state = {"run_id": run_id, "state": "converging", "round": 0,
             "gap_counts": [], "rounds": [], "outcome": None}
    pj.save_convergence(pid, state)

    flat = 0
    prev_gaps: int | None = None

    for rnd in range(1, max_rounds + 1):
        if cancelled():
            state.update(state=CANCELLED, outcome=CANCELLED)
            pj.save_convergence(pid, state)
            yield {"type": "cancelled", "stage": "converge"}
            return

        state["round"] = rnd
        yield {"type": "round", "round": rnd, "total": max_rounds, "status": "start"}

        # 1. refine everything below threshold (no-op when nothing is below)
        for ev in refine.iter_refine_for_project(pid, run_id, client=client,
                                                 should_cancel=should_cancel):
            yield {**ev, "round": rnd}
        if cancelled():
            state.update(state=CANCELLED, outcome=CANCELLED)
            pj.save_convergence(pid, state)
            yield {"type": "cancelled", "stage": "converge"}
            return

        # 2. coverage — the progress signal
        gaps = None
        cov_run_id = f"{run_id}-r{rnd}"
        for ev in coverage_mod.iter_coverage_for_project(pid, client=client,
                                                         should_cancel=should_cancel):
            if ev.get("type") == "coverage":
                data = ev["data"]
                gaps = len(data.get("gaps") or [])
                pj.save_coverage_run(pid, cov_run_id, data, {
                    "run_id": cov_run_id, "project_id": pid, "kind": "coverage",
                    "finished_at": pj._now(), "converge_round": rnd,
                    "requirement_count": data.get("requirement_count"), "gap_count": gaps})
            else:
                yield {**ev, "round": rnd}
        if cancelled():
            state.update(state=CANCELLED, outcome=CANCELLED)
            pj.save_convergence(pid, state)
            yield {"type": "cancelled", "stage": "converge"}
            return
        if gaps is None:                       # coverage produced nothing usable
            state.update(state=STALLED, outcome=STALLED,
                         reason="coverage produced no result")
            pj.save_convergence(pid, state)
            yield {"type": "converge_done", "outcome": STALLED,
                   "reason": "coverage produced no result", "round": rnd}
            return

        quality = _quality_state(pid, run_id)
        state["gap_counts"].append(gaps)
        yield {"type": "round_signal", "round": rnd, "gaps": gaps,
               "previous_gaps": prev_gaps, "quality": quality}

        # 3. done?
        if gaps == 0 and _quality_clean(quality):
            state["rounds"].append({"round": rnd, "gaps": gaps, "quality": quality,
                                    "authored": 0})
            state.update(state=CONVERGED, outcome=CONVERGED)
            pj.save_convergence(pid, state)
            yield {"type": "converge_done", "outcome": CONVERGED, "round": rnd,
                   "gaps": 0, "quality": quality}
            return

        # 4. progress? (a real drop, not variance)
        if prev_gaps is not None and gaps > prev_gaps - MIN_DROP:
            flat += 1
        else:
            flat = 0
        prev_gaps = gaps

        if flat >= FLAT_ROUNDS_BEFORE_STALL:
            state["rounds"].append({"round": rnd, "gaps": gaps, "quality": quality,
                                    "authored": 0})
            reason = (f"gap count stopped dropping ({state['gap_counts']}) — the "
                      f"remaining {gaps} gap(s) need information the documents do not contain")
            qs = questions_mod.collect_questions(pid, run_id)
            state.update(state=STALLED, outcome=STALLED, reason=reason,
                         questions=qs, question_summary=questions_mod.summarize(qs))
            pj.save_convergence(pid, state)
            yield {"type": "converge_done", "outcome": STALLED, "round": rnd,
                   "gaps": gaps, "quality": quality, "reason": reason,
                   "questions": qs, "question_summary": questions_mod.summarize(qs)}
            return

        # 5. author for the open gaps (last round: skip — nothing would score it)
        authored = 0
        if rnd < max_rounds:
            for ev in authoring.iter_author_for_project(pid, run_id, client=client,
                                                        should_cancel=should_cancel):
                if ev.get("type") == "author_summary":
                    authored = ev["data"].get("authored", 0)
                yield {**ev, "round": rnd}

        state["rounds"].append({"round": rnd, "gaps": gaps, "quality": quality,
                                "authored": authored})
        pj.save_convergence(pid, state)
        yield {"type": "round", "round": rnd, "status": "done", "gaps": gaps,
               "authored": authored}

    quality = _quality_state(pid, run_id)
    reason = f"reached the {max_rounds}-round cap without converging"
    qs = questions_mod.collect_questions(pid, run_id)
    state.update(state=CAPPED, outcome=CAPPED, reason=reason,
                 questions=qs, question_summary=questions_mod.summarize(qs))
    pj.save_convergence(pid, state)
    yield {"type": "converge_done", "outcome": CAPPED, "round": max_rounds,
           "gaps": prev_gaps, "quality": quality, "reason": reason,
           "questions": qs, "question_summary": questions_mod.summarize(qs)}
