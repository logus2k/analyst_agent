"""Semantic-plausibility check — the guard INCOSE scoring can't provide.

The INCOSE judges (C1–C9) score a requirement's FORM — singular, unambiguous, verifiable —
in isolation. A requirement can be perfectly well-formed yet NONSENSE for the product: a false
premise ("resolve the tenant's NAME from its ADDRESS data"), a category error, a
self-contradiction. Those sail through the quality gate (high INCOSE score) and then surface
downstream as unanswerable planner questions.

This module runs a complementary judge (`analyst_sense_judge`) that reads each requirement
AGAINST the problem statement and asks only: does this cohere as a plausible capability for
THIS product? It deliberately does NOT penalise vagueness/under-specification (that is INCOSE's
job) — it flags genuine incoherence, so a human can fix or drop the requirement before the
Architect/Planner build on it.

Registered in agent_server as `analyst_sense_judge`; SENSE_SYSTEM_PROMPT below is the source
of truth (re-register via the admin API after editing).
"""

from __future__ import annotations

from analyst_agent import store as pj
from analyst_agent.coverage import compact_problem_statement
from analyst_agent.llm.client import AgentServerClient

SENSE_AGENT = "analyst_sense_judge"

SENSE_SYSTEM_PROMPT = """\
You are a requirements SEMANTIC PLAUSIBILITY judge. INCOSE quality checks whether a requirement is WELL-FORMED (singular, unambiguous, verifiable). YOU check something different and complementary: does this requirement make COHERENT SENSE as a capability of THIS product, given the problem statement?

You are given the PROBLEM STATEMENT (what the product is) and ONE REQUIREMENT. Judge ONLY semantic coherence — NOT grammar, completeness, or missing detail (those are INCOSE's job).

Mark "plausible": false ONLY when the requirement falls into one of these categories — and name which one in "issue":

1. FALSE PREMISE — the stated operation is impossible: X cannot be derived from / produced by Y. (e.g. "derive a person's NAME from their ADDRESS" — an address does not determine a name.)

2. WRONG SUBJECT / OUT-OF-DOMAIN ACTOR — the thing that performs the requirement (the "shall" subject) is NOT the product under development, nor an actor/role, nor a data entity/component that exists in THIS product's domain. In particular, flag a subject that belongs to the BUILD or DESIGN process rather than to the running product: a design or build tool, an "agent" that builds the software, the developer/architect/designer, or any element absent from the product itself. (e.g. "The Architect Agent shall validate the vision input" — "The Architect Agent" builds software; it is not a component of the product. But "The system shall…", "The Tenant Administrator shall…", and "The Reservation entity shall store…" all have valid in-domain subjects.)

3. DESIGN-TIME META-REQUIREMENT — the requirement describes producing the DESIGN or SPECIFICATION itself rather than a RUNTIME behavior of the product: "shall DEFINE / DESIGN / SPECIFY / DETERMINE the fields / schema / interface / data model / requirements". Producing a schema is the designer's job, not something the product does at runtime. (e.g. "shall define the specific fields necessary for the Location structure".) IMPORTANT: storing, persisting, reading, validating, or displaying concrete data AT RUNTIME IS a product behavior — only flag the act of DESIGNING the specification.

4. SELF-CONTRADICTION, or a capability that makes no sense in the domain at all.

Otherwise mark "plausible": true — INCLUDING when the requirement is vague, under-specified, or its data-model SHAPE is awkward (which fields live on which entity, storing related fields together, whether a record should be split). Those are INCOSE's or the designer's concern, NOT incoherence. When unsure, choose true. Do NOT flag a requirement merely for being vague, high-level, untidy, or missing detail.

Output ONLY JSON: {"plausible": true|false, "issue": "<if implausible, WHICH category (1-4) and the specific problem in one sentence; else empty>", "confidence": 0.0-1.0}
"""


def judge_one(req_text: str, problem: str, client: AgentServerClient) -> dict:
    """Semantic-plausibility verdict for one requirement. Fails OPEN (plausible=true) on any
    malformed/empty model output — a judge error must never silently condemn a requirement."""
    user = f"PROBLEM STATEMENT:\n{problem}\n\nREQUIREMENT:\n{req_text}"
    out = client.complete_json(SENSE_AGENT, user) or {}
    p = out.get("plausible", True)
    plausible = bool(p) if isinstance(p, (bool, int)) else str(p).strip().lower() != "false"
    return {"plausible": plausible,
            "issue": (out.get("issue") or "").strip() if not plausible else "",
            "confidence": out.get("confidence")}


def run(pid: str, run_id: str | None = None, client: AgentServerClient | None = None,
        progress=None) -> dict:
    """Judge every requirement's semantic plausibility against the problem statement. Reads the
    CURRENT text (merged scorecard = reviewed/edited text where present). Persists and returns
    {run_id, results{rid:{plausible,issue,confidence}}, implausible:[rid,…]}."""
    client = client or AgentServerClient()
    sc = pj.merged_scorecard(pid, run_id) or pj.get_quality_scorecard(pid, run_id) or {}
    reqs = [(r.get("req_id"), (r.get("text") or "").strip())
            for r in sc.get("requirements", []) if r.get("req_id") and (r.get("text") or "").strip()]
    problem = compact_problem_statement(pj.get_problem_statement(pid))

    results: dict[str, dict] = {}
    total = len(reqs)
    for i, (rid, text) in enumerate(reqs, 1):
        results[rid] = judge_one(text, problem, client)
        if progress:
            progress(i, total, rid, results[rid])

    data = {"run_id": run_id, "total": total, "results": results,
            "implausible": sorted(rid for rid, j in results.items() if not j["plausible"])}
    pj.save_sense(pid, data)
    return data
