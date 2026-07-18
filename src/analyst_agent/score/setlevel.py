"""Set-level INCOSE analysis (characteristics C10–C15).

These assess the requirement SET as a whole by relating requirements to each
other, which the per-requirement judges never do. Two parts:

  1. find_overlaps() — concrete, reranker-based near-duplicate/overlap detection
     across the set (hard evidence for C11 Consistent). Reuses the reranker that
     cleanly separates true overlap (~0.95) from mere topical similarity (<0.05).
  2. assess_set() — a holistic LLM set-judge that rates C10–C15 from a set
     summary plus the detected overlaps, with justifications and findings.

The overlaps are grounded evidence; the set-judge is a holistic opinion informed
by that evidence and a representative sample.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from analyst_agent.llm.client import AgentServerClient, LLMError
from analyst_agent.llm.retrieval import rerank

logger = logging.getLogger(__name__)

SET_JUDGE_AGENT = "incose_set_judge"
OVERLAP_CONFIRMER_AGENT = "incose_overlap_confirmer"
OVERLAP_THRESHOLD = 0.8   # reranker sigmoid; ~0.95 true overlap vs <0.05 related


@dataclass
class OverlapPair:
    a_id: str
    b_id: str
    score: float


def find_overlaps(reqs: list[dict], threshold: float = OVERLAP_THRESHOLD,
                  max_compare: int | None = None) -> list[OverlapPair]:
    """Detect duplicate/overlapping requirement pairs with the reranker.

    `reqs` is a list of {"id", "text"}. For each requirement we rerank it against
    all others and keep pairs whose score clears `threshold`. Pairs are emitted
    once (a<b by index). O(n) rerank calls."""
    texts = [r["text"] for r in reqs]
    ids = [r["id"] for r in reqs]
    seen: set[tuple[int, int]] = set()
    pairs: list[OverlapPair] = []
    n = len(reqs) if max_compare is None else min(len(reqs), max_compare)
    for i in range(n):
        others = texts[:i] + texts[i + 1:]
        try:
            scores = rerank(texts[i], others)
        except Exception as e:
            logger.warning("overlap rerank failed at %d: %s", i, e)
            continue
        for k, s in enumerate(scores):
            j = k if k < i else k + 1          # map back past the removed self
            if s >= threshold:
                key = (min(i, j), max(i, j))
                if key not in seen:
                    seen.add(key)
                    pairs.append(OverlapPair(ids[key[0]], ids[key[1]], round(s, 3)))
    pairs.sort(key=lambda p: -p.score)
    return pairs


def confirm_overlaps(pairs: list[OverlapPair], by_id: dict[str, str],
                     batch: int = 15,
                     client: AgentServerClient | None = None) -> list[OverlapPair]:
    """LLM-confirm reranker candidate pairs: keep only true duplicates/overlaps,
    dropping the merely-related ones (the reranker's false positives)."""
    client = client or AgentServerClient()
    confirmed: list[OverlapPair] = []
    for s in range(0, len(pairs), batch):
        chunk = pairs[s:s + batch]
        user = "\n".join(
            f"[{i}] A: {by_id.get(p.a_id, '')}\n    B: {by_id.get(p.b_id, '')}"
            for i, p in enumerate(chunk))
        try:
            res = client.complete_json(OVERLAP_CONFIRMER_AGENT, user)
            keep = {v["index"] for v in res.get("pairs", [])
                    if isinstance(v, dict) and v.get("overlap")}
        # AttributeError: _parse_json returns whatever json.loads yields, so a
        # model that emits a bare array gives a list, not the promised dict.
        except (LLMError, AttributeError, KeyError, TypeError) as e:
            logger.warning("confirm_overlaps batch @%d failed: %s", s, e)
            keep = set()
        for i, p in enumerate(chunk):
            if i in keep:
                confirmed.append(p)
    return confirmed


def build_summary(reqs: list[dict], overlaps: list[OverlapPair], sample_n: int = 30) -> str:
    n = len(reqs)
    fr = sum(1 for r in reqs if r["id"].upper().startswith("FR"))
    nfr = sum(1 for r in reqs if r["id"].upper().startswith("NFR"))
    step = max(1, n // sample_n)
    sample = reqs[::step][:sample_n]
    lines = [f"SUMMARY: {n} requirements (FR={fr}, NFR={nfr}, other={n-fr-nfr}).",
             f"\nREPRESENTATIVE SAMPLE ({len(sample)}):"]
    for r in sample:
        lines.append(f"  {r['id']}: {r['text']}")
    lines.append(f"\nDETECTED OVERLAP PAIRS ({len(overlaps)}):")
    if overlaps:
        for p in overlaps[:25]:
            lines.append(f"  {p.a_id} ~ {p.b_id} (score {p.score})")
    else:
        lines.append("  (none detected)")
    return "\n".join(lines)


def assess_set(reqs: list[dict], client: AgentServerClient | None = None) -> dict:
    """Full set-level analysis: overlaps + C10–C15 scores. Returns
    {overlaps: [...], set_assessment: [...]}."""
    client = client or AgentServerClient()
    by_id = {r["id"]: r["text"] for r in reqs}
    candidates = find_overlaps(reqs)
    # drop reranker false positives (shares the caller's client)
    overlaps = confirm_overlaps(candidates, by_id, client=client)
    logger.info("set overlaps: %d candidates -> %d confirmed", len(candidates), len(overlaps))
    summary = build_summary(reqs, overlaps)
    res = client.complete_json(SET_JUDGE_AGENT, summary)
    return {
        "overlaps": [p.__dict__ for p in overlaps],
        "set_assessment": res.get("set_assessment", []),
    }
