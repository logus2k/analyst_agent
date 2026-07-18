"""Precision judge (adversarial second opinion) over identified requirements.

Same model, different role. For each candidate requirement the judge returns a
three-way verdict — requirement / not_requirement / uncertain — plus a short
justification. "uncertain" is a first-class outcome: genuinely borderline items
(e.g. use-case scenario steps) are surfaced as borderline, not forced.

This measures PRECISION (false positives among the extracted set). Recall (what
was missed) is a separate analysis — it needs a pass over the full source, not
over the extracted set.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from analyst_agent.llm.client import AgentServerClient, LLMError
from analyst_agent.segment.model import DiscreteRequirement
from analyst_agent.segment.prompts import JUDGE_AGENT_NAME

logger = logging.getLogger(__name__)

VERDICTS = {"requirement", "not_requirement", "uncertain"}
JUDGE_BATCH = 20


@dataclass
class Verdict:
    req_id: str
    text: str
    verdict: str          # requirement | not_requirement | uncertain
    justification: str


def _render_batch(batch: list[DiscreteRequirement]) -> str:
    lines = []
    for i, r in enumerate(batch):
        lines.append(f"[{i}] (section: {r.provenance.section_path}) {r.text}")
    return "\n".join(lines)


def judge_requirements(
    requirements: list[DiscreteRequirement],
    client: AgentServerClient | None = None,
    batch_size: int = JUDGE_BATCH,
) -> list[Verdict]:
    """Judge each requirement for precision. Returns one Verdict per input
    (order preserved). A batch that fails to parse yields 'uncertain' verdicts
    so nothing is silently dropped."""
    client = client or AgentServerClient()
    out: list[Verdict] = []

    for start in range(0, len(requirements), batch_size):
        batch = requirements[start:start + batch_size]
        try:
            result = client.complete_json(JUDGE_AGENT_NAME, _render_batch(batch))
            raw = result.get("verdicts")
            if not isinstance(raw, list):
                raise LLMError(f"'verdicts' missing/not a list: {result!r}")
            by_index: dict[int, dict] = {}
            for v in raw:
                if isinstance(v, dict) and isinstance(v.get("index"), int):
                    by_index[v["index"]] = v
        except LLMError as e:
            logger.warning("judge batch @%d failed: %s", start, e)
            by_index = {}

        for i, r in enumerate(batch):
            v = by_index.get(i)
            verdict = (v or {}).get("verdict")
            just = (v or {}).get("justification") or ""
            if verdict not in VERDICTS:
                verdict, just = "uncertain", just or "judge did not return a valid verdict for this item"
            out.append(Verdict(req_id=r.req_id, text=r.text, verdict=verdict, justification=just.strip()))

    return out
