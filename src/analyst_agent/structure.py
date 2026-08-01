"""Requirement tree — single-parent feature branches + per-node cross-cutting tags.

See `documents/vocabulary_and_structure_redesign.md`. The Architect designs per branch
(one coherent component/interface per aspect instead of scattered per-requirement
duplicates), and the Planner turns each branch into an epic. Cross-cutting relationships
are carried by TAGS (from `vocabulary.py`), never by a second parent — a reservation that
needs auth stays under Reservations and is tagged `authentication`; it does not become a
child of Authentication.

Two steps:
  1. propose branches — one global call, the project's feature areas (collectively
     exhaustive, mutually distinct).
  2. assign each requirement to exactly ONE branch — per-item (the house pattern).

Tags are inverted from the vocabulary pass (which already tagged each requirement), so a
node carries its distinct concerns without re-deriving them.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, asdict

from analyst_agent.llm.client import AgentServerClient, LLMError
from analyst_agent.vocabulary import Vocabulary
from analyst_agent import vocabulary_prompts as vp

#: Per-item assignment parallelism. agent_server runs --parallel 2; keep modest.
WORKERS = 4


@dataclass
class Branch:
    name: str
    scope: str = ""
    req_ids: list[str] = field(default_factory=list)


@dataclass
class Node:
    """One requirement placed in the tree."""
    req_id: str
    branch: str | None            # single parent; None = unassigned (flagged)
    tags: list[str] = field(default_factory=list)   # distinct cross-cutting concerns


@dataclass
class Tree:
    branches: list[Branch] = field(default_factory=list)
    nodes: list[Node] = field(default_factory=list)
    unassigned: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"branches": [asdict(b) for b in self.branches],
                "nodes": [asdict(n) for n in self.nodes],
                "unassigned": list(self.unassigned)}


def _tags_by_req(vocab: Vocabulary) -> dict[str, list[str]]:
    """Invert the vocabulary's per-tag req lists into per-requirement tag lists."""
    out: dict[str, list[str]] = {}
    for tag in vocab.tags:
        for rid in tag.req_ids:
            out.setdefault(rid, []).append(tag.name)
    return out


def propose_branches(reqs: list[dict], vocab: Vocabulary,
                     client: AgentServerClient) -> list[Branch]:
    """One global call: the project's feature branches."""
    payload = json.dumps({
        "requirements": [{"req_id": r.get("req_id"), "text": r.get("text", "")} for r in reqs],
        "concern_tags": [t.name for t in vocab.tags],
    })
    try:
        out = client.complete_json(vp.BRANCH_PROPOSER_AGENT_NAME, payload)
    except LLMError:
        return []
    return [Branch(name=b["name"].strip(), scope=b.get("scope", ""))
            for b in out.get("branches", []) if b.get("name")]


def assign(reqs: list[dict], branches: list[Branch], client: AgentServerClient,
           workers: int = WORKERS) -> dict[str, str | None]:
    """Assign each requirement to exactly one branch (per-item). Returns req_id -> branch
    name (or None). An assignment that names an unknown branch is treated as unassigned —
    the model must pick from the offered list."""
    valid = {b.name for b in branches}
    catalogue = [{"name": b.name, "scope": b.scope} for b in branches]

    def one(req: dict) -> tuple[str, str | None]:
        rid = req.get("req_id")
        payload = json.dumps({"requirement": req.get("text", ""), "branches": catalogue})
        try:
            out = client.complete_json(vp.BRANCH_ASSIGNER_AGENT_NAME, payload)
        except LLMError:
            return rid, None
        chosen = out.get("branch")
        return rid, (chosen if chosen in valid else None)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        return dict(ex.map(one, reqs))


def build_tree(reqs: list[dict], vocab: Vocabulary,
               client: AgentServerClient | None = None) -> Tree:
    """Full Phase-2 build: propose branches, assign every requirement to one, attach the
    cross-cutting tags each requirement already carries from the vocabulary pass."""
    client = client or AgentServerClient()
    branches = propose_branches(reqs, vocab, client)
    assignment = assign(reqs, branches, client) if branches else {}
    tags = _tags_by_req(vocab)

    by_name = {b.name: b for b in branches}
    nodes, unassigned = [], []
    for req in reqs:
        rid = req.get("req_id")
        br = assignment.get(rid)
        nodes.append(Node(req_id=rid, branch=br, tags=sorted(tags.get(rid, []))))
        if br and rid not in by_name[br].req_ids:
            by_name[br].req_ids.append(rid)
        elif not br:
            unassigned.append(rid)
    return Tree(branches=branches, nodes=nodes, unassigned=unassigned)


def run(pid: str, client: AgentServerClient | None = None):
    """Build and persist the project's vocabulary + requirement tree.

    Reads requirements from the project's latest quality scorecard, extracts the
    controlled vocabulary (glossary + tags), builds the single-parent tree, and stores
    the combined artifact. Returns the stored dict, or None if the project has no
    requirements yet.
    """
    from analyst_agent import store as pj
    from analyst_agent.vocabulary import extract

    scorecard = pj.get_quality_scorecard(pid)
    if not scorecard:
        return None
    reqs = [{"req_id": r["req_id"], "text": r["text"]} for r in scorecard.get("requirements", [])]
    if not reqs:
        return None

    client = client or AgentServerClient()
    vocab = extract(reqs, client=client)
    tree = build_tree(reqs, vocab, client=client)
    data = {"vocabulary": vocab.to_dict(), "tree": tree.to_dict()}
    pj.save_structure(pid, data)
    return data
