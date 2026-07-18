"""Open questions — what the Analyst needs from a human to finish converging.

Decision 1 makes the quality floor absolute: a requirement below threshold is
resolved by supplying the missing information, never by accepting it as-is. So
when the loop stalls it must say *what to answer*, not merely that it plateaued.

NO NEW LLM CALL. Everything here is already stored by work that has run:

  placeholder  the text carries an unfilled value — `[LATENCY_VALUE]`, `TBD`.
               `authoring.unresolved_placeholders` finds them deterministically.
  advisory     the INCOSE reviewer already emits `{characteristic, issue,
               suggestion}` per defect, e.g. issue "the required timing context is
               not quantified" / suggestion "specify the maximum latency". That is
               a question in all but punctuation. All 85 requirements of a real run
               carry reviewer output.
  authored_gap the gap author recorded `provenance.open_question` when it could
               draft the obligation but not its value.

Collecting beats asking: an LLM pass to "generate questions" would restate what
these fields already say, cost a call per requirement, and vary between runs.

Questions asking for the same thing across several requirements are merged so a
human answers once — a stalled NIST run would otherwise present dozens of
near-identical "specify the latency" prompts.
"""

from __future__ import annotations

from analyst_agent import store as pj
from analyst_agent.authoring import unresolved_placeholders
from analyst_agent.llm.retrieval import embed

# Only these statuses are still waiting on a human. Anything else has either been
# resolved or was never blocked.
_UNRESOLVED = ("needs_human", "unreviewed", None)

# Questions this similar are asking the same thing. MEASURED, not guessed: over the
# 167 real questions of a NIST run the pairwise cosine distribution is
# p50 0.551 · p90 0.644 · p99 0.735 · p99.5 0.758 · max 0.930, so 0.85 sits well
# inside the genuine tail. At that level the pairs really are equivalent, e.g.
# "Specify the frequency or trigger conditions for the maintenance actions." ~
# "Specify the conditions or frequency under which the maintenance actions must be
# performed." (0.930).
SIMILARITY_THRESHOLD = 0.85


def _cosine(a: list[float], b: list[float]) -> float:
    """bge-m3 vectors arrive L2-normalized, so the dot product IS the cosine."""
    return sum(x * y for x, y in zip(a, b))


def _merge_similar(items: list[dict], threshold: float = SIMILARITY_THRESHOLD) -> list[dict]:
    """Fold questions that ask the same thing into one, keeping every requirement.

    EMBEDDINGS, NOT THE RERANKER. The reranker answers "is this document relevant
    to this query" — asymmetric relevance — so every advisory about improving a
    requirement scores related to every other one. Measured: at 0.7 it chained 167
    questions into a single 147-member blob. This is a symmetric *equivalence*
    question, which is what embedding cosine measures. (The codebase's documented
    rejection of cosine in `llm/retrieval.py` is about summary-vs-detail
    SUBSUMPTION — asymmetric containment, where a cross-encoder is right. Different
    relation, different tool.)

    NO TRANSITIVITY. Star clustering: a seed absorbs its above-threshold
    neighbours, and an absorbed question can never itself absorb. A~B and B~C
    therefore does not drag A and C together, which is the chaining that produced
    the blob. `score.setlevel.find_overlaps` stops at pairs for the same reason.

    PLACEHOLDERS ARE NEVER MERGED. "What value should replace [1] in REQ-0052?" and
    "...[2] in REQ-0051?" score 0.922 — near-identical sentence template, entirely
    different answers. They are req_id-specific by construction, so similarity is
    meaningless for them.
    """
    fixed = [q for q in items if "placeholder" in q["sources"]]
    pool = [q for q in items if "placeholder" not in q["sources"]]
    if len(pool) < 2:
        return items

    try:
        vecs = embed([q["question"] for q in pool])
    except Exception:                                  # noqa: BLE001 — embeddings down
        return items                                   # show duplicates rather than
    if len(vecs) != len(pool):                         # silently collapse distinct asks
        return items

    absorbed: set[int] = set()
    out: list[dict] = []
    # Seeds in order of how many requirements they already cover, so the most
    # broadly-useful phrasing becomes the representative.
    for i in sorted(range(len(pool)), key=lambda k: (-len(pool[k]["req_ids"]),
                                                     len(pool[k]["question"]))):
        if i in absorbed:
            continue
        rep = dict(pool[i])
        variants = []
        for j in range(len(pool)):
            if j == i or j in absorbed:
                continue
            if _cosine(vecs[i], vecs[j]) >= threshold:
                absorbed.add(j)
                other = pool[j]
                variants.append(other["question"])
                for rid in other["req_ids"]:
                    if rid not in rep["req_ids"]:
                        rep["req_ids"].append(rid)
                for src in other["sources"]:
                    if src not in rep["sources"]:
                        rep["sources"].append(src)
                rep["blocking"] = rep["blocking"] or other["blocking"]
        absorbed.add(i)
        if variants:
            rep["variants"] = variants                 # what was folded in, for audit
        out.append(rep)
    return out + fixed


