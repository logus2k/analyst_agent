"""Phase D — the gap ledger and closure checking.

The loop's whole progress signal runs through here. Two failure directions, very
unequal in cost: a gap wrongly declared CLOSED is deleted from the analysis and a
real hole ships to the Architect; a gap wrongly left OPEN costs one more round.
So every ambiguous path in these tests must fall toward "open".
"""

import pytest

from analyst_agent import gaps


class FakeClient:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def complete_json(self, agent, user_content):
        self.calls.append((agent, user_content))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


GAP = {"domain": "data", "title": "Data Retention", "severity": "critical",
       "detail": "No retention periods are defined.",
       "question": "What retention periods must the system enforce?"}
GAP2 = {"domain": "security", "title": "Audit Logging", "severity": "high",
        "detail": "No audit trail.", "question": "What must be logged?"}

REQS = [{"req_id": "REQ-0001", "text": "The system shall retain records for 7 years."},
        {"req_id": "REQ-0002", "text": "The system shall render a dashboard."}]


@pytest.fixture(autouse=True)
def tmp_store(tmp_path, monkeypatch):
    from analyst_agent import store as pj
    monkeypatch.setattr(pj, "STORE", str(tmp_path))
    monkeypatch.setattr(pj, "PROJECTS_DIR", str(tmp_path / "projects"))
    # deterministic candidate selection: identity order
    monkeypatch.setattr(gaps, "rerank", lambda q, docs: [1.0 - i * 0.1 for i in range(len(docs))])
    return tmp_path


# --- ledger ---------------------------------------------------------------

def test_ledger_mints_one_entry_per_gap():
    led = gaps.mint_ledger({"gaps": [GAP, GAP2]})
    assert len(led["gaps"]) == 2
    assert all(e["status"] == gaps.OPEN for e in led["gaps"].values())


def test_ledger_carries_the_gap_object_verbatim():
    """Identity is the carried object; re-deriving is what we are avoiding."""
    led = gaps.mint_ledger({"gaps": [GAP]})
    assert list(led["gaps"].values())[0]["gap"] == GAP


def test_ledger_deduplicates_identical_gaps_in_one_run():
    led = gaps.mint_ledger({"gaps": [GAP, dict(GAP)]})
    assert len(led["gaps"]) == 1


def test_merge_adds_only_new_gaps():
    led = gaps.mint_ledger({"gaps": [GAP]})
    led, added = gaps.merge_new_gaps(led, {"gaps": [GAP, GAP2]})
    assert added == [gaps.gap_id(GAP2)]
    assert len(led["gaps"]) == 2


def test_merge_does_not_reset_work_already_done():
    """A fresh panel re-describing a carried gap must not wipe its history."""
    led = gaps.mint_ledger({"gaps": [GAP]})
    gid = gaps.gap_id(GAP)
    led["gaps"][gid].update(status=gaps.CLOSED, checks=3, covered_by=["REQ-0001"])
    led, added = gaps.merge_new_gaps(led, {"gaps": [dict(GAP, detail="reworded entirely")]})
    assert added == []
    assert led["gaps"][gid]["status"] == gaps.CLOSED
    assert led["gaps"][gid]["checks"] == 3


def test_partial_counts_as_open():
    led = gaps.mint_ledger({"gaps": [GAP, GAP2]})
    led["gaps"][gaps.gap_id(GAP)]["status"] = gaps.PARTIAL
    led["gaps"][gaps.gap_id(GAP2)]["status"] = gaps.CLOSED
    assert [e["gap_id"] for e in gaps.open_gaps(led)] == [gaps.gap_id(GAP)]


def test_ledger_round_trips_through_the_store():
    from analyst_agent import store as pj
    p = pj.create_project("P")
    pj.save_coverage_run(p["id"], "c1", {"gaps": [GAP]},
                         {"run_id": "c1", "finished_at": "2026-01-01T00:00:00+00:00"})
    led = gaps.ensure_ledger(p["id"])
    assert len(led["gaps"]) == 1
    assert pj.get_gap_ledger(p["id"])["gaps"] == led["gaps"]


def test_ensure_ledger_does_not_remint_over_existing_state():
    from analyst_agent import store as pj
    p = pj.create_project("P")
    pj.save_coverage_run(p["id"], "c1", {"gaps": [GAP]},
                         {"run_id": "c1", "finished_at": "x"})
    led = gaps.ensure_ledger(p["id"])
    led["gaps"][gaps.gap_id(GAP)]["status"] = gaps.CLOSED
    pj.save_gap_ledger(p["id"], led)
    assert gaps.ensure_ledger(p["id"])["gaps"][gaps.gap_id(GAP)]["status"] == gaps.CLOSED


