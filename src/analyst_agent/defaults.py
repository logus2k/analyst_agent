"""Conventional defaults — assume what a competent analyst would assume.

The Analyst asked a restaurant-menu project 37 questions, among them the target
p95/p99 latency, the backup cadence, the retry policy and which regulatory regime
applies. Every one has a defensible conventional answer; none is a decision the
owner of a menu site should be interrogated about before anything can be built.

So the default is inverted. Instead of

    cannot find a value  ->  ask the human

it becomes

    cannot find a value  ->  assume the conventional one, RECORD it as an
                             assumption with its rationale, and ask only when no
                             defensible default exists

The human then reviews a short list of assumptions — a skim — rather than
answering an interrogation, and can override any of them.

WHAT IS NEVER ASSUMED. A default is defensible when the profession has a settled
answer (retry with exponential backoff; GDPR for an EU-facing service handling
personal data). It is NOT defensible when the answer is specific to this
business: a budget ceiling, a brand policy, a contractual SLA, a price. Those
stay questions — guessing them is worse than asking, because a wrong assumption
ships silently while a question merely waits.
"""

from __future__ import annotations

from analyst_agent.llm.client import AgentServerClient, LLMError

DEFAULTS_AGENT = "analyst_default_proposer"

#: subjects whose answer belongs to the business, never to convention
BUSINESS_SPECIFIC = (
    "budget", "cost ceiling", "price", "pricing", "sla", "contract", "contractual",
    "brand", "trademark", "vendor selection", "which vendor", "commercial",
    "licence fee", "license fee", "headcount", "staffing",
)

PROMPT = """\
You propose the CONVENTIONAL DEFAULT for an open question about a software system, \
so the project is not blocked waiting on a decision that has a settled professional answer.

You are given the SYSTEM (what it is and does) and one QUESTION.

Answer with a default ONLY when the profession has a settled, defensible answer for a \
system of this kind and size — a standard retry policy, a customary retention period, the \
regulation that plainly applies to the data described, a normal latency expectation. \
Size matters: a single-restaurant menu site is not a payments platform, and the default \
must be proportionate to the system in front of you.

Identifying which LAW OR STANDARD plainly applies is conventional, not a business choice: \
derive it from the data the system holds and where it operates (a European service storing \
personal data falls under the GDPR; card data brings PCI-DSS). Name the regime and say what \
follows from it. Only a CHOICE between regimes that the business must make is a question.

Refuse, and leave it as a question, when the answer belongs to THIS business rather than to \
the profession: a budget ceiling, a price, a contractual SLA, a brand or vendor choice, an \
internal policy document. A wrong assumption ships silently; an unanswered question merely \
waits. When in doubt, refuse.

Output ONLY JSON:
{"has_default": true|false,
 "default": "<the value or rule, one sentence, concrete>",
 "rationale": "<why this is the conventional answer for THIS system, one sentence>",
 "confidence": "high"|"medium"|"low"}
"""


def _looks_business_specific(question: str) -> bool:
    """Cheap, deterministic pre-filter — never spend a model call on a question
    whose answer is obviously the business's to give."""
    low = (question or "").lower()
    return any(term in low for term in BUSINESS_SPECIFIC)


def propose_default(question: str, system: str,
                    client: AgentServerClient | None = None) -> dict:
    """{has_default, default, rationale, confidence} for one open question.

    Fails CLOSED: any error, any unusable shape, or a low-confidence verdict
    leaves the question unanswered. A question that stays a question costs a
    human a minute; a fabricated default corrupts the requirement set.
    """
    empty = {"has_default": False, "default": "", "rationale": "", "confidence": "low"}
    if not (question or "").strip():
        return empty
    if _looks_business_specific(question):
        return {**empty, "rationale": "the answer is specific to this business, not conventional"}

    client = client or AgentServerClient()
    user = f"SYSTEM\n{system}\n\nQUESTION\n{question}"
    try:
        r = client.complete_json(DEFAULTS_AGENT, user)
    except (LLMError, AttributeError, KeyError, TypeError) as e:
        return {**empty, "rationale": f"{type(e).__name__}: {e}"}
    if not isinstance(r, dict) or not r.get("has_default"):
        return empty

    value = str(r.get("default") or "").strip()
    conf = str(r.get("confidence") or "").strip().lower()
    if not value or conf not in ("high", "medium"):
        # low confidence is not a default, it is a guess wearing one's clothes
        return empty
    return {"has_default": True, "default": value[:400],
            "rationale": str(r.get("rationale") or "")[:300], "confidence": conf}


def propose_for_questions(questions: list, system: str,
                          client: AgentServerClient | None = None) -> list:
    """Annotate each question with its proposed default, in place-safe copies.

    Nothing is applied here: this only turns "answer these 37" into "review these
    assumptions and answer the few that are genuinely yours".
    """
    client = client or AgentServerClient()
    out = []
    for q in questions or []:
        if not isinstance(q, dict):
            continue
        d = propose_default(q.get("question", ""), system, client)
        out.append({**q, "proposed_default": d if d.get("has_default") else None,
                    "needs_human": not d.get("has_default")})
    return out
