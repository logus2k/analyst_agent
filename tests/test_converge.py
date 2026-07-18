"""Phase D — convergence loop termination.

The loop spends many minutes and many LLM calls per round, so its exit conditions
must be provable rather than observed. Everything below runs offline: refine,
coverage and authoring are stubbed, and each test scripts a sequence of gap counts
to drive the loop down a specific path.

The four outcomes:
  converged  gaps == 0 AND quality clean
  stalled    gap count stopped dropping
  capped     MAX_ROUNDS reached (a backstop, never the completion test)
  cancelled  aborted mid-run
"""

import pytest

from analyst_agent import converge


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A project with one quality run, and stubbed refine/coverage/author.

    `gap_sequence` drives what coverage reports each round; `quality` is the
    quality state the loop reads back.
    """
    from analyst_agent import store as pj
    monkeypatch.setattr(pj, "STORE", str(tmp_path))
    monkeypatch.setattr(pj, "PROJECTS_DIR", str(tmp_path / "projects"))
    proj = pj.create_project("P")
    pid = proj["id"]
    pj.save_quality_run(pid, "r1", {"requirements": [{"req_id": "REQ-0001", "text": "t",
                                                     "overall": 4.8, "judges_ok": 9,
                                                     "judges_total": 9}]},
                        {"run_id": "r1", "finished_at": "2026-01-01T00:00:00+00:00"})
    pj.get_review(pid, "r1")

    calls = {"refine": 0, "coverage": 0, "author": 0}

    def setup(gap_sequence, quality=None, author_raises=False):
        seq = list(gap_sequence)

        def fake_refine(*a, **k):
            calls["refine"] += 1
            return iter([{"type": "stage", "stage": "refine", "status": "done"}])

        def fake_coverage(*a, **k):
            calls["coverage"] += 1
            n = seq.pop(0) if seq else 0
            gaps = [{"domain": "d", "title": f"g{i}"} for i in range(n)]
            return iter([{"type": "coverage", "data": {"gaps": gaps,
                                                       "requirement_count": 1}}])

        def fake_author(*a, **k):
            calls["author"] += 1
            if author_raises:
                raise RuntimeError("author blew up")
            return iter([{"type": "author_summary", "data": {"authored": 1}}])

        monkeypatch.setattr(converge.refine, "iter_refine_for_project", fake_refine)
        monkeypatch.setattr(converge.coverage_mod, "iter_coverage_for_project", fake_coverage)
        monkeypatch.setattr(converge.authoring, "iter_author_for_project", fake_author)
        monkeypatch.setattr(converge, "_quality_state", lambda *a, **k: quality or {
            "total": 1, "threshold": 4.3, "below_threshold": 0,
            "incompletely_judged": 0, "with_placeholders": 0})
        return pid, calls

    return setup


def _run(pid, **kw):
    return list(converge.iter_converge(pid, "r1", client=object(), **kw))


def _outcome(events):
    done = [e for e in events if e.get("type") == "converge_done"]
    return done[-1]["outcome"] if done else None


# --- converged ------------------------------------------------------------

def test_converges_when_gaps_reach_zero(project):
    pid, calls = project([5, 2, 0])
    assert _outcome(_run(pid)) == converge.CONVERGED


def test_converges_immediately_when_nothing_is_open(project):
    pid, calls = project([0])
    assert _outcome(_run(pid)) == converge.CONVERGED
    assert calls["author"] == 0                 # nothing to author for


def test_zero_gaps_but_dirty_quality_does_not_converge(project):
    """The loop's exit test must agree with the release gate: a set with a
    below-threshold requirement is not converged however clean coverage looks."""
    pid, _ = project([0, 0, 0], quality={"total": 1, "threshold": 4.3,
                                         "below_threshold": 1,
                                         "incompletely_judged": 0,
                                         "with_placeholders": 0})
    assert _outcome(_run(pid)) != converge.CONVERGED


def test_placeholders_alone_prevent_convergence(project):
    pid, _ = project([0, 0, 0], quality={"total": 1, "threshold": 4.3,
                                         "below_threshold": 0,
                                         "incompletely_judged": 0,
                                         "with_placeholders": 1})
    assert _outcome(_run(pid)) != converge.CONVERGED


def test_incomplete_judging_alone_prevents_convergence(project):
    pid, _ = project([0, 0, 0], quality={"total": 1, "threshold": 4.3,
                                         "below_threshold": 0,
                                         "incompletely_judged": 1,
                                         "with_placeholders": 0})
    assert _outcome(_run(pid)) != converge.CONVERGED


# --- stalled --------------------------------------------------------------

def test_stalls_when_the_count_stops_dropping(project):
    pid, _ = project([10, 10, 10, 10])
    events = _run(pid)
    assert _outcome(events) == converge.STALLED


def test_a_single_flat_round_is_tolerated_as_noise(project):
    """Coverage is an LLM; one flat round is variance, not a plateau."""
    pid, _ = project([10, 10, 4, 0])
    assert _outcome(_run(pid)) == converge.CONVERGED


def test_a_count_that_rises_counts_as_no_progress(project):
    pid, _ = project([10, 12, 13])
    assert _outcome(_run(pid)) == converge.STALLED


def test_stall_reason_names_the_counts(project):
    pid, _ = project([8, 8, 8])
    done = [e for e in _run(pid) if e.get("type") == "converge_done"][-1]
    assert "[8, 8, 8]" in done["reason"]


def test_slow_but_real_progress_is_not_a_stall(project):
    pid, _ = project([10, 9, 8, 7, 6, 5])
    assert _outcome(_run(pid)) == converge.CAPPED       # ran out of rounds, not stalled


# --- capped ---------------------------------------------------------------

def test_round_cap_is_a_backstop_not_a_completion_test(project):
    pid, _ = project([20, 15, 10, 5, 3, 2])
    done = [e for e in _run(pid) if e.get("type") == "converge_done"][-1]
    assert done["outcome"] == converge.CAPPED
    assert done["outcome"] != converge.CONVERGED       # stopping != done
    assert done["gaps"] == 2                          # and it says what is left


def test_max_rounds_is_honoured(project):
    pid, calls = project([9, 8, 7])
    _run(pid, max_rounds=2)
    assert calls["coverage"] == 2


def test_last_round_does_not_author(project):
    """Authoring on the final round would add unscored requirements nothing
    then judges — worse than not authoring at all."""
    pid, calls = project([5, 4])
    _run(pid, max_rounds=2)
    assert calls["author"] == 1                       # round 1 only


# --- cancellation ---------------------------------------------------------

def test_cancel_before_the_first_round(project):
    pid, calls = project([5, 0])
    events = _run(pid, should_cancel=lambda: True)
    assert any(e["type"] == "cancelled" for e in events)
    assert calls["coverage"] == 0


def test_cancel_persists_state(project):
    from analyst_agent import store as pj
    pid, _ = project([5, 0])
    _run(pid, should_cancel=lambda: True)
    assert pj.get_convergence(pid)["outcome"] == converge.CANCELLED


# --- state persistence ----------------------------------------------------

def test_state_persisted_at_every_round_boundary(project):
    from analyst_agent import store as pj
    pid, _ = project([5, 2, 0])
    _run(pid)
    st = pj.get_convergence(pid)
    assert st["outcome"] == converge.CONVERGED
    assert st["gap_counts"] == [5, 2, 0]


def test_state_records_authored_counts_per_round(project):
    from analyst_agent import store as pj
    pid, _ = project([5, 2, 0])
    _run(pid)
    rounds = pj.get_convergence(pid)["rounds"]
    assert [r["round"] for r in rounds] == [1, 2, 3]
    assert rounds[0]["authored"] == 1


def test_missing_quality_run_errors_cleanly(project):
    pid, _ = project([0])
    events = list(converge.iter_converge(pid, "no-such-run", client=object()))
    assert events[0]["type"] == "error"


def test_coverage_producing_nothing_stalls_rather_than_looping(project):
    pid, _ = project([])                 # coverage yields no `coverage` event
    from analyst_agent import store as pj

    def empty_coverage(*a, **k):
        return iter([{"type": "stage", "stage": "judges", "status": "done"}])
    import analyst_agent.converge as c
    c.coverage_mod.iter_coverage_for_project = empty_coverage
    events = _run(pid)
    assert _outcome(events) == converge.STALLED


# --- stalling must be ANSWERABLE -----------------------------------------
# The absolute quality floor means a stall is only resolvable by a human
# supplying information. So a stall that cannot say what to answer is a dead end.

def test_stall_carries_open_questions(project, monkeypatch):
    pid, _ = project([7, 7, 7])
    monkeypatch.setattr(converge.questions_mod, "collect_questions",
                        lambda *a, **k: [{"id": "Q-0001", "question": "What latency?",
                                          "req_ids": ["REQ-0001"], "blocking": True,
                                          "sources": ["advisory"]}])
    done = [e for e in _run(pid) if e.get("type") == "converge_done"][-1]
    assert done["outcome"] == converge.STALLED
    assert done["questions"][0]["question"] == "What latency?"
    assert done["question_summary"]["blocking"] == 1


def test_cap_carries_open_questions(project, monkeypatch):
    pid, _ = project([9, 8, 7, 6, 5, 4])
    monkeypatch.setattr(converge.questions_mod, "collect_questions",
                        lambda *a, **k: [{"id": "Q-0001", "question": "What limit?",
                                          "req_ids": ["REQ-0001"], "blocking": True,
                                          "sources": ["placeholder"]}])
    done = [e for e in _run(pid) if e.get("type") == "converge_done"][-1]
    assert done["outcome"] == converge.CAPPED
    assert done["questions"]


def test_questions_persisted_with_the_stalled_state(project, monkeypatch):
    from analyst_agent import store as pj
    pid, _ = project([7, 7, 7])
    monkeypatch.setattr(converge.questions_mod, "collect_questions",
                        lambda *a, **k: [{"id": "Q-0001", "question": "What latency?",
                                          "req_ids": ["REQ-0001"], "blocking": True,
                                          "sources": ["advisory"]}])
    _run(pid)
    st = pj.get_convergence(pid)
    assert st["outcome"] == converge.STALLED
    assert st["questions"][0]["id"] == "Q-0001"
