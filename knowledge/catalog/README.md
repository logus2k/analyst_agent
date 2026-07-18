# reqoach coverage catalog

The reference knowledge base for **Requirements Coverage** (see
`specs/projects_mode/`). Read‑mostly; bind‑mounted into the `reqoach` container.

## Layout

- **`domains.json`** — the fixed set of **coverage domains** (quality/constraint areas).
  Each domain maps to one specialized coverage judge and carries a generic baseline
  (concerns + questions) grounded in the standards.
- **`project_types/<id>.json`** — the **archetypes** (system types). Each is a
  *system‑type × domain knowledge slice*: for the domains where the archetype has
  distinctive priors, it lists the concerns, typical requirement categories, and the
  pointed questions a domain judge should ask. Archetypes are **composed**, not matched:
  a coverage run pulls the relevant per‑domain slices from several archetypes.
- **`standards/<pack>.json`** — **standard packs**: a standard mapped to coverage leaves +
  matching heuristics. The active reference switches packs on/off.

## Archetype file schema (`project_types/*.json`)

```
{
  "id": "kebab-id",
  "name": "Human name",
  "class": "data-dominant | computation-dominant | control-dominant | systems-software | interaction-client",
  "aliases": ["synonyms used to recognise this type in a brief"],
  "summary": "1–2 sentences: what this system type is.",
  "matching_signals": ["keywords/phrases hinting this archetype in a problem statement"],
  "salient_domains": ["<domain-id> ... the domains that matter most for this type"],
  "grounding": ["standard pack ids this archetype leans on"],
  "domains": {
    "<domain-id>": {                         // ONLY the domains with distinctive priors
      "emphasis": "critical | high | medium | low",
      "concerns":            ["what typically matters here for this system type"],
      "typical_requirements":["requirement categories usually expected"],
      "questions":           ["pointed questions the domain judge asks against the input"]
    }
  }
}
```

`<domain-id>` values are the ids in `domains.json`: `functional, data, performance,
reliability, security, safety, compatibility, usability, accessibility, maintainability,
portability, operational, legal-privacy, constraints, cultural-political, quality-in-use`.

**Gold reference:** `project_types/web-saas.json`.
