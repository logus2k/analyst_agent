"""The approval gate: identify → judge → (drop | accept | refine-loop).

The Judge is an approval gate; it never writes requirements. Per requirement:
  - requirement      → ACCEPTED
  - not_requirement  → DROPPED immediately (retained + justification, for audit)
  - uncertain        → sent back to the Refiner, which either isolates the real
                       requirement (staying grounded to source) or drops it; then
                       the Judge re-gates. Bounded at `max_iters` rounds; still
                       uncertain after that → ESCALATE_TO_HUMAN.

Membership only — INCOSE quality is a separate workflow step. Refined text is
validated against the source (near-match); a refinement that can't be grounded
escalates rather than being trusted (no invention).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace

from analyst_agent.llm.client import AgentServerClient, LLMError
from analyst_agent.segment.judge import judge_requirements
from analyst_agent.segment.model import DiscreteRequirement
from analyst_agent.segment.prompts import REFINER_AGENT_NAME
from analyst_agent.segment.verify import traceability

logger = logging.getLogger(__name__)

ACCEPTED = "accepted"
DROPPED = "dropped"
ESCALATED = "escalated"


@dataclass
class GateRound:
    role: str                 # "judge" | "refiner"
    outcome: str              # verdict (judge) or action (refiner)
    justification: str
    text_after: str | None    # requirement text in effect after this round


@dataclass
class GatedRequirement:
    requirement: DiscreteRequirement   # final text (possibly refined)
    disposition: str                   # accepted | dropped | escalated
    reason: str                        # justification for the final disposition
    rounds: list[GateRound]


@dataclass
class _RefineResult:
    action: str               # "refine" | "drop"
    text: str | None
    justification: str


def _refine_one(client: AgentServerClient, req: DiscreteRequirement,
                reviewer_reason: str, source_text: str) -> _RefineResult:
    user = (
        f"Reviewer reason: {reviewer_reason}\n"
        f"Original source block: {source_text}\n"
        f"Candidate requirement: {req.text}"
    )
    try:
        r = client.complete_json(REFINER_AGENT_NAME, user)
    except LLMError as e:
        logger.warning("refiner failed: %s", e)
        return _RefineResult("drop", None, f"refiner error: {e}")
    action = r.get("action")
    if action not in ("refine", "drop"):
        return _RefineResult("drop", None, "refiner returned no valid action")
    return _RefineResult(action, (r.get("text") or "").strip() or None,
                         (r.get("justification") or "").strip())


def gate_requirements(
    requirements: list[DiscreteRequirement],
    source_by_order: dict[int, str],
    client: AgentServerClient | None = None,
    max_iters: int = 3,
) -> list[GatedRequirement]:
    """Run every requirement through the approval gate."""
    client = client or AgentServerClient()
    verdicts = judge_requirements(requirements, client)  # batched first pass

    results: list[GatedRequirement] = []
    for req, v in zip(requirements, verdicts):
        rounds = [GateRound("judge", v.verdict, v.justification, req.text)]

        if v.verdict == "requirement":
            results.append(GatedRequirement(req, ACCEPTED, v.justification, rounds))
            continue
        if v.verdict == "not_requirement":
            results.append(GatedRequirement(req, DROPPED, v.justification, rounds))
            continue

        # uncertain → refinement loop
        current = req
        last_reason = v.justification
        disposition: tuple[str, str] | None = None
        source_text = source_by_order.get(req.provenance.order, req.text)

        for _ in range(max_iters):
            ref = _refine_one(client, current, last_reason, source_text)
            rounds.append(GateRound("refiner", ref.action, ref.justification, ref.text))

            if ref.action == "drop" or not ref.text:
                disposition = (DROPPED, ref.justification or "refiner dropped it")
                break

            traceable, _ = traceability(ref.text, source_text)
            if not traceable:
                disposition = (ESCALATED, "refined text not traceable to source")
                break

            current = replace(current, text=ref.text, origin="refined")
            v2 = judge_requirements([current], client)[0]
            rounds.append(GateRound("judge", v2.verdict, v2.justification, current.text))
            last_reason = v2.justification
            if v2.verdict == "requirement":
                disposition = (ACCEPTED, v2.justification)
                break
            if v2.verdict == "not_requirement":
                disposition = (DROPPED, v2.justification)
                break
            # still uncertain → next iteration

        if disposition is None:
            disposition = (ESCALATED, f"still uncertain after {max_iters} refinement rounds")

        results.append(GatedRequirement(current, disposition[0], disposition[1], rounds))

    return results
