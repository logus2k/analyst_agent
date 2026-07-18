"""Streaming assessment job — the pipeline as an event-emitting generator.

This is the job body behind the orchestration API (spec §8.5/§8.6 step 1). It
runs the same pipeline as `scripts/produce_scorecard.py` — ingest → segment →
gate → score → review → set-level — but instead of a batch `log()` it yields
**structured events** so a UI can fill in live:

  {"type":"stage", "stage":..., "status":"start"|"done", "done":k,"total":n,"message":...}
  {"type":"requirement", "data":{...full record...}}     one per req, the moment
                                                          its 9 judges finish
  {"type":"set_level", "data":{overlaps, set_assessment}}
  {"type":"aggregates", "data":{...}}                     final roll-up
  {"type":"scorecard", "data":{...}}                      the assembled scorecard
  {"type":"error", "stage":..., "message":...}

Scoring keeps the full (req × judge) thread pool for throughput, but tracks a
per-requirement completion counter so each requirement is finalized and emitted
as soon as all 9 of its judges return — no 20-minute blank wait. The assembled
scorecard is byte-for-byte the shape `produce_scorecard.py` wrote, so the
existing dashboard consumes it unchanged.
"""

from __future__ import annotations

import collections
import os
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable, Iterator

from analyst_agent.assess import _judge, _review
from analyst_agent.ingest import ingest_file
from analyst_agent.ingest.dispatch import MARKDOWN_EXTENSIONS
from analyst_agent.ingest.model import SourceItem
from analyst_agent.llm.client import AgentServerClient
from analyst_agent.score import check_requirement
from analyst_agent.score.characteristics import CHARACTERISTICS
from analyst_agent.score.setlevel import assess_set
from analyst_agent.segment.gate import ACCEPTED, gate_requirements
from analyst_agent.ingest.ingestion_client import parse_document
from analyst_agent.segment.pipeline import segment_items

REVIEW_IF_MIN_SCORE_AT_MOST = 3
WORKERS = 8


@dataclass
class JobOptions:
    review: bool = True          # run the Reviewer on defective requirements
    set_level: bool = True       # run C10–C15 set analysis
    workers: int = WORKERS


def _ingest(path: str) -> list[SourceItem]:
    # Markdown is structured text — parse it in-process (line maps give real
    # char-span provenance). Binary/structured formats go to the shared
    # ingestion-server (:8700) via `strategy=structural`; the Analyst carries no
    # Docling of its own.
    ext = os.path.splitext(path)[1].lower()
    if ext in MARKDOWN_EXTENSIONS:
        return ingest_file(path, source_file=os.path.basename(path)).items
    return parse_document(path)


def _record(req) -> dict:
    """Empty per-requirement record in the scorecard shape."""
    p = req.provenance
    return {
        "req_id": req.req_id, "text": req.text,
        "provenance": {"source_file": p.source_file, "page": p.page, "bbox": p.bbox,
                       "section_path": p.section_path,
                       "char_span": list(p.char_span) if p.char_span else None},
        "lineage": {"origin": req.origin, "was_compound": req.was_compound,
                    "derived_from": req.derived_from, "duplicate_of": req.duplicate_of},
        "characteristics": {}, "deterministic_findings": [],
        "overall": None, "review": None, "_order": p.order,
    }


def _finalize_scores(rec: dict) -> None:
    """Compute `overall` plus the judge-health counters.

    `overall` averages only the judges that answered. That is the right mean, but
    on its own it is indistinguishable from a fully-judged one — so `judges_ok`/
    `judges_total` travel with it and a partial result is never mistaken for
    complete. Filtering is `is not None`, not truthiness: a literal 0 is a score.
    """
    rec["deterministic_findings"] = [f.to_dict() for f in check_requirement(rec["text"])]
    vs = [s for c, _, _ in CHARACTERISTICS
          if (s := rec["characteristics"][c].get("score")) is not None]
    rec["overall"] = round(statistics.mean(vs), 2) if vs else None
    rec["judges_ok"] = len(vs)
    rec["judges_total"] = len(CHARACTERISTICS)


