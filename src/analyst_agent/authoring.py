"""Gap authoring — the Analyst closes coverage gaps instead of only reporting them.

`coverage.py` says what is missing. This module writes the requirement that fills
it, scores it with the same nine judges, and refines it to the acceptance
threshold like any other requirement. See remaining_work.md decisions 2 and 3:
the Analyst owns completeness, the human owns truth.

FABRICATION IS THE RISK HERE. An authored requirement is one no stakeholder asked
for, and a plausible-sounding one survives review by looking reasonable. Three
things keep that honest, and none of them is optional:

  1. Every authored requirement cites the gap that motivated it — `provenance`
     carries `gap_id`, `gap_title`, `domain` and the catalog `grounding`. It can
     always be traced back to the analysis that demanded it.
  2. `provenance.origin = "analyst_authored"` and there is deliberately NO
     `source_document`/`page`/`bbox`. It can never be mistaken for source content.
  3. `provenance.ratified = False` until a human accepts it, and unratified
     authored requirements BLOCK release (see `package.build_package`).

Authored requirements are appended to the run's scorecard with `GAP-` ids, so the
package/review path treats them exactly like extracted ones — except for the
flags above, which every downstream surface must honour.
"""

from __future__ import annotations

import hashlib
import re
from typing import Iterator

from analyst_agent import store as pj
from analyst_agent.assess import assess_requirement
from analyst_agent.llm.client import AgentServerClient, LLMError
from analyst_agent.llm.retrieval import rerank
from analyst_agent.refine import _refine_one
from analyst_agent.score.characteristics import CHARACTERISTICS

GAP_AUTHOR_AGENT = "incose_gap_author"

# Unresolved placeholders in requirement text. OBSERVED LIVE: the author produced
# "...latency of less than [LATENCY_VALUE] for 95 percent..." and the nine judges
# scored it 4.56 — above a 4.3 threshold. The judges assess the *form* of the
# statement, and a parameterized statement is well-formed; they do not know the
# parameter was never filled in. Without this check a requirement containing
# [X] would satisfy the absolute quality floor and reach the Architect.
#
# Deterministic, not a judge: this is a lexical fact about the text, and it must
# not be subject to LLM variance.
_PLACEHOLDER_RE = re.compile(
    r"\[[^\]]{0,60}\]"              # [LATENCY_VALUE], [X], [specify maximum ...]
    r"|<[A-Za-z_][^>]{0,60}>"       # <value>, <TBD>
    r"|\bTBD\b|\bTBC\b|\bXXX\b"     # explicit markers
    r"|\bN/?A\b"
    r"|\{\{[^}]{0,60}\}\}",         # {{template}}
    re.IGNORECASE)


def unresolved_placeholders(text: str) -> list[str]:
    """Placeholder tokens left in a requirement, in order of appearance.

    A requirement carrying one states an obligation whose value nobody supplied.
    It is not releasable however well it scores.
    """
    return [m.group(0) for m in _PLACEHOLDER_RE.finditer(text or "")]

# A candidate scoring at or above this against an existing requirement is already
# covered — authoring it again would duplicate the set. Same reranker and the same
# threshold `segment.dedup` uses, for the same reason: cosine cannot separate
# "already stated" from "same topic".
DUPLICATE_THRESHOLD = 0.6

# How many existing requirements to show the author for style/vocabulary.
STYLE_SAMPLE = 12

# Authored requirements are flushed to the store every this many. A full 78-gap
# pass takes ~12 minutes; persisting only at the end means a crash or a cancel at
# gap 77 throws away 76 requirements that each cost a draft + 9 judges + possible
# refinement. Flushing in batches bounds that loss to at most FLUSH_EVERY items.
FLUSH_EVERY = 10


