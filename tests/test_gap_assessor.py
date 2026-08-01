"""Gap assessor — triage each coverage gap into author / needs_input / dismiss.

The unequal risk drives the tests: a wrong `dismiss` ships a hole to the Architect,
so every ambiguous or failed path must fall to `needs_input` (surface to a human),
never to `dismiss` or a silent drop.
"""

import pytest

from analyst_agent import gap_assessor


class FakeClient:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def complete_json(self, agent, user_content):
        self.calls.append((agent, user_content))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


GAP = {"domain": "security", "domain_name": "Security",
       "title": "Input Validation", "severity": "critical",
       "detail": "No input validation is specified.",
       "question": "What validation rules apply?"}


# --- per-gap disposition --------------------------------------------------

def test_author_disposition_passes_through():
    c = FakeClient({"disposition": "author", "applies_to_system": True,
                    "rationale": "the system takes user input"})
    out = gap_assessor.assess_gap(GAP, "a system", [], client=c)
    assert out["disposition"] == "author"
    assert c.calls[0][0] == "incose_gap_assessor"


def test_dismiss_disposition_passes_through():
    c = FakeClient({"disposition": "dismiss", "applies_to_system": False,
                    "rationale": "budget/schedule is project management, not a requirement"})
    assert gap_assessor.assess_gap(GAP, "", [], client=c)["disposition"] == "dismiss"


def test_needs_input_keeps_the_question():
    c = FakeClient({"disposition": "needs_input", "applies_to_system": True,
                    "rationale": "real but the SLO target is external",
                    "question": "What availability target applies?"})
    out = gap_assessor.assess_gap(GAP, "", [], client=c)
    assert out["disposition"] == "needs_input"
    assert "availability target" in out["question"]


def test_question_dropped_for_non_needs_input():
    c = FakeClient({"disposition": "author", "question": "leftover"})
    assert gap_assessor.assess_gap(GAP, "", [], client=c)["question"] == ""


# --- the safe direction: ambiguity/failure -> needs_input -----------------

def test_unknown_disposition_falls_to_needs_input():
    c = FakeClient({"disposition": "probably fine", "rationale": "x"})
    assert gap_assessor.assess_gap(GAP, "", [], client=c)["disposition"] == "needs_input"


def test_llm_failure_falls_to_needs_input_not_dismiss():
    from analyst_agent.llm.client import LLMError
    out = gap_assessor.assess_gap(GAP, "", [], client=FakeClient(LLMError("boom")))
    assert out["disposition"] == "needs_input"        # NOT dismiss — never drop on error
    assert "error" in out


def test_list_response_falls_to_needs_input():
    out = gap_assessor.assess_gap(GAP, "", [], client=FakeClient([1, 2]))
    assert out["disposition"] == "needs_input" and "error" in out


def test_failed_assessment_carries_the_gap_question_forward():
    from analyst_agent.llm.client import LLMError
    out = gap_assessor.assess_gap(GAP, "", [], client=FakeClient(LLMError("x")))
    assert out["question"] == GAP["question"]


# --- prompt content -------------------------------------------------------

def test_prompt_carries_gap_detail_and_problem():
    c = FakeClient({"disposition": "author"})
    gap_assessor.assess_gap(GAP, "the problem statement", ["An existing req."], client=c)
    prompt = c.calls[0][1]
    assert "No input validation is specified." in prompt
    assert "the problem statement" in prompt
    assert "An existing req." in prompt


# --- full run + persistence ----------------------------------------------

@pytest.fixture
def project(tmp_path, monkeypatch):
    from analyst_agent import store as pj
    monkeypatch.setattr(pj, "STORE", str(tmp_path))
    monkeypatch.setattr(pj, "PROJECTS_DIR", str(tmp_path / "projects"))
    proj = pj.create_project("P")
    pid = proj["id"]
    pj.save_quality_run(pid, "r1", {"requirements": [{"req_id": "REQ-0001", "text": "t"}]},
                        {"run_id": "r1", "finished_at": "x"})
    pj.save_coverage_run(pid, "c1", {"gaps": [
        dict(GAP, title="A"), dict(GAP, title="B", severity="high"),
        dict(GAP, title="C", severity="high")]},
        {"run_id": "c1", "finished_at": "x"})
    return pid


def test_full_run_persists_dispositions(project, monkeypatch):
    from analyst_agent import store as pj
    replies = iter([
        {"disposition": "author", "rationale": "a"},
        {"disposition": "needs_input", "rationale": "b", "question": "q"},
        {"disposition": "dismiss", "rationale": "c"}])
    monkeypatch.setattr(gap_assessor, "assess_gap",
                        lambda gap, prob, sample, client=None: next(replies))
    events = list(gap_assessor.iter_assess_gaps(project, client=object()))
    summary = [e for e in events if e["type"] == "gap_assessment_summary"][-1]["data"]
    assert summary["total"] == 3
    saved = pj.get_gap_assessment(project)
    assert saved is not None
    assert sum(saved["by_disposition"].values()) == 3
    assert len(saved["gaps"]) == 3
    assert all("disposition" in g for g in saved["gaps"])


