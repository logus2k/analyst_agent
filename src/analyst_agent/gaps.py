"""Gap ledger + closure checking — the identity mechanism the convergence loop needs.

THE PROBLEM THIS SOLVES. Coverage gaps carry no id, and their `title`/`detail`/
`question` are LLM-generated afresh on every run. Measured on the NIST project:
only 6 of 130 `grounding` strings match a catalog concern exactly (4 %), and
grounding mixes domain concerns, archetype concerns and standards leaves — so
there is no structural key to match on either. Round 2's phrasing of "retention
policies are undefined" will not equal round 1's.

Without identity the loop cannot cap attempts per gap, cannot tell whether it is
making progress, and will author near-duplicates forever.

THE MECHANISM. Mint each gap ONCE from the first coverage run and carry the object
forward. Each round asks a judge "does the current set cover this gap?" instead of
re-deriving the gap list. Identity stops being a matching problem — the gap object
is its own identity. A fresh full panel runs only on the first round (to discover)
and the final round (to confirm nothing new opened).

COST. One judge call per open gap per round — 78 on the NIST project — against a
16-domain panel that would re-derive everything. Each call sees only the
`TOP_K_CANDIDATES` requirements the reranker judges most relevant, not all 137, so
the prompt stays small as the set grows.

BIAS. The judge is deliberately strict, and `partial` is a first-class answer:
wrongly declaring a gap closed deletes it from the analysis permanently and ships
a real hole to the Architect. A gap wrongly left open only costs another round.
"""

from __future__ import annotations

from typing import Iterator

from analyst_agent import store as pj
from analyst_agent.llm.client import AgentServerClient, LLMError
from analyst_agent.llm.retrieval import rerank

CLOSURE_JUDGE_AGENT = "incose_gap_closure_judge"

# How many requirements the closure judge sees per gap. The reranker picks them,
# so this bounds prompt size independently of how large the set grows.
TOP_K_CANDIDATES = 12

# Only `closed` retires a gap. `partial` stays open — it means the obligation is
# named but its value is missing, which is exactly the case the placeholder guard
# also catches, and it must keep blocking release.
CLOSED = "closed"
PARTIAL = "partial"
OPEN = "open"
_STATUSES = (CLOSED, PARTIAL, OPEN)


def gap_id(gap: dict) -> str:
    """Re-exported from `authoring` so the ledger and the author agree on ids."""
    from analyst_agent.authoring import gap_id as _gid
    return _gid(gap)


def mint_ledger(coverage: dict) -> dict:
    """Build the carried gap ledger from a coverage run. Called ONCE per project;
    later rounds update statuses in place rather than re-minting."""
    entries = {}
    for gap in coverage.get("gaps") or []:
        gid = gap_id(gap)
        if gid in entries:                      # same (domain,title) twice in one run
            continue
        entries[gid] = {
            "gap_id": gid,
            "status": OPEN,
            "gap": gap,                         # the full object, carried verbatim
            "checks": 0,                        # closure checks performed
            "author_attempts": 0,               # requirements authored for it
            "covered_by": [],
            "reasoning": "",
            "still_missing": "",
        }
    return {"version": 1, "minted_from_requirement_count": coverage.get("requirement_count"),
            "gaps": entries}


def ensure_ledger(pid: str, coverage: dict | None = None) -> dict | None:
    """Existing ledger, or a freshly minted one from the project's coverage run."""
    ledger = pj.get_gap_ledger(pid)
    if ledger:
        return ledger
    coverage = coverage or pj.get_coverage(pid)
    if not coverage:
        return None
    return pj.save_gap_ledger(pid, mint_ledger(coverage))


def merge_new_gaps(ledger: dict, coverage: dict) -> tuple[dict, list[str]]:
    """Fold a later full-panel run into the ledger, adding only genuinely new gaps.

    Used on the final round to confirm nothing new opened. A gap already carried
    keeps its status and history — a fresh panel re-describing it must not reset
    the work done on it.
    """
    added = []
    for gap in coverage.get("gaps") or []:
        gid = gap_id(gap)
        if gid in ledger["gaps"]:
            continue
        ledger["gaps"][gid] = {"gap_id": gid, "status": OPEN, "gap": gap, "checks": 0,
                               "author_attempts": 0, "covered_by": [], "reasoning": "",
                               "still_missing": ""}
        added.append(gid)
    return ledger, added


def open_gaps(ledger: dict) -> list[dict]:
    """Every gap not yet closed — `partial` counts as open."""
    return [e for e in (ledger.get("gaps") or {}).values() if e.get("status") != CLOSED]


def _candidates(gap: dict, requirements: list[dict], top_k: int = TOP_K_CANDIDATES
                ) -> list[dict]:
    """The requirements most relevant to this gap, by reranker.

    Falls back to the whole set (truncated) if the reranker is unavailable —
    judging against something is better than declaring a gap open by default,
    which would spin the loop.
    """
    if not requirements:
        return []
    query = f"{gap.get('title', '')}. {gap.get('detail', '')} {gap.get('question', '')}"
    texts = [r.get("text", "") for r in requirements]
    try:
        scores = rerank(query, texts)
    except Exception:                                          # noqa: BLE001
        return requirements[:top_k]
    if not scores:
        return requirements[:top_k]
    order = sorted(range(len(scores)), key=lambda i: -scores[i])
    return [requirements[i] for i in order[:top_k]]


