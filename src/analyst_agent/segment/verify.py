"""Deterministic validation of the LLM's identification output (spec §6.4).

This does NOT re-judge whether something is a requirement (no modal/shape gate —
that would smuggle the regex approach back in). It only:
  - rejects text not traceable to its source block (anti-hallucination),
  - deduplicates,
  - enforces a minimum length.
"""

from __future__ import annotations

import re

MIN_TEXT_LEN = 12
NEAR_MATCH_CONTAINMENT = 0.6  # for split/reworded compound pieces

_WORD_RE = re.compile(r"\w+", re.UNICODE)


def _norm(s: str) -> str:
    return " ".join(s.lower().split())


def _tokens(s: str) -> list[str]:
    return _WORD_RE.findall(s.lower())


def traceability(candidate_text: str, source_text: str) -> tuple[bool, float]:
    """Return (is_traceable, confidence).

    Verbatim substring → confidence 1.0. Otherwise a near-match by token
    containment (fraction of candidate tokens present in the source), which
    covers legitimately reworded compound splits. Below the threshold → not
    traceable (treated as hallucinated / invented).
    """
    ct, st = _norm(candidate_text), _norm(source_text)
    if not ct:
        return False, 0.0
    if ct in st:
        return True, 1.0
    cand_tokens = _tokens(candidate_text)
    if not cand_tokens:
        return False, 0.0
    src_tokens = set(_tokens(source_text))
    contained = sum(1 for t in cand_tokens if t in src_tokens) / len(cand_tokens)
    return (contained >= NEAR_MATCH_CONTAINMENT), contained


def is_valid_length(text: str) -> bool:
    return len(text.strip()) >= MIN_TEXT_LEN


def dedup_key(source_order: int, text: str) -> tuple[int, str]:
    return (source_order, _norm(text))
