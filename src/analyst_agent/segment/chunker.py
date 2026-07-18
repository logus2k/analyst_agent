"""Structure-bounded chunking of the SourceItem stream for the LLM.

We do NOT use raw character windows. Items are grouped in reading order, bounded
by item-count and character budgets, and preferentially broken at heading
boundaries so a requirement is never split across chunks. Each chunk renders its
items with LOCAL indices ([0], [1], ...); the identifier maps those back to the
original SourceItems.
"""

from __future__ import annotations

from dataclasses import dataclass

from analyst_agent.ingest.model import BlockType, SourceItem

MAX_ITEMS_PER_CHUNK = 25
MAX_CHARS_PER_CHUNK = 4000
MAX_LINE_CHARS = 1200  # cap a single block's rendered text (long tables, etc.)


@dataclass
class Chunk:
    items: list[SourceItem]      # local index == position in this list
    section_hint: str

    def render(self) -> str:
        lines = [f"Section: {self.section_hint}"]
        for i, it in enumerate(self.items):
            text = it.text.replace("\n", " ").strip()
            if len(text) > MAX_LINE_CHARS:
                text = text[:MAX_LINE_CHARS] + " …"
            lines.append(f"[{i}] ({it.block_type.value}) {text}")
        return "\n".join(lines)


def chunk_items(
    items: list[SourceItem],
    *,
    max_items: int = MAX_ITEMS_PER_CHUNK,
    max_chars: int = MAX_CHARS_PER_CHUNK,
) -> list[Chunk]:
    """Group items into structure-bounded chunks. Starts a new chunk when the
    size budget would be exceeded, preferring to break just before a heading."""
    chunks: list[Chunk] = []
    cur: list[SourceItem] = []
    cur_chars = 0

    def flush():
        nonlocal cur, cur_chars
        if cur:
            hint = _section_hint(cur)
            chunks.append(Chunk(items=cur, section_hint=hint))
            cur = []
            cur_chars = 0

    for it in items:
        line_len = min(len(it.text), MAX_LINE_CHARS)
        over_budget = cur and (len(cur) >= max_items or cur_chars + line_len > max_chars)
        # Prefer to break right before a heading when we're near the budget.
        if over_budget or (cur and it.block_type == BlockType.HEADING and cur_chars > max_chars * 0.6):
            flush()
        cur.append(it)
        cur_chars += line_len
    flush()
    return chunks


def _section_hint(items: list[SourceItem]) -> str:
    """A representative section path for the chunk (first item's, or the first
    heading's section_path)."""
    for it in items:
        if it.block_type == BlockType.HEADING:
            return it.section_path
    return items[0].section_path if items else "(root)"
