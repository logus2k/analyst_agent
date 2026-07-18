"""Normalized item model shared by every ingestion path.

Both the Markdown parser and the Docling adapter emit `SourceItem`s so that
Component 2 (segmentation) reads one uniform stream regardless of the source
file format. `block_type` is a normalized vocabulary that both paths map into
(Markdown AST node kinds and Docling `DocItemLabel`s collapse to the same set).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum


class BlockType(str, Enum):
    """Format-agnostic block classification.

    Markdown node kinds and Docling `DocItemLabel`s both map into this set so
    the router in Component 2 has one vocabulary to branch on.
    """

    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST_ITEM = "list_item"
    TABLE = "table"
    CODE = "code"
    CAPTION = "caption"
    QUOTE = "quote"
    PICTURE = "picture"
    TOC = "toc"          # table of contents / document index (Docling DOCUMENT_INDEX)
    OTHER = "other"


@dataclass
class SourceItem:
    """One normalized block of source content with provenance.

    Not a requirement — just a structural unit. Component 2 decides which of
    these carry requirements.

    Attributes
    ----------
    text:
        The block's plain text (tables are rendered to Markdown).
    block_type:
        Normalized `BlockType`.
    section_path:
        ' > '-joined heading trail this item sits under (e.g.
        "3 Requirements > 3.2 Performance"). "(root)" when no heading applies.
    source_file:
        Original filename the item came from.
    order:
        Zero-based reading-order index within the document.
    page:
        1-based page number for paginated sources (PDF); None otherwise.
    bbox:
        [x0, y0, x1, y1] bounding box for paginated sources; None otherwise.
    char_span:
        (start, end) character offsets into the ORIGINAL source text for
        text-based sources (Markdown); None for binary sources where a char
        offset is not meaningful. Feeds span-grounding downstream.
    heading_level:
        For HEADING items, the level (1 = top). None otherwise.
    """

    text: str
    block_type: BlockType
    section_path: str
    source_file: str
    order: int
    page: int | None = None
    bbox: list[float] | None = None
    char_span: tuple[int, int] | None = None
    heading_level: int | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["block_type"] = self.block_type.value
        # asdict turns the tuple into a list; keep it JSON-friendly as-is.
        return d


@dataclass
class IngestResult:
    """Envelope returned by an ingestion path."""

    source_file: str
    format: str                       # "markdown" | "docling"
    items: list[SourceItem] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "source_file": self.source_file,
            "format": self.format,
            "item_count": len(self.items),
            "items": [it.to_dict() for it in self.items],
        }
