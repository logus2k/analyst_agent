"""Phase A/E — the release gate reports blockers, and never grants readiness.

Decision 1 (remaining_work.md): the quality floor is ABSOLUTE. Being below
threshold blocks release regardless of review status; there is no human-override
path. These tests pin that, since the old behaviour exempted anything a human had
marked `accepted`.
"""

import json
import os

import pytest

from analyst_agent import package


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A store on tmp_path with one quality run, wired through `package`'s own
    store module so nothing touches the real store."""
    from analyst_agent import store as pj
    monkeypatch.setattr(pj, "STORE", str(tmp_path))
    monkeypatch.setattr(pj, "PROJECTS_DIR", str(tmp_path / "projects"))

    proj = pj.create_project("Test project")
    pid = proj["id"]

    def build(reqs, review_entries=None, release_status=None, threshold=4.3):
        run_id = "run1"
        scorecard = {"requirements": reqs, "set_level": {}, "aggregates": {},
                     "characteristic_names": {}}
        pj.save_quality_run(pid, run_id, scorecard,
                            {"run_id": run_id, "finished_at": "2026-07-18T00:00:00+00:00"})
        review = pj.get_review(pid, run_id)
        review["threshold"] = {"mode": "avg_ge", "value": threshold}
        if release_status:
            review["release_status"] = release_status
        for rid, patch in (review_entries or {}).items():
            review["requirements"].setdefault(rid, {}).update(patch)
        pj.save_review(pid, run_id, review)
        return package.build_package(pid, run_id)

    return build


def _req(rid, overall, judges_ok=9, classes=("functional",), provenance=None):
    return {"req_id": rid, "text": f"The system shall {rid}.", "overall": overall,
            "characteristics": {}, "deterministic_findings": [],
            "judges_ok": judges_ok, "judges_total": 9,
            "provenance": provenance or {}, "lineage": {}}


def _classified(rid, classes=("functional",)):
    return {rid: {"classification": {"classes": list(classes), "type": "functional",
                                     "constraints": []}}}


def test_below_threshold_blocks(project):
    pkg = project([_req("REQ-0001", 3.0)], _classified("REQ-0001"))
    assert pkg["manifest"]["architect_ready"] is False
    assert any("below threshold" in b for b in pkg["manifest"]["blockers"])


def test_human_acceptance_does_not_unblock_below_threshold(project):
    """Decision 1: the floor is absolute. Previously `status: accepted` exempted
    a requirement from the below-threshold blocker — that escape hatch is gone."""
    entries = _classified("REQ-0001")
    entries["REQ-0001"]["status"] = "accepted"
    pkg = project([_req("REQ-0001", 3.0)], entries)
    assert any("below threshold" in b for b in pkg["manifest"]["blockers"])
    assert pkg["manifest"]["counts"]["below_threshold"] == 1


def test_incomplete_judging_blocks(project):
    """A 6-of-9 mean can sit above threshold and still must not release."""
    pkg = project([_req("REQ-0001", 5.0, judges_ok=6)], _classified("REQ-0001"))
    assert any("fewer than all judges" in b for b in pkg["manifest"]["blockers"])
    assert pkg["manifest"]["counts"]["incompletely_judged"] == 1


def test_unscored_requirement_blocks(project):
    pkg = project([_req("REQ-0001", None)], _classified("REQ-0001"))
    assert any("below threshold" in b for b in pkg["manifest"]["blockers"])


def test_unratified_authored_requirement_blocks(project):
    """Decision 2: the Analyst may author gap fillers, but a human ratifies them."""
    prov = {"origin": "analyst_authored", "gap_id": "G1", "ratified": False}
    pkg = project([_req("REQ-0001", 4.8, provenance=prov)], _classified("REQ-0001"))
    assert any("not ratified" in b for b in pkg["manifest"]["blockers"])


def test_ratified_authored_requirement_does_not_block(project):
    prov = {"origin": "analyst_authored", "gap_id": "G1", "ratified": True}
    pkg = project([_req("REQ-0001", 4.8, provenance=prov)], _classified("REQ-0001"))
    assert not any("not ratified" in b for b in pkg["manifest"]["blockers"])


def test_missing_classes_blocks(project):
    pkg = project([_req("REQ-0001", 4.8)], {})
    assert any("routing classes" in b for b in pkg["manifest"]["blockers"])


def test_sign_off_always_required(project):
    """The Analyst never self-promotes: even a clean set is not architect_ready
    until a human flips release_status."""
    pkg = project([_req("REQ-0001", 4.8)], _classified("REQ-0001"))
    assert any("no human sign-off" in b for b in pkg["manifest"]["blockers"])
    assert pkg["manifest"]["architect_ready"] is False


def test_duplicates_are_excluded_not_released(project):
    reqs = [_req("REQ-0001", 4.8), _req("REQ-0002", 4.8)]
    reqs[1]["lineage"] = {"duplicate_of": "REQ-0001"}
    pkg = project(reqs, _classified("REQ-0001"))
    assert pkg["manifest"]["counts"]["excluded_duplicates"] == 1
    assert [r["req_id"] for r in pkg["requirements"]] == ["REQ-0001"]


def test_judges_health_reaches_the_architect(project):
    pkg = project([_req("REQ-0001", 4.8, judges_ok=9)], _classified("REQ-0001"))
    analysis = pkg["requirements"][0]["analysis"]
    assert (analysis["judges_ok"], analysis["judges_total"]) == (9, 9)


# --- generated requirements must be unmissable in the rendering -----------

def test_markdown_flags_generated_requirements(project):
    prov = {"origin": "analyst_authored", "generated_to_fill_coverage_gap": True,
            "ratified": False, "gap_id": "GAP-abc", "gap_title": "Data Retention",
            "gap_severity": "critical", "domain_name": "Data & information",
            "rationale": "closes the retention gap", "assumptions": ["period unspecified"]}
    pkg = project([_req("GAP-0001", 4.8, provenance=prov)], _classified("GAP-0001"))
    md = package.render_markdown(pkg)
    assert "GENERATED" in md
    assert "not by a stakeholder" in md            # header summary
    assert "no stakeholder wrote this" in md.lower()
    assert "Data Retention" in md                   # traceable to its gap
    assert "Ratified: **NO**" in md
    assert "generated, not extracted from any document" in md


def test_markdown_does_not_flag_extracted_requirements(project):
    prov = {"source_document": "srs.pdf", "page": 9, "section_path": "3) Resources"}
    pkg = project([_req("REQ-0001", 4.8, provenance=prov)], _classified("REQ-0001"))
    md = package.render_markdown(pkg)
    assert "GENERATED" not in md
    assert "srs.pdf" in md


def test_authored_count_surfaces_in_manifest(project):
    prov = {"origin": "analyst_authored", "ratified": False}
    reqs = [_req("REQ-0001", 4.8), _req("GAP-0001", 4.8, provenance=prov)]
    entries = {**_classified("REQ-0001"), **_classified("GAP-0001")}
    pkg = project(reqs, entries)
    assert pkg["manifest"]["counts"]["analyst_authored"] == 1
    assert pkg["manifest"]["counts"]["unratified_authored"] == 1


def test_placeholder_text_blocks_release_even_above_threshold(project):
    """The live failure: 4.56 > 4.3 threshold, but the text says [LATENCY_VALUE]."""
    req = _req("GAP-0001", 4.56)
    req["text"] = "The VDS shall maintain a streaming latency of less than [LATENCY_VALUE]."
    pkg = project([req], _classified("GAP-0001"))
    assert any("unfilled" in b for b in pkg["manifest"]["blockers"])
    assert pkg["manifest"]["counts"]["with_placeholders"] == 1
    assert pkg["manifest"]["architect_ready"] is False


def test_clean_text_does_not_trip_the_placeholder_blocker(project):
    pkg = project([_req("REQ-0001", 4.8)], _classified("REQ-0001"))
    assert not any("unfilled" in b for b in pkg["manifest"]["blockers"])
    assert pkg["manifest"]["counts"]["with_placeholders"] == 0
