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
from analyst_agent.llm.retrieval import rerank

GAP_ASSESSOR_AGENT = "incose_gap_assessor"
DISPOSITIONS = ("author", "needs_input", "dismiss")

# How many existing requirements to show the assessor for context.
SAMPLE_SIZE = 12


# --------------------------------------------------------------------------- #
# Declared scope is EVIDENCE, not decoration                                    #
# --------------------------------------------------------------------------- #
# The problem statement carries an explicit `scope.out`, and it was already being
# passed to the assessor — which nonetheless dismissed 1 gap in 66 on a live run,
# while asking the human about backup cadence and Kubernetes topology for a
# single-restaurant SQLite app. Prompt text alone does not move this model (the
# same lesson the Architect learned with its omission judge), so the scope signal
# is computed deterministically and applied in CODE.
#
# Semantic, so the RERANKER decides it — never string matching: "backup and
# restore procedures" and "disaster recovery" are the same subject in different
# words, and no amount of normalising finds that.
#
# Two bands, because a wrong dismiss ships a real hole to the Architect:
#   >= HARD  the gap IS the out-of-scope subject      -> dismissed in code, audited
#   >= SOFT  same neighbourhood                       -> handed to the assessor as
#            explicit evidence, which still makes the call
SCOPE_OUT_HARD = 0.85     # reranker sigmoid; deliberately above the 0.8 used for overlap
SCOPE_OUT_SOFT = 0.60     # the dedup threshold — "related", not "the same"


def _scope_out_match(gap: dict, scope_out: list) -> tuple:
    """(score, matched_out_scope_item) for the best match, or (0.0, "")."""
    items = [str(x).strip() for x in (scope_out or []) if str(x).strip()]
    if not items:
        return 0.0, ""
    subject = " ".join(str(gap.get(k) or "") for k in ("title", "detail")).strip()
    if not subject:
        return 0.0, ""
    try:
        scores = rerank(subject, items)
    except Exception:      # noqa: BLE001 — no reranker → no scope evidence, never a crash
        return 0.0, ""
    if not scores:
        return 0.0, ""
    best = max(range(len(scores)), key=lambda i: scores[i])
    return float(scores[best]), items[best]


def _assessor_input(gap: dict, problem: str, sample: list[str],
                    scope_evidence: str = "") -> str:
    lines = ["GAP",
             f"  domain:   {gap.get('domain_name') or gap.get('domain', '')}",
             f"  title:    {gap.get('title', '')}",
             f"  severity: {gap.get('severity', '')}",
             f"  detail:   {gap.get('detail', '')}",
             f"  question: {gap.get('question', '')}"]
    if scope_evidence:
        lines += ["", "SCOPE EVIDENCE", scope_evidence]
    if problem:
        lines += ["", "PROBLEM STATEMENT", problem]
    if sample:
        lines += ["", f"EXISTING REQUIREMENTS (sample, {len(sample)})"]
        lines += [f"  - {t}" for t in sample]
    return "\n".join(lines)


def assess_gap(gap: dict, problem: str, sample: list[str],
               client: AgentServerClient | None = None,
               scope_out: list | None = None) -> dict:
    """Triage one gap. Never raises.

    On any failure the disposition is `needs_input` — the SAFE direction: a gap the
    assessor could not judge surfaces to a human rather than being auto-authored or
    silently dismissed.
    """
    client = client or AgentServerClient()
    fallback = {"disposition": "needs_input", "applies_to_system": True,
                "rationale": "", "question": gap.get("question", "")}

    score, item = _scope_out_match(gap, scope_out or [])
    if score >= SCOPE_OUT_HARD:
        return {"disposition": "dismiss", "applies_to_system": False,
                "rationale": (f"outside the declared scope of this system — the problem "
                              f"statement puts \"{item}\" out of scope (match {score:.2f})"),
                "question": "", "scope_out_score": round(score, 3), "scope_out_item": item}
    evidence = (f"This gap's subject matches an item the problem statement declares OUT of "
                f"scope: \"{item}\" (similarity {score:.2f}). Weigh that before choosing a "
                f"disposition.") if score >= SCOPE_OUT_SOFT else ""
    try:
        r = client.complete_json(GAP_ASSESSOR_AGENT,
                                 _assessor_input(gap, problem, sample, evidence))
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
    statement = ps_doc.get("statement") or {}
    problem = coverage_mod.compact_problem_statement(statement)
    # The declared out-of-scope list, applied as evidence per gap (see _scope_out_match).
    scope_out = ((statement.get("scope") or {}).get("out")) or []
    scorecard = pj.get_quality_scorecard(pid) or {}
    sample = [r.get("text", "") for r in scorecard.get("requirements", [])[:SAMPLE_SIZE]]

    yield {"type": "stage", "stage": "gap_assess", "status": "start", "done": 0,
           "total": len(gaps), "unit": "gaps",
           "message": f"assessing {len(gaps)} coverage gap(s)"}

    # Bounded concurrency == the server's slot count (config.LLM_CONCURRENCY).
    assessed: list[dict] = []
    done = 0
    with ThreadPoolExecutor(max_workers=config.LLM_CONCURRENCY) as ex:
        futures = {ex.submit(assess_gap, g, problem, sample, client, scope_out): g for g in gaps}
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
