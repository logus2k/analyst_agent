"""Phase A — a partial score must never pass as a complete one.

The convergence loop exits on "every requirement >= threshold". If a mean over
6 surviving judges is shaped identically to a mean over 9, the loop can exit —
and the set can be released — on scores that were never fully computed.
"""

from analyst_agent import jobs
from analyst_agent.assess import _judge_health, _needs_review, _overall
from analyst_agent.score.characteristics import CHARACTERISTICS

ALL_IDS = [cid for cid, _, _ in CHARACTERISTICS]


def _chars(scores: dict) -> list[dict]:
    """Characteristic list in canonical order; a missing id means that judge failed."""
    return [{"id": cid, "score": scores.get(cid)} for cid in ALL_IDS]


def _rec(scores: dict, text: str = "The system shall do the thing.") -> dict:
    return {"req_id": "REQ-0001", "text": text,
            "characteristics": {cid: {"id": cid, "score": scores.get(cid)}
                                for cid in ALL_IDS}}


# --- _overall -------------------------------------------------------------

def test_overall_averages_only_answering_judges():
    assert _overall(_chars({c: 4 for c in ALL_IDS})) == 4.0


def test_overall_is_none_when_every_judge_failed():
    assert _overall(_chars({})) is None


def test_overall_keeps_a_literal_zero():
    """Truthiness filtering would drop a 0 as 'no answer' and inflate the mean."""
    scores = {c: 5 for c in ALL_IDS}
    scores[ALL_IDS[0]] = 0
    # mean of eight 5s and one 0 == 40/9, NOT 5.0
    assert _overall(_chars(scores)) == round(40 / 9, 2)


# --- judge health ---------------------------------------------------------

def test_judge_health_counts_answers():
    partial = {c: 4 for c in ALL_IDS[:6]}
    assert _judge_health(_chars(partial)) == {"judges_ok": 6, "judges_total": 9}


def test_partial_and_complete_means_are_indistinguishable_without_health():
    """The exact confusion this phase exists to remove: identical means, and
    only the health counters tell them apart."""
    complete = _chars({c: 4 for c in ALL_IDS})
    partial = _chars({c: 4 for c in ALL_IDS[:6]})
    assert _overall(complete) == _overall(partial)          # both 4.0
    assert _judge_health(complete) != _judge_health(partial)


# --- _needs_review --------------------------------------------------------

def test_failed_judge_forces_review():
    """`or 5` treated a failed judge as excellent, suppressing review on exactly
    the requirements whose scores were least trustworthy."""
    assert _needs_review(_chars({c: 5 for c in ALL_IDS[:8]})) is True


def test_all_good_scores_need_no_review():
    assert _needs_review(_chars({c: 5 for c in ALL_IDS})) is False


def test_low_score_needs_review():
    scores = {c: 5 for c in ALL_IDS}
    scores[ALL_IDS[0]] = 3
    assert _needs_review(_chars(scores)) is True


# --- batch path (jobs.py) mirrors the interactive path --------------------

def test_finalize_scores_records_health():
    rec = _rec({c: 4 for c in ALL_IDS[:6]})
    jobs._finalize_scores(rec)
    assert rec["overall"] == 4.0
    assert rec["judges_ok"] == 6
    assert rec["judges_total"] == 9


def test_finalize_scores_complete():
    rec = _rec({c: 4 for c in ALL_IDS})
    jobs._finalize_scores(rec)
    assert (rec["judges_ok"], rec["judges_total"]) == (9, 9)


def test_jobs_needs_review_forces_on_failure():
    assert jobs._needs_review(_rec({c: 5 for c in ALL_IDS[:8]})) is True
    assert jobs._needs_review(_rec({c: 5 for c in ALL_IDS})) is False


def test_aggregates_flags_incompletely_judged():
    good = _rec({c: 4 for c in ALL_IDS})
    bad = _rec({c: 4 for c in ALL_IDS[:6]})
    bad["req_id"] = "REQ-0002"
    for r in (good, bad):
        jobs._finalize_scores(r)
    agg = jobs._aggregates([good, bad])
    assert agg["incompletely_judged"] == ["REQ-0002"]


def test_aggregates_distribution_keeps_zero_overall():
    """`if r["overall"]` would silently drop a 0.0 from the distribution."""
    rec = _rec({c: 0 for c in ALL_IDS})
    jobs._finalize_scores(rec)
    assert rec["overall"] == 0.0
    assert jobs._aggregates([rec])["score_distribution"] == {0: 1}
