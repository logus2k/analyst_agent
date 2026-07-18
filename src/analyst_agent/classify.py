"""Requirement classification — the Analyst's half of the Architect contract.

Every released requirement carries routing metadata so the Architect Agent can send it
to the right module without re-deriving it:

  classes[]     MULTI-label routing labels; each maps to one Architect module and one
                SysML v2 construct (functional/structural/interface/behavioral/
                constraint/allocation).
  type          ONE primary label, for reporting and the UI.
  constraints[] the quality attributes the requirement actually bounds.

WHY BOTH `classes` AND `type` (this reconciles two contract documents):
`analyst_agent/documents/technical_architecture.md` §7 specifies a single-label `type`
from an 11-value taxonomy. `architect_agent/documents/implementation.md` §2.1.1 argues —
correctly — that routing is inherently MULTI-label: "The system shall allocate GPUs
fairly within 100 ms" is both `functional` and `constraint`, and a single label
"silently drops the timing budget". A single label cannot drive the Architect's routing,
and the routing labels are too coarse for human reporting. So we emit both: `classes`
is the machine contract, `type` is the human/UI one. Neither document has to lose.

Classification is per-item with no cross-item context, so it parallelises freely.
It is READ-ONLY with respect to requirement text — this module never rewrites anything.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Iterator

from analyst_agent import store as pj
from analyst_agent.llm.client import AgentServerClient, LLMError

WORKERS = 8
PRESET = "incose_classifier"

# The Architect's routing taxonomy (implementation.md §2.1.1) — each maps to a distinct
# downstream module and SysML v2 construct, so an unknown label is dropped, not guessed.
CLASSES = ("functional", "structural", "interface", "behavioral", "constraint", "allocation")

# The reporting taxonomy (technical_architecture.md §7).
TYPES = ("functional", "performance", "safety", "security", "usability", "reliability",
         "interface", "data", "operational", "constraint", "other")

# Closed vocabulary: a bounded set keeps `constraints[]` machine-consumable. Anything
# outside it is discarded rather than passed through as free text.
CONSTRAINT_VOCAB = (
    "performance", "latency", "throughput", "capacity", "scalability", "availability",
    "reliability", "safety", "security", "privacy", "compliance", "usability",
    "accessibility", "portability", "maintainability", "interoperability", "resource",
    "cost", "schedule", "fairness", "environmental", "data_retention", "traceability",
)


def _coerce(raw: dict) -> dict:
    """Validate the model's answer against the three vocabularies.

    The gate is "every requirement carries a VALID type and constraints[]", so this
    never returns a partially-invalid record: unknown labels are dropped and `classes`
    falls back to `functional` (the Architect must still receive a routing target).
    """
    classes = [c for c in _as_list(raw.get("classes")) if c in CLASSES]
    if not classes:
        classes = ["functional"]
    rtype = raw.get("type")
    if rtype not in TYPES:
        # Fall back to the primary routing label when it is also a valid type.
        rtype = classes[0] if classes[0] in TYPES else "other"
    constraints = [c for c in _as_list(raw.get("constraints")) if c in CONSTRAINT_VOCAB]
    return {
        "classes": classes,
        "type": rtype,
        "constraints": sorted(set(constraints)),
        "justification": str(raw.get("justification") or "")[:400],
    }


def _as_list(v) -> list[str]:
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list):
        return []
    return [str(x).strip().lower() for x in v if str(x).strip()]


def classify_requirement(text: str, client: AgentServerClient | None = None) -> dict:
    """Classify one requirement. Never raises: a failure returns a valid-but-flagged
    record so a single bad call cannot abort a whole run (same contract as the judges)."""
    client = client or AgentServerClient()
    try:
        raw = client.complete_json(PRESET, text)
    except LLMError as e:
        return {"classes": ["functional"], "type": "other", "constraints": [],
                "justification": "", "error": f"{type(e).__name__}: {e}"}
    except Exception as e:  # noqa: BLE001 — contract: classification NEVER raises
        return {"classes": ["functional"], "type": "other", "constraints": [],
                "justification": "", "error": f"{type(e).__name__}: {e}"}
    return _coerce(raw if isinstance(raw, dict) else {})


def iter_classify_for_project(pid: str, run_id: str,
                              client: AgentServerClient | None = None,
                              should_cancel=None) -> Iterator[dict]:
    """Classify every requirement of a quality run; yield progress events.

    Classifies the CURRENT text (`final_text` if refinement improved it, else the
    original), so the labels describe what will actually be released.
    """
    client = client or AgentServerClient()
    cancelled = lambda: bool(should_cancel and should_cancel())  # noqa: E731

    review = pj.get_review(pid, run_id)
    if not review:
        yield {"type": "error", "stage": "classify", "message": "no review session for this run"}
        return

    reqs = review.get("requirements") or {}
    todo = [(rid, (e.get("final_text") or e.get("original_text") or "").strip())
            for rid, e in reqs.items()]
    todo = [(rid, t) for rid, t in todo if t]

    yield {"type": "stage", "stage": "classify", "status": "start", "done": 0,
           "total": len(todo), "unit": "requirements",
           "message": f"classifying {len(todo)} requirements"}

    done = failed = 0
    ex = ThreadPoolExecutor(max_workers=WORKERS)
    try:
        # Submit in bounded waves so cancellation is responsive rather than waiting on
        # a fully-queued pool (the same trade-off jobs.py makes).
        for start in range(0, len(todo), WORKERS):
            if cancelled():
                yield {"type": "cancelled", "stage": "classify"}
                return
            wave = todo[start:start + WORKERS]
            futures = [(rid, ex.submit(classify_requirement, text, client)) for rid, text in wave]
            for rid, fut in futures:
                out = fut.result()
                if out.get("error"):
                    failed += 1
                pj.upsert_req_review(pid, run_id, rid, {"classification": out})
                done += 1
                yield {"type": "classified", "req_id": rid, "done": done, "total": len(todo),
                       "classes": out["classes"], "req_type": out["type"],
                       "constraints": out["constraints"]}
    finally:
        ex.shutdown(wait=False, cancel_futures=True)

    # Distribution is what a human actually wants to see at the end of a run.
    by_type: dict[str, int] = {}
    by_class: dict[str, int] = {}
    for e in (pj.get_review(pid, run_id) or {}).get("requirements", {}).values():
        c = e.get("classification")
        if not c:
            continue
        by_type[c["type"]] = by_type.get(c["type"], 0) + 1
        for cl in c["classes"]:
            by_class[cl] = by_class.get(cl, 0) + 1

    yield {"type": "stage", "stage": "classify", "status": "done",
           "done": done, "total": len(todo),
           "message": f"{done} classified" + (f", {failed} fell back on error" if failed else "")}
    yield {"type": "classify_summary", "data": {
        "classified": done, "errors": failed,
        "by_type": dict(sorted(by_type.items(), key=lambda kv: -kv[1])),
        "by_class": dict(sorted(by_class.items(), key=lambda kv: -kv[1]))}}
