"""Extension dispatch: route a file to the right ingestion path.

  .md / .markdown        → parse_markdown  (no Docling)
  .pdf .docx .pptx       → Docling
  .html .htm             → Docling

Mirrors the format split proven in the noted graph service: Markdown is already
structured text and bypasses Docling; everything else flows through it.
"""

from __future__ import annotations

import os

from analyst_agent.ingest.model import IngestResult

MARKDOWN_EXTENSIONS = {".md", ".markdown"}
DOCLING_EXTENSIONS = {".pdf", ".docx", ".pptx", ".html", ".htm"}
SUPPORTED_EXTENSIONS = MARKDOWN_EXTENSIONS | DOCLING_EXTENSIONS


class UnsupportedFormatError(ValueError):
    """Raised for a file extension outside SUPPORTED_EXTENSIONS."""


def ingest_file(abs_path: str, source_file: str | None = None) -> IngestResult:
    """Ingest one file into a normalized `IngestResult`.

    Parameters
    ----------
    abs_path:
        Path to the file on disk.
    source_file:
        Name to record as provenance (defaults to the basename of abs_path).
    """
    name = source_file or os.path.basename(abs_path)
    ext = os.path.splitext(name)[1].lower()

    if ext in MARKDOWN_EXTENSIONS:
        # Import here so the Docling-free path has no heavy import cost.
        from analyst_agent.ingest.markdown import parse_markdown
        with open(abs_path, "r", encoding="utf-8") as f:
            text = f.read()
        return parse_markdown(text, name)

    if ext in DOCLING_EXTENSIONS:
        from analyst_agent.ingest.docling_adapter import parse_with_docling
        return parse_with_docling(abs_path, name)

    raise UnsupportedFormatError(
        f"unsupported extension {ext!r}; supported: {sorted(SUPPORTED_EXTENSIONS)}"
    )
