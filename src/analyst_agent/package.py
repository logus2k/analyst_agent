"""The Architect handover package — the Analyst's output contract.

Everything the Analyst knows about a requirement lives in two files that nobody
joins: the quality run's `scorecard.json` (text, provenance, lineage, the nine
characteristic scores, deterministic findings, set-level analysis) and the review
session's `review.json` (classification, refined text, refinement history, status).
This module performs that join and emits one self-describing document.

DESIGN RULE: **deliver more, not less.** The Architect can ignore a field it does
not need; it cannot invent one that was dropped. So the package carries the full
characteristic objects, every deterministic finding, the refinement history and
the original text — not just the summary score. The one thing that is NOT optional
is `classes[]`: without it the Architect has to re-run its own classifier, which is
precisely the duplicated work classification exists to remove.

FIELD NAMING: the key is **`req_id`**, matching the scorecard and the Architect's
documented contract ("trace links must use it verbatim"). The Analyst's own
technical_architecture.md §7 example says `id`; that example is the one that is
wrong, because the trace key has to be stable across both agents.

RELEASE STATUS: this module reports readiness, it does not grant it. `release_status`
is whatever the project actually holds — `draft` until a human signs off (see §8 of
technical_architecture.md). A draft package is a legitimate deliverable for Architect
development; it simply must never be mistaken for an approved one, which is exactly
what the manifest prevents.
"""
from __future__ import annotations

from analyst_agent import store as pj
from analyst_agent.authoring import unresolved_placeholders

# Coverage-gap severities that BLOCK release. Critical and high are the ones that
# damage downstream design (user directive, 2026-07-31); medium/low are reported
# but do not block. Set to the full tuple to enforce decision 3 ("all severities").
BLOCKING_GAP_SEVERITIES = ("critical", "high")

def _requirement_record(req: dict, review_entry: dict | None) -> dict:
    """Join one scorecard requirement with its review/classification state."""
    e = review_entry or {}
    cls = e.get("classification") or {}
    chars = req.get("characteristics") or {}
    findings = req.get("deterministic_findings") or []

    # The released text is the refined one when refinement improved it; the original
    # is kept alongside so semantic drift stays auditable downstream.
    original = e.get("original_text") or req.get("text", "")
    current = e.get("final_text") or req.get("text", "")
    score = e.get("overall_after") if e.get("overall_after") is not None else req.get("overall")

    return {
        "req_id": req.get("req_id"),
        "text": current,

        # --- Architect routing contract ---
        "classes": cls.get("classes") or [],
        "type": cls.get("type"),
        "constraints": cls.get("constraints") or [],
        "classification_rationale": cls.get("justification") or "",

        # --- quality evidence ---
        "analysis": {
            "score": score,
            "score_before_refinement": e.get("overall_before"),
            "characteristics": chars,                    # full objects, not just scores
            "characteristic_scores": {k: v.get("score") for k, v in chars.items()
                                      if isinstance(v, dict)},
            "rules_triggered": sorted({f["rule_id"] for f in findings if f.get("rule_id")}),
            "deterministic_findings": findings,
            "review": req.get("review"),                 # reviewer rewrites/advisories
            # How many of the 9 judges actually answered. A score averaged over
            # fewer is not comparable to a complete one and must not clear the gate.
            "judges_ok": req.get("judges_ok"),
            "judges_total": req.get("judges_total"),
            "status": e.get("status") or "unreviewed",
            "original_text": original,
            "text_changed": current.strip() != original.strip(),
            "refinement": e.get("refinement"),           # per-attempt history, if refined
        },

        # Human answers to this requirement's planner-surfaced gaps (question + resolution). The
        # Planner injects these as authoritative context so an answered question is not re-asked.
        "answered_gaps": e.get("answered_gaps") or [],
        "lineage": req.get("lineage"),
        "provenance": req.get("provenance"),
    }


