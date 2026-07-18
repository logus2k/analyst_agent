"""Markdown ingestion path (no Docling).

Markdown is already structured text, so we parse it directly with
`markdown-it-py`. We walk the token stream, track the heading stack to build
`section_path`, and convert each block's line range into character offsets so
downstream span-grounding has real `char_span`s.
"""

from __future__ import annotations

from markdown_it import MarkdownIt

from analyst_agent.ingest.model import BlockType, IngestResult, SourceItem


def _line_char_offsets(text: str) -> list[int]:
    """Return a list where element i is the character offset at which line i
    starts. Length = number of lines + 1 (final entry = len(text)) so a token
    map [start_line, end_line) maps to chars [offsets[start], offsets[end])."""
    offsets = [0]
    for line in text.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    return offsets


def _span_for(token, line_offsets: list[int], text_len: int) -> tuple[int, int] | None:
    """Char span for a token from its `.map` (line range), clamped safely."""
    if not token.map:
        return None
    start_line, end_line = token.map
    if start_line < 0 or start_line >= len(line_offsets):
        return None
    start = line_offsets[start_line]
    end = line_offsets[end_line] if end_line < len(line_offsets) else text_len
    return (start, end)


def _render_table(tokens, i: int, text: str, line_offsets: list[int]) -> tuple[str, tuple[int, int] | None]:
    """Reconstruct the raw markdown of a table from the source using the
    table_open token's line map (keeps the original pipe syntax rather than
    re-rendering, so the text stays faithful to the source)."""
    open_tok = tokens[i]
    span = _span_for(open_tok, line_offsets, len(text))
    raw = text[span[0]:span[1]].strip() if span else ""
    return raw, span


def parse_markdown(text: str, source_file: str) -> IngestResult:
    """Parse Markdown text into normalized `SourceItem`s."""
    md = MarkdownIt("commonmark").enable("table")
    tokens = md.parse(text)
    line_offsets = _line_char_offsets(text)
    text_len = len(text)

    items: list[SourceItem] = []
    heading_stack: list[tuple[int, str]] = []  # (level, title)
    order = 0

    def section_path() -> str:
        return " > ".join(t for _, t in heading_stack) or "(root)"

    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]

        # ── Headings ──────────────────────────────────────────────────
        if tok.type == "heading_open":
            level = int(tok.tag[1])  # h1 -> 1
            inline = tokens[i + 1] if i + 1 < n else None
            title = (inline.content if inline else "").strip()
            span = _span_for(tok, line_offsets, text_len)
            # Pop siblings/deeper, then push this heading.
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, title))
            items.append(SourceItem(
                text=title,
                block_type=BlockType.HEADING,
                section_path=section_path(),
                source_file=source_file,
                order=order,
                char_span=span,
                heading_level=level,
            ))
            order += 1
            i += 3  # heading_open, inline, heading_close
            continue

        # ── Paragraphs ────────────────────────────────────────────────
        if tok.type == "paragraph_open":
            inline = tokens[i + 1] if i + 1 < n else None
            content = (inline.content if inline else "").strip()
            span = _span_for(tok, line_offsets, text_len)
            if content:
                items.append(SourceItem(
                    text=content,
                    block_type=BlockType.PARAGRAPH,
                    section_path=section_path(),
                    source_file=source_file,
                    order=order,
                    char_span=span,
                ))
                order += 1
            i += 3
            continue

        # ── List items (one SourceItem per item) ──────────────────────
        if tok.type == "list_item_open":
            span = _span_for(tok, line_offsets, text_len)
            # Gather inline content of this list item (may hold nested blocks;
            # we take the concatenated inline text as the item's text).
            depth = 0
            parts: list[str] = []
            j = i
            while j < n:
                t = tokens[j]
                if t.type == "list_item_open":
                    depth += 1
                elif t.type == "list_item_close":
                    depth -= 1
                    if depth == 0:
                        break
                elif t.type == "inline":
                    parts.append(t.content.strip())
                j += 1
            content = " ".join(p for p in parts if p).strip()
            if content:
                items.append(SourceItem(
                    text=content,
                    block_type=BlockType.LIST_ITEM,
                    section_path=section_path(),
                    source_file=source_file,
                    order=order,
                    char_span=span,
                ))
                order += 1
            i = j + 1
            continue

        # ── Tables ────────────────────────────────────────────────────
        if tok.type == "table_open":
            raw, span = _render_table(tokens, i, text, line_offsets)
            if raw:
                items.append(SourceItem(
                    text=raw,
                    block_type=BlockType.TABLE,
                    section_path=section_path(),
                    source_file=source_file,
                    order=order,
                    char_span=span,
                ))
                order += 1
            # Skip to matching table_close.
            j = i
            while j < n and tokens[j].type != "table_close":
                j += 1
            i = j + 1
            continue

        # ── Fenced/indented code ──────────────────────────────────────
        if tok.type in ("fence", "code_block"):
            span = _span_for(tok, line_offsets, text_len)
            content = tok.content.rstrip("\n")
            if content:
                items.append(SourceItem(
                    text=content,
                    block_type=BlockType.CODE,
                    section_path=section_path(),
                    source_file=source_file,
                    order=order,
                    char_span=span,
                ))
                order += 1
            i += 1
            continue

        # ── Blockquotes ───────────────────────────────────────────────
        if tok.type == "blockquote_open":
            span = _span_for(tok, line_offsets, text_len)
            depth = 0
            parts: list[str] = []
            j = i
            while j < n:
                t = tokens[j]
                if t.type == "blockquote_open":
                    depth += 1
                elif t.type == "blockquote_close":
                    depth -= 1
                    if depth == 0:
                        break
                elif t.type == "inline":
                    parts.append(t.content.strip())
                j += 1
            content = " ".join(p for p in parts if p).strip()
            if content:
                items.append(SourceItem(
                    text=content,
                    block_type=BlockType.QUOTE,
                    section_path=section_path(),
                    source_file=source_file,
                    order=order,
                    char_span=span,
                ))
                order += 1
            i = j + 1
            continue

        i += 1

    return IngestResult(source_file=source_file, format="markdown", items=items)