def test_run_without_coverage_errors(project, monkeypatch):
    from analyst_agent import store as pj
    monkeypatch.setattr(pj, "get_coverage", lambda pid: None)
    events = list(gap_assessor.iter_assess_gaps(project, client=object()))
    assert events[0]["type"] == "error"


# --- dismiss + readiness integration -------------------------------------

def test_dismiss_and_undismiss_round_trip(tmp_path, monkeypatch):
    from analyst_agent import store as pj
    monkeypatch.setattr(pj, "STORE", str(tmp_path))
    monkeypatch.setattr(pj, "PROJECTS_DIR", str(tmp_path / "projects"))
    p = pj.create_project("P")
    d = pj.dismiss_gap(p["id"], "security::Input Validation", "handled by framework",
                       by="human", title="Input Validation", severity="critical")
    assert d["reason"] == "handled by framework"
    assert "security::Input Validation" in pj.get_dismissed_gaps(p["id"])
    assert pj.undismiss_gap(p["id"], "security::Input Validation") is True
    assert pj.get_dismissed_gaps(p["id"]) == {}


def test_dismissed_gap_does_not_block_release(tmp_path, monkeypatch):
    from analyst_agent import store as pj, package
    monkeypatch.setattr(pj, "STORE", str(tmp_path))
    monkeypatch.setattr(pj, "PROJECTS_DIR", str(tmp_path / "projects"))
    p = pj.create_project("P")
    pid = p["id"]
    pj.save_quality_run(pid, "r1", {"requirements": [
        {"req_id": "REQ-0001", "text": "The system shall x.", "overall": 4.8,
         "judges_ok": 9, "judges_total": 9, "characteristics": {}, "deterministic_findings": []}]},
        {"run_id": "r1", "finished_at": "x"})
    rv = pj.get_review(pid, "r1")
    rv["threshold"] = {"mode": "avg_ge", "value": 4.0}
    rv["requirements"]["REQ-0001"]["classification"] = {"classes": ["functional"], "type": "functional", "constraints": []}
    pj.save_review(pid, "r1", rv)
    pj.save_problem_statement(pid, {"purpose": "x"}, ratified=True)
    pj.save_coverage_run(pid, "c1",
                         {"gaps": [{"severity": "critical", "title": "G1", "domain": "security"}]},
                         {"run_id": "c1", "finished_at": "x"})
    # blocks first
    assert any("coverage gap" in b for b in package.readiness(pid)["hard_blockers"])
    # dismiss it -> no longer blocks
    pj.dismiss_gap(pid, "security::G1", "out of scope", by="human")
    assert not any("coverage gap" in b for b in package.readiness(pid)["hard_blockers"])


def test_authoring_skips_dismissed_and_dismiss_disposition(tmp_path, monkeypatch):
    """Authoring must not draft a requirement for an out-of-scope gap."""
    from analyst_agent import store as pj, authoring
    monkeypatch.setattr(pj, "STORE", str(tmp_path))
    monkeypatch.setattr(pj, "PROJECTS_DIR", str(tmp_path / "projects"))
    p = pj.create_project("P"); pid = p["id"]
    pj.save_quality_run(pid, "r1", {"requirements": [{"req_id": "REQ-0001", "text": "t"}]},
                        {"run_id": "r1", "finished_at": "x"})
    pj.get_review(pid, "r1")
    pj.save_coverage_run(pid, "c1", {"gaps": [
        {"domain": "d", "title": "keep", "severity": "high"},
        {"domain": "d", "title": "dismissed", "severity": "high"},
        {"domain": "d", "title": "dispdismiss", "severity": "high"}]},
        {"run_id": "c1", "finished_at": "x"})
    pj.dismiss_gap(pid, "d::dismissed", "no", by="human")
    pj.save_gap_assessment(pid, {"gaps": [
        {"gap_key": "d::keep", "disposition": "author"},
        {"gap_key": "d::dispdismiss", "disposition": "dismiss"}]})
    # stub the LLM-heavy calls so only the SELECTION is exercised
    monkeypatch.setattr(authoring, "author_for_gap",
                        lambda gap, prob, sample, client=None: {"text": "The system shall keep.", "needs_input": False})
    monkeypatch.setattr(authoring, "assess_requirement",
                        lambda text, client=None, review=True: {"text": text, "overall": 4.8,
                                                                "characteristics": [], "deterministic": [], "review": None})
    monkeypatch.setattr(authoring, "is_duplicate", lambda c, e, **k: (False, None))
    events = list(authoring.iter_author_for_project(pid, "r1", client=object()))
    start = [e for e in events if e["type"] == "stage" and e.get("status") == "start"][0]
    assert start["total"] == 1                # only "keep" — dismissed + dispdismiss skipped
    assert "2 dismissed/out-of-scope" in start["message"]
