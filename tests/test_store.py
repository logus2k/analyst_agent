"""Phase B — the filesystem store.

The store is authoritative: reqoach holds no analysis data, so a lost or
half-written file here is data loss with no second copy. These pin the round
trips and the write semantics that protect against it.
"""

import json
import os

import pytest

from analyst_agent import store as pj


@pytest.fixture(autouse=True)
def tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(pj, "STORE", str(tmp_path))
    monkeypatch.setattr(pj, "PROJECTS_DIR", str(tmp_path / "projects"))
    return tmp_path


# --- projects -------------------------------------------------------------

def test_create_and_read_project():
    p = pj.create_project("My project")
    assert pj.get_project(p["id"])["name"] == "My project"


def test_blank_name_gets_a_placeholder():
    assert pj.create_project("   ")["name"] == "Untitled project"


def test_unknown_project_is_none_not_an_error():
    assert pj.get_project("nope") is None


def test_projects_listed_newest_first():
    a = pj.create_project("A")
    b = pj.create_project("B")
    b["created_at"] = "2099-01-01T00:00:00+00:00"
    pj._write_json(os.path.join(pj._project_dir(b["id"]), "meta.json"), b)
    assert [p["id"] for p in pj.list_projects()][0] == b["id"]
    assert {p["id"] for p in pj.list_projects()} == {a["id"], b["id"]}


def test_delete_project_removes_everything():
    p = pj.create_project("doomed")
    pj.add_document(p["id"], "a.md", ".md", b"# hi")
    assert pj.delete_project(p["id"]) is True
    assert pj.get_project(p["id"]) is None


def test_delete_unknown_project_reports_false():
    assert pj.delete_project("nope") is False


# --- documents ------------------------------------------------------------

def test_document_round_trip():
    p = pj.create_project("P")
    d = pj.add_document(p["id"], "srs.md", ".md", b"# spec")
    assert d["size"] == 6
    assert pj.list_documents(p["id"])[0]["id"] == d["id"]
    with open(pj.document_path(p["id"], d["id"]), "rb") as f:
        assert f.read() == b"# spec"


def test_document_on_unknown_project_is_none():
    assert pj.add_document("nope", "a.md", ".md", b"x") is None


def test_document_path_unknown_is_none():
    p = pj.create_project("P")
    assert pj.document_path(p["id"], "nope") is None


# --- quality runs ---------------------------------------------------------

def _run(pid, run_id, finished, reqs=1):
    pj.save_quality_run(pid, run_id,
                        {"requirements": [{"req_id": f"REQ-{i:04d}"} for i in range(reqs)]},
                        {"run_id": run_id, "finished_at": finished})


def test_latest_quality_run_wins_when_unspecified():
    p = pj.create_project("P")
    _run(p["id"], "old", "2026-01-01T00:00:00+00:00", reqs=1)
    _run(p["id"], "new", "2026-06-01T00:00:00+00:00", reqs=3)
    assert len(pj.get_quality_scorecard(p["id"])["requirements"]) == 3


def test_explicit_run_id_is_honoured():
    p = pj.create_project("P")
    _run(p["id"], "old", "2026-01-01T00:00:00+00:00", reqs=1)
    _run(p["id"], "new", "2026-06-01T00:00:00+00:00", reqs=3)
    assert len(pj.get_quality_scorecard(p["id"], "old")["requirements"]) == 1


def test_resaving_a_run_replaces_its_meta_not_appends():
    p = pj.create_project("P")
    _run(p["id"], "r1", "2026-01-01T00:00:00+00:00")
    _run(p["id"], "r1", "2026-02-01T00:00:00+00:00")
    assert len(pj.list_quality_runs(p["id"])) == 1


def test_no_runs_yields_none():
    p = pj.create_project("P")
    assert pj.get_quality_scorecard(p["id"]) is None


# --- review sessions ------------------------------------------------------

def test_review_is_seeded_from_the_scorecard():
    p = pj.create_project("P")
    pj.save_quality_run(p["id"], "r1",
                        {"requirements": [{"req_id": "REQ-0001", "text": "t", "overall": 3.2}]},
                        {"run_id": "r1", "finished_at": "2026-01-01T00:00:00+00:00"})
    rv = pj.get_review(p["id"], "r1")
    e = rv["requirements"]["REQ-0001"]
    assert e["status"] == "unreviewed"
    assert e["original_text"] == "t" and e["final_text"] == "t"
    assert e["overall_before"] == 3.2 and e["overall_after"] is None
    assert rv["threshold"]["value"] == 4.3


