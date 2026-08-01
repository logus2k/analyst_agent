# Redesign: Project Vocabulary & Requirement Structure

**Status:** in progress (living document — the Status section is updated as implementation advances).
**Owner discussion:** Analyst ↔ Architect design thread, 2026-08.
**Scope:** the Analyst learns to organise a project's requirements into a structure the
Architect can design against coherently, and to hand over the project's controlled
vocabulary. This is an Analyst-side redesign; the Architect and Planner consume its output.

---

## 1. Why — the problem this fixes

Verified on the first `architect_ready` package (Restaurant Menu Manager, 60 requirements,
run 2026-08). The Architect produced **valid but noisy** output because it processes each
requirement in isolation and reconstructs shared concepts *after* generation:

- **Concept inflation.** One concern — Google login — produced **six** authentication
  interfaces (`AuthenticationInterface`, `UserAuthenticationInterface`,
  `OAuth2AuthenticationInterface`, `GoogleOAuthAuthenticationInterface`, with literal
  duplicates). 33 interface defs for 60 requirements — more than every other element kind
  combined.
- **Missing entities.** The requirements say "item" constantly, but no `MenuItem` component
  was ever minted — "item" was interpreted differently per requirement and never converged.
- **Root cause.** The Architect's symbol registry tries to merge near-synonyms *by string
  normalisation, after generation* — the weakest possible place and time to converge.

The fix is to converge **before** generation, at the vocabulary level, where a semantic
reranker can actually do the job — and to feed the Architect **semantically grouped
context** so it designs an aspect as a unit instead of a requirement at a time.

## 2. What we're adding

Three new Analyst artifacts, all part of the output package and all human-reviewable:

### 2.1 Glossary — the controlled vocabulary of *things*
Canonical domain entities for the project. A term is a **noun you can own and build**
(`MenuItem`, `Tenant`, `Reservation`). Pins meaning: "item = a menu item, canonical entity
`MenuItem`" so the Architect anchors its `part def`s to agreed nouns instead of inventing
variants. Fixes **noun-level** convergence.

### 2.2 Tag vocabulary — the controlled vocabulary of *concerns*
Cross-cutting concepts (`authentication`, `payment`, `notifications`). A tag is a
**concern that cross-cuts**, adjectival, not ownable. Fixes **concept-level** convergence.

Rules (decided in discussion):
- **One canonical tag per concept. No synonyms in the vocabulary** — never `auth` +
  `authentication` + `login` for one thing.
- **A node may carry several tags, each a *distinct* concept** — Reservations →
  `authentication`, `payment`. Multiple concepts per node: yes. Multiple words for one
  concept: no.
- **A tags list is maintained during assignment** — check-and-reuse against the canonical
  set as tags are assigned, so the same concept never gets two strings. Same mechanism the
  Architect's symbol registry uses for names.
- **Matching is semantic, not lexical.** The tag signals a relationship for the LLM/reranker
  to follow; it is not a string key requiring exact equality. (We are not doing IR
  query-expansion — no synonym decoration to boost recall.)

**Term vs tag boundary:** a term is a *thing* (noun → a component); a tag is a *concern*
(cross-cutting → an aspect). "Authentication" may appear as both — the tag `authentication`
and the glossary term `AuthenticationService` — in different roles. Keep the two lists
distinct so they don't collapse.

### 2.3 Requirement tree — single-parent structure + tags
Each requirement is placed under exactly **one** parent branch/aspect (a strict tree, not a
DAG). Cross-cutting relationships are carried by **tags**, not by multiple parents:
Reservations lives under its own branch and is tagged `authentication`; it does **not**
become a child of Authentication.

- **Branches** seed how the Architect batches design work (design an aspect as a unit) and
  how the Planner forms epics (one branch → one epic, its requirements → stories).
- **Ownership rule (critical, prevents re-inflation):** the branch that *owns* a concept
  designs its component **once**. Tagged-but-not-owning branches are **consumers**, not
  co-designers. When the Architect designs auth, it filters nodes tagged `authentication`
  to learn *who consumes auth* — but Reservations does not get to model auth itself. The tag
  grants **read-context, not write-ownership.** This maps onto supplier/consumer, which the
  model already emits.