def build_package(pid: str, run_id: str | None = None) -> dict | None:
    """Assemble the full handover package for a project's quality run.

    Returns None if the project or run has no scorecard. Never mutates state.
    """
    project = pj.get_project(pid)
    if not project:
        return None

    runs = pj.list_quality_runs(pid)
    if not runs:
        return None
    if run_id is None:
        run_id = sorted(runs, key=lambda r: r.get("finished_at") or "")[-1]["run_id"]

    # Use the MERGED scorecard (raw run overlaid with the review's re-scored characteristics
    # where an edit/refine produced an authoritative breakdown) — the same view the dashboard
    # reads. The raw scorecard keeps the PRE-EDIT C1–C9 for every edited requirement, so its
    # per-characteristic scores contradict the (correctly updated) overall_after. The package
    # feeds the Architect/Planner and the release gate, so it must show the current text's
    # scores, not the stale ones.
    scorecard = pj.merged_scorecard(pid, run_id) or pj.get_quality_scorecard(pid, run_id)
    if not scorecard:
        return None

    review = pj.get_review(pid, run_id) or {}
    structure = pj.get_structure(pid) or {}
    entries = review.get("requirements") or {}
    threshold = float((review.get("threshold") or {}).get("value", 4.3))
    # Semantic-plausibility verdicts (analyst_sense_judge). Overlaid onto each requirement and
    # gated as a hard blocker — a well-formed-but-nonsense requirement must not reach the Architect.
    sense = pj.get_sense(pid) or {}
    sense_results = sense.get("results") or {}

    # The Architect consumes a clean set: duplicates never leave the Analyst.
    records, excluded = [], 0
    for req in scorecard.get("requirements", []):
        if (req.get("lineage") or {}).get("duplicate_of"):
            excluded += 1
            continue
        rec = _requirement_record(req, entries.get(req.get("req_id")))
        rec["analysis"]["plausibility"] = sense_results.get(rec["req_id"])
        records.append(rec)
    # Implausible = a sense verdict of plausible:false, restricted to requirements actually in
    # this set (not excluded duplicates).
    in_set = {r["req_id"] for r in records}
    implausible = sorted(rid for rid in (sense.get("implausible") or []) if rid in in_set)

    scored = [r["analysis"]["score"] for r in records if r["analysis"]["score"] is not None]
    at_or_above = sum(1 for s in scored if s >= threshold)
    # Absolute floor (remaining_work.md decision 1): being below threshold blocks
    # release regardless of review status. There is no human-override path — a
    # human resolves it by supplying the missing information and re-scoring.
    below = [r["req_id"] for r in records
             if r["analysis"]["score"] is None or r["analysis"]["score"] < threshold]
    unclassified = [r["req_id"] for r in records if not r["classes"]]
    # A mean over fewer than 9 judges is not comparable to a complete one.
    incomplete = [r["req_id"] for r in records
                  if (r["analysis"].get("judges_ok") is not None
                      and r["analysis"].get("judges_total") is not None
                      and r["analysis"]["judges_ok"] < r["analysis"]["judges_total"])]
    # Unfilled placeholders. Observed live: an authored requirement reading
    # "...latency of less than [LATENCY_VALUE]..." scored 4.56 and would have
    # cleared a 4.3 threshold — the judges rate the form of a statement, and a
    # parameterized statement is well-formed. Checked here for EVERY requirement,
    # not just authored ones, because a human edit can introduce one too.
    placeholdered = [r["req_id"] for r in records
                     if unresolved_placeholders(r.get("text", ""))]
    # Analyst-authored gap fillers need human ratification before release.
    authored = [r["req_id"] for r in records
                if (r.get("provenance") or {}).get("origin") == "analyst_authored"]
    unratified = [r["req_id"] for r in records
                  if (r.get("provenance") or {}).get("origin") == "analyst_authored"
                  and not (r.get("provenance") or {}).get("ratified")]

    ps = pj.get_problem_statement(pid)
    coverage = pj.get_coverage(pid)
    profile = pj.get_coverage_profile(pid)

    # Readiness is REPORTED so a consumer can decide for itself; the Analyst never
    # self-promotes to `validated` (technical_architecture.md §8).
    release_status = review.get("release_status") or "draft"
    # HARD blockers are the analytical preconditions a human cannot wave through — they must
    # be FIXED, not approved around. The missing sign-off is NOT one of them (it is exactly
    # what the human is about to provide), so `can_release` excludes it: a set may be ready
    # to sign off while still `draft`.
    hard_blockers = []
    if below:
        hard_blockers.append(f"{len(below)} requirement(s) below threshold {threshold}")
    if incomplete:
        hard_blockers.append(f"{len(incomplete)} requirement(s) scored on fewer than all judges")
    if placeholdered:
        hard_blockers.append(f"{len(placeholdered)} requirement(s) contain unfilled "
                             f"placeholders (e.g. [VALUE], TBD)")
    if unratified:
        hard_blockers.append(f"{len(unratified)} analyst-authored requirement(s) not ratified")
    if unclassified:
        hard_blockers.append(f"{len(unclassified)} requirement(s) missing routing classes")
    if not ps:
        hard_blockers.append("no problem statement")
    elif not ps.get("ratified"):
        hard_blockers.append("problem statement not ratified")
    if not coverage:
        hard_blockers.append("no coverage run")
    else:
        # Open coverage gaps at a damaging severity block release. A gap is a hole in
        # the SET's completeness; shipping it lets an incomplete spec reach the
        # Architect, which is exactly what the gap-authoring + convergence loop exists
        # to prevent. Critical and high are the severities that "damage the next
        # phases" (user directive, 2026-07-31); medium/low are reported, not blocking.
        # Closing a gap = authoring a requirement that covers it (a fresh coverage run
        # then no longer reports it), never editing this list.
        # Dismissed gaps (human-confirmed out of scope) no longer block — but they
        # stay recorded and visible; a dismissal is auditable, not a silent drop.
        dismissed = pj.get_dismissed_gaps(pid)
        blocking_gaps = [g for g in (coverage.get("gaps") or [])
                         if g.get("severity") in BLOCKING_GAP_SEVERITIES
                         and f"{g.get('domain','')}::{g.get('title','')}" not in dismissed]
        if blocking_gaps:
            import collections
            by_sev = collections.Counter(g.get("severity") for g in blocking_gaps)
            detail = ", ".join(f"{by_sev[s]} {s}" for s in BLOCKING_GAP_SEVERITIES if by_sev[s])
            hard_blockers.append(f"{len(blocking_gaps)} open coverage gap(s) "
                                 f"must be closed before release ({detail})")
    blockers = list(hard_blockers)
    if release_status != "validated":
        blockers.append("no human sign-off (release_status is not 'validated')")
    # Semantic-plausibility is ADVISORY, not a hard gate: the judge is a useful signal but
    # over-flags mild modelling awkwardness, so a human reviews the flagged requirements rather
    # than release being auto-blocked. Surfaced via manifest.implausible_requirements + per-req.
    plausibility_advisory = (
        f"{len(implausible)} requirement(s) flagged by the semantic-plausibility check for review "
        f"(well-formed but possibly incoherent): {', '.join(implausible)}" if implausible else None)

    return {
        "manifest": {
            "contract_version": "1.0",
            "generated_by": "analyst_agent",
            "project_id": pid,
            "project_name": project.get("name"),
            "run_id": run_id,
            "threshold": threshold,
            "release_status": release_status,
            "released_at": review.get("released_at"),
            "released_by": review.get("released_by"),
            "release_note": review.get("release_note"),
            "architect_ready": not blockers,
            "can_release": not hard_blockers,          # cleared to sign off (may still be draft)
            "hard_blockers": hard_blockers,
            "blockers": blockers,
            "implausible_requirements": implausible,
            "plausibility_advisory": plausibility_advisory,
            "counts": {
                "total": len(records),
                "excluded_duplicates": excluded,
                "scored": len(scored),
                "at_or_above_threshold": at_or_above,
                "below_threshold": len(below),
                "incompletely_judged": len(incomplete),
                "with_placeholders": len(placeholdered),
                # How much of this set the Analyst wrote rather than extracted.
                "analyst_authored": len(authored),
                "unratified_authored": len(unratified),
                "unclassified": len(unclassified),
                "mean_score": round(sum(scored) / len(scored), 2) if scored else None,
            },
            "below_threshold_ids": below,
            "source_documents": project.get("documents") or [],
        },
        "requirements": records,
        "set_level": scorecard.get("set_level") or {},
        "aggregates": scorecard.get("aggregates") or {},
        "characteristic_names": scorecard.get("characteristic_names") or {},
        "problem_statement": ps,
        "coverage": coverage,
        "coverage_profile": profile,
        # Project vocabulary + requirement tree (glossary of entities, controlled tags,
        # single-parent feature branches with per-node cross-cutting tags). Empty {} when
        # the structure stage has not been run. The Architect designs per branch and
        # anchors entity names to the glossary; see the Architect contract.
        "glossary": (structure.get("vocabulary") or {}).get("glossary") or [],
        "tags": (structure.get("vocabulary") or {}).get("tags") or [],
        "tree": structure.get("tree") or {},
    }


