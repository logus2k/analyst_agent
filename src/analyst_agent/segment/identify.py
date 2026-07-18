"""The LLM identification pass (spec §6.2).

For each chunk, ask the `requirement_identifier` preset which blocks are
requirements (splitting compounds), then map the returned local indices back to
their SourceItems. Compound detection is DERIVED here (multiple outputs sharing
one index) rather than trusted from the model.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from analyst_agent.ingest.model import SourceItem
from analyst_agent.llm.client import AgentServerClient, LLMError
from analyst_agent.segment.chunker import Chunk
from analyst_agent.segment.prompts import IDENTIFIER_AGENT_NAME, TABLE_IDENTIFIER_AGENT_NAME

logger = logging.getLogger(__name__)


@dataclass
class RawIdentification:
    """One requirement the LLM returned, mapped back to its source block."""

    source_item: SourceItem
    text: str
    was_compound: bool
    existing_id: str | None = None   # author's ID when the model can name it (tables)


def identify_chunk(client: AgentServerClient, chunk: Chunk) -> list[RawIdentification]:
    """Identify requirements in one chunk. Returns [] on LLM/parse failure
    (logged) so one bad chunk doesn't sink the whole document."""
    try:
        result = client.complete_json(IDENTIFIER_AGENT_NAME, chunk.render())
    except LLMError as e:
        logger.warning("identify_chunk failed: %s", e)
        return []

    reqs = result.get("requirements")
    if not isinstance(reqs, list):
        logger.warning("identify_chunk: 'requirements' missing/not a list: %r", result)
        return []

    # Group by source index to derive compound-ness (>1 output per index).
    by_index: dict[int, list[str]] = {}
    for r in reqs:
        if not isinstance(r, dict):
            continue
        idx = r.get("index")
        text = r.get("text")
        if not isinstance(idx, int) or not isinstance(text, str) or not text.strip():
            continue
        if 0 <= idx < len(chunk.items):
            by_index.setdefault(idx, []).append(text.strip())

    out: list[RawIdentification] = []
    for idx, texts in by_index.items():
        compound = len(texts) > 1
        for t in texts:
            out.append(RawIdentification(
                source_item=chunk.items[idx],
                text=t,
                was_compound=compound,
            ))
    return out


def identify_table(client: AgentServerClient, table_item: SourceItem) -> list[RawIdentification]:
    """Identify requirements in ONE table block via the table-aware role.

    The LLM classifies the table (requirements vs definitions/revision/etc.) and
    emits one requirement per requirement-row, with the row's ID when present.
    Non-requirements tables yield []. Grounding is enforced downstream by verify
    (each text must be traceable to the table block) — no parsing here.
    """
    try:
        result = client.complete_json(TABLE_IDENTIFIER_AGENT_NAME, table_item.text)
    except LLMError as e:
        logger.warning("identify_table failed: %s", e)
        return []

    if not result.get("is_requirements_table"):
        return []
    reqs = result.get("requirements")
    if not isinstance(reqs, list):
        logger.warning("identify_table: 'requirements' missing/not a list: %r", result)
        return []

    out: list[RawIdentification] = []
    for r in reqs:
        if not isinstance(r, dict):
            continue
        text = r.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        rid = r.get("id")
        rid = rid.strip() if isinstance(rid, str) and rid.strip() else None
        out.append(RawIdentification(
            source_item=table_item,
            text=text.strip(),
            was_compound=False,
            existing_id=rid,
        ))
    return out