## 3. How the Architect consumes it (contract change)

- **Per-branch design** replaces per-requirement generation: the Architect sees all
  requirements in a branch together, so it produces one coherent component/interface per
  aspect instead of scattered per-requirement duplicates. (This does **not** violate the
  batch=1 rule — that rule is about *judging* requirements, where conflation is bad;
  *designing* an aspect wants the related requirements seen together.)
- **Glossary-anchored naming:** `part def`s and attributes anchor to glossary terms. The
  symbol registry's job shrinks — shared entities arrive pre-identified.
- **Tag-filtered context:** designing a component pulls nodes carrying its tag as context
  and consumers.

## 4. Human review (fits the approval loop)

Curating the **glossary and tag list** — and the tree placement — is higher-leverage review
than eyeballing 33 generated interfaces. New UI tabs (in reqoach, the consolidated control
panel): a **Glossary/Tags** view and the **requirements tree** view. Reviewing the
organisation once makes the downstream design coherent by construction. The tree/glossary/
tags become a reviewable, approvable artifact before the Architect runs.

## 5. Reuse, not greenfield
- **Reranker** (`embeddings :8601`, `/v1/rerank`) for canonicalising terms/tags — semantic
  sameness, per house convention (never string-matching for semantic identity).
- reqqa/Analyst already has a **coverage domain taxonomy** — candidate seed for the tree's
  top level.
- Possible existing **concept extraction** in the Analyst — reuse if present (to verify).

## 6. Non-goals (explicitly out for now)
- No graph *database*. This is an organisational tree + tags + glossary, not ArcadeDB.
- No Planner changes yet — Planner epics/stories from branches come after the Architect
  produces reviewed, approved output.
- No synonym/query expansion of any kind.

## 7. Implementation phases

| Phase | Deliverable | Where | Verifies |
|---|---|---|---|
| 1 | Glossary + tag vocabulary extraction (canonical, reranker-deduped) | Analyst | On Restaurant text: no synonym dupes; `MenuItem`, `authentication` appear once |
| 2 | Requirement tree: single-parent branches + per-node tags | Analyst | Every req has one parent + distinct tags; auth reqs cluster |
| 3 | Extend output package with glossary/tags/tree; bump Architect contract | Analyst + sdk | Package carries the three artifacts; Architect `load_package` reads them |
| 4 | Architect: per-branch design + glossary-anchored naming | Architect | Auth 6→1 interface; `MenuItem` present; interface count sane |
| 5 | reqoach UI: Glossary/Tags tab + tree view + review/approve | reqoach | Human curates vocabulary and approves before design |

**Sequencing rationale:** vocabulary (Phase 1) is the foundation everything anchors to, and
it is independently testable on real data before any pipeline/UI wiring. Prove the payoff
cheap, then wire it in.

## 8. Status (living)

- **2026-08 — Plan written.** This document created.
- **2026-08 — Phase 1 built + tested on real Restaurant data (60 reqs, 32s).**
  `src/analyst_agent/vocabulary.py` + `vocabulary_prompts.py`; preset
  `analyst_vocab_extractor` registered.
  - **Tags: strong success.** 19 canonical concerns, **zero synonym duplication**.
    `authentication` = one tag over 15 reqs (vs the Architect's 6 scattered auth
    interfaces); `multi-tenancy` one tag over 20; `localization` one over 8. The
    reranker canonicalisation proves the thesis at the concept level.
  - **Glossary: partial — needs a different identity approach.** 42 terms with heavy
    overlap the 0.80 pairwise threshold missed: "AI description" fragmented 4 ways,
    "image" 6 ways; and one WRONG merge (`MenuImage` absorbed `MenuItem` — different
    things). Entities are compositional (shared tokens fool similarity), so pairwise
    reranker-at-a-threshold over-merges on shared tokens and under-merges on synonyms.
    **Next:** replace term dedup with an LLM canonicalisation pass that adjudicates a
    candidate against the current glossary (the reranker narrows candidates; the LLM
    rules on identity), rather than a fixed similarity threshold.
