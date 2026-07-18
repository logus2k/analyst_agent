"""Component 1: ingestion.

Turns a supported document (.md / .pdf / .docx / .html / .pptx) into a
normalized stream of `SourceItem`s with provenance. This module does NOT
decide what is or isn't a requirement — that is Component 2 (segmentation).
"""

from analyst_agent.ingest.model import BlockType, SourceItem
from analyst_agent.ingest.dispatch import ingest_file, SUPPORTED_EXTENSIONS

__all__ = ["BlockType", "SourceItem", "ingest_file", "SUPPORTED_EXTENSIONS"]
