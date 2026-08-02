"""Project vocabulary — the controlled glossary of *things* and tag set of *concerns*.

See `documents/vocabulary_and_structure_redesign.md` for the why. In short: the
Architect inflates and fails to converge shared concepts because it reconstructs them
by string-matching generated names *after* generation. This module converges them
*before*, at the vocabulary level, where the reranker can judge semantic sameness —
which is the house tool for "are these the same?" (string matching is for lexical
facts only).

Two controlled vocabularies, built by one coherent pass that maintains a canonical
list as it goes (the registry pattern), so the same concept never gets two strings:

  glossary  domain ENTITIES you can own and build — nouns → Architect `part def`s
            (MenuItem, Tenant, Reservation). Tighter identity: near-synonyms merge only
            when they clearly name the same thing.
  tags      cross-cutting CONCERNS — adjectival, not ownable (authentication, payment).
            A node may carry several DISTINCT tags; the vocabulary holds no synonyms.

Canonicalisation is semantic (reranker), never lexical. Tags are relationship signals
for the LLM/reranker to follow, not string keys — we do NOT expand synonyms for recall.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict

from analyst_agent import config
from analyst_agent.llm import retrieval
from analyst_agent.llm.client import AgentServerClient, LLMError
from analyst_agent import vocabulary_prompts as vp

#: Tags canonicalise on the reranker alone — concerns are coarse and a threshold works
#: (proven on Restaurant: 19 clean tags, zero synonym dupes).
TAG_SAME_THRESHOLD = float(config.__dict__.get("VOCAB_TAG_THRESHOLD", 0.70))

#: Entities do NOT canonicalise well on a threshold — verified on Restaurant, a fixed
#: pairwise reranker score both over-merged on shared tokens (MenuImage<-MenuItem) and
#: under-merged synonyms (Image/ImageFile/FoodImage fragmented). So the reranker only
#: NARROWS: any existing term scoring above this low bar is a *candidate* for identity;
#: an LLM then adjudicates which (if any) the term is truly the same as.
TERM_CANDIDATE_THRESHOLD = float(config.__dict__.get("VOCAB_TERM_CANDIDATE_THRESHOLD", 0.30))
TERM_CANDIDATE_TOPK = int(config.__dict__.get("VOCAB_TERM_TOPK", 5))

EXTRACTOR_AGENT = "analyst_vocab_extractor"
CANONICALIZER_AGENT = vp.CANONICALIZER_AGENT_NAME
KIND_CLASSIFIER_AGENT = "analyst_vocab_kind_classifier"
#: Terms per kind-classification call. Small: labelling is independent per term, but the
#: house pattern keeps batches modest so the model does not conflate items.
KIND_CHUNK = 12


@dataclass
class Term:
    """One canonical glossary entry."""
    name: str                       # canonical entity name, PascalCase (MenuItem)
    definition: str                 # one-sentence meaning, pins ambiguous words
    kind: str = "entity"            # actor | entity | value — set by classify_kinds (LLM)
    aliases: list[str] = field(default_factory=list)   # surface forms seen, for audit
    req_ids: list[str] = field(default_factory=list)


@dataclass
class Tag:
    """One canonical cross-cutting concern."""
    name: str                       # canonical concept, lower-kebab (authentication)
    description: str = ""
    req_ids: list[str] = field(default_factory=list)


class Vocabulary:
    """Canonical glossary + tag set for one project. Plain data + reranker calls;
    no LLM reasoning lives here (extraction is a separate step)."""

    def __init__(self, rerank=retrieval.rerank, client: "AgentServerClient | None" = None) -> None:
        self._terms: dict[str, Term] = {}
        self._tags: dict[str, Tag] = {}
        self._rerank = rerank
        self._client = client   # for LLM term adjudication; lazily created if None

    # -- tags: reranker threshold is enough (coarse concerns) ------------------

    def _canonical_tag(self, surface: str) -> str | None:
        existing = list(self._tags)
        if not existing:
            return None
        scores = self._rerank(surface, existing)
        best_i, best = max(enumerate(scores), key=lambda p: p[1], default=(-1, 0.0))
        return existing[best_i] if best >= TAG_SAME_THRESHOLD else None

    # -- terms: reranker NARROWS, LLM ADJUDICATES identity --------------------

    def _adjudicate_term(self, surface: str, definition: str) -> str | None:
        """Return the existing term `surface` is the SAME entity as, or None if new.

        The reranker proposes the top-K most-similar existing terms; the LLM rules on
        identity by meaning, using definitions — so shared tokens don't force a merge
        and different words don't force a split.
        """
        existing = list(self._terms)
        if not existing:
            return None
        scores = self._rerank(surface, existing)
        ranked = sorted(zip(existing, scores), key=lambda p: p[1], reverse=True)
        candidates = [name for name, s in ranked if s >= TERM_CANDIDATE_THRESHOLD][:TERM_CANDIDATE_TOPK]
        if not candidates:
            return None
        if self._client is None:
            from analyst_agent.llm.client import AgentServerClient as _C
            self._client = _C()
        payload = json.dumps({
            "candidate": {"name": surface, "definition": definition},
            "existing": [{"name": n, "definition": self._terms[n].definition} for n in candidates],
        })
        try:
            out = self._client.complete_json(CANONICALIZER_AGENT, payload)
        except LLMError:
            return None   # on failure, treat as new — a spurious split beats a wrong merge
        same = out.get("same_as")
        return same if same in self._terms else None

    def add_term(self, surface: str, definition: str, req_id: str | None = None) -> Term:
        surface = surface.strip()
        match = surface if surface in self._terms else self._adjudicate_term(surface, definition)
        if match:
            term = self._terms[match]
            if surface != term.name and surface not in term.aliases:
                term.aliases.append(surface)
        else:
            term = Term(name=surface, definition=definition.strip())
            self._terms[surface] = term
        if definition and not term.definition:
            term.definition = definition.strip()
        if req_id and req_id not in term.req_ids:
            term.req_ids.append(req_id)
        return term

    def add_tag(self, surface: str, description: str = "", req_id: str | None = None) -> Tag:
        surface = surface.strip().lower()
        match = self._canonical_tag(surface)
        name = match or surface
        tag = self._tags.get(name)
        if tag is None:
            tag = Tag(name=name, description=description.strip())
            self._tags[name] = tag
        if description and not tag.description:
            tag.description = description.strip()
        if req_id and req_id not in tag.req_ids:
            tag.req_ids.append(req_id)
        return tag

    # -- kind classification: the LLM labels each term actor|entity|value -------

    def classify_kinds(self, client: "AgentServerClient | None" = None) -> None:
        """Label every glossary term with its KIND — actor (a role/person/external system
        that interacts with the system), entity (data the system stores/manages), or value
        (a typed value or status, e.g. Price, ReservationStatus). The LLM decides from the
        definition; this is the signal the Architect uses to keep actors, data and concerns
        apart. Failures leave the default 'entity' — a safe, non-destructive fallback."""
        client = client or self._client
        if client is None:
            from analyst_agent.llm.client import AgentServerClient as _C
            client = self._client = _C()
        terms = list(self._terms.values())
        for i in range(0, len(terms), KIND_CHUNK):
            chunk = terms[i:i + KIND_CHUNK]
            payload = json.dumps({"terms": [{"name": t.name, "definition": t.definition}
                                            for t in chunk]})
            try:
                out = client.complete_json(KIND_CLASSIFIER_AGENT, payload)
            except LLMError:
                continue
            kinds = out.get("kinds", {})
            for t in chunk:
                k = str(kinds.get(t.name, "")).strip().lower()
                if k in ("actor", "entity", "value"):
                    t.kind = k

    # -- access ---------------------------------------------------------------

    @property
    def terms(self) -> list[Term]:
        return sorted(self._terms.values(), key=lambda t: t.name)

    @property
    def tags(self) -> list[Tag]:
        return sorted(self._tags.values(), key=lambda t: t.name)

    def to_dict(self) -> dict:
        return {"glossary": [asdict(t) for t in self.terms],
                "tags": [asdict(t) for t in self.tags]}


def extract(reqs: list[dict], client: AgentServerClient | None = None,
            rerank=retrieval.rerank) -> Vocabulary:
    """Build the project vocabulary from requirement records.

    One extraction call per requirement (per-item, the house pattern), each proposing
    candidate entities and concerns; the Vocabulary canonicalises them into a single
    controlled set via the reranker. The extractor never invents canonical names — it
    proposes surface forms; canonicalisation decides identity.
    """
    client = client or AgentServerClient()
    vocab = Vocabulary(rerank=rerank, client=client)
    for req in reqs:
        rid, text = req.get("req_id"), req.get("text", "")
        if not text:
            continue
        try:
            out = client.complete_json(EXTRACTOR_AGENT, json.dumps({"req_id": rid, "text": text}))
        except LLMError:
            continue
        for t in out.get("terms", []):
            if t.get("term"):
                vocab.add_term(t["term"], t.get("definition", ""), req_id=rid)
        for c in out.get("tags", []):
            name = c if isinstance(c, str) else c.get("tag")
            if name:
                vocab.add_tag(name, req_id=rid)
    vocab.classify_kinds(client)          # label each canonical term actor|entity|value
    return vocab
