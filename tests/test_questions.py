"""Phase D — open questions.

When the loop stalls, the absolute quality floor means the only way forward is a
human supplying missing information. So the stall must be *answerable*: specific
questions, tied to requirements, merged so one answer unblocks many.

All of this is aggregation over data already stored — no LLM call, so these tests
are exact rather than tolerant.
"""

import pytest

from analyst_agent import questions


@pytest.fixture
def project(tmp_path, monkeypatch):
    from analyst_agent import store as pj
    monkeypatch.setattr(pj, "STORE", str(tmp_path))
    monkeypatch.setattr(pj, "PROJECTS_DIR", str(tmp_path / "projects"))
    proj = pj.create_project("P")
    pid = proj["id"]

    def build(reqs, entries=None, threshold=4.3):
        pj.save_quality_run(pid, "r1", {"requirements": reqs},
                            {"run_id": "r1", "finished_at": "2026-01-01T00:00:00+00:00"})
        review = pj.get_review(pid, "r1")
        review["threshold"] = {"mode": "avg_ge", "value": threshold}
        for rid, patch in (entries or {}).items():
            review["requirements"].setdefault(rid, {}).update(patch)
        pj.save_review(pid, "r1", review)
        return questions.collect_questions(pid, "r1")

    return build


def _req(rid, text, overall=3.0, review=None, provenance=None):
    return {"req_id": rid, "text": text, "overall": overall,
            "characteristics": {}, "deterministic_findings": [],
            "review": review, "provenance": provenance or {}, "lineage": {}}


ADVISORY = {"rewrites": [], "advisories": [
    {"characteristic": "C4", "issue": "The timing context is not quantified.",
     "suggestion": "Specify the maximum latency for the monitoring activities."}]}


# --- placeholders ---------------------------------------------------------

def test_placeholder_becomes_a_concrete_question(project):
    qs = project([_req("REQ-0001", "The system shall respond within [LATENCY_VALUE].")])
    ph = [q for q in qs if "placeholder" in q["sources"]]
    assert ph and "[LATENCY_VALUE]" in ph[0]["question"]
    assert ph[0]["req_ids"] == ["REQ-0001"]


def test_placeholder_question_is_always_blocking(project):
    """Even at a passing score: the value is missing, so it cannot release."""
    qs = project([_req("REQ-0001", "shall respond within [X]", overall=4.9)])
    assert [q for q in qs if "placeholder" in q["sources"]][0]["blocking"] is True


def test_multiple_placeholders_yield_multiple_questions(project):
    qs = project([_req("REQ-0001", "between [MIN] and [MAX] seconds")])
    asked = {q["question"] for q in qs}
    assert any("[MIN]" in a for a in asked) and any("[MAX]" in a for a in asked)


# --- reviewer advisories --------------------------------------------------

def test_advisory_suggestion_becomes_the_question(project):
    qs = project([_req("REQ-0001", "clean text", overall=3.0, review=ADVISORY)])
    adv = [q for q in qs if "advisory" in q["sources"]]
    assert adv[0]["question"] == "Specify the maximum latency for the monitoring activities."
    assert adv[0]["why"] == "The timing context is not quantified."
    assert adv[0]["characteristic"] == "C4"


def test_advisory_ignored_when_the_requirement_already_passes(project):
    """A passing requirement is not blocked, so its advisories are not questions."""
    qs = project([_req("REQ-0001", "clean text", overall=4.8, review=ADVISORY)])
    assert [q for q in qs if "advisory" in q["sources"]] == []


def test_advisory_ignored_once_a_human_has_resolved_it(project):
    qs = project([_req("REQ-0001", "clean text", overall=3.0, review=ADVISORY)],
                 {"REQ-0001": {"status": "accepted"}})
    assert [q for q in qs if "advisory" in q["sources"]] == []


def test_needs_human_status_still_asks(project):
    qs = project([_req("REQ-0001", "clean text", overall=3.0, review=ADVISORY)],
                 {"REQ-0001": {"status": "needs_human"}})
    assert [q for q in qs if "advisory" in q["sources"]]


# --- authored-gap questions ----------------------------------------------

def test_authored_open_question_is_surfaced(project):
    prov = {"origin": "analyst_authored", "open_question": "What retention period applies?",
            "gap_title": "Data Retention"}
    qs = project([_req("GAP-0001", "shall enforce retention", overall=4.5, provenance=prov)])
    assert any(q["question"] == "What retention period applies?" for q in qs)


# --- merging --------------------------------------------------------------

def test_identical_questions_merge_across_requirements(project):
    """A stalled run must not present the same ask twenty times."""
    qs = project([_req("REQ-0001", "a", overall=3.0, review=ADVISORY),
                  _req("REQ-0002", "b", overall=3.0, review=ADVISORY),
                  _req("REQ-0003", "c", overall=3.0, review=ADVISORY)])
    adv = [q for q in qs if "advisory" in q["sources"]]
    assert len(adv) == 1
    assert adv[0]["req_ids"] == ["REQ-0001", "REQ-0002", "REQ-0003"]
    assert adv[0]["affects"] == 3


# The similarity merge is exercised with a stubbed cosine so the LOGIC is tested
# offline and deterministically. An earlier version of this file asserted that
# "Specify the maximum latency." merged with "specify the maximum latency" — the
# only pair the old regex could ever join. It passed, and proved nothing.

def _stub_similarity(monkeypatch, matrix):
    """Drive _merge_similar with an explicit question-text similarity matrix."""
    monkeypatch.setattr(questions, "embed", lambda texts: [[float(i)] for i in range(len(texts))])

    def cos(a, b):
        return matrix.get((int(a[0]), int(b[0])), 0.0)
    monkeypatch.setattr(questions, "_cosine", cos)