def _needs_review(rec: dict) -> bool:
    """Review when any judge is defective OR failed. A failed judge is unknown,
    not fine — treating None as 5 would hide exactly the requirements whose
    scores are least trustworthy."""
    for c, _, _ in CHARACTERISTICS:
        score = rec["characteristics"][c].get("score")
        if score is None or score <= REVIEW_IF_MIN_SCORE_AT_MOST:
            return True
    return False


def _aggregates(records: list[dict]) -> dict:
    per_char = {}
    for c, _, _ in CHARACTERISTICS:
        vs = [s for r in records
              if (s := r["characteristics"].get(c, {}).get("score")) is not None]
        if vs:
            per_char[c] = round(statistics.mean(vs), 2)
    rule_counts: collections.Counter = collections.Counter()
    for r in records:
        rs: set[str] = set()
        for c, _, _ in CHARACTERISTICS:
            rs.update(r["characteristics"].get(c, {}).get("rules_triggered") or [])
        for f in r["deterministic_findings"]:
            rs.add(f["rule_id"])
        for rid in rs:
            rule_counts[rid] += 1
    dist = collections.Counter(round(r["overall"]) for r in records
                               if r["overall"] is not None)
    incomplete = [r["req_id"] for r in records
                  if r.get("judges_ok", len(CHARACTERISTICS)) < len(CHARACTERISTICS)]
    return {"per_characteristic_mean": per_char,
            "per_rule_violation_count": dict(rule_counts.most_common()),
            "score_distribution": dict(sorted(dist.items())),
            "total": len(records),
            # Non-empty means some scores are means over fewer than 9 judges.
            "incompletely_judged": incomplete}


def _score_and_assemble(records: list[dict], opts: JobOptions,
                        client: AgentServerClient, t0: float, head: dict,
                        should_cancel=None) -> Iterator[dict]:
    """Score (req × 9 judges) → review defectives → set-level → aggregates → scorecard.
    Shared by the single-document (`iter_job`) and project (`iter_project_job`) runs.
    `head` seeds the scorecard (e.g. `source_file`, `documents`). `should_cancel` is an
    optional zero-arg predicate polled between items so a long run can be aborted; when it
    trips, in-flight judges are left to drain and queued ones are cancelled, and the run
    ends with a `cancelled` event (no scorecard)."""
    cancelled = lambda: bool(should_cancel and should_cancel())  # noqa: E731
    n = len(records)
    total_tasks = n * len(CHARACTERISTICS)
    remaining = [len(CHARACTERISTICS)] * n
    yield {"type": "stage", "stage": "score", "status": "start", "done": 0, "total": total_tasks}

    completed_reqs = 0
    ex = ThreadPoolExecutor(max_workers=opts.workers)
    try:
        fut_meta = {ex.submit(_judge, client, cid, suffix, records[i]["text"]): (i, cid)
                    for i in range(n)
                    for cid, suffix, _ in CHARACTERISTICS}
        for fut in as_completed(fut_meta):
            if cancelled():
                break
            i, cid = fut_meta[fut]
            records[i]["characteristics"][cid] = fut.result()
            remaining[i] -= 1
            if remaining[i] == 0:
                _finalize_scores(records[i])
                completed_reqs += 1
                rec = {k: v for k, v in records[i].items() if k != "_order"}
                yield {"type": "requirement", "data": rec, "scored": completed_reqs, "total": n}
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    if cancelled():
        yield {"type": "cancelled", "stage": "score"}
        return
    yield {"type": "stage", "stage": "score", "status": "done",
           "done": total_tasks, "total": total_tasks, "message": f"{n} requirements scored"}

    if opts.review:
        to_review = [i for i in range(n) if _needs_review(records[i])]
        yield {"type": "stage", "stage": "review", "status": "start", "done": 0, "total": len(to_review)}
        reviewed = 0
        ex = ThreadPoolExecutor(max_workers=opts.workers)
        try:
            fut_i = {ex.submit(_review, client, records[i]["text"],
                               [records[i]["characteristics"][c] for c, _, _ in CHARACTERISTICS],
                               records[i]["deterministic_findings"]): i
                     for i in to_review}
            for fut in as_completed(fut_i):
                if cancelled():
                    break
                i = fut_i[fut]
                records[i]["review"] = fut.result()
                reviewed += 1
                yield {"type": "review_result", "req_id": records[i]["req_id"],
                       "data": records[i]["review"], "done": reviewed, "total": len(to_review)}
        finally:
            ex.shutdown(wait=False, cancel_futures=True)
        if cancelled():
            yield {"type": "cancelled", "stage": "review"}
            return
        yield {"type": "stage", "stage": "review", "status": "done",
               "done": len(to_review), "total": len(to_review)}

    set_block = {"overlaps": [], "set_assessment": []}
    if opts.set_level:
        if cancelled():
            yield {"type": "cancelled", "stage": "set_level"}
            return
        yield {"type": "stage", "stage": "set_level", "status": "start"}
        set_block = assess_set([{"id": r["req_id"], "text": r["text"]} for r in records], client=client)
        yield {"type": "set_level", "data": set_block}
        yield {"type": "stage", "stage": "set_level", "status": "done",
               "message": f"{len(set_block.get('overlaps', []))} overlaps"}

    aggregates = _aggregates(records)
    yield {"type": "aggregates", "data": aggregates}
    scorecard = {
        **head,
        "produced_in_s": round(time.time() - t0),
        "requirements": [{k: v for k, v in r.items() if k != "_order"} for r in records],
        "set_level": set_block,
        "aggregates": aggregates,
        "characteristic_names": {c: name for c, _, name in CHARACTERISTICS},
    }
    yield {"type": "scorecard", "data": scorecard}