- **2026-08 — Phase 1b: LLM glossary canonicalisation built + tested on Restaurant.**
  Added `analyst_vocab_canonicalizer` preset; `add_term` now: reranker narrows to top-K
  candidates, LLM adjudicates identity by meaning.
  - **Critical wrong-merge fixed (verified):** `MenuItem` is now its own term, no longer
    absorbed into `MenuImage`. Good merges: `Menu`←RestaurantMenu/MenuEntity,
    `Tenant`←TenantId, `WorkingHours`←WorkHours/OpeningHours. 42 → 34 terms.
  - **Still imperfect (honest):** under-merges images/descriptions (LLM conservative by
    instruction — prefer a split over a wrong merge, per the spec's own rule); one
    consistency glitch (`Image` appears as both a term and an alias — ordering artifact).
  - **Verdict:** acceptable to proceed. The dangerous failure mode (wrong merge erasing a
    distinction) is fixed; the remaining failure mode (a split) is recoverable downstream.
    Perfect entity canonicalisation is not blocking the tree work, which is higher impact.
- **2026-08 — Phase 2: requirement tree built + tested on Restaurant (44s total).**
  `src/analyst_agent/structure.py` + presets `analyst_branch_proposer`,
  `analyst_branch_assigner`. Single-level feature branches; each requirement assigned to
  exactly one; cross-cutting tags inverted from the vocabulary pass.
  - **Verified correct:** 7 sensible branches (User Authentication, Tenant Administration,
    Menu Management, Reservations, Image Processing & AI, System Infrastructure, UI &
    Display); 60/60 assigned, 0 unassigned; single-parent invariant holds.
  - **The ownership mechanism works (the whole point):** 14 reqs tagged `authentication`
    land as User Authentication:6 (owner) + Reservations:5 / Menu:1 / Image:1 / Tenant:1
    (consumers). The owning branch will design ONE auth component; the others carry the
    tag as consumers. This is the demonstrated fix for the Architect's 6-interface auth
    inflation.
- **2026-08 — Phase 3 (core): package now carries glossary/tags/tree.** Added
  `store.save_structure`/`get_structure`, `structure.run(pid)` (reads the quality
  scorecard, builds vocab+tree, persists `structure.json`), and `package.build_package`
  now emits `glossary`, `tags`, `tree`. **Verified in-process on Restaurant:** package
  carries 38 glossary terms, 24 tags, 7 branches; `architect_ready` still True.
  - **NOT done (Phase 3 remaining):** (a) an API job endpoint `structure:run` on the
    Analyst service (matching classify:run/coverage:run); (b) service redeploy to load the
    new modules — the running :7803 container still has the old code; (c) bump the
    **Architect contract** (`architect_agent/sdk/how_to.md`) and its `load_package` to read
    glossary/tags/tree. The store is root/container-owned, so `structure.run` executes
    server-side, not from a local process.
- **2026-08 — Phase 3 LIVE: structure:run endpoint + service redeployed + verified.**
  Added `POST /projects/{pid}/structure:run` (JobManager `_run_structure`/
  `create_structure_run`, mirroring classify:run). Rebuilt + restarted `analyst-agent`.
  Verified end-to-end: ran the build in the redeployed container (38 terms, 24 tags, 6
  branches), `structure.json` persisted to the store, and the **live package endpoint now
  returns glossary/tags/tree**. Caveat: the HTTP `:run` POST is auth-gated (401 without a
  session, like every write endpoint) — verified the build path in-container; a signed-in
  UI user triggers it normally.
  - **NOT done (Phase 3 remaining):** bump the **Architect contract**
    (`architect_agent/sdk/how_to.md`) and its `load_package` to read glossary/tags/tree.
- _(updated as phases land)_

## 9. Open decisions
- Tree top-level: emergent per-project vs seeded from the coverage domain taxonomy. (Leaning:
  seed + allow project-specific branches.)
- Glossary/tag extraction: single coherent pass maintaining the canonical list (registry
  pattern) — confirmed approach; granularity of what counts as a "term" still to tune on
  real data.