def readiness(pid: str, run_id: str | None = None) -> dict | None:
    """The release-gate verdict WITHOUT the heavy requirements payload — for the UI's
    Release panel and the sign-off endpoint. Returns the package manifest (blockers,
    `hard_blockers`, `can_release`, counts, release metadata) or None if there is no run.

    Computed from the same source as the package, so the two can never disagree. The
    requirement records are assembled (cheap, no LLM) and discarded; only the verdict is kept.
    """
    pkg = build_package(pid, run_id)
    return pkg["manifest"] if pkg else None


def render_markdown(package: dict) -> str:
    """Human-readable companion to the JSON — for review, not for machines."""
    m = package["manifest"]
    out = [f"# Requirements — {m.get('project_name') or m['project_id']}", "",
           f"- Run: `{m['run_id']}`",
           f"- Release status: **{m['release_status']}**"
           f" ({'architect-ready' if m['architect_ready'] else 'NOT architect-ready'})",
           f"- Threshold: {m['threshold']}",
           f"- Requirements: {m['counts']['total']} "
           f"({m['counts']['at_or_above_threshold']} at/above threshold, "
           f"mean {m['counts']['mean_score']})"]
    if m["counts"].get("analyst_authored"):
        out.append(f"- ⚠️ **{m['counts']['analyst_authored']} analyst-generated** to fill "
                   f"coverage gaps ({m['counts'].get('unratified_authored', 0)} not yet "
                   f"ratified) — these were written by the Analyst, not by a stakeholder")
    out.append("")
    if m["blockers"]:
        out += ["## Blockers", ""] + [f"- {b}" for b in m["blockers"]] + [""]
    out += ["## Requirements", ""]
    for r in package["requirements"]:
        a = r["analysis"]
        prov = r.get("provenance") or {}
        loc = " · ".join(str(x) for x in
                         (prov.get("source_document") or prov.get("source_file"),
                          prov.get("section_path"),
                          f"p.{prov['page']}" if prov.get("page") else None) if x)
        authored = prov.get("origin") == "analyst_authored"
        heading = f"### {r['req_id']}"
        if authored:
            # Must be unmissable: nobody asked for this requirement.
            heading += "  ⚠️ GENERATED"
        out += [heading, "", r["text"], ""]
        if authored:
            out += [f"> **⚠️ Analyst-generated to fill a coverage gap** — no stakeholder "
                    f"wrote this. Gap: _{prov.get('gap_title', '?')}_ "
                    f"({prov.get('gap_severity', '?')}, domain "
                    f"{prov.get('domain_name') or prov.get('domain', '?')}). "
                    f"Ratified: **{'yes' if prov.get('ratified') else 'NO'}**.", ""]
            if prov.get("rationale"):
                out += [f"> Rationale: {prov['rationale']}", ""]
            if prov.get("assumptions"):
                out += ["> Assumptions: " + "; ".join(prov["assumptions"]), ""]
        out += [f"- Classes: {', '.join(r['classes']) or '—'} | Type: {r['type'] or '—'}"
                f" | Constraints: {', '.join(r['constraints']) or '—'}",
                f"- Score: {a['score']} | Status: {a['status']}"
                f"{' | rules: ' + ', '.join(a['rules_triggered']) if a['rules_triggered'] else ''}"]
        if loc:
            out.append(f"- Source: {loc}")
        elif authored:
            out.append("- Source: **none — generated, not extracted from any document**")
        if a["text_changed"]:
            out.append(f"- Original: _{a['original_text']}_")
        out.append("")
    return "\n".join(out)