def _closure_input(gap: dict, candidates: list[dict]) -> str:
    lines = ["GAP",
             f"  title:    {gap.get('title', '')}",
             f"  detail:   {gap.get('detail', '')}",
             f"  question: {gap.get('question', '')}",
             "", f"CANDIDATE REQUIREMENTS ({len(candidates)})"]
    for r in candidates:
        lines.append(f"  [{r.get('req_id')}] {r.get('text', '')}")
    return "\n".join(lines)


def check_closure(gap: dict, requirements: list[dict],
                  client: AgentServerClient | None = None) -> dict:
    """Is `gap` covered by the current set? Never raises.

    On any failure the gap stays OPEN — the safe direction. Declaring closure on a
    failed call would silently delete a real gap.
    """
    client = client or AgentServerClient()
    candidates = _candidates(gap, requirements)
    if not candidates:
        return {"status": OPEN, "covered_by": [], "reasoning": "no requirements to judge",
                "still_missing": gap.get("question", "")}
    try:
        r = client.complete_json(CLOSURE_JUDGE_AGENT, _closure_input(gap, candidates))
    except (LLMError, AttributeError, KeyError, TypeError) as e:
        return {"status": OPEN, "covered_by": [], "reasoning": "",
                "still_missing": "", "error": f"{type(e).__name__}: {e}"}
    if not isinstance(r, dict):
        return {"status": OPEN, "covered_by": [], "reasoning": "",
                "still_missing": "", "error": f"unexpected shape: {type(r).__name__}"}

    status = str(r.get("status") or "").strip().lower()
    if status not in _STATUSES:
        status = OPEN                                   # unknown verdict -> keep it open
    valid_ids = {c.get("req_id") for c in candidates}
    covered = [c for c in (r.get("covered_by") or []) if c in valid_ids]
    # A "closed" verdict citing nothing is not evidence of closure.
    if status == CLOSED and not covered:
        status = PARTIAL
    return {"status": status, "covered_by": covered,
            "reasoning": str(r.get("reasoning") or "")[:400],
            "still_missing": str(r.get("still_missing") or "")[:400]}


def iter_check_closure(pid: str, run_id: str | None = None,
                       client: AgentServerClient | None = None,
                       should_cancel=None) -> Iterator[dict]:
    """Re-check every open gap against the project's current requirement set.

    This is the loop's progress signal: it turns "78 gaps" into "how many did the
    authored requirements actually close?", which is otherwise unknowable without
    re-running the full panel and losing gap identity.
    """
    client = client or AgentServerClient()
    cancelled = lambda: bool(should_cancel and should_cancel())  # noqa: E731

    scorecard = pj.get_quality_scorecard(pid, run_id)
    if not scorecard:
        yield {"type": "error", "stage": "closure", "message": "no quality run"}
        return
    ledger = ensure_ledger(pid)
    if not ledger:
        yield {"type": "error", "stage": "closure", "message": "no coverage run to mint gaps from"}
        return

    requirements = [r for r in scorecard.get("requirements", [])
                    if not (r.get("lineage") or {}).get("duplicate_of")]
    todo = open_gaps(ledger)
    yield {"type": "stage", "stage": "closure", "status": "start", "done": 0,
           "total": len(todo), "unit": "gaps",
           "message": f"re-checking {len(todo)} open gap(s) against {len(requirements)} requirements"}

    closed = partial = still_open = 0
    for i, entry in enumerate(todo, 1):
        if cancelled():
            pj.save_gap_ledger(pid, ledger)          # keep the checks already paid for
            yield {"type": "cancelled", "stage": "closure"}
            return
        verdict = check_closure(entry["gap"], requirements, client=client)
        entry["status"] = verdict["status"]
        entry["covered_by"] = verdict["covered_by"]
        entry["reasoning"] = verdict["reasoning"]
        entry["still_missing"] = verdict["still_missing"]
        entry["checks"] = entry.get("checks", 0) + 1
        if verdict["status"] == CLOSED:
            closed += 1
        elif verdict["status"] == PARTIAL:
            partial += 1
        else:
            still_open += 1
        yield {"type": "gap_checked", "gap_id": entry["gap_id"], "done": i, "total": len(todo),
               "status": verdict["status"], "covered_by": verdict["covered_by"],
               "title": entry["gap"].get("title", "")}

    pj.save_gap_ledger(pid, ledger)
    remaining = len(open_gaps(ledger))
    yield {"type": "stage", "stage": "closure", "status": "done",
           "done": len(todo), "total": len(todo),
           "message": f"{closed} closed, {partial} partial, {still_open} open"}
    yield {"type": "closure_summary", "data": {
        "checked": len(todo), "closed": closed, "partial": partial, "open": still_open,
        "total_gaps": len(ledger.get("gaps") or {}), "still_open": remaining}}
