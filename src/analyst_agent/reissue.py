"""Reissue — reconstruct a corrected, content-complete requirements specification.

A review changes requirement text; the reissue is the clean document that reflects those
corrections, so a stakeholder can read the fixed spec rather than diff a scorecard. Per the
decisions in `documents/technical_architecture.md` §7 it is a **content-complete
replacement**, not a facsimile: the ORIGINAL layout/branding is not reproduced. What is
preserved is the content and its structure — requirements grouped by their source section
(`provenance.section_path`), in document order, using the released (`final_text`) wording.

Three renderings share one reconstruction:
  build_markdown → a portable .md
  build_html     → a styled, print-ready HTML
  build_pdf      → that HTML through WeasyPrint (a real, selectable-text PDF, not a raster)

Source of truth is the Architect package, so the reissue can never disagree with what is
handed over. Nothing here mutates state.
"""
from __future__ import annotations

import html as _html

from analyst_agent import package as package_mod


def _grouped(records: list[dict]) -> list[tuple[str, list[dict]]]:
    """Requirements grouped by source section, preserving first-seen (document) order."""
    order: list[str] = []
    groups: dict[str, list[dict]] = {}
    for r in records:
        sec = ((r.get("provenance") or {}).get("section_path") or "General").strip() or "General"
        if sec not in groups:
            groups[sec] = []
            order.append(sec)
        groups[sec].append(r)
    return [(s, groups[s]) for s in order]


def _problem_text(ps: dict | None) -> str:
    """Best-effort plain text for the problem statement, whatever shape it is stored in."""
    if not ps:
        return ""
    st = ps.get("statement", ps)
    if isinstance(st, str):
        return st.strip()
    if isinstance(st, dict):
        for k in ("summary", "problem", "statement", "description", "text"):
            v = st.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return ""


def _meta_line(m: dict) -> str:
    c = m.get("counts", {})
    bits = [f"Status: {m.get('release_status', 'draft')}"]
    if m.get("released_by"):
        bits.append(f"approved by {m['released_by']}")
    bits.append(f"{c.get('total', 0)} requirements")
    if c.get("mean_score") is not None:
        bits.append(f"mean quality {c['mean_score']}/5")
    return " · ".join(bits)


def build_markdown(pid: str, run_id: str | None = None) -> str | None:
    pkg = package_mod.build_package(pid, run_id)
    if not pkg:
        return None
    m = pkg["manifest"]
    out = [f"# {m.get('project_name') or 'Requirements'} — Requirements Specification (Reissued)",
           "", f"_{_meta_line(m)}_", ""]
    problem = _problem_text(pkg.get("problem_statement"))
    if problem:
        out += ["## Problem Statement", "", problem, ""]
    out += ["## Requirements", ""]
    for section, reqs in _grouped(pkg["requirements"]):
        out += [f"### {section}", ""]
        for r in reqs:
            labels = []
            if r.get("classes"):
                labels.append("Classes: " + ", ".join(r["classes"]))
            if r.get("type"):
                labels.append("Type: " + r["type"])
            if r.get("constraints"):
                labels.append("Constraints: " + ", ".join(r["constraints"]))
            out += [f"**{r.get('req_id')}** — {r.get('text', '').strip()}"]
            if labels:
                out.append("  \n_" + " · ".join(labels) + "_")
            out.append("")
    return "\n".join(out)


def build_html(pid: str, run_id: str | None = None) -> str | None:
    pkg = package_mod.build_package(pid, run_id)
    if not pkg:
        return None
    m = pkg["manifest"]
    e = _html.escape
    title = e(m.get("project_name") or "Requirements")
    parts = [f"<h1>{title}</h1>",
             f"<div class='sub'>Requirements Specification (Reissued)</div>",
             f"<div class='meta'>{e(_meta_line(m))}</div>"]
    problem = _problem_text(pkg.get("problem_statement"))
    if problem:
        parts.append(f"<h2>Problem Statement</h2><p class='ps'>{e(problem)}</p>")
    parts.append("<h2>Requirements</h2>")
    for section, reqs in _grouped(pkg["requirements"]):
        parts.append(f"<h3>{e(section)}</h3>")
        for r in reqs:
            labels = []
            if r.get("classes"):
                labels.append("Classes: " + e(", ".join(r["classes"])))
            if r.get("type"):
                labels.append("Type: " + e(r["type"]))
            if r.get("constraints"):
                labels.append("Constraints: " + e(", ".join(r["constraints"])))
            meta = f"<div class='rmeta'>{' · '.join(labels)}</div>" if labels else ""
            parts.append(f"<div class='req'><span class='rid'>{e(r.get('req_id') or '')}</span>"
                         f"<span class='rtext'>{e((r.get('text') or '').strip())}</span>{meta}</div>")
    body = "\n".join(parts)
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
@page {{ size: A4; margin: 22mm 18mm; @bottom-right {{ content: counter(page) ' / ' counter(pages); font-size: 9px; color: #999; }} }}
body {{ font-family: 'DejaVu Sans', Helvetica, Arial, sans-serif; color: #1a1a1a; font-size: 11px; line-height: 1.5; }}
h1 {{ font-size: 22px; margin: 0 0 2px; }}
.sub {{ font-size: 13px; color: #555; margin-bottom: 6px; }}
.meta {{ font-size: 10px; color: #888; border-bottom: 1px solid #ddd; padding-bottom: 10px; margin-bottom: 14px; }}
h2 {{ font-size: 15px; margin: 18px 0 8px; border-bottom: 1px solid #eee; padding-bottom: 3px; }}
h3 {{ font-size: 12.5px; margin: 14px 0 6px; color: #34495e; }}
.ps {{ color: #333; }}
.req {{ margin: 0 0 9px; padding-left: 8px; border-left: 2px solid #e2e6ea; page-break-inside: avoid; }}
.rid {{ font-family: 'DejaVu Sans Mono', monospace; font-weight: bold; font-size: 10px; color: #2c6; margin-right: 8px; }}
.rid {{ color: #226; }}
.rtext {{ }}
.rmeta {{ font-size: 9px; color: #999; margin-top: 2px; }}
</style></head><body>{body}</body></html>"""


def build_pdf(pid: str, run_id: str | None = None) -> bytes | None:
    """Render the reissue to a real PDF via WeasyPrint. Imported lazily so the rest of the
    service runs even if the (heavy) PDF stack is unavailable."""
    html_str = build_html(pid, run_id)
    if html_str is None:
        return None
    from weasyprint import HTML  # lazy: keeps import cost off the hot path
    return HTML(string=html_str).write_pdf()
