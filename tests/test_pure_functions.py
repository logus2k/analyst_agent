"""Phase B — the pure functions that carry the contract.

These need no LLM and no network. They are the pieces whose silent misbehaviour
would corrupt everything downstream: rule ids that pollute aggregates,
traceability that lets a hallucinated requirement through, vocabularies that
leak free text into the Architect's routing contract.
"""

import pytest

from analyst_agent.classify import CLASSES, CONSTRAINT_VOCAB, TYPES, _coerce
from analyst_agent.score.characteristics import CHARACTERISTICS, normalize_rule_ids
from analyst_agent.score.deterministic import (RuleFinding, check_requirement,
                                               load_deterministic_rules)
from analyst_agent.segment.verify import (MIN_TEXT_LEN, dedup_key, is_valid_length,
                                          traceability)


# --- characteristics ------------------------------------------------------

def test_nine_characteristics_in_canonical_order():
    assert [c[0] for c in CHARACTERISTICS] == [f"C{i}" for i in range(1, 10)]


def test_preset_names_follow_the_convention():
    assert [f"incose_{c[1]}" for c in CHARACTERISTICS][0] == "incose_c1_necessary"


def test_rule_id_stripped_of_its_name():
    assert normalize_rule_ids(["R30 Unique Expression"]) == ["R30"]


def test_rule_ids_deduplicated_preserving_order():
    assert normalize_rule_ids(["R19", "R5", "R19"]) == ["R19", "R5"]


def test_hallucinated_rule_tokens_dropped():
    """`R_C8` is not a rule; letting it through pollutes the violation chart."""
    assert normalize_rule_ids(["R_C8", "nonsense", ""]) == []


def test_rule_ids_outside_1_to_42_dropped():
    assert normalize_rule_ids(["R0", "R43", "R99"]) == []
    assert normalize_rule_ids(["R1", "R42"]) == ["R1", "R42"]


def test_none_and_empty_are_safe():
    assert normalize_rule_ids(None) == []
    assert normalize_rule_ids([]) == []


# --- traceability (anti-hallucination) ------------------------------------

SOURCE = "The system shall terminate an idle session after fifteen minutes of inactivity."


def test_verbatim_substring_is_full_confidence():
    ok, conf = traceability("terminate an idle session", SOURCE)
    assert ok is True and conf == 1.0


def test_traceability_ignores_whitespace_and_case():
    ok, conf = traceability("TERMINATE   an  IDLE session", SOURCE)
    assert ok is True and conf == 1.0


def test_reworded_split_still_traceable_by_containment():
    ok, conf = traceability("session shall terminate after fifteen minutes", SOURCE)
    assert ok is True and conf >= 0.6


def test_invented_text_is_not_traceable():
    """The case that matters: a requirement the model made up must be rejected."""
    ok, conf = traceability("The system shall encrypt backups using AES-256.", SOURCE)
    assert ok is False and conf < 0.6


def test_empty_candidate_is_not_traceable():
    assert traceability("", SOURCE) == (False, 0.0)


def test_length_floor():
    assert is_valid_length("x" * MIN_TEXT_LEN) is True
    assert is_valid_length("too short") is False
    assert is_valid_length("   " + "x" * MIN_TEXT_LEN + "  ") is True


def test_dedup_key_normalizes_but_keeps_order_distinct():
    assert dedup_key(3, "The  System   SHALL x") == dedup_key(3, "the system shall x")
    assert dedup_key(3, "same text") != dedup_key(4, "same text")


# --- deterministic rules --------------------------------------------------

def test_catalog_loads_only_deterministic_rules_with_terms():
    rules = load_deterministic_rules()
    assert rules, "no deterministic rules loaded"
    assert all(r["detector"] == "deterministic" and r["terms"] for r in rules)


def test_vague_term_is_flagged():
    findings = check_requirement("The system shall be user-friendly and appropriate.")
    assert findings, "expected a vague-term finding"
    assert all(isinstance(f, RuleFinding) for f in findings)


def test_clean_requirement_triggers_nothing():
    assert check_requirement("The system shall encrypt stored data.") == []


def test_rules_are_high_recall_by_design():
    """A well-formed requirement still trips rules: R5 fires on the indefinite
    article "an", R35 on "after". That is intentional — the checker is
    deliberately high-recall and the characteristic judge decides severity. A
    finding is evidence for a judge, NOT a defect on its own."""
    text = "The system shall terminate an idle session after 15 minutes of inactivity."
    fired = {f.rule_id for f in check_requirement(text)}
    assert {"R5", "R35"} <= fired


def test_word_boundary_prevents_substring_false_positives():
    """A rule term must not fire inside a larger word — 'all' inside 'allocate'."""
    rules = [{"id": "RX", "name": "test", "terms": ["all"], "detector": "deterministic"}]
    assert check_requirement("The system shall allocate memory.", rules=rules) == []
    hit = check_requirement("The system shall log all events.", rules=rules)
    assert len(hit) == 1 and hit[0].matches[0][0] == "all"


def test_matching_is_case_insensitive():
    rules = [{"id": "RX", "name": "test", "terms": ["adequate"], "detector": "deterministic"}]
    assert check_requirement("Performance shall be ADEQUATE.", rules=rules)


