"""Overview-dedup: fold terse summary bullets into their detailed requirement.

An SRS often states a capability twice - once as a terse bullet in an overview/
scope section ("Job creation and publishing") and once as a detailed requirement
("The system SHALL allow employers to create and publish job postings."). Both
are extracted; the summary double-counts.

We detect the redundancy with the RERANKER (not cosine - cosine can't separate a
summary from a merely-related requirement in an SRS; the reranker scores true
subsumption ~0.95 vs <0.05). Each summary-candidate is reranked against the
detailed pool; if its best match clears the threshold, it is MARKED
`duplicate_of` that detailed requirement (retained for audit, excluded from the
primary set). Marking never deletes, and a candidate with no strong match stays a
primary - so a capability stated only in an overview keeps its requirement.
"""

from __future__ import annotations

import logging

from analyst_agent.llm.retrieval import rerank
from analyst_agent.segment.model import DiscreteRequirement

logger = logging.getLogger(__name__)

DEDUP_THRESHOLD = 0.6    # reranker sigmoid
MAX_SUMMARY_RATIO = 0.6  # a summary must be < this fraction of its detail's length


def _is_detailed(r: DiscreteRequirement) -> bool:
    """Detailed = a requirement that should be a PRIMARY, never folded away:
    it carries an author ID (FR/QR/DEMO-SRS...) or was assembled from a table.
    Summary-candidates are the generated-id prose requirements."""
    return (not r.req_id.startswith("REQ-")) or r.origin == "assembled"


def _words(s: str) -> int:
    return len(s.split())


def dedup_overview(
    requirements: list[DiscreteRequirement],
    threshold: float = DEDUP_THRESHOLD,
) -> list[DiscreteRequirement]:
    """Mark terse summary requirements as duplicate_of their detailed counterpart.

    Two guards keep this from folding distinct requirements (a recall risk):
      1. reranker score must clear `threshold` (subsumption, not mere topical
         similarity - the reranker separates these ~0.95 vs <0.05);
      2. the candidate must be substantially SHORTER than its match - a summary
         is terser than its detail. A full-length requirement is never folded,
         even if it relates strongly to another.
    """
    detailed = [r for r in requirements if _is_detailed(r)]
    candidates = [r for r in requirements if not _is_detailed(r)]
    if not detailed or not candidates:
        return requirements

    det_texts = [d.text for d in detailed]
    n_marked = 0
    for c in candidates:
        try:
            scores = rerank(c.text, det_texts)
        except Exception as e:  # never let dedup break the pipeline
            logger.warning("dedup rerank failed for %s: %s", c.req_id, e)
            continue
        if not scores:
            continue
        best_i = max(range(len(scores)), key=lambda i: scores[i])
        best = detailed[best_i]
        # Guard 2: only fold a genuinely terser candidate into a longer detail.
        if scores[best_i] >= threshold and _words(c.text) < MAX_SUMMARY_RATIO * _words(best.text):
            c.duplicate_of = best.req_id
            n_marked += 1

    logger.info("overview-dedup: %d candidates vs %d detailed -> %d marked duplicate",
                len(candidates), len(detailed), n_marked)
    return requirements
