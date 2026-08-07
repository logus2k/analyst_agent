"""Requirements Coverage — the domain-judge panel (Phase 4). See specs/projects_mode/.

A panel of coverage judges, one per domain (`catalog/domains.json`) — the set-level
analog of the per-requirement INCOSE C1–C9 judges. Each judge, in parallel, reads the
project's PROBLEM STATEMENT + REQUIREMENTS + the domain's knowledge (relevant archetype
slices + standard-pack leaves) and returns: coverage level, addressed concerns, and
GAPS (missing/underspecified requirements) with severity, a pointed question, and
grounding. The results are aggregated into a coverage report. One generic `coverage_judge`
preset is called per domain (the domain context is passed in the user content).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Iterator

from analyst_agent import framing
from analyst_agent import config
from analyst_agent import store as pj
from analyst_agent.jobs import _ingest
from analyst_agent.llm.client import AgentServerClient
from analyst_agent.llm.retrieval import embed, rerank
from analyst_agent.segment.pipeline import segment_items

_SEV = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def gather_requirements(pid: str, client: AgentServerClient) -> list[dict]:
    """The requirement set to assess. Prefer the latest Quality run's requirements
    (already segmented); otherwise segment the project's documents on the fly so
    Coverage can run standalone."""
    sc = pj.get_quality_scorecard(pid)
    if sc and sc.get("requirements"):
        return [{"req_id": r.get("req_id"), "text": r.get("text"),
                 "source": (r.get("provenance") or {}).get("source_document")}
                for r in sc["requirements"] if r.get("text")]
    reqs: list[dict] = []
    for d in pj.list_documents(pid):
        path = pj.document_path(pid, d["id"])
        if not path:
            continue
        try:
            for r in segment_items(_ingest(path), client=client):
                if r.duplicate_of is None:
                    reqs.append({"req_id": r.req_id, "text": r.text, "source": d["filename"]})
        except Exception:  # noqa: BLE001 — a bad doc shouldn't sink the run
            continue
    return reqs


def _v(x) -> str:
    return x.get("value", "") if isinstance(x, dict) else (x or "")


def compact_problem_statement(ps: dict | None) -> str:
    if not ps:
        return "(no problem statement — infer intent from the requirements)"
    parts = [f"purpose: {_v(ps.get('purpose'))}"]
    if ps.get("context"):
        parts.append(f"context: {_v(ps.get('context'))}")
    caps = [_v(c) for c in ps.get("capabilities", []) if _v(c)]
    if caps:
        parts.append("capabilities: " + "; ".join(caps))
    sc = ps.get("scope") or {}
    if sc.get("in"):
        parts.append("scope in: " + "; ".join(sc["in"]))
    if sc.get("out"):
        parts.append("scope out: " + "; ".join(sc["out"]))
    cons = [_v(c) for c in ps.get("constraints", []) if _v(c)]
    if cons:
        parts.append("constraints: " + "; ".join(cons))
    return "\n".join(parts)


def _domain_input(dom: dict, prof_archetypes: list[str], archetypes: dict,
                  standards: list[dict], ps_compact: str, reqs_text: str,
                  dismissed: list[dict] | None = None) -> str:
    lines = [f"DOMAIN: {dom['id']} — {dom['name']}",
             f"DOMAIN CONCERNS: {'; '.join(dom.get('concerns', []))}",
             f"DOMAIN QUESTIONS: {'; '.join(dom.get('questions', []))}", ""]
    # Gaps a human already reviewed and ruled OUT OF SCOPE for this system. Re-raising
    # them is churn: coverage is stateless, so without this it re-finds the same
    # out-of-scope gaps (budget, geopolitics, schedule) on every run. Measured: this is
    # the single biggest source of the loop failing to converge.
    if dismissed:
        lines.append("ALREADY REVIEWED AND RULED OUT OF SCOPE — DO NOT raise these again:")
        for g in dismissed:
            lines.append(f"  - {g.get('title', '')}: {g.get('reason', '')}")
        lines.append("")
    priors = []
    for aid in prof_archetypes:
        a = archetypes.get(aid)
        sl = a and a.get("domains", {}).get(dom["id"])
        if sl:
            priors.append(f"[{a['name']}] concerns: {'; '.join(sl.get('concerns', []))}. "
                          f"typical requirements: {'; '.join(sl.get('typical_requirements', []))}")
    if priors:
        lines.append("ARCHETYPE PRIORS FOR THIS DOMAIN (systems like this):")
        lines.extend(priors)
        lines.append("")
    leaves = [f"{lf['name']} ({pack['id']}): {lf.get('description', '')}"
              for pack in standards for lf in pack.get("leaves", []) if lf.get("domain") == dom["id"]]
    if leaves:
        lines.append("STANDARD EXPECTATIONS FOR THIS DOMAIN:")
        lines.extend(leaves[:12])
        lines.append("")
    lines += ["PROBLEM STATEMENT:", ps_compact, "",
              "REQUIREMENTS (what is currently specified):", reqs_text]
    return "\n".join(lines)


def iter_coverage_for_project(pid: str, client: AgentServerClient | None = None,
                              should_cancel=None) -> Iterator[dict]:
    """Run the coverage domain-judge panel over a project; yield progress events and a
    final `coverage` event. Judges run in parallel; each domain emits as it completes.
    `should_cancel` is an optional zero-arg predicate polled between domains for abort."""
    client = client or AgentServerClient()
    domains = framing.load_domains().get("domains", [])
    archetypes = {a["id"]: a for a in framing.load_archetypes()}
    standards = framing.load_standards()

    ps_doc = pj.get_problem_statement(pid) or {}
    ps_compact = compact_problem_statement(ps_doc.get("statement"))
    profile = (pj.get_coverage_profile(pid) or {}).get("profile") or {}
    prof_archetypes = [a["id"] for a in profile.get("archetypes", [])] or list(archetypes)

    # Dismissed gaps, grouped by domain, fed back so the judge stops re-raising them.
    dismissed_by_domain: dict[str, list[dict]] = {}
    for g in pj.get_dismissed_gaps(pid).values():
        dismissed_by_domain.setdefault(g.get("domain", ""), []).append(g)

    yield {"type": "stage", "stage": "requirements", "status": "start"}
    reqs = gather_requirements(pid, client)
    reqs_text = "\n".join(f"{i + 1}. {r['text']}" for i, r in enumerate(reqs)) or "(no requirements specified yet)"
    yield {"type": "stage", "stage": "requirements", "status": "done",
           "message": f"{len(reqs)} requirements"}

    yield {"type": "stage", "stage": "judges", "status": "start", "total": len(domains)}
    results: list[dict] = [None] * len(domains)

    def judge(idx: int, dom: dict):
        body = _domain_input(dom, prof_archetypes, archetypes, standards, ps_compact, reqs_text,
                             dismissed=dismissed_by_domain.get(dom["id"]))
        try:
            out = client.complete_json("coverage_judge", body)
        except Exception as e:  # noqa: BLE001
            out = {"coverage": "unknown", "addressed": [], "gaps": [], "error": str(e)}
        out["id"], out["name"] = dom["id"], dom["name"]
        out.setdefault("gaps", [])
        out.setdefault("addressed", [])
        return idx, out

    done = 0
    ex = ThreadPoolExecutor(max_workers=config.LLM_CONCURRENCY)
    try:
        futs = {ex.submit(judge, i, d): i for i, d in enumerate(domains)}
        for f in as_completed(futs):
            if should_cancel and should_cancel():
                break
            idx, out = f.result()
            results[idx] = out
            done += 1
            yield {"type": "domain", "data": {"id": out["id"], "coverage": out.get("coverage"),
                                              "gaps": len(out.get("gaps", []))},
                   "done": done, "total": len(domains)}
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    if should_cancel and should_cancel():
        yield {"type": "cancelled", "stage": "judges"}
        return
    yield {"type": "stage", "stage": "judges", "status": "done",
           "done": len(domains), "total": len(domains)}

    all_gaps = []
    for out in results:
        for g in out.get("gaps", []):
            all_gaps.append({**g, "domain": out["id"], "domain_name": out["name"]})
    all_gaps.sort(key=lambda g: _SEV.get(g.get("severity"), 4))

    # Fix 1 — reworded-dismissed suppression. The prompt tells the judge not to
    # re-raise dismissed gaps, but a judge can reword around it (measured: 1 of 6
    # leaked). Catch the rewordings semantically: auto-dismiss any gap whose meaning
    # matches an already-dismissed gap. Embedding cosine (equivalence), not the
    # reranker (relevance) — same reasoning as `questions._merge_similar`.
    suppressed = _auto_dismiss_reworded(pid, all_gaps)
    if suppressed:
        yield {"type": "stage", "stage": "judges", "status": "note",
               "message": f"{suppressed} reworded dismissed gap(s) auto-suppressed"}
    # Collapse cross-domain duplicates (the same concern raised by several domain judges).
    _before = len(all_gaps)
    all_gaps = dedup_gaps(all_gaps)
    if len(all_gaps) != _before:
        yield {"type": "stage", "stage": "judges", "status": "note",
               "message": f"merged {_before - len(all_gaps)} cross-domain duplicate gap(s)"}
    summary = {}
    for out in results:
        summary[out.get("coverage", "unknown")] = summary.get(out.get("coverage", "unknown"), 0) + 1
    enrichments = [{"domain": out["id"], "domain_name": out["name"], "text": enr}
                   for out in results for enr in (out.get("enrichments") or [])]

    # Cross-domain synthesis: merge/rank into top priorities + overall coverage & confidence.
    yield {"type": "stage", "stage": "synthesis", "status": "start"}
    synthesis = _synthesize(all_gaps, summary, len(reqs), ps_doc, client)
    yield {"type": "stage", "stage": "synthesis", "status": "done"}

    coverage = {
        "problem_statement_version": ps_doc.get("version"),
        "requirement_count": len(reqs),
        "domains": results,
        "gaps": all_gaps,
        "summary": summary,
        "enrichments": enrichments,
        "synthesis": synthesis,
    }
    yield {"type": "coverage", "data": coverage}


DEDUP_SIMILARITY = 0.80     # reranker score; two gaps at/above this name the SAME concern
_SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _gap_desc(g: dict) -> str:
    return f"{g.get('title','')}. {g.get('detail','')}"[:400]


def dedup_gaps(gaps: list[dict], threshold: float = DEDUP_SIMILARITY, desc_fn=None) -> list[dict]:
    """Collapse gaps/questions that name the SAME underlying concern (the 16-domain panel raises
    'auth'/'validation'/'error-handling' separately in several domains; the planner asks 'the
    fields of X' from several requirements). Greedy RERANKER clustering: the first item of a
    cluster is the representative; each later duplicate folds into it — recording every `domain`
    it spanned (`domains`), every `req_id` (`req_ids`), and the count (`duplicate_count`) — and
    the cluster keeps the most severe severity. `desc_fn` builds the text compared (default:
    title+detail; pass a question-based one for planner gaps). Fails OPEN (returns the input
    unchanged) if the reranker is unavailable — never silently drops anything."""
    desc_fn = desc_fn or _gap_desc
    kept: list[dict] = []
    reps: list[str] = []
    for g in gaps or []:
        merged = None
        if reps:
            try:
                scores = rerank(desc_fn(g), reps)
            except Exception:                       # noqa: BLE001 — reranker down -> keep distinct
                scores = []
            if scores:
                bi = max(range(len(scores)), key=lambda i: scores[i])
                if scores[bi] >= threshold:
                    merged = kept[bi]
        if merged is None:
            g2 = dict(g)
            g2["domains"] = [g["domain"]] if g.get("domain") else []
            g2["req_ids"] = [g["req_id"]] if g.get("req_id") else []
            g2["duplicate_count"] = 1
            kept.append(g2)
            reps.append(desc_fn(g))
        else:
            merged["duplicate_count"] = merged.get("duplicate_count", 1) + 1
            if g.get("domain") and g["domain"] not in merged.get("domains", []):
                merged.setdefault("domains", []).append(g["domain"])
            if g.get("req_id") and g["req_id"] not in merged.get("req_ids", []):
                merged.setdefault("req_ids", []).append(g["req_id"])
            if _SEV_ORDER.get(g.get("severity"), 9) < _SEV_ORDER.get(merged.get("severity"), 9):
                merged["severity"] = g.get("severity")
    return kept


DISMISS_SIMILARITY = 0.80   # embedding cosine; a gap this close to a dismissed one is the same concern
                            # (measured: reworded tech-stack pair titles ~0.75, title+detail higher)


def _auto_dismiss_reworded(pid: str, gaps: list[dict]) -> int:
    """Auto-dismiss gaps that are a reworded version of an already-dismissed gap.
    Recorded (by="auto:reworded-match") and undismissable, never a silent drop.
    Returns how many were suppressed. Fails open (0) if embeddings are unavailable."""
    dismissed = pj.get_dismissed_gaps(pid)
    if not dismissed or not gaps:
        return 0
    dis = list(dismissed.values())
    def desc(g):
        # title + the gap's OWN content (detail). Never the dismissal `reason` — that
        # describes WHY it was dismissed, not WHAT it is, and tanks the match (0.62 vs 0.80).
        return f"{g.get('title','')}. {g.get('detail','')}"[:400]
    dis_keys = set(dismissed)
    fresh = [g for g in gaps
             if f"{g.get('domain','')}::{g.get('title','')}" not in dis_keys]
    if not fresh:
        return 0
    try:
        dvecs = embed([desc(d) for d in dis])
        gvecs = embed([desc(g) for g in fresh])
    except Exception:                                  # noqa: BLE001 — embeddings down
        return 0
    if len(dvecs) != len(dis) or len(gvecs) != len(fresh):
        return 0
    n = 0
    for g, gv in zip(fresh, gvecs):
        best = max((sum(a * b for a, b in zip(gv, dv)) for dv in dvecs), default=0.0)
        if best >= DISMISS_SIMILARITY:
            key = f"{g.get('domain','')}::{g.get('title','')}"
            pj.dismiss_gap(pid, key,
                           f"reworded duplicate of a dismissed gap (cosine {best:.2f})",
                           by="auto:reworded-match", title=g.get("title", ""),
                           severity=g.get("severity", ""), domain=g.get("domain", ""),
                           detail=g.get("detail", ""))
            n += 1
    return n


def _fallback_synthesis(all_gaps: list[dict], summary: dict, req_count: int, ps_doc: dict) -> dict:
    order = ["absent", "weak", "partial", "strong"]
    present = [k for k in order if summary.get(k)]
    crit = len([g for g in all_gaps if g.get("severity") == "critical"])
    ndoms = len({g["domain"] for g in all_gaps})
    return {"overall_coverage": present[0] if present else "unknown",
            "confidence": "low" if req_count < 5 else "medium",
            "headline": f"{crit} critical gap(s) across {ndoms} domain(s); requirements are under-specified."
                        if all_gaps else "No material coverage gaps found.",
            "top_priorities": [{"title": g["title"], "severity": g.get("severity"),
                                "domains": [g["domain"]], "why": g.get("detail", ""),
                                "question": g.get("question", "")} for g in all_gaps[:10]],
            "fallback": True}


def _synthesize(all_gaps: list[dict], summary: dict, req_count: int,
                ps_doc: dict, client: AgentServerClient) -> dict:
    fallback = _fallback_synthesis(all_gaps, summary, req_count, ps_doc)
    if not all_gaps:
        return fallback
    body = (f"CONTEXT: {req_count} requirements; per-domain coverage summary: {summary}.\n\n"
            f"GAPS (domain | severity | title):\n"
            + "\n".join(f"{g['domain']} | {g.get('severity')} | {g.get('title')}" for g in all_gaps))
    try:
        out = client.complete_json("coverage_synthesizer", body)
        if isinstance(out, dict) and isinstance(out.get("top_priorities"), list):
            return out
    except Exception:  # noqa: BLE001 — synthesis is best-effort; fall back
        pass
    return fallback
