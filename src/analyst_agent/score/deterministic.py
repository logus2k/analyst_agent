"""Deterministic INCOSE rule detectors.

The 15 rules the GtWR expresses as explicit term/symbol lists are detected here
exactly — no LLM. Each finding cites the offending token and its offset, so it is
auditable and never hallucinated (the R21-miss the LLM made). The term lists live
in incose/catalog.json (detector == "deterministic").

These are HIGH-RECALL detectors: some rules (R19 "and"/"or", R5 "a") fire very
often by design. Presence is a signal; severity/context is decided by the
characteristic judge that consumes these findings.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

from analyst_agent import config

_CATALOG = os.path.join(config.KNOWLEDGE, "incose", "catalog.json")
_SYMBOLS = {"(", ")", "[", "]", "/"}


@dataclass
class RuleFinding:
    rule_id: str
    name: str
    matches: list[tuple[str, int]]   # (term, char offset)

    def to_dict(self) -> dict:
        return {"rule_id": self.rule_id, "name": self.name,
                "matches": [{"term": t, "offset": o} for t, o in self.matches]}


def load_deterministic_rules(catalog_path: str | None = None) -> list[dict]:
    with open(catalog_path or _CATALOG, encoding="utf-8") as f:
        cat = json.load(f)
    return [r for r in cat["rules"] if r.get("detector") == "deterministic" and r.get("terms")]


def _find(text: str, term: str) -> list[int]:
    """Offsets of `term` in `text`. Symbols match literally; words/phrases match
    on word boundaries, case-insensitively."""
    if term in _SYMBOLS:
        return [m.start() for m in re.finditer(re.escape(term), text)]
    pat = re.compile(r"(?<!\w)" + re.escape(term) + r"(?!\w)", re.IGNORECASE)
    return [m.start() for m in pat.finditer(text)]


def check_requirement(text: str, rules: list[dict] | None = None) -> list[RuleFinding]:
    """Run every deterministic rule against one requirement statement."""
    rules = rules if rules is not None else load_deterministic_rules()
    findings: list[RuleFinding] = []
    for r in rules:
        matches: list[tuple[str, int]] = []
        for term in r["terms"]:
            for off in _find(text, term):
                matches.append((term, off))
        if matches:
            matches.sort(key=lambda m: m[1])
            findings.append(RuleFinding(rule_id=r["id"], name=r["name"], matches=matches))
    return findings