def iter_project_job(docs: list[dict], source_file: str,
                     options: JobOptions | None = None,
                     client: AgentServerClient | None = None,
                     should_cancel=None) -> Iterator[dict]:
    """Project (multi-document) quality run. Ingest+segment+gate EACH document,
    tag every requirement with its source document (traceability), merge, then
    score the combined set — set-level (overlaps/C10–C15) runs across ALL documents.
    `docs` = [{"path", "source_file", "document_id"}]; `source_file` = the run label."""
    opts = options or JobOptions()
    client = client or AgentServerClient()
    t0 = time.time()
    records: list[dict] = []
    seen: dict[str, int] = {}                       # req_id collisions across documents

    # Ingest → segment → gate emit as DISTINCT stages (like the single-doc run) so a long
    # pre-scoring phase shows what it's doing ("Reading documents" → "Segmenting" →
    # "Filtering") instead of one silent "Reading documents". Docling ingest and the
    # segment/gate batch calls have no per-item hook, so these phases show a label +
    # elapsed (no bar); scoring keeps its real per-requirement bar + ETA.
    ndocs = len(docs)
    total_pages = 0
    for di, d in enumerate(docs):
        dn = d["source_file"]
        pre = f"document {di + 1}/{ndocs}: {dn} — " if ndocs > 1 else ""
        if should_cancel and should_cancel():
            yield {"type": "cancelled", "stage": "ingest"}
            return
        yield {"type": "stage", "stage": "ingest", "status": "start",
               "message": f"{pre}reading{'' if ndocs > 1 else ' ' + dn}…".strip()}
        items = _ingest(d["path"])
        pages = max((it.page or 0 for it in items), default=0)
        total_pages += pages
        yield {"type": "stage", "stage": "segment", "status": "start",
               "message": f"{pre}{pages} page(s) read — identifying requirements…"}
        reqs_all = segment_items(items, client=client)
        primaries = [r for r in reqs_all if r.duplicate_of is None]
        src_by_order = {it.order: it.text for it in items}
        yield {"type": "stage", "stage": "gate", "status": "start",
               "message": f"{pre}{len(primaries)} candidate(s) — filtering non-requirements…"}
        gated = gate_requirements(primaries, src_by_order, client=client)
        accepted = [g.requirement for g in gated if g.disposition == ACCEPTED]
        for r in accepted:
            rec = _record(r)
            rid = rec["req_id"]
            if rid in seen:                         # keep ids unique across the project set
                seen[rid] += 1
                rid = f"{rid}#{seen[rid]}"
                rec["req_id"] = rid
            else:
                seen[rid] = 0
            rec["provenance"]["source_document_id"] = d["document_id"]
            rec["provenance"]["source_document"] = d["source_file"]
            rec["_order"] = di * 100000 + (rec.get("_order") or 0)
            records.append(rec)
    # Per-document completion is implicit in the stage transitions above; this closes the
    # pre-scoring phase with a summary (kept on the last stage label to avoid a bar flash).
    yield {"type": "stage", "stage": "gate", "status": "done", "pages": total_pages,
           "message": f"{len(records)} requirement(s) from {ndocs} document(s), {total_pages} page(s)"}

    head = {"source_file": source_file,
            "documents": [{"document_id": d["document_id"], "filename": d["source_file"]} for d in docs]}
    yield from _score_and_assemble(records, opts, client, t0, head, should_cancel=should_cancel)


