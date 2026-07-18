"""Phase C — gap authoring.

The point of these tests is the *flagging*, not the drafting. An authored
requirement is one no stakeholder asked for; if it can ever be mistaken for
source content, the Analyst has fabricated a requirement into the Architect's
input. So: origin is marked, no source provenance is invented, ratification
defaults to false, and the gap that motivated it is always traceable.
"""

import pytest

from analyst_agent import authoring


class FakeClient:
    """Stands in for agent_server. `reply` is returned from every call."""

    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def complete_json(self, agent, user_content):
        self.calls.append((agent, user_content))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


GAP = {"domain": "data", "domain_name": "Data & information",
       "title": "Data Retention, Archival, and Deletion Policies",
       "severity": "critical", "detail": "No retention periods are defined.",
       "question": "What retention periods must the system enforce?",
       "grounding": ["Data & information domain concerns: retention, archival, deletion"]}


# --- gap identity ---------------------------------------------------------

def test_gap_id_is_stable_for_same_domain_and_title():
    assert authoring.gap_id(GAP) == authoring.gap_id(dict(GAP))


def test_gap_id_differs_across_gaps():
    other = dict(GAP, title="Audit Logging")
    assert authoring.gap_id(GAP) != authoring.gap_id(other)


def test_gap_id_ignores_severity_and_detail_churn():
    """Severity/detail are LLM-regenerated per run; identity must not move with them."""
    churned = dict(GAP, severity="high", detail="totally different wording")
    assert authoring.gap_id(churned) == authoring.gap_id(GAP)


# --- drafting -------------------------------------------------------------

def test_author_returns_structured_draft():
    client = FakeClient({"text": "The system shall retain audit records.",
                         "rationale": "closes the retention gap",
                         "assumptions": ["retention period unspecified"],
                         "needs_input": False, "question": ""})
    out = authoring.author_for_gap(GAP, "a system", [], client=client)
    assert out["text"] == "The system shall retain audit records."
    assert out["assumptions"] == ["retention period unspecified"]
    assert client.calls[0][0] == "incose_gap_author"


def test_author_never_raises_on_llm_failure():
    from analyst_agent.llm.client import LLMError
    out = authoring.author_for_gap(GAP, "", [], client=FakeClient(LLMError("boom")))
    assert out["text"] == "" and "error" in out


def test_author_survives_a_list_response():
    """`complete_json` returns whatever json.loads yields, including a list."""
    out = authoring.author_for_gap(GAP, "", [], client=FakeClient([1, 2, 3]))
    assert out["text"] == "" and "error" in out


def test_needs_input_is_propagated():
    client = FakeClient({"text": "", "needs_input": True,
                         "question": "What is the mandated retention period?"})
    out = authoring.author_for_gap(GAP, "", [], client=client)
    assert out["needs_input"] is True
    assert "retention period" in out["question"]


def test_gap_detail_reaches_the_prompt():
    client = FakeClient({"text": "x"})
    authoring.author_for_gap(GAP, "the problem", ["An existing requirement."],
                             client=client)
    prompt = client.calls[0][1]
    assert "No retention periods are defined." in prompt
    assert "An existing requirement." in prompt
    assert "the problem" in prompt


# --- the flagging contract ------------------------------------------------

def _record():
    assessment = {"text": "The system shall retain audit records for a defined period.",
                  "characteristics": [{"id": f"C{i}", "score": 5} for i in range(1, 10)],
                  "deterministic": [], "overall": 4.8, "review": None}
    drafted = {"rationale": "closes the gap", "assumptions": ["period unspecified"]}
    return authoring._scorecard_record("GAP-0001", assessment, GAP, drafted, None)


def test_authored_requirement_is_flagged_as_generated():
    prov = _record()["provenance"]
    assert prov["origin"] == "analyst_authored"
    assert prov["generated_to_fill_coverage_gap"] is True


def test_authored_requirement_is_unratified_by_default():
    assert _record()["provenance"]["ratified"] is False


def test_authored_requirement_invents_no_source_provenance():
    """The killer failure: an authored requirement carrying a document/page would
    be indistinguishable from something a stakeholder actually wrote."""
    prov = _record()["provenance"]
    for forbidden in ("source_document", "source_document_id", "page", "bbox", "section_path"):
        assert forbidden not in prov


def test_authored_requirement_traces_back_to_its_gap():
    prov = _record()["provenance"]
    assert prov["gap_id"] == authoring.gap_id(GAP)
    assert prov["gap_title"] == GAP["title"]
    assert prov["domain"] == "data"
    assert prov["grounding"] == GAP["grounding"]
    assert prov["assumptions"] == ["period unspecified"]


def test_authored_requirement_carries_judge_health():
    rec = _record()
    assert (rec["judges_ok"], rec["judges_total"]) == (9, 9)


def test_partial_judging_is_recorded_on_authored_requirements_too():
    assessment = {"text": "x", "overall": 5.0, "deterministic": [], "review": None,
                  "characteristics": [{"id": f"C{i}", "score": 5} for i in range(1, 7)]}
    rec = authoring._scorecard_record("GAP-0002", assessment, GAP, {}, None)
    assert rec["judges_ok"] == 6


# --- duplicate suppression ------------------------------------------------

def test_duplicate_detected(monkeypatch):
    monkeypatch.setattr(authoring, "rerank", lambda q, docs: [0.9, 0.1])
    dup, idx = authoring.is_duplicate("The system shall retain records.",
                                      ["The system shall retain records.", "Unrelated."])
    assert dup is True and idx == 0