def gap_id(gap: dict) -> str:
    """Stable id for a gap, minted once at discovery.

    Coverage gaps carry no id and their text is LLM-generated per run, so a gap
    cannot be re-identified across rounds by matching its wording. The convergence
    loop therefore mints the id ONCE and carries the gap object forward, asking
    "is this covered now?" rather than re-deriving. This hash exists to make that
    id deterministic for a given (domain, title), not to match across rephrasings.
    """
    basis = f"{gap.get('domain', '')}|{gap.get('title', '')}".strip().lower()
    return "GAP-" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:8]


def _author_input(gap: dict, problem_statement: str, sample: list[str]) -> str:
    lines = [
        "GAP",
        f"  domain:   {gap.get('domain_name') or gap.get('domain', '')}",
        f"  title:    {gap.get('title', '')}",
        f"  severity: {gap.get('severity', '')}",
        f"  detail:   {gap.get('detail', '')}",
        f"  question: {gap.get('question', '')}",
    ]
    grounding = gap.get("grounding") or []
    if grounding:
        lines.append("  grounding:")
        lines.extend(f"    - {g}" for g in grounding)
    if problem_statement:
        lines += ["", "PROBLEM STATEMENT", problem_statement]
    if sample:
        lines += ["", f"EXISTING REQUIREMENTS (style sample, {len(sample)})"]
        lines.extend(f"  - {t}" for t in sample)
    return "\n".join(lines)


def author_for_gap(gap: dict, problem_statement: str, sample: list[str],
                   client: AgentServerClient | None = None) -> dict:
    """Draft one requirement closing `gap`. Never raises — a failure returns a
    record with `error`, matching the judge/classifier contract."""
    client = client or AgentServerClient()
    try:
        r = client.complete_json(GAP_AUTHOR_AGENT,
                                 _author_input(gap, problem_statement, sample))
    except (LLMError, AttributeError, KeyError, TypeError) as e:
        return {"text": "", "error": f"{type(e).__name__}: {e}"}
    if not isinstance(r, dict):
        return {"text": "", "error": f"unexpected shape: {type(r).__name__}"}
    return {
        "text": (r.get("text") or "").strip(),
        "rationale": (r.get("rationale") or "").strip()[:400],
        "assumptions": [str(a) for a in (r.get("assumptions") or [])][:5],
        "needs_input": bool(r.get("needs_input")),
        "question": (r.get("question") or "").strip()[:400],
    }


def is_duplicate(candidate: str, existing: list[str],
                 threshold: float = DUPLICATE_THRESHOLD) -> tuple[bool, int | None]:
    """Is `candidate` already stated by one of `existing`? Returns (dup, index).

    The loop must not author a requirement the set already has — that is one of
    the ways gap authoring could fail to terminate.
    """
    if not existing or not candidate.strip():
        return False, None
    try:
        scores = rerank(candidate, existing)
    except Exception:                                  # noqa: BLE001 — reranker down
        return False, None                             # fail open: authoring a near-dup
    if not scores:                                     # is recoverable, dropping a
        return False, None                             # real gap filler is not
    best = max(range(len(scores)), key=lambda i: scores[i])
    return (scores[best] >= threshold), (best if scores[best] >= threshold else None)


def _scorecard_record(req_id: str, assessment: dict, gap: dict, drafted: dict,
                      refinement: dict | None) -> dict:
    """Shape an authored requirement exactly like an extracted one, plus the
    provenance flags that mark it as generated."""
    chars = {c["id"]: c for c in assessment.get("characteristics", [])}
    ok = sum(1 for c in chars.values() if c.get("score") is not None)
    return {
        "req_id": req_id,
        "text": assessment.get("text", ""),
        "characteristics": chars,
        "deterministic_findings": assessment.get("deterministic", []),
        "overall": assessment.get("overall"),
        "judges_ok": ok,
        "judges_total": len(CHARACTERISTICS),
        "review": assessment.get("review"),
        "lineage": {"duplicate_of": None},
        # No source_document/page/bbox: this text is in no source document, and
        # nothing downstream may present it as though it were.
        "provenance": {
            "origin": "analyst_authored",
            "generated_to_fill_coverage_gap": True,
            "ratified": False,
            "gap_id": gap_id(gap),
            "gap_title": gap.get("title", ""),
            "gap_severity": gap.get("severity", ""),
            "domain": gap.get("domain", ""),
            "domain_name": gap.get("domain_name", ""),
            "grounding": gap.get("grounding") or [],
            "rationale": drafted.get("rationale", ""),
            "assumptions": drafted.get("assumptions") or [],
            # Set when the author could draft the obligation but not the value it
            # needs. Carried on the requirement so the question is answerable in
            # context, and so nothing downstream can mistake it for settled.
            "open_question": drafted.get("question", "") if drafted.get("needs_input") else "",
            # Non-empty ⇒ the text carries an unfilled value and is not releasable
            # no matter what the judges scored it. See `unresolved_placeholders`.
            "unresolved_placeholders": unresolved_placeholders(assessment.get("text", "")),
        },
        "refinement": refinement,
    }