def iter_job(path: str, options: JobOptions | None = None,
             client: AgentServerClient | None = None,
             source_file: str | None = None, should_cancel=None) -> Iterator[dict]:
    """Run the full pipeline for one document, yielding events as it goes.

    `source_file` is the display name recorded in the scorecard (the original
    upload filename). Defaults to the basename of `path` — but when the caller
    stored the upload under a synthetic name (e.g. `source.md`), it should pass
    the real filename so the dashboard/library shows it."""
    opts = options or JobOptions()
    client = client or AgentServerClient()
    t0 = time.time()
    source_file = source_file or os.path.basename(path)

    # 1. Ingest
    yield {"type": "stage", "stage": "ingest", "status": "start"}
    items = _ingest(path)
    yield {"type": "stage", "stage": "ingest", "status": "done",
           "done": len(items), "total": len(items), "message": f"{len(items)} items"}

    # 2. Segment
    yield {"type": "stage", "stage": "segment", "status": "start"}
    reqs_all = segment_items(items, client=client)
    primaries = [r for r in reqs_all if r.duplicate_of is None]
    yield {"type": "stage", "stage": "segment", "status": "done",
           "done": len(primaries), "total": len(reqs_all),
           "message": f"{len(reqs_all)} identified, {len(primaries)} primary"}

    # 3. Gate
    yield {"type": "stage", "stage": "gate", "status": "start"}
    src_by_order = {it.order: it.text for it in items}
    gated = gate_requirements(primaries, src_by_order, client=client)
    accepted = [g.requirement for g in gated if g.disposition == ACCEPTED]
    yield {"type": "stage", "stage": "gate", "status": "done",
           "done": len(accepted), "total": len(primaries),
           "message": f"{len(accepted)} accepted"}

    # 4-7. Score → review → set-level → scorecard (shared with the project run).
    records = [_record(r) for r in accepted]
    yield from _score_and_assemble(records, opts, client, t0, {"source_file": source_file},
                                   should_cancel=should_cancel)


def run_job(path: str, emit: Callable[[dict], None] | None = None,
            options: JobOptions | None = None,
            client: AgentServerClient | None = None,
            source_file: str | None = None) -> dict:
    """Run the job to completion, forwarding each event to `emit`, and return
    the assembled scorecard. Convenience wrapper over `iter_job` for callers
    that want a callback + final result rather than a generator."""
    scorecard: dict = {}
    for event in iter_job(path, options=options, client=client, source_file=source_file):
        if event.get("type") == "scorecard":
            scorecard = event["data"]
        if emit is not None:
            emit(event)
    return scorecard
