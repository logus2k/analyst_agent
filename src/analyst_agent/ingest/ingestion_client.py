"""Client for the shared ingestion-server (:8700).

The Analyst does not parse documents itself. Binary/structured formats go to the
shared ingestion agent's **stateless** `POST /v1/parse` with `strategy=structural`,
which returns one block per document item — un-merged — carrying a normalized
`kind` plus page/bbox provenance. That is the shape requirement segmentation and
faithful document reconstruction need; the default `pdf_docling` strategy chunks
for *retrieval* and would merge blocks away.

See documents/technical_architecture.md §9.2.
"""
from __future__ import annotations

import os

import httpx

from analyst_agent import config
from analyst_agent.ingest.model import BlockType, SourceItem

# Layout models on a cold document are slow; the old in-repo service used 900s.
PARSE_TIMEOUT = float(os.environ.get("INGESTION_TIMEOUT", "900"))


def parse_document(path: str, source_file: str | None = None) -> list[SourceItem]:
    """Parse a binary/structured document into ordered `SourceItem`s."""
    name = source_file or os.path.basename(path)
    with open(path, "rb") as f:
        r = httpx.post(
            f"{config.INGESTION_URL}/v1/parse",
            params={"strategy": "structural"},
            files={"file": (name, f)},
            timeout=PARSE_TIMEOUT,
        )
    r.raise_for_status()
    return [_to_item(b, name) for b in (r.json().get("blocks") or [])]


def _to_item(b: dict, source_file: str) -> SourceItem:
    """ingestion-server `Chunk` -> our `SourceItem`.

    `kind` already uses the BlockType vocabulary (both derive from Docling's
    `DocItemLabel`), so it maps straight across; an unrecognised kind degrades to
    OTHER rather than failing the whole document.
    """
    try:
        block_type = BlockType(b.get("kind") or "other")
    except ValueError:
        block_type = BlockType.OTHER
    return SourceItem(
        text=b.get("text") or "",
        block_type=block_type,
        section_path=b.get("section_path") or "(root)",
        source_file=source_file,
        order=b.get("index", 0),
        page=b.get("page_no"),
        bbox=b.get("bbox"),
        char_span=None,          # binary source: character offsets are not meaningful
        heading_level=None,
    )