def _current_text(req: dict, entry: dict) -> str:
    return entry.get("final_text") or req.get("text", "")


def _score(req: dict, entry: dict) -> float | None:
    after = entry.get("overall_after")
    return after if after is not None else req.get("overall")


def collect_questions(pid: str, run_id: str | None = None) -> list[dict]:
    """Every open question blocking release, merged and ordered.

    A question is `blocking` when the requirement it belongs to would fail the
    release gate — below threshold, or carrying an unfilled placeholder.
    """
    scorecard = pj.get_quality_scorecard(pid, run_id)
    if not scorecard:
        return []
    review = pj.get_review(pid, run_id) if run_id else None
    if review is None:
        runs = pj.list_quality_runs(pid)
        if runs:
            latest = sorted(runs, key=lambda r: r.get("finished_at") or "")[-1]["run_id"]
            review = pj.get_review(pid, latest)
    entries = (review or {}).get("requirements") or {}
    threshold = float(((review or {}).get("threshold") or {}).get("value", 4.3))

    merged: dict[str, dict] = {}

    def add(text: str, why: str, req_id: str, source: str, blocking: bool,
            characteristic: str | None = None) -> None:
        text = (text or "").strip()
        if not text:
            return
        key = text
        q = merged.setdefault(key, {
            "question": text, "why": why or "", "req_ids": [], "sources": [],
            "characteristic": characteristic, "blocking": False})
        if req_id not in q["req_ids"]:
            q["req_ids"].append(req_id)
        if source not in q["sources"]:
            q["sources"].append(source)
        # Blocking wins: one blocked requirement makes the question blocking.
        q["blocking"] = q["blocking"] or blocking

    for req in scorecard.get("requirements", []):
        if (req.get("lineage") or {}).get("duplicate_of"):
            continue
        rid = req.get("req_id")
        entry = entries.get(rid) or {}
        text = _current_text(req, entry)
        score = _score(req, entry)
        prov = req.get("provenance") or {}
        below = score is None or score < threshold
        holes = unresolved_placeholders(text)

        # 1. unfilled placeholder — the most concrete ask there is
        for ph in holes:
            add(f"What value should replace {ph} in {rid}?",
                text, rid, "placeholder", True)

        # 2. the gap author's own recorded question
        if prov.get("open_question"):
            add(prov["open_question"],
                f"Needed to complete {rid}, generated for gap: {prov.get('gap_title', '')}",
                rid, "authored_gap", bool(below or holes))

        # 3. reviewer advisories on requirements still unresolved
        if entry.get("status") in _UNRESOLVED and (below or holes):
            for adv in ((req.get("review") or {}).get("advisories") or []):
                if not isinstance(adv, dict):
                    continue
                add(adv.get("suggestion", ""), adv.get("issue", ""), rid, "advisory",
                    bool(below), adv.get("characteristic"))

    # Identical strings merged above; embedding cosine now folds the ones that ASK
    # the same thing in different words.
    out = _merge_similar(list(merged.values()))
    # Blocking first, then the questions that unblock the most requirements.
    out.sort(key=lambda q: (not q["blocking"], -len(q["req_ids"])))
    for i, q in enumerate(out, 1):
        q["id"] = f"Q-{i:04d}"
        q["affects"] = len(q["req_ids"])
    return out


def summarize(questions: list[dict]) -> dict:
    return {"total": len(questions),
            "blocking": sum(1 for q in questions if q["blocking"]),
            "requirements_affected": len({r for q in questions for r in q["req_ids"]}),
            "by_source": {s: sum(1 for q in questions if s in q["sources"])
                          for s in ("placeholder", "advisory", "authored_gap")}}