def test_review_not_seeded_when_seed_is_false():
    p = pj.create_project("P")
    assert pj.get_review(p["id"], "r1", seed=False) is None


def test_upsert_updates_only_known_fields():
    p = pj.create_project("P")
    pj.save_quality_run(p["id"], "r1", {"requirements": [{"req_id": "REQ-0001", "text": "t"}]},
                        {"run_id": "r1", "finished_at": "x"})
    e = pj.upsert_req_review(p["id"], "r1", "REQ-0001",
                             {"status": "accepted", "bogus_field": "ignored"})
    assert e["status"] == "accepted"
    assert "bogus_field" not in e


def test_upsert_unknown_requirement_is_none():
    p = pj.create_project("P")
    pj.save_quality_run(p["id"], "r1", {"requirements": [{"req_id": "REQ-0001"}]},
                        {"run_id": "r1", "finished_at": "x"})
    assert pj.upsert_req_review(p["id"], "r1", "REQ-9999", {"status": "accepted"}) is None


def test_classification_does_not_forge_a_reviewed_at():
    """`reviewed_at` means a human or the refinement loop touched this. Machine
    metadata about unchanged text must not fake human review."""
    p = pj.create_project("P")
    pj.save_quality_run(p["id"], "r1", {"requirements": [{"req_id": "REQ-0001"}]},
                        {"run_id": "r1", "finished_at": "x"})
    e = pj.upsert_req_review(p["id"], "r1", "REQ-0001",
                             {"classification": {"classes": ["functional"]}})
    assert e["reviewed_at"] is None


def test_refinement_does_set_reviewed_at():
    p = pj.create_project("P")
    pj.save_quality_run(p["id"], "r1", {"requirements": [{"req_id": "REQ-0001"}]},
                        {"run_id": "r1", "finished_at": "x"})
    e = pj.upsert_req_review(p["id"], "r1", "REQ-0001", {"final_text": "improved"})
    assert e["reviewed_at"] is not None


def test_threshold_round_trip():
    p = pj.create_project("P")
    pj.save_quality_run(p["id"], "r1", {"requirements": []}, {"run_id": "r1", "finished_at": "x"})
    t = pj.set_threshold(p["id"], "r1", {"mode": "avg_ge", "value": "4.8"})
    assert t["value"] == 4.8
    assert pj.get_review(p["id"], "r1")["threshold"]["value"] == 4.8


# --- versioned documents --------------------------------------------------

def test_problem_statement_versions_increment():
    p = pj.create_project("P")
    assert pj.save_problem_statement(p["id"], {"purpose": "a"})["version"] == 1
    assert pj.save_problem_statement(p["id"], {"purpose": "b"})["version"] == 2
    assert pj.get_problem_statement(p["id"])["statement"]["purpose"] == "b"


def test_problem_statement_ratification_is_explicit():
    p = pj.create_project("P")
    assert pj.save_problem_statement(p["id"], {"purpose": "a"})["ratified"] is False
    assert pj.save_problem_statement(p["id"], {"purpose": "a"}, ratified=True)["ratified"] is True


def test_coverage_profile_versions_increment():
    p = pj.create_project("P")
    assert pj.save_coverage_profile(p["id"], {"archetypes": []})["version"] == 1
    assert pj.save_coverage_profile(p["id"], {"archetypes": ["web-saas"]})["version"] == 2


def test_coverage_run_round_trip():
    p = pj.create_project("P")
    pj.save_coverage_run(p["id"], "c1", {"gaps": [{"title": "g"}]},
                         {"run_id": "c1", "finished_at": "2026-01-01T00:00:00+00:00"})
    assert len(pj.get_coverage(p["id"])["gaps"]) == 1


# --- write semantics ------------------------------------------------------

def test_writes_are_atomic_leaving_no_tmp_file(tmp_store):
    p = pj.create_project("P")
    pj.save_problem_statement(p["id"], {"purpose": "x"})
    leftovers = [f for _, _, fs in os.walk(tmp_store) for f in fs if f.endswith(".tmp")]
    assert leftovers == []


def test_corrupt_json_reads_as_none_not_an_exception():
    """A truncated file must degrade to 'absent', not crash the API."""
    p = pj.create_project("P")
    with open(os.path.join(pj._project_dir(p["id"]), "meta.json"), "w") as f:
        f.write("{not json")
    assert pj.get_project(p["id"]) is None