def test_ensure_ledger_without_coverage_is_none():
    from analyst_agent import store as pj
    p = pj.create_project("P")
    assert gaps.ensure_ledger(p["id"]) is None


# --- closure verdicts -----------------------------------------------------

def test_closed_verdict_accepted_with_citation():
    c = FakeClient({"status": "closed", "covered_by": ["REQ-0001"],
                    "reasoning": "retention stated", "still_missing": ""})
    out = gaps.check_closure(GAP, REQS, client=c)
    assert out["status"] == gaps.CLOSED and out["covered_by"] == ["REQ-0001"]
    assert c.calls[0][0] == "incose_gap_closure_judge"


def test_closed_without_citation_is_downgraded_to_partial():
    """An uncited 'closed' is an assertion, not evidence."""
    out = gaps.check_closure(GAP, REQS, client=FakeClient(
        {"status": "closed", "covered_by": []}))
    assert out["status"] == gaps.PARTIAL


def test_citations_outside_the_candidate_set_are_dropped():
    """A hallucinated req_id must not be able to justify closing a gap."""
    out = gaps.check_closure(GAP, REQS, client=FakeClient(
        {"status": "closed", "covered_by": ["REQ-9999"]}))
    assert out["covered_by"] == []
    assert out["status"] == gaps.PARTIAL          # nothing valid left to cite


def test_unknown_status_falls_open():
    out = gaps.check_closure(GAP, REQS, client=FakeClient(
        {"status": "probably fine", "covered_by": ["REQ-0001"]}))
    assert out["status"] == gaps.OPEN


def test_llm_failure_leaves_the_gap_open():
    from analyst_agent.llm.client import LLMError
    out = gaps.check_closure(GAP, REQS, client=FakeClient(LLMError("boom")))
    assert out["status"] == gaps.OPEN and "error" in out


def test_list_response_leaves_the_gap_open():
    out = gaps.check_closure(GAP, REQS, client=FakeClient([1, 2]))
    assert out["status"] == gaps.OPEN and "error" in out


def test_no_requirements_means_open():
    c = FakeClient({"status": "closed", "covered_by": ["REQ-0001"]})
    out = gaps.check_closure(GAP, [], client=c)
    assert out["status"] == gaps.OPEN
    assert c.calls == []                          # no point asking


def test_partial_is_preserved_not_rounded_to_closed():
    out = gaps.check_closure(GAP, REQS, client=FakeClient(
        {"status": "partial", "covered_by": ["REQ-0001"],
         "still_missing": "the actual retention period"}))
    assert out["status"] == gaps.PARTIAL
    assert "retention period" in out["still_missing"]


# --- candidate selection --------------------------------------------------

def test_candidates_are_bounded_by_top_k(monkeypatch):
    many = [{"req_id": f"REQ-{i:04d}", "text": f"req {i}"} for i in range(50)]
    monkeypatch.setattr(gaps, "rerank", lambda q, docs: [1.0] * len(docs))
    assert len(gaps._candidates(GAP, many)) == gaps.TOP_K_CANDIDATES


def test_candidates_ordered_by_rerank_score(monkeypatch):
    reqs = [{"req_id": "A", "text": "a"}, {"req_id": "B", "text": "b"},
            {"req_id": "C", "text": "c"}]
    monkeypatch.setattr(gaps, "rerank", lambda q, docs: [0.1, 0.9, 0.5])
    assert [r["req_id"] for r in gaps._candidates(GAP, reqs, top_k=2)] == ["B", "C"]


def test_candidates_fall_back_when_reranker_is_down(monkeypatch):
    def boom(q, docs):
        raise RuntimeError("down")
    monkeypatch.setattr(gaps, "rerank", boom)
    got = gaps._candidates(GAP, REQS)
    assert len(got) == 2          # judged against something rather than spinning


def test_candidate_prompt_carries_ids_and_the_question():
    c = FakeClient({"status": "open"})
    gaps.check_closure(GAP, REQS, client=c)
    prompt = c.calls[0][1]
    assert "[REQ-0001]" in prompt
    assert "What retention periods must the system enforce?" in prompt
