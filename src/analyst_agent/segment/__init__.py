"""Component 2: segmentation / requirement identification.

Turns the normalized `SourceItem` stream from ingestion into a list of
`DiscreteRequirement`s. The LLM is the sole identifier (no regex/modal gate);
deterministic code only validates its output. See specs §6.
"""

from analyst_agent.segment.model import DiscreteRequirement, Provenance
from analyst_agent.segment.pipeline import segment_items

__all__ = ["DiscreteRequirement", "Provenance", "segment_items"]
