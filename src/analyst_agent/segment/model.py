"""Data model for identified requirements (spec §7)."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass
class Provenance:
    source_file: str
    section_path: str
    order: int                       # index of the source SourceItem it came from
    page: int | None = None          # paginated sources (PDF)
    bbox: list[float] | None = None  # PDF highlight
    char_span: tuple[int, int] | None = None  # text sources (Markdown)


@dataclass
class DiscreteRequirement:
    req_id: str                 # existing ID, or generated (DOC-0007)
    text: str                   # clean, singular requirement statement
    provenance: Provenance
    origin: str                 # "extracted" | "derived" | "assembled"
    derived_from: str | None    # parent req_id if split from a compound
    was_compound: bool          # feeds the Singular score directly
    identification_confidence: float
    # For origin == "assembled": the source-item orders of the pieces that were
    # joined across blocks (cross-block reassembly by ID). None otherwise.
    component_orders: list[int] | None = None
    # Set by overview-dedup: the req_id of the more-detailed requirement this one
    # is a summary/duplicate of. None = primary. Retained (not deleted) for audit;
    # excluded from the primary set used for scoring/counting.
    duplicate_of: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        return d
