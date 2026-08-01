"""Gap assessor — decide how to proceed on each coverage gap.

Coverage finds gaps; this triages each one, for THIS system, into a disposition:

  author       real + in scope + closeable by a requirement  → draft one (authoring)
  needs_input  real + in scope, but needs a VALUE only a human has (an SLO target, a
               retention period, which compliance regime applies) → a focused question
  dismiss      genuinely out of scope for this system         → recorded, not authored

NO GAP IS SILENTLY DROPPED. A dismiss carries a rationale and stays visible in the
package. The assessor is deliberately CONSERVATIVE about dismiss — a wrong dismiss
ships a real hole to the Architect — so it biases toward author / needs_input.

Measured on the Restaurant project's 42 blocking gaps: 17 author, 24 needs_input,
1 dismiss (Budget/Schedule — correctly, as project management, not a requirement).

This is the triage layer between coverage and action: it turns "42 blockers" into a
plan the human approves, rather than authoring a requirement for every gap
indiscriminately (which would draft a sanctions-compliance requirement for a menu app).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Iterator

from analyst_agent import config, coverage as coverage_mod, store as pj
from analyst_agent.llm.client import AgentServerClient, LLMError

GAP_ASSESSOR_AGENT = "incose_gap_assessor"
DISPOSITIONS = ("author", "needs_input", "dismiss")

# How many existing requirements to show the assessor for context.
SAMPLE_SIZE = 12


def _assessor_input(gap: dict, problem: str, sample: list[str]) -> str:
    lines = ["GAP",
             f"  domain:   {gap.get('domain_name') or gap.get('domain', '')}",
             f"  title:    {gap.get('title', '')}",
             f"  severity: {gap.get('severity', '')}",
             f"  detail:   {gap.get('detail', '')}",
             f"  question: {gap.get('question', '')}"]
    if problem:
        lines += ["", "PROBLEM STATEMENT", problem]
    if sample:
        lines += ["", f"EXISTING REQUIREMENTS (sample, {len(sample)})"]
        lines += [f"  - {t}" for t in sample]
    return "\n".join(lines)


def assess_gap(gap: dict, problem: str, sample: list[str],
               client: AgentServerClient | None = None) -> dict:
    """Triage one gap. Never raises.

    On any failure the disposition is `needs_input` — the SAFE direction: a gap the
    assessor could not judge surfaces to a human rather than being auto-authored or
    silently dismissed.
    """
    client = client or AgentServerClient()
    fallback = {"disposition": "needs_input", "applies_to_system": True,
                "rationale": "", "question": gap.get("question", "")}
    try:
        r = client.complete_json(GAP_ASSESSOR_AGENT, _assessor_input(gap, problem, sample))
    except (LLMError, AttributeError, KeyError, TypeError) as e:
        return {**fallback, "error": f"{type(e).__name__}: {e}"}
    if not isinstance(r, dict):
        return {**fallback, "error": f"unexpected shape: {type(r).__name__}"}

    disp = str(r.get("disposition") or "").strip().lower()
    if disp not in DISPOSITIONS:
        disp = "needs_input"                          # unknown verdict → surface, don't drop
    return {
        "disposition": disp,
        "applies_to_system": bool(r.get("applies_to_system", disp != "dismiss")),
        "rationale": str(r.get("rationale") or "")[:500],
        "question": str(r.get("question") or "")[:400] if disp == "needs_input" else "",
    }


def _gap_key(gap: dict) -> str:
    return f"{gap.get('domain', '')}::{gap.get('title', '')}"


def iter_assess_gaps(pid: str, client: AgentServerClient | None = None,
                     should_cancel=None) -> Iterator[dict]:
    """Assess every coverage gap for a project; yield progress and persist the result.

    Emits one `gap_assessed` event per gap and a final `gap_assessment_summary`. The
    persisted assessment is keyed to the coverage run it was computed from, so a later
    coverage run does not silently invalidate it.
    """
    client = client or AgentServerClient()
    cancelled = lambda: bool(should_cancel and should_cancel())  # noqa: E731

    coverage = pj.get_coverage(pid)
    if not coverage:
        yield {"type": "error", "stage": "gap_assess", "message": "no coverage run to assess"}
        return
    gaps = coverage.get("gaps") or []

    ps_doc = pj.get_problem_statement(pid) or {}
    problem = coverage_mod.compact_problem_statement(ps_doc.get("statement") or {})
    scorecard = pj.get_quality_scorecard(pid) or {}
    sample = [r.get("text", "") for r in scorecard.get("requirements", [])[:SAMPLE_SIZE]]

    yield {"type": "stage", "stage": "gap_assess", "status": "start", "done": 0,
           "total": len(gaps), "unit": "gaps",
           "message": f"assessing {len(gaps)} coverage gap(s)"}

    # Bounded concurrency == the server's slot count (config.LLM_CONCURRENCY).
    assessed: list[dict] = []
    done = 0
    with ThreadPoolExecutor(max_workers=config.LLM_CONCURRENCY) as ex:
        futures = {ex.submit(assess_gap, g, problem, sample, client): g for g in gaps}
        from concurrent.futures import as_completed
        for fut in as_completed(futures):
            if cancelled():
                ex.shutdown(wait=False, cancel_futures=True)
                yield {"type": "cancelled", "stage": "gap_assess"}
                return
            gap = futures[fut]
            verdict = fut.result()
            record = {**gap, "gap_key": _gap_key(gap), **verdict}
            assessed.append(record)
            done += 1
            yield {"type": "gap_assessed", "gap_key": record["gap_key"], "done": done,
                   "total": len(gaps), "disposition": verdict["disposition"],
                   "severity": gap.get("severity"), "title": gap.get("title", "")}

    import collections
    by_disp = dict(collections.Counter(a["disposition"] for a in assessed))
    # Stamp the coverage run this was computed from, so a later coverage run makes
    # the assessment detectably STALE rather than silently mismatched (adjustment 2).
    runs = pj.list_coverage_runs(pid)
    latest_cov = sorted(runs, key=lambda r: r.get("finished_at") or "")[-1]["run_id"] if runs else None
    result = {"coverage_run": latest_cov, "coverage_run_gaps": len(gaps),
              "problem_statement_version": ps_doc.get("version"),
              "by_disposition": by_disp, "gaps": assessed}
    pj.save_gap_assessment(pid, result)

    yield {"type": "stage", "stage": "gap_assess", "status": "done",
           "done": len(gaps), "total": len(gaps),
           "message": f"{by_disp}"}
    yield {"type": "gap_assessment_summary", "data": {
        "total": len(gaps), "by_disposition": by_disp,
        "author": by_disp.get("author", 0), "needs_input": by_disp.get("needs_input", 0),
        "dismiss": by_disp.get("dismiss", 0)}}
