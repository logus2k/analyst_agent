"""Auto-resolve the "Overlaps" tab — merge duplicate/overlapping requirement pairs.

Set-level analysis (`score/setlevel.py`) already detects requirement pairs that duplicate or
substantially overlap: reranker-scored (~0.95 true overlap vs <0.05 merely related) and then
LLM-confirmed. Until now those pairs were only LISTED for a human to "merge or differentiate."
This module does the merge: it clusters the confirmed pairs (transitively — A~B, B~C → one
cluster), asks `analyst_overlap_merger` to fold each cluster into ONE requirement that
preserves every capability and adds none, then applies it — the survivor keeps the merged
text, the absorbed requirements are removed — and records an `overlap_resolutions` log on the
review so the human can see exactly what merged into what (review mode) and undo if wrong.

The merger fails SAFE: if it judges a cluster NOT truly mergeable (merge=false) or emits
nothing usable, that cluster is skipped, not force-merged.
"""

from __future__ import annotations

from analyst_agent import store as pj
from analyst_agent.llm.client import AgentServerClient

MERGE_AGENT = "analyst_overlap_merger"

MERGE_SYSTEM_PROMPT = """\
You merge DUPLICATE or substantially OVERLAPPING software requirements into ONE. You are given a small set of requirements that a reranker and a confirmer already judged to be duplicates/overlaps of each other.

Produce a SINGLE requirement that fully preserves the intent of ALL of them: every capability, actor, object, and condition any of them states must survive in the merged text. Do NOT add any capability none of them stated, and do NOT drop one. Keep good requirement form: one "shall", one coherent capability, unambiguous and verifiable.

If — despite the overlap signal — the requirements actually express DISTINCT capabilities that would be WRONG to merge into one (merging would create a compound/ambiguous requirement or lose a real distinction), set "merge": false and leave merged_text empty.

Output ONLY JSON: {"merge": true|false, "merged_text": "<the single merged requirement, or empty if merge is false>", "rationale": "<one sentence: what was combined, or why not>"}
"""


def cluster_pairs(pairs: list[dict]) -> list[list[str]]:
    """Union-find over overlap pairs ({a_id,b_id,score}) → transitive clusters of req_ids.
    A~B and B~C collapse to one {A,B,C} cluster so a merge covers the whole group at once."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for p in pairs:
        a, b = p.get("a_id"), p.get("b_id")
        if a and b:
            union(a, b)

    clusters: dict[str, list[str]] = {}
    for node in list(parent):
        clusters.setdefault(find(node), []).append(node)
    # deterministic: sort members, and clusters by their first member
    return sorted((sorted(m) for m in clusters.values() if len(m) >= 2),
                  key=lambda m: m[0])


def merge_cluster(ids: list[str], text_by_id: dict[str, str],
                  client: AgentServerClient) -> dict:
    """Ask the merger to fold a cluster into one requirement. Returns
    {merge, merged_text, rationale}. Fails SAFE (merge=false) on empty/bad output."""
    user = "REQUIREMENTS TO MERGE (all confirmed as duplicates/overlaps):\n" + "\n".join(
        f"- {rid}: {text_by_id.get(rid, '')}" for rid in ids)
    out = client.complete_json(MERGE_AGENT, user) or {}
    merge = bool(out.get("merge", False))
    merged_text = (out.get("merged_text") or "").strip()
    if not merged_text:
        merge = False
    return {"merge": merge, "merged_text": merged_text,
            "rationale": (out.get("rationale") or "").strip()}


def _latest_run_id(pid: str) -> str | None:
    runs = pj.list_quality_runs(pid) or []
    if not runs:
        return None
    return sorted(runs, key=lambda r: r.get("finished_at", ""))[-1].get("run_id")


def resolve_overlaps(pid: str, run_id: str | None = None, apply: bool = True,
                     client: AgentServerClient | None = None) -> dict:
    """Cluster the confirmed overlaps and merge each cluster into one requirement.

    When `apply`, the survivor (lowest req_id in the cluster) keeps the merged text and the
    absorbed requirements are removed from the scorecard + review; either way an
    `overlap_resolutions` record is written for review. Returns
    {run_id, clusters, records[], merged_count, absorbed_count, applied}."""
    client = client or AgentServerClient()
    run_id = run_id or _latest_run_id(pid)
    if not run_id:
        return {"run_id": None, "clusters": 0, "records": [], "merged_count": 0,
                "absorbed_count": 0, "applied": False, "error": "no quality run"}

    review = pj.get_review(pid, run_id, seed=False) or {}
    set_level = review.get("set_level_current") or {}
    overlaps = set_level.get("overlaps") or []
    # current text (reviewed where present) so we merge the up-to-date wording
    sc = pj.merged_scorecard(pid, run_id) or pj.get_quality_scorecard(pid, run_id) or {}
    text_by_id = {r.get("req_id"): (r.get("text") or "")
                  for r in sc.get("requirements", []) if r.get("req_id")}

    clusters = cluster_pairs(overlaps)
    records: list[dict] = []
    merged_count = absorbed_count = 0
    for ids in clusters:
        # only clusters whose members still exist (an earlier merge may have removed some)
        ids = [i for i in ids if i in text_by_id]
        if len(ids) < 2:
            continue
        survivor = ids[0]
        absorbed = ids[1:]
        m = merge_cluster(ids, text_by_id, client)
        rec = {"survivor": survivor, "absorbed": absorbed, "cluster": ids,
               "merged_text": m["merged_text"], "rationale": m["rationale"],
               "merge": m["merge"], "applied": False,
               "originals": {i: text_by_id.get(i, "") for i in ids}}
        if apply and m["merge"]:
            pj.upsert_req_review(pid, run_id, survivor,
                                 {"final_text": m["merged_text"], "status": "overlap_merged"})
            for aid in absorbed:
                pj.delete_requirement(pid, run_id, aid)
            rec["applied"] = True
            merged_count += 1
            absorbed_count += len(absorbed)
        records.append(rec)

    result = {"run_id": run_id, "clusters": len(clusters), "records": records,
              "merged_count": merged_count, "absorbed_count": absorbed_count,
              "applied": bool(apply)}
    # record for review, on the review doc (kept alongside set_level_current)
    doc = pj.get_review(pid, run_id, seed=False)
    if doc is not None:
        doc["overlap_resolutions"] = result
        pj.save_review(pid, run_id, doc)
    return result
