"""Cross-block reassembly by requirement ID (spec: Groups A/B/C fix).

A single requirement is often scattered across blocks that share one author ID:
a table row gives the label ("QR7 System Availability"), a separate Planguage
block gives the measurable target ("MUST: More than 98% of the time"). Neither
piece is a complete requirement alone, so the gate drops both.

This step groups identified requirements by their author ID — taken from the
req_id (table rows) or from the "ID: <X>" heading Docling captured in the
section_path — and, for any ID with more than one piece, asks the assembler role
to compose ONE requirement from the pieces' own words. Grounding is enforced by
near-match against the UNION of the piece texts; if the assembly can't be
grounded, the pieces are kept separate (never invent).

Grouping by a structural ID label is association, not lexical requirement
identification — the LLM still owns "is this a requirement".
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict

from analyst_agent.llm.client import AgentServerClient, LLMError
from analyst_agent.segment.model import DiscreteRequirement
from analyst_agent.segment.prompts import ASSEMBLER_AGENT_NAME
from analyst_agent.segment.verify import traceability

logger = logging.getLogger(__name__)

# An author requirement ID: 1-5 letters then digits (FR1, QR13, SR5, NFR12).
_REQ_ID = re.compile(r"^([A-Za-z]{1,5}\d+)(?:-[a-z]+)?$")
_SECTION_ID = re.compile(r"\bID:\s*([A-Za-z]{1,5}\d+)\b")


def group_key(req: DiscreteRequirement) -> str | None:
    """The author ID this requirement belongs to, or None. From the req_id
    (table rows carry FR/QR ids) or the 'ID: X' heading in the section path."""
    m = _REQ_ID.match(req.req_id or "")
    if m:
        return m.group(1).upper()
    m2 = _SECTION_ID.search(req.provenance.section_path or "")
    if m2:
        return m2.group(1).upper()
    return None


def _assemble_group(client: AgentServerClient, gid: str,
                    pieces: list[DiscreteRequirement]) -> DiscreteRequirement | None:
    """Compose one requirement from a group's pieces. Returns None (→ keep the
    pieces separate) on LLM failure or ungroundable output."""
    union = "\n".join(p.text for p in pieces)
    listing = "\n".join(f"- {p.text}" for p in pieces)
    user = f"Requirement ID: {gid}\nPieces:\n{listing}"
    try:
        res = client.complete_json(ASSEMBLER_AGENT_NAME, user)
    except LLMError as e:
        logger.warning("assemble %s failed: %s", gid, e)
        return None
    text = (res.get("text") or "").strip()
    if not text:
        return None
    traceable, conf = traceability(text, union)
    if not traceable:
        logger.debug("assembled %s not grounded, keeping pieces: %r", gid, text[:80])
        return None
    primary = pieces[0]
    return DiscreteRequirement(
        req_id=gid,
        text=text,
        provenance=primary.provenance,
        origin="assembled",
        derived_from=None,
        was_compound=False,
        identification_confidence=round(conf, 3),
        component_orders=sorted({p.provenance.order for p in pieces}),
    )


def reassemble(
    requirements: list[DiscreteRequirement],
    client: AgentServerClient | None = None,
) -> list[DiscreteRequirement]:
    """Merge multi-piece ID groups into single assembled requirements.

    Singletons (one piece per ID, or no ID) pass through unchanged. A group that
    fails to assemble/ground keeps its original pieces.
    """
    client = client or AgentServerClient()
    groups: dict[str, list[DiscreteRequirement]] = defaultdict(list)
    passthrough: list[DiscreteRequirement] = []

    for r in requirements:
        k = group_key(r)
        if k:
            groups[k].append(r)
        else:
            passthrough.append(r)

    out = list(passthrough)
    n_assembled = 0
    for gid, pieces in groups.items():
        if len(pieces) == 1:
            out.append(pieces[0])
            continue
        assembled = _assemble_group(client, gid, pieces)
        if assembled is not None:
            out.append(assembled)
            n_assembled += 1
        else:
            out.extend(pieces)  # fallback: keep separate, never drop

    logger.info("reassembly: %d ID groups, %d assembled across blocks",
                len(groups), n_assembled)
    return out