def test_not_duplicate_below_threshold(monkeypatch):
    monkeypatch.setattr(authoring, "rerank", lambda q, docs: [0.2, 0.1])
    dup, idx = authoring.is_duplicate("A novel obligation.", ["Unrelated.", "Also unrelated."])
    assert dup is False and idx is None


def test_duplicate_check_fails_open_when_reranker_is_down(monkeypatch):
    """Authoring a near-duplicate is recoverable; silently dropping a real gap
    filler is not. So a reranker outage must not suppress authoring."""
    def boom(q, docs):
        raise RuntimeError("reranker down")
    monkeypatch.setattr(authoring, "rerank", boom)
    assert authoring.is_duplicate("anything", ["something"]) == (False, None)


def test_empty_set_is_never_a_duplicate():
    assert authoring.is_duplicate("The system shall do a thing.", []) == (False, None)


# --- id sequencing --------------------------------------------------------

def test_gap_sequence_continues_across_rounds():
    sc = {"requirements": [{"req_id": "REQ-0001"}, {"req_id": "GAP-0003"},
                           {"req_id": "GAP-0007"}]}
    assert authoring._next_gap_seq(sc) == 7


def test_gap_sequence_starts_at_zero_for_a_fresh_set():
    assert authoring._next_gap_seq({"requirements": [{"req_id": "REQ-0001"}]}) == 0


def test_gap_sequence_ignores_malformed_ids():
    sc = {"requirements": [{"req_id": "GAP-abc"}, {"req_id": "GAP-0002"}]}
    assert authoring._next_gap_seq(sc) == 2


# --- needs_input coexisting with a usable draft ---------------------------
# Observed live on the first real gap: the author wrote a valid obligation
# ("shall enforce data retention periods") while correctly flagging that the
# periods themselves are unknown. Discarding that draft loses a real requirement
# and leaves the gap open, so the text is kept and the question rides along.

def test_needs_input_with_text_keeps_the_question_on_the_requirement():
    drafted = {"rationale": "r", "assumptions": [], "needs_input": True,
               "question": "What are the defined retention periods?"}
    assessment = {"text": "The system shall enforce data retention periods.",
                  "characteristics": [{"id": f"C{i}", "score": 4} for i in range(1, 10)],
                  "deterministic": [], "overall": 4.0, "review": None}
    rec = authoring._scorecard_record("GAP-0001", assessment, GAP, drafted, None)
    assert rec["provenance"]["open_question"] == "What are the defined retention periods?"
    assert rec["text"] == "The system shall enforce data retention periods."


def test_no_open_question_when_author_was_not_blocked():
    drafted = {"rationale": "r", "assumptions": [], "needs_input": False, "question": ""}
    assessment = {"text": "x", "characteristics": [], "deterministic": [],
                  "overall": 4.8, "review": None}
    rec = authoring._scorecard_record("GAP-0001", assessment, GAP, drafted, None)
    assert rec["provenance"]["open_question"] == ""


# --- incremental persistence ---------------------------------------------
# A full 78-gap pass takes ~12 minutes and each requirement costs a draft + 9
# judges + possible refinement. Persisting only at the end means a crash or a
# cancel at gap 77 discards 76 paid-for requirements.

def test_flush_batches_are_bounded():
    assert 0 < authoring.FLUSH_EVERY <= 25


def test_persist_appends_without_duplicating(tmp_path, monkeypatch):
    """_persist is called repeatedly with only the unflushed batch; calling it
    twice must append 2 batches, not re-append the first."""
    from analyst_agent import store as pj
    monkeypatch.setattr(pj, "STORE", str(tmp_path))
    monkeypatch.setattr(pj, "PROJECTS_DIR", str(tmp_path / "projects"))
    p = pj.create_project("P")
    pid = p["id"]
    pj.save_quality_run(pid, "r1", {"requirements": [{"req_id": "REQ-0001", "text": "t"}]},
                        {"run_id": "r1", "finished_at": "2026-01-01T00:00:00+00:00"})
    pj.get_review(pid, "r1")                     # seed

    sc = pj.get_quality_scorecard(pid, "r1")
    batch1 = [{"req_id": "GAP-0001", "text": "a", "overall": 4.5}]
    authoring._persist(pid, "r1", sc, batch1)
    batch2 = [{"req_id": "GAP-0002", "text": "b", "overall": 4.6}]
    authoring._persist(pid, "r1", sc, batch2)

    ids = [r["req_id"] for r in pj.get_quality_scorecard(pid, "r1")["requirements"]]
    assert ids == ["REQ-0001", "GAP-0001", "GAP-0002"]


def test_persist_seeds_review_entries_for_authored(tmp_path, monkeypatch):
    from analyst_agent import store as pj
    monkeypatch.setattr(pj, "STORE", str(tmp_path))
    monkeypatch.setattr(pj, "PROJECTS_DIR", str(tmp_path / "projects"))
    p = pj.create_project("P")
    pid = p["id"]
    pj.save_quality_run(pid, "r1", {"requirements": []},
                        {"run_id": "r1", "finished_at": "2026-01-01T00:00:00+00:00"})
    pj.get_review(pid, "r1")
    sc = pj.get_quality_scorecard(pid, "r1")
    authoring._persist(pid, "r1", sc, [{"req_id": "GAP-0001", "text": "a", "overall": 4.5}])
    entry = pj.get_review(pid, "r1")["requirements"]["GAP-0001"]
    assert entry["original_text"] == "a" and entry["overall_before"] == 4.5
    assert entry["status"] == "unreviewed"
