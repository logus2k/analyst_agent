"""Characteristic-judge helpers for the scoring pipeline.

Includes rule-id normalization: judges occasionally emit a rule id with its name
("R30 Unique Expression") or a hallucinated non-rule token ("R_C8"). This maps
`rules_triggered` to canonical bare ids (R<number>) and drops anything that isn't
a real rule, so aggregates (the rule-violation chart) stay clean.
"""

from __future__ import annotations

import re

# The 9 individual characteristics, in order: (id, judge-preset-suffix, name).
CHARACTERISTICS = [
    ("C1", "c1_necessary", "Necessary"),
    ("C2", "c2_appropriate", "Appropriate"),
    ("C3", "c3_unambiguous", "Unambiguous"),
    ("C4", "c4_complete", "Complete"),
    ("C5", "c5_singular", "Singular"),
    ("C6", "c6_feasible", "Feasible"),
    ("C7", "c7_verifiable", "Verifiable"),
    ("C8", "c8_correct", "Correct"),
    ("C9", "c9_conforming", "Conforming"),
]

_RULE_ID = re.compile(r"\bR(\d{1,2})\b")


def normalize_rule_ids(rules_triggered) -> list[str]:
    """Canonicalize a judge's rules_triggered to unique bare ids (R1..R42).

    "R30 Unique Expression" -> "R30", "R19" -> "R19", "R_C8" -> dropped.
    """
    out: list[str] = []
    for item in rules_triggered or []:
        m = _RULE_ID.search(str(item))
        if not m:
            continue
        n = int(m.group(1))
        if not (1 <= n <= 42):
            continue
        rid = f"R{n}"
        if rid not in out:
            out.append(rid)
    return out
