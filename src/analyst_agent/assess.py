"""Single-requirement assessment — the interactive-editor path.

Unlike `scripts/produce_scorecard.py` (a batch driver over a whole document),
this scores ONE requirement statement the user is typing. No ingest, no
segmentation, no set-level (all of which need a document / the whole set).

Two tiers, matching the editor UX:
  - deterministic findings — pure regex, sub-millisecond, safe to run on every
    keystroke (`analyst_agent.score.deterministic.check_requirement`).
  - LLM tier — the 9 C1–C9 judges (batch=1) + the Reviewer for rewrite
    suggestions. Local-LLM bound (~4s for all 9 warm), so the editor runs this
    on a debounce, not per keystroke.

`assess_requirement` returns the full result in one shot. `iter_assessment`
yields the same information incrementally (deterministic first, then one event
per judge as it returns, then the review) so the editor can stream results —
the single GPU serializes the judges anyway, so first-score-in-~0.5s beats
waiting ~4s for a parallel batch.
"""

from __future__ import annotations

import statistics
from concurrent.futures import ThreadPoolExecutor
from typing import Iterator

from analyst_agent.llm.client import AgentServerClient, LLMError
from analyst_agent.score.characteristics import CHARACTERISTICS, normalize_rule_ids
from analyst_agent.score.deterministic import check_requirement

# Run the Reviewer when any characteristic scores at or below this (1–5 scale).
REVIEW_IF_MIN_SCORE_AT_MOST = 3

# Fast-lane: the highest-signal characteristics stream FIRST so the editor shows
# a stable headline verdict in ~2s, before the remaining judges fill in. The
# single GPU serializes the judges, so emission order == what the user sees when.
FAST_LANE = ("C3", "C4", "C5", "C7")


def _priority_order() -> list[tuple[str, str, str]]:
    """CHARACTERISTICS reordered fast-lane-first, everything else after."""
    fast = [c for c in CHARACTERISTICS if c[0] in FAST_LANE]
    rest = [c for c in CHARACTERISTICS if c[0] not in FAST_LANE]
    return fast + rest


def _judge(client: AgentServerClient, cid: str, suffix: str, text: str) -> dict:
    """Score `text` on one characteristic (batch=1). Never raises — a failed
    judge is reported as score=None with an error so the others still land."""
    try:
        a = client.complete_json(f"incose_{suffix}", f"[0] {text}")["assessments"][0]
        return {
            "id": cid,
            "score": a.get("score"),
            "rules_triggered": normalize_rule_ids(a.get("rules_triggered")),
            "evidence": a.get("evidence", ""),
            "justification": a.get("justification", ""),
        }
    except Exception as e:  # noqa: BLE001 — contract: a judge NEVER raises (a transient timeout /
        # JSON / connection error must not abort the stream and drop the terminal 'done').
        return {"id": cid, "score": None, "error": f"{type(e).__name__}: {e}"}


def _review(client: AgentServerClient, text: str, characteristics: list[dict],
            deterministic: list[dict]) -> dict:
    """Ask the Reviewer for rewrites/advisories, given the defective scores and
    deterministic findings. Mirrors the bundle produced in produce_scorecard."""
    names = {cid: name for cid, _, name in CHARACTERISTICS}
    bundle = [f"REQUIREMENT: {text}", "\nASSESSMENT:"]
    for c in characteristics:
        if c.get("score") and c["score"] < 5:
            bundle.append(
                f"  {c['id']} {names.get(c['id'], '')} score={c['score']} "
                f"rules={c.get('rules_triggered')} :: {c.get('justification', '')}")
    bundle.append("\nDETERMINISTIC FINDINGS:")
    for f in deterministic:
        bundle.append(f"  {f['rule_id']} {f['name']}: {[m['term'] for m in f['matches']]}")
    try:
        r = client.complete_json("incose_reviewer", "\n".join(bundle))
        return {"rewrites": r.get("rewrites", []), "advisories": r.get("advisories", [])}
    except Exception as e:  # noqa: BLE001 — review is the LAST step before 'done'; a failure here
        # must NOT drop the terminal event (that's what left AVG SCORE stuck on "—").
        return {"error": f"{type(e).__name__}: {e}"}


def _overall(characteristics: list[dict]) -> float | None:
    scores = [c["score"] for c in characteristics if c.get("score")]
    return round(statistics.mean(scores), 2) if scores else None


def _needs_review(characteristics: list[dict]) -> bool:
    return any((c.get("score") or 5) <= REVIEW_IF_MIN_SCORE_AT_MOST
               for c in characteristics)


def assess_requirement(text: str, client: AgentServerClient | None = None,
                       review: bool = True, workers: int = 9) -> dict:
    """Full single-requirement assessment: deterministic + 9 judges + review.

    Returns {text, deterministic, characteristics, overall, review}. The judges
    run concurrently; on one GPU they largely serialize, but concurrency still
    trims wall-clock vs strictly sequential."""
    text = text.strip()
    client = client or AgentServerClient()
    deterministic = [f.to_dict() for f in check_requirement(text)]

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(_judge, client, cid, suffix, text)
                   for cid, suffix, _ in CHARACTERISTICS]
        by_id = {f.result()["id"]: f.result() for f in futures}
    characteristics = [by_id[cid] for cid, _, _ in CHARACTERISTICS]

    result = {
        "text": text,
        "deterministic": deterministic,
        "characteristics": characteristics,
        "overall": _overall(characteristics),
        "review": None,
    }
    if review and _needs_review(characteristics):
        result["review"] = _review(client, text, characteristics, deterministic)
    return result


def iter_assessment(text: str, client: AgentServerClient | None = None,
                    review: bool = True) -> Iterator[dict]:
    """Stream the assessment as events for a live editor:

      {"type": "deterministic", "findings": [...]}          (immediate, ~3ms)
      {"type": "characteristic", "data": {...}, "i": k, "n": 9}   (per judge)
      {"type": "review", "data": {...}}                     (if defective)
      {"type": "done", "overall": <float|null>}

    Judges stream fast-lane-first (see FAST_LANE) so the headline verdict is
    stable in ~2s. The reviewer runs last and is the costliest step; pass
    review=False to skip it (e.g. run it only on a longer pause / explicit
    request) and call `review_requirement` separately."""
    text = text.strip()
    client = client or AgentServerClient()

    deterministic = [f.to_dict() for f in check_requirement(text)]
    yield {"type": "deterministic", "findings": deterministic}

    characteristics: list[dict] = []
    order = _priority_order()
    n = len(order)
    for i, (cid, suffix, _) in enumerate(order, 1):
        c = _judge(client, cid, suffix, text)
        characteristics.append(c)
        yield {"type": "characteristic", "data": c, "i": i, "n": n}

    if review and _needs_review(characteristics):
        yield {"type": "review", "data": _review(client, text, characteristics, deterministic)}

    yield {"type": "done", "overall": _overall(characteristics)}


def review_requirement(text: str, characteristics: list[dict],
                       deterministic: list[dict] | None = None,
                       client: AgentServerClient | None = None) -> dict:
    """Standalone Reviewer call — for the editor's deferred 'suggest fix' step,
    reusing characteristics already scored by a prior assessment."""
    client = client or AgentServerClient()
    if deterministic is None:
        deterministic = [f.to_dict() for f in check_requirement(text)]
    return _review(client, text.strip(), characteristics, deterministic)
