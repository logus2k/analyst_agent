# Analyst Agent

Turns raw stakeholder material (specifications, notes, a one-line request) into a
**validated, INCOSE-compliant requirements set** for the Architect Agent to design
against. First stage of the chain: **Analyst → Architect → Planner**.

Headless service on **:7803**. No UI — [reqoach](../../labs/requirements) is a thin
BFF that serves the frontend and proxies to this service.

## What it does

The Analyst owns delivering a *complete* set at or above the acceptance threshold.
It does not report problems and hand them over — it drives them to closure and
demands human input only when the information is genuinely external to the
documents.

```
ingest → segment → gate → score → refine → classify → coverage → package → sign-off → release
```

- **Ingest** — delegates parsing to the shared ingestion-server (`:8700`,
  `structural` strategy). This service ships no Docling.
- **Segment** — chunk → identify → assemble → dedup → gate (membership, not quality).
- **Score** — 9 INCOSE characteristics (C1–C9), one judge each at batch=1, plus 14
  deterministic writing rules and set-level checks (C10–C15, overlap detection).
- **Refine** — bounded rewrite/re-score loop. Originals are immutable; every
  attempt is recorded as a diff so semantic drift stays auditable.
- **Classify** — `classes[]` (multi-label, Architect routing) + `type`
  (single-label, reporting) + `constraints[]` (closed vocabulary).
- **Coverage** — domain-judge panel over a catalog of archetypes/standards → gaps.
- **Package** — the Architect handover contract.

## Run it

```bash
cd compose && docker compose up -d --build
curl localhost:7803/health
curl localhost:7803/dependencies      # are the three shared services reachable?
```

### Dependencies (all shared, all required)

| Service | Default | Used for |
|---|---|---|
| agent_server | `:7701` | every LLM preset (judges, reviewer, framing, coverage, classify) |
| ingestion-server | `:8700` | document parsing (`structural`) |
| embeddings / reranker | `:8601` | `segment.dedup`, `score.setlevel` overlap detection |

Config is env-overridable — see [src/analyst_agent/config.py](src/analyst_agent/config.py).
`ANALYST_STORE` holds all state; the store is authoritative and reqoach holds none.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Unit tests are offline — no LLM, no network. Tests needing live services are
marked `live` and excluded by default.

## Documents

| Doc | What it is |
|---|---|
| [technical_architecture.md](documents/technical_architecture.md) | design, contracts, integration |
| [implementation.md](documents/implementation.md) | migration inventory, service surface, build order |
| [migration_plan.md](documents/migration_plan.md) | **historical phase ledger** (P0–P8) with gate evidence |
| [remaining_work.md](documents/remaining_work.md) | **forward plan** — outstanding phases + decisions |

`migration_plan.md`'s status log and `remaining_work.md` are the authoritative
record of what is built and what is not. The repo is a single squashed commit, so
git history gives no phase granularity.

> ⚠️ Where `technical_architecture.md` and `remaining_work.md` disagree, the
> decisions block in `remaining_work.md` wins — notably the **absolute quality
> floor** (no human override for below-threshold requirements), which supersedes
> §8.