def _q(text, req_ids, sources=("advisory",), blocking=True):
    return {"question": text, "why": "", "req_ids": list(req_ids),
            "sources": list(sources), "characteristic": None, "blocking": blocking}


def test_similar_questions_merge(monkeypatch):
    items = [_q("Specify the maximum latency.", ["REQ-0001"]),
             _q("Specify the required response time.", ["REQ-0002"])]
    _stub_similarity(monkeypatch, {(0, 1): 0.93, (1, 0): 0.93})
    out = questions._merge_similar(items)
    assert len(out) == 1
    assert sorted(out[0]["req_ids"]) == ["REQ-0001", "REQ-0002"]
    assert out[0]["variants"] == ["Specify the required response time."]


def test_dissimilar_questions_stay_separate(monkeypatch):
    items = [_q("Specify the maximum latency.", ["REQ-0001"]),
             _q("Add a rationale for this requirement.", ["REQ-0002"])]
    _stub_similarity(monkeypatch, {(0, 1): 0.42, (1, 0): 0.42})
    assert len(questions._merge_similar(items)) == 2


def test_no_transitive_chaining(monkeypatch):
    """A~B and B~C must NOT drag A and C together. Measured consequence of getting
    this wrong: the reranker version chained 167 questions into one 147-member blob."""
    items = [_q("A", ["REQ-0001"]), _q("B", ["REQ-0002"]), _q("C", ["REQ-0003"])]
    _stub_similarity(monkeypatch, {(0, 1): 0.90, (1, 0): 0.90,
                                   (1, 2): 0.90, (2, 1): 0.90,
                                   (0, 2): 0.40, (2, 0): 0.40})
    out = questions._merge_similar(items)
    assert len(out) == 2                       # {A,B} and {C}, never one group of 3
    assert max(len(q["req_ids"]) for q in out) == 2


def test_placeholder_questions_are_never_merged(monkeypatch):
    """'[1] in REQ-0052' and '[2] in REQ-0051' score 0.922 — same sentence template,
    different answers. Merging them would ask one question for two values."""
    items = [_q("What value should replace [1] in REQ-0052?", ["REQ-0052"], ["placeholder"]),
             _q("What value should replace [2] in REQ-0051?", ["REQ-0051"], ["placeholder"])]
    _stub_similarity(monkeypatch, {(0, 1): 0.99, (1, 0): 0.99})
    assert len(questions._merge_similar(items)) == 2


def test_embeddings_unavailable_falls_back_to_no_merging(monkeypatch):
    """Showing duplicates is recoverable; silently collapsing distinct asks is not."""
    def boom(texts):
        raise RuntimeError("embeddings down")
    monkeypatch.setattr(questions, "embed", boom)
    items = [_q("A", ["REQ-0001"]), _q("B", ["REQ-0002"])]
    assert len(questions._merge_similar(items)) == 2


def test_representative_is_the_question_covering_most_requirements(monkeypatch):
    items = [_q("narrow ask", ["REQ-0001"]),
             _q("broad ask", ["REQ-0002", "REQ-0003"])]
    _stub_similarity(monkeypatch, {(0, 1): 0.95, (1, 0): 0.95})
    out = questions._merge_similar(items)
    assert out[0]["question"] == "broad ask"
    assert len(out[0]["req_ids"]) == 3


def test_blocking_wins_when_merged(project):
    """One blocked requirement makes a shared question blocking."""
    prov_ok = {"origin": "analyst_authored", "open_question": "What is the limit?"}
    qs = project([_req("GAP-0001", "fine", overall=4.9, provenance=prov_ok),
                  _req("GAP-0002", "bad", overall=2.0, provenance=prov_ok)])
    q = next(q for q in qs if q["question"] == "What is the limit?")
    assert q["blocking"] is True
    assert set(q["req_ids"]) == {"GAP-0001", "GAP-0002"}


# --- ordering and ids -----------------------------------------------------

def test_blocking_questions_come_first(project):
    prov = {"origin": "analyst_authored", "open_question": "Non blocking ask?"}
    qs = project([_req("GAP-0001", "clean", overall=4.9, provenance=prov),
                  _req("REQ-0002", "within [X] ms", overall=4.9)])
    assert qs[0]["blocking"] is True


def test_ids_are_assigned_in_presentation_order(project):
    qs = project([_req("REQ-0001", "within [X] ms"),
                  _req("REQ-0002", "within [Y] ms")])
    assert [q["id"] for q in qs] == ["Q-0001", "Q-0002"]


def test_no_questions_for_a_clean_set(project):
    assert project([_req("REQ-0001", "The system shall log events.", overall=4.8)]) == []


def test_duplicates_are_skipped(project):
    reqs = [_req("REQ-0001", "within [X] ms")]
    reqs[0]["lineage"] = {"duplicate_of": "REQ-0009"}
    assert project(reqs) == []


# --- summary --------------------------------------------------------------

def test_summary_counts(project):
    qs = project([_req("REQ-0001", "within [X] ms", overall=3.0, review=ADVISORY),
                  _req("REQ-0002", "clean", overall=3.0, review=ADVISORY)])
    s = questions.summarize(qs)
    assert s["total"] == len(qs)
    assert s["requirements_affected"] == 2
    assert s["by_source"]["placeholder"] == 1
    assert s["by_source"]["advisory"] == 1


def test_missing_run_yields_no_questions(tmp_path, monkeypatch):
    from analyst_agent import store as pj
    monkeypatch.setattr(pj, "STORE", str(tmp_path))
    monkeypatch.setattr(pj, "PROJECTS_DIR", str(tmp_path / "projects"))
    p = pj.create_project("P")
    assert questions.collect_questions(p["id"], "nope") == []