def test_symbol_terms_match_literally():
    rules = [{"id": "R21", "name": "parens", "terms": ["("], "detector": "deterministic"}]
    hit = check_requirement("The system shall log events (verbosely).", rules=rules)
    assert len(hit) == 1


def test_findings_carry_offsets_for_audit():
    rules = [{"id": "RX", "name": "test", "terms": ["fast"], "detector": "deterministic"}]
    hit = check_requirement("The system shall be fast.", rules=rules)
    term, offset = hit[0].matches[0]
    assert term == "fast"
    assert "The system shall be fast.".index("fast") == offset


def test_finding_serializes_for_the_scorecard():
    f = RuleFinding("R7", "Vague Terms", [("fast", 20)])
    assert f.to_dict() == {"rule_id": "R7", "name": "Vague Terms",
                           "matches": [{"term": "fast", "offset": 20}]}


# --- classification vocabularies -----------------------------------------

def test_valid_labels_pass_through():
    out = _coerce({"classes": ["functional", "constraint"], "type": "performance",
                   "constraints": ["latency", "throughput"]})
    assert out["classes"] == ["functional", "constraint"]
    assert out["type"] == "performance"
    assert out["constraints"] == ["latency", "throughput"]


def test_out_of_vocabulary_labels_are_dropped_not_passed_through():
    """Free text in `constraints[]` would break the Architect's machine contract."""
    out = _coerce({"classes": ["functional", "made_up"], "type": "performance",
                   "constraints": ["latency", "vibes", "web-scale"]})
    assert out["classes"] == ["functional"]
    assert out["constraints"] == ["latency"]


def test_classes_always_yield_a_routing_target():
    out = _coerce({"classes": ["nonsense"], "type": "functional"})
    assert out["classes"] == ["functional"]


def test_invalid_type_falls_back_to_primary_class_then_other():
    assert _coerce({"classes": ["interface"], "type": "bogus"})["type"] == "interface"
    # `structural` is a routing class but NOT a reporting type -> "other"
    assert _coerce({"classes": ["structural"], "type": "bogus"})["type"] == "other"


def test_string_instead_of_list_is_accepted():
    out = _coerce({"classes": "functional", "type": "functional", "constraints": "latency"})
    assert out["classes"] == ["functional"] and out["constraints"] == ["latency"]


def test_constraints_are_deduplicated_and_sorted():
    out = _coerce({"classes": ["functional"], "type": "functional",
                   "constraints": ["throughput", "latency", "latency"]})
    assert out["constraints"] == ["latency", "throughput"]


def test_justification_is_truncated():
    out = _coerce({"classes": ["functional"], "type": "functional",
                   "justification": "x" * 900})
    assert len(out["justification"]) == 400


def test_empty_input_still_yields_a_valid_record():
    out = _coerce({})
    assert out["classes"] == ["functional"]
    assert out["type"] in TYPES
    assert out["constraints"] == []


@pytest.mark.parametrize("vocab", [CLASSES, TYPES, CONSTRAINT_VOCAB])
def test_vocabularies_are_closed_tuples(vocab):
    """Tuples, not lists — these are contracts, not defaults to be mutated."""
    assert isinstance(vocab, tuple) and vocab


# --- unresolved placeholders ---------------------------------------------
# OBSERVED LIVE during the 78-gap authoring run: the author produced
# "...streaming latency of less than [LATENCY_VALUE] for 95 percent..." and the
# nine judges scored it 4.56 — clearing a 4.3 threshold. The judges rate the FORM
# of a statement and a parameterized statement is well-formed; they cannot know
# the parameter was never filled. So this check is deterministic, not a judge.

from analyst_agent.authoring import unresolved_placeholders


def test_real_observed_placeholder_is_caught():
    text = ("The VDS shall maintain a streaming latency of less than [LATENCY_VALUE] "
            "for 95 percent of sessions.")
    assert unresolved_placeholders(text) == ["[LATENCY_VALUE]"]


def test_percent_placeholder_is_caught():
    assert unresolved_placeholders("shall maintain an availability of [X]% ") == ["[X]"]


def test_bracketed_instruction_is_caught():
    """The P4 finding: refinement converges to '[specify maximum response time]'."""
    assert unresolved_placeholders("within [specify maximum response time].")


@pytest.mark.parametrize("text", [
    "The value is TBD.", "Latency TBC.", "Set to XXX.",
    "Availability <VALUE> percent.", "Use {{threshold}} here.",
])
def test_placeholder_markers_are_caught(text):
    assert unresolved_placeholders(text), f"missed placeholder in {text!r}"


def test_clean_requirement_has_no_placeholders():
    text = "The system shall terminate an idle session after 15 minutes of inactivity."
    assert unresolved_placeholders(text) == []


def test_ordinary_punctuation_is_not_a_placeholder():
    """Must not fire on normal prose — a false positive here blocks release."""
    for text in ["The system shall log events (verbosely) and retain them.",
                 "Latency shall be < 100 ms.",
                 "The system shall support a > b comparisons."]:
        assert unresolved_placeholders(text) == [], text


def test_empty_and_none_are_safe():
    assert unresolved_placeholders("") == []
    assert unresolved_placeholders(None) == []
