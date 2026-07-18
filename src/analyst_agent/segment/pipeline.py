"""Component 2 pipeline: SourceItem stream → List[DiscreteRequirement].

chunk → LLM identify → deterministic verify → assign IDs + provenance.
"""

from __future__ import annotations

import logging
import re

from analyst_agent.ingest.model import BlockType, SourceItem
from analyst_agent.llm.client import AgentServerClient
from analyst_agent.segment.chunker import chunk_items
from analyst_agent.segment.identify import identify_chunk, identify_table
from analyst_agent.segment.model import DiscreteRequirement, Provenance
from analyst_agent.segment import verify
from analyst_agent.segment.assemble import reassemble
from analyst_agent.segment.dedup import dedup_overview

logger = logging.getLogger(__name__)

# Explicit requirement IDs authors use, e.g. [DEMO-SRS-53], REQ-001, PERF-002.
_ID_RE = re.compile(r"\[?\b([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+)\b\]?")


def _extract_existing_id(text: str) -> str | None:
    m = _ID_RE.match(text.strip())
    if m:
        return m.group(1)
    # Also catch a bracketed id anywhere near the start.
    m2 = _ID_RE.search(text[:40])
    return m2.group(1) if m2 else None


def segment_items(
    items: list[SourceItem],
    client: AgentServerClient | None = None,
    assemble: bool = True,
    dedup: bool = True,
) -> list[DiscreteRequirement]:
    """Identify discrete requirements in an ingested item stream.

    When `assemble` is True, a cross-block reassembly pass merges pieces that
    share an author ID (e.g. a table label + its Planguage metric) into one
    requirement (see analyst_agent.segment.assemble).

    When `dedup` is True, an overview-dedup pass marks terse summary bullets as
    `duplicate_of` their detailed requirement (see analyst_agent.segment.dedup). Marked
    duplicates are returned (for audit) but should be excluded from the primary
    set used for scoring: `[r for r in result if r.duplicate_of is None]`.
    """
    client = client or AgentServerClient()

    # Route by structural label from ingestion: tables go to the table-aware
    # identifier (one call per table), everything else to the prose identifier.
    # This is a structural route off Docling's label, not a lexical gate.
    prose_items = [it for it in items if it.block_type != BlockType.TABLE]
    table_items = [it for it in items if it.block_type == BlockType.TABLE]
    chunks = chunk_items(prose_items)
    logger.info("segmenting %d items (%d prose chunks, %d tables)",
                len(items), len(chunks), len(table_items))

    raws = []
    for chunk in chunks:
        raws.extend(identify_chunk(client, chunk))
    for t in table_items:
        raws.extend(identify_table(client, t))

    requirements: list[DiscreteRequirement] = []
    seen: set[tuple[int, str]] = set()
    used_ids: set[str] = set()
    gen_counter = 0
    n_dropped_untraceable = 0

    def unique_id(base: str | None) -> str:
        nonlocal gen_counter
        if base:
            cand = base
            suffix = 0
            while cand in used_ids:
                suffix += 1
                cand = f"{base}-{chr(ord('a') + suffix - 1)}"
            used_ids.add(cand)
            return cand
        gen_counter += 1
        cand = f"REQ-{gen_counter:04d}"
        while cand in used_ids:
            gen_counter += 1
            cand = f"REQ-{gen_counter:04d}"
        used_ids.add(cand)
        return cand

    for raw in raws:
            text = raw.text.strip()
            if not verify.is_valid_length(text):
                continue
            traceable, conf = verify.traceability(text, raw.source_item.text)
            if not traceable:
                n_dropped_untraceable += 1
                logger.debug("dropped untraceable: %r (src %r)", text[:60], raw.source_item.text[:60])
                continue
            key = verify.dedup_key(raw.source_item.order, text)
            if key in seen:
                continue
            seen.add(key)

            # Prefer an ID the model named (tables). For prose, extract a leading
            # author ID from the item text. Never regex a whole table block.
            if raw.existing_id:
                existing = raw.existing_id
            elif raw.source_item.block_type == BlockType.TABLE:
                existing = None
            else:
                existing = _extract_existing_id(raw.source_item.text)
            if existing:
                existing = re.sub(r"[.\s]+$", "", existing) or None   # a trailing '.' isn't part of the ID
            req_id = unique_id(existing)
            src = raw.source_item
            requirements.append(DiscreteRequirement(
                req_id=req_id,
                text=text,
                provenance=Provenance(
                    source_file=src.source_file,
                    section_path=src.section_path,
                    order=src.order,
                    page=src.page,
                    bbox=src.bbox,
                    char_span=src.char_span,
                ),
                origin="derived" if raw.was_compound else "extracted",
                derived_from=existing if raw.was_compound else None,
                was_compound=raw.was_compound,
                identification_confidence=round(conf, 3),
            ))

    logger.info("identified %d requirements (dropped %d untraceable)",
                len(requirements), n_dropped_untraceable)

    if assemble:
        requirements = reassemble(requirements, client)
        logger.info("after reassembly: %d requirements", len(requirements))
    if dedup:
        requirements = dedup_overview(requirements)
        n_dup = sum(1 for r in requirements if r.duplicate_of)
        logger.info("after dedup: %d primary, %d marked duplicate",
                    len(requirements) - n_dup, n_dup)
    return requirements