def iter_author_for_project(pid: str, run_id: str, coverage: dict | None = None,
                            client: AgentServerClient | None = None,
                            should_cancel=None) -> Iterator[dict]:
    """Author + score + refine a requirement for every open coverage gap.

    Yields progress events and appends the accepted candidates to the run's
    scorecard and review session. All severities are in scope (decision 3).
    """
    client = client or AgentServerClient()
    cancelled = lambda: bool(should_cancel and should_cancel())  # noqa: E731

    coverage = coverage or pj.get_coverage(pid)
    if not coverage:
        yield {"type": "error", "stage": "author", "message": "no coverage run for this project"}
        return
    scorecard = pj.get_quality_scorecard(pid, run_id)
    if not scorecard:
        yield {"type": "error", "stage": "author", "message": "no quality run for this project"}
        return

    review = pj.get_review(pid, run_id) or {}
    threshold = float((review.get("threshold") or {}).get("value", 4.3))

    existing = [r.get("text", "") for r in scorecard.get("requirements", [])
                if r.get("text")]
    ps_doc = pj.get_problem_statement(pid) or {}
    problem = _compact_statement(ps_doc.get("statement") or {})
    step = max(1, len(existing) // STYLE_SAMPLE)
    sample = existing[::step][:STYLE_SAMPLE]

    gaps = coverage.get("gaps") or []
    yield {"type": "stage", "stage": "author", "status": "start", "done": 0,
           "total": len(gaps), "unit": "gaps",
           "message": f"authoring for {len(gaps)} coverage gap(s)"}

    authored: list[dict] = []          # everything authored this run
    pending: list[dict] = []           # not yet flushed to the store
    skipped_dup = failed = blocked = 0
    seq = _next_gap_seq(scorecard)

    for i, gap in enumerate(gaps, 1):
        if cancelled():
            # Persist what is already done — a cancel must not discard work that
            # has been paid for.
            if pending:
                _persist(pid, run_id, scorecard, pending)
            yield {"type": "cancelled", "stage": "author",
                   "authored": len(authored), "persisted": True}
            return

        drafted = author_for_gap(gap, problem, sample, client=client)
        if drafted.get("error") or not drafted.get("text"):
            failed += 1
            yield {"type": "authored", "gap_id": gap_id(gap), "done": i, "total": len(gaps),
                   "status": "failed", "message": drafted.get("error", "no text")}
            continue
        # `needs_input` and a usable draft are NOT mutually exclusive — observed on
        # the first live gap: the author correctly wrote "shall enforce data
        # retention periods" while flagging that the periods themselves are unknown.
        # Discarding that text would lose a real requirement and leave the gap open.
        # Keep it: the missing value makes it score low on Complete/Verifiable, the
        # refinement loop plateaus, and it lands as `needs_human` — which under the
        # absolute floor blocks release until someone answers. The question rides
        # along on the requirement rather than being tracked separately.
        if drafted.get("needs_input") and not drafted.get("text"):
            blocked += 1
            yield {"type": "authored", "gap_id": gap_id(gap), "done": i, "total": len(gaps),
                   "status": "needs_input", "question": drafted.get("question", "")}
            continue

        dup, idx = is_duplicate(drafted["text"], existing)
        if dup:
            skipped_dup += 1
            yield {"type": "authored", "gap_id": gap_id(gap), "done": i, "total": len(gaps),
                   "status": "duplicate", "duplicate_of_text": existing[idx][:120]}
            continue

        # Same bar as every other requirement: score, then refine to threshold.
        assessment = assess_requirement(drafted["text"], client=client, review=True)
        refinement = None
        if (assessment.get("overall") is None
                or assessment["overall"] < threshold):
            out = _refine_one(drafted["text"], assessment.get("overall"), threshold, client)
            if out["final_text"] != drafted["text"]:
                assessment = assess_requirement(out["final_text"], client=client, review=True)
            refinement = {"attempts": out["attempts"], "history": out["history"]}

        seq += 1
        req_id = f"GAP-{seq:04d}"
        rec = _scorecard_record(req_id, assessment, gap, drafted, refinement)
        authored.append(rec)
        pending.append(rec)
        existing.append(rec["text"])                   # later gaps dedup against it
        if len(pending) >= FLUSH_EVERY:
            _persist(pid, run_id, scorecard, pending)
            pending = []
        yield {"type": "authored", "gap_id": gap_id(gap), "req_id": req_id,
               "done": i, "total": len(gaps), "status": "authored",
               "score": rec["overall"], "text": rec["text"]}

    if pending:
        _persist(pid, run_id, scorecard, pending)

    yield {"type": "stage", "stage": "author", "status": "done",
           "done": len(gaps), "total": len(gaps),
           "message": f"{len(authored)} authored, {skipped_dup} duplicate, "
                      f"{blocked} need input, {failed} failed"}
    yield {"type": "author_summary", "data": {
        "gaps": len(gaps), "authored": len(authored), "duplicates": skipped_dup,
        "needs_input": blocked, "failed": failed, "threshold": threshold,
        "authored_ids": [r["req_id"] for r in authored]}}


def _next_gap_seq(scorecard: dict) -> int:
    """Continue the GAP- numbering rather than restarting it each round."""
    seq = 0
    for r in scorecard.get("requirements", []):
        rid = r.get("req_id") or ""
        if rid.startswith("GAP-"):
            try:
                seq = max(seq, int(rid.split("-", 1)[1]))
            except ValueError:
                continue
    return seq


def _persist(pid: str, run_id: str, scorecard: dict, authored: list[dict]) -> None:
    """Append authored requirements to the scorecard and seed their review entries."""
    scorecard.setdefault("requirements", []).extend(authored)
    meta = next((m for m in pj.list_quality_runs(pid) if m.get("run_id") == run_id), None) or {
        "run_id": run_id, "project_id": pid, "kind": "quality"}
    meta["total"] = len(scorecard["requirements"])
    pj.save_quality_run(pid, run_id, scorecard, meta)

    review = pj.get_review(pid, run_id)
    if not review:
        return
    for rec in authored:
        review["requirements"][rec["req_id"]] = {
            "status": "unreviewed",
            "original_text": rec["text"],
            "final_text": rec["text"],
            "note": "",
            "overall_before": rec["overall"],
            "overall_after": None,
            "reviewed_at": None,
            "refinement": rec.get("refinement"),
        }
    pj.save_review(pid, run_id, review)


def _compact_statement(statement: dict) -> str:
    """Flatten the provenance-graded problem statement to plain lines."""
    def _v(x):
        return x.get("value") if isinstance(x, dict) and "value" in x else x

    out = []
    for key in ("purpose", "context", "scope_in", "constraints"):
        val = _v(statement.get(key))
        if isinstance(val, list):
            out.extend(f"  - {_v(v)}" for v in val[:8])
        elif val:
            out.append(f"{key}: {val}")
    return "\n".join(out)[:4000]
