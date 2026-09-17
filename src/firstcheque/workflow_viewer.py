"""Generate workflow.html: an offline node-and-connector map of the
FirstCheque workflow, paired with a saved-run inspector, built entirely
from real data under memos/.

This module does not call an LLM and does not run the workflow. It reads
whatever is actually saved (memo.json, claim_checks.json, sources.json,
run.json, events.jsonl, review.md) for every run it finds, and embeds that
data as JSON directly in the generated HTML -- so opening workflow.html
via a file:// URL works with no fetch() calls and no server, and a run
that never happened cannot show up as if it had.

Regenerate with: `python -m firstcheque.cli generate-workflow-viewer`
"""

from __future__ import annotations

import html
import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from . import storage
from .schema import ClaimCheckResult, Memo, Source, citation_support_rate
from .stages import STAGES, STAGES_BY_ID

_RUN_DIR_RE = re.compile(r"^run-(\d{3,})$")


# ---------------------------------------------------------------------------
# Discovery: walk memos/ for real runs, load only what's actually there.
# ---------------------------------------------------------------------------


def _json_default(obj):
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    return str(obj)


def _model_dump(obj):
    return json.loads(json.dumps(obj.model_dump(mode="json"), default=_json_default))


def _review_status(run_dir: Path) -> str:
    review_path = run_dir / "review.md"
    if not review_path.exists():
        return "missing"
    text = review_path.read_text(encoding="utf-8")
    return "blank" if storage.is_review_blank(text) else "completed"


def _rel(run_dir: Path, filename: str) -> Optional[str]:
    path = run_dir / filename
    if not path.exists():
        return None
    return str(path).replace("\\", "/")


def _load_run(run_dir: Path, company_slug: str, previous_run_dir: Optional[Path]) -> dict:
    memo_data = storage.read_json(run_dir / "memo.json")
    memo: Optional[Memo] = None
    memo_error: Optional[str] = None
    if memo_data is not None:
        try:
            memo = Memo.model_validate(memo_data)
        except Exception as exc:  # noqa: BLE001 -- surface any schema error to the viewer, don't hide it
            memo_error = str(exc)

    sources = storage.load_sources(run_dir)
    checks = storage.load_claim_checks(run_dir)
    run_meta = storage.load_run_meta(run_dir)
    events = storage.load_events(run_dir)

    citation = {"supported": 0, "denominator": 0, "rate": None}
    if memo is not None:
        supported, denominator, rate = citation_support_rate(memo.claims)
        citation = {"supported": supported, "denominator": denominator, "rate": rate}

    changes_path = run_dir / "changes.md"

    return {
        "id": f"{company_slug}/{run_dir.name}",
        "company_slug": company_slug,
        "run_id": run_dir.name,
        "company_display": memo.company if memo else company_slug,
        "has_previous": previous_run_dir is not None,
        "previous_id": f"{company_slug}/{previous_run_dir.name}" if previous_run_dir else None,
        "memo": _model_dump(memo) if memo else None,
        "memo_error": memo_error,
        "citation": citation,
        "claim_checks": {name: _model_dump(r) for name, r in checks.items()},
        "sources": [_model_dump(s) for s in sources],
        "run_meta": _model_dump(run_meta) if run_meta else None,
        "events": events,
        "review_status": _review_status(run_dir),
        "has_previous_review_file": (run_dir / "previous_review.md").exists(),
        "changes_md": changes_path.read_text(encoding="utf-8") if changes_path.exists() else None,
        "paths": {
            "memo_html": _rel(run_dir, "memo.html"),
            "memo_md": _rel(run_dir, "memo.md"),
            "memo_json": _rel(run_dir, "memo.json"),
            "sources_json": _rel(run_dir, "sources.json"),
            "claim_checks_json": _rel(run_dir, "claim_checks.json"),
            "review_md": _rel(run_dir, "review.md"),
            "previous_review_md": _rel(run_dir, "previous_review.md"),
            "changes_md": _rel(run_dir, "changes.md"),
            "run_json": _rel(run_dir, "run.json"),
            "events_jsonl": _rel(run_dir, "events.jsonl"),
        },
    }


def discover_runs(root: Path = storage.MEMOS_ROOT) -> list[dict]:
    """Every real run under `root`, one dict per run-NNN directory,
    ordered by company slug then run number. Nothing here is invented:
    a company with zero runs simply contributes nothing, and a run
    missing an artifact just has `None`/empty for that field."""
    runs: list[dict] = []
    if not root.exists():
        return runs
    for company_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        run_dirs = sorted(
            (p for p in company_dir.iterdir() if p.is_dir() and _RUN_DIR_RE.match(p.name)),
            key=lambda p: int(_RUN_DIR_RE.match(p.name).group(1)),
        )
        for i, run_dir in enumerate(run_dirs):
            previous = run_dirs[i - 1] if i > 0 else None
            runs.append(_load_run(run_dir, company_dir.name, previous))
    return runs


# ---------------------------------------------------------------------------
# Diagram layout -- hand-placed coordinates for a small, fixed set of
# stages. This is a real n8n-style canvas (free node positions + explicit
# connectors), not an auto-layout algorithm; with 11 nodes that is more
# legible than anything generic.
# ---------------------------------------------------------------------------

NODE_W, NODE_H = 200, 96

# stage_id -> (x, y, [label lines])
LAYOUT: dict[str, tuple[int, int, list[str]]] = {
    "company_input": (20, 40, ["Company", "Input"]),
    "start_run": (260, 40, ["Identify /", "Start Run"]),
    "plan_research": (500, 40, ["Plan Targeted", "Research"]),
    "collect_evidence": (740, 40, ["Collect & Save", "Evidence"]),
    "extract_draft": (980, 40, ["Structured", "Extraction"]),
    "citation_check": (1220, 40, ["Citation-Support", "Check"]),
    "follow_up_pass": (1220, 260, ["Follow-Up Pass", "(if gaps)"]),
    "compute_claims": (980, 260, ["Python Claim", "Checks"]),
    "render_memo": (740, 260, ["Render", "Memo"]),
    "human_review": (500, 260, ["Analyst", "Review"]),
    "compare_previous_run": (260, 260, ["Compare With", "Previous Run"]),
}

VIEWBOX = "-20 -20 1480 420"

EDGES: list[dict] = [
    {"from": "company_input", "to": "start_run"},
    {"from": "start_run", "to": "plan_research"},
    {"from": "plan_research", "to": "collect_evidence"},
    {"from": "collect_evidence", "to": "extract_draft"},
    {"from": "extract_draft", "to": "citation_check"},
    {"from": "citation_check", "to": "follow_up_pass", "label": "if gaps found", "port": "vertical"},
    {"from": "citation_check", "to": "compute_claims", "label": "if no gaps", "port": "diagonal"},
    {
        "from": "follow_up_pass",
        "to": "collect_evidence",
        "label": "re-collect evidence, once",
        "port": "loopback",
        "dashed": True,
    },
    {"from": "follow_up_pass", "to": "compute_claims"},
    {"from": "compute_claims", "to": "render_memo"},
    {"from": "render_memo", "to": "human_review"},
    {
        "from": "human_review",
        "to": "compare_previous_run",
        "label": "next run, same company",
        "conditional": True,
        "dashed": True,
    },
]

EXECUTOR_CLASS = {"claude": "exec-claude", "python": "exec-python", "human": "exec-human"}
EXECUTOR_LABEL = {"claude": "Claude Code", "python": "Python", "human": "You"}


def _port(x: int, y: int, side: str) -> tuple[int, int]:
    if side == "top":
        return (x + NODE_W // 2, y)
    if side == "bottom":
        return (x + NODE_W // 2, y + NODE_H)
    if side == "left":
        return (x, y + NODE_H // 2)
    return (x + NODE_W, y + NODE_H // 2)  # right


def _edge_path(edge: dict) -> tuple[str, tuple[int, int]]:
    """Returns (svg path d attribute, label anchor point)."""
    fx, fy, _ = LAYOUT[edge["from"]]
    tx, ty, _ = LAYOUT[edge["to"]]
    kind = edge.get("port", "horizontal")

    if kind == "vertical":
        x1, y1 = _port(fx, fy, "bottom")
        x2, y2 = _port(tx, ty, "top")
        return f"M{x1},{y1} L{x2},{y2}", ((x1 + x2) // 2 + 14, (y1 + y2) // 2)

    if kind == "diagonal":
        x1, y1 = _port(fx, fy, "bottom")
        x2, y2 = _port(tx, ty, "top")
        return f"M{x1},{y1} L{x2},{y2}", ((x1 + x2) // 2 - 70, (y1 + y2) // 2)

    if kind == "loopback":
        x1, y1 = _port(fx, fy, "top")
        x2, y2 = _port(tx, ty, "top")
        top_y = -10
        return f"M{x1},{y1} L{x1},{top_y} L{x2},{top_y} L{x2},{y2}", ((x1 + x2) // 2, top_y - 8)

    # horizontal: direction depends on relative x (row 2 flows right-to-left)
    if fx > tx:
        x1, y1 = _port(fx, fy, "left")
        x2, y2 = _port(tx, ty, "right")
    else:
        x1, y1 = _port(fx, fy, "right")
        x2, y2 = _port(tx, ty, "left")
    return f"M{x1},{y1} L{x2},{y2}", ((x1 + x2) // 2, y1 - 12)


def _e(text) -> str:
    return html.escape(str(text), quote=True)


def _build_svg() -> str:
    parts = [
        f'<svg id="workflow-svg" viewBox="{VIEWBOX}" xmlns="http://www.w3.org/2000/svg">',
        "<defs>",
        '<marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">',
        '<path d="M0,0 L10,5 L0,10 z" class="arrowhead"/>',
        "</marker>",
        "</defs>",
        '<g id="edges">',
    ]
    for edge in EDGES:
        d, (lx, ly) = _edge_path(edge)
        dash_cls = " edge-dashed" if edge.get("dashed") else ""
        cond_attr = ' data-conditional="1"' if edge.get("conditional") else ""
        parts.append(f'<path d="{d}" class="edge{dash_cls}" marker-end="url(#arrow)"{cond_attr}></path>')
        if edge.get("label"):
            parts.append(
                f'<text x="{lx}" y="{ly}" class="edge-label">{_e(edge["label"])}</text>'
            )
    parts.append("</g>")

    parts.append('<g id="nodes">')
    for stage in STAGES:
        if stage.id not in LAYOUT:
            continue
        x, y, lines = LAYOUT[stage.id]
        exec_classes = " ".join(EXECUTOR_CLASS[e] for e in stage.executors)
        chips = "".join(
            f'<span class="chip {EXECUTOR_CLASS[e]}">{_e(EXECUTOR_LABEL[e])}</span>' for e in stage.executors
        )
        text_lines = "".join(
            f'<tspan x="{x + NODE_W // 2}" dy="{0 if i == 0 else 18}">{_e(line)}</tspan>'
            for i, line in enumerate(lines)
        )
        conditional_mark = ' data-conditional-node="1"' if stage.conditional else ""
        parts.append(
            f'<g class="node {exec_classes}" data-stage="{stage.id}" tabindex="0" role="button" '
            f'aria-label="{_e(stage.label)}"{conditional_mark}>'
            f'<foreignObject x="{x}" y="{y}" width="{NODE_W}" height="24">'
            f'<div xmlns="http://www.w3.org/1999/xhtml" class="chip-row">{chips}</div>'
            f"</foreignObject>"
            f'<rect x="{x}" y="{y + 20}" width="{NODE_W}" height="{NODE_H - 20}" rx="10" class="node-box"></rect>'
            f'<text x="{x + NODE_W // 2}" y="{y + 52}" text-anchor="middle" class="node-label">{text_lines}</text>'
            f"</g>"
        )
    parts.append("</g>")
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# HTML assembly. Uses plain token replacement, not str.format(), because
# the CSS/JS below contains far too many literal braces to escape safely.
# ---------------------------------------------------------------------------

_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FirstCheque -- Workflow Map &amp; Saved-Run Inspector</title>
<style>
  :root {
    --paper: #faf8f4; --ink: #1f1c18; --muted: #6b6459; --line: #e4ddd0;
    --accent: #2f5233; --meet: #1f7a3d; --track: #b8860b; --pass: #a33a3a;
    --claude: #3a5aa8; --python-c: #2f7a5f; --human: #b8860b;
    --panel-bg: #fff;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    margin: 0; background: var(--paper); color: var(--ink);
    font-family: -apple-system, Helvetica, Arial, sans-serif; font-size: 15px;
  }
  .topbar {
    padding: 0.9rem 1.25rem; border-bottom: 1px solid var(--line); background: #fff;
    display: flex; flex-wrap: wrap; align-items: center; gap: 1rem;
  }
  .topbar h1 {
    font-family: Georgia, serif; font-size: 1.25rem; margin: 0; flex: 0 0 auto;
  }
  .topbar .subtitle { color: var(--muted); font-size: 0.82rem; margin-top: 0.1em; }
  .topbar-controls { display: flex; align-items: center; gap: 0.6rem; margin-left: auto; flex-wrap: wrap; }
  .tabs { display: flex; gap: 0.3rem; }
  .tab-btn {
    font: inherit; padding: 0.4em 0.9em; border: 1px solid var(--line); background: #fff;
    border-radius: 6px; cursor: pointer; color: var(--ink);
  }
  .tab-btn.active { background: var(--accent); color: #fff; border-color: var(--accent); }
  select, .btn {
    font: inherit; padding: 0.4em 0.7em; border: 1px solid var(--line); border-radius: 6px;
    background: #fff; color: var(--ink); cursor: pointer;
  }
  .disclaimer {
    background: #fdf3f0; border-bottom: 1px solid #eccbc2; color: #7a3b2e;
    font-size: 0.82rem; padding: 0.5em 1.25rem;
  }
  .layout { display: flex; height: calc(100vh - 96px); }
  .canvas-wrap {
    position: relative; flex: 1 1 auto; overflow: hidden; background:
      radial-gradient(circle, #e9e3d6 1px, transparent 1px) 0 0 / 22px 22px, var(--paper);
  }
  #workflow-svg { position: absolute; top: 0; left: 0; transform-origin: 0 0; }
  .node { cursor: pointer; }
  .node-box { fill: #fff; stroke: var(--line); stroke-width: 1.5; }
  .node.exec-claude .node-box { stroke: var(--claude); }
  .node.exec-python .node-box { stroke: var(--python-c); }
  .node.exec-human .node-box { stroke: var(--human); }
  .node.selected .node-box { stroke-width: 3; filter: drop-shadow(0 0 4px rgba(47,82,51,0.35)); }
  .node-label { font-size: 13px; font-weight: 600; fill: var(--ink); }
  .chip-row { display: flex; gap: 4px; }
  .chip {
    font-size: 9px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.03em;
    padding: 2px 6px; border-radius: 999px; color: #fff; white-space: nowrap;
  }
  .chip.exec-claude { background: var(--claude); }
  .chip.exec-python { background: var(--python-c); }
  .chip.exec-human { background: var(--human); }
  .edge { fill: none; stroke: #9c9384; stroke-width: 1.6; }
  .edge-dashed { stroke-dasharray: 6,5; }
  .edge[data-conditional="1"].dim { stroke: #d8d2c4; }
  .arrowhead { fill: #9c9384; }
  .edge-label { font-size: 11px; fill: var(--muted); font-style: italic; }
  .zoom-controls { position: absolute; right: 14px; bottom: 14px; display: flex; gap: 6px; z-index: 5; }
  .legend {
    position: absolute; left: 14px; bottom: 14px; background: #fffdf9; border: 1px solid var(--line);
    border-radius: 8px; padding: 0.6em 0.9em; font-size: 0.8rem; z-index: 5; max-width: 300px;
  }
  .legend-row { display: flex; align-items: center; gap: 6px; margin: 2px 0; }
  .legend-dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
  .side-panel {
    width: 380px; flex: 0 0 auto; border-left: 1px solid var(--line); background: var(--panel-bg);
    overflow-y: auto; padding: 1.1rem 1.2rem;
  }
  .side-panel h2 { font-family: Georgia, serif; font-size: 1.1rem; margin: 0 0 0.2em; }
  .side-panel .meta-line { color: var(--muted); font-size: 0.82rem; margin-bottom: 0.9em; }
  .side-panel h3 {
    font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted);
    border-bottom: 1px solid var(--line); padding-bottom: 0.25em; margin: 1.1em 0 0.5em;
  }
  .side-panel p, .side-panel li { font-size: 0.88rem; line-height: 1.5; }
  .side-panel code { background: #f1ede3; padding: 1px 5px; border-radius: 4px; font-size: 0.82rem; }
  .badge { display: inline-block; font-size: 0.68rem; font-weight: 700; text-transform: uppercase;
    padding: 0.1em 0.55em; border-radius: 999px; color: #fff; margin-right: 4px; }
  .badge-ok { background: var(--meet); }
  .badge-error { background: var(--pass); }
  .badge-skipped { background: #999; }
  .badge-started { background: var(--track); }
  .badge-notrecorded { background: #bbb; }
  .event-row { border: 1px solid var(--line); border-radius: 6px; padding: 0.4em 0.6em; margin-bottom: 0.4em; font-size: 0.82rem; }
  .placeholder { color: var(--muted); font-style: italic; padding: 2rem 1rem; text-align: center; }
  .run-view { padding: 1.4rem 1.8rem; max-width: 900px; overflow-y: auto; height: 100%; }
  .run-view h2 { font-family: Georgia, serif; }
  .rec-pill { display: inline-block; font-weight: 700; padding: 0.25em 0.8em; border-radius: 999px; color: #fff; font-size: 0.85rem; }
  .rec-meet { background: var(--meet); } .rec-track { background: var(--track); } .rec-pass { background: var(--pass); }
  table.data-table { border-collapse: collapse; width: 100%; margin: 0.6em 0 1.2em; font-size: 0.85rem; }
  table.data-table th, table.data-table td { border: 1px solid var(--line); padding: 0.4em 0.6em; text-align: left; vertical-align: top; }
  table.data-table th { background: #f5f1e8; }
  a { color: var(--accent); }
  .hidden { display: none !important; }
  .claim-supported { color: var(--meet); font-weight: 700; }
  .claim-unsupported { color: var(--pass); font-weight: 700; }
  .source-passage { font-style: italic; color: var(--muted); }
</style>
</head>
<body>
<div class="topbar">
  <div>
    <h1>FirstCheque -- Workflow Map &amp; Saved-Run Inspector</h1>
    <div class="subtitle">This page reads saved run data. It does not execute the research workflow.</div>
  </div>
  <div class="topbar-controls">
    <div class="tabs">
      <button class="tab-btn active" data-tab="workflow">Workflow</button>
      <button class="tab-btn" data-tab="rundetails">Run details</button>
    </div>
    <select id="run-select"></select>
  </div>
</div>
<div class="disclaimer">Workflow map and saved-run inspector only -- there is no Run button here. Company research happens through the <code>/company-brief</code> skill in a Claude Code session; this page only visualises what was actually saved.</div>

<div class="layout">
  <div id="tab-workflow" class="canvas-wrap">
    __SVG__
    <div class="zoom-controls">
      <button class="btn" id="zoom-out">-</button>
      <button class="btn" id="zoom-fit">Fit</button>
      <button class="btn" id="zoom-in">+</button>
    </div>
    <div class="legend">
      <div class="legend-row"><span class="legend-dot" style="background:var(--claude)"></span> Claude Code (research, judgment, writing)</div>
      <div class="legend-row"><span class="legend-dot" style="background:var(--python-c)"></span> Python (validation, arithmetic, rendering)</div>
      <div class="legend-row"><span class="legend-dot" style="background:var(--human)"></span> You (review and final judgment)</div>
      <div class="legend-row" style="margin-top:4px;">Dashed edge = conditional / loop, runs only sometimes.</div>
      <div class="legend-row">All nodes shown are implemented today; none are planned-only.</div>
    </div>
  </div>
  <div id="tab-rundetails" class="run-view hidden"></div>
  <div id="side-panel" class="side-panel">
    <div class="placeholder">Click a node to see what it does, who runs it, and (if a run is selected) the real data from that run.</div>
  </div>
</div>

<script id="firstcheque-data" type="application/json">__DATA_JSON__</script>
<script id="firstcheque-stages" type="application/json">__STAGES_JSON__</script>
<script>
(function () {
  "use strict";
  var DATA = JSON.parse(document.getElementById("firstcheque-data").textContent);
  var STAGES = JSON.parse(document.getElementById("firstcheque-stages").textContent);
  var STAGES_BY_ID = {};
  STAGES.forEach(function (s) { STAGES_BY_ID[s.id] = s; });
  var RUNS_BY_ID = {};
  DATA.runs.forEach(function (r) { RUNS_BY_ID[r.id] = r; });

  function esc(s) {
    if (s === null || s === undefined) return "";
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function fmtPct(rate) {
    if (rate === null || rate === undefined) return "n/a";
    return Math.round(rate * 100) + "%";
  }

  // ---- run selector ----
  var select = document.getElementById("run-select");
  DATA.runs.forEach(function (r) {
    var opt = document.createElement("option");
    opt.value = r.id;
    opt.textContent = r.company_display + " -- " + r.run_id;
    select.appendChild(opt);
  });
  var defaultRun = RUNS_BY_ID["kaleidofin/run-002"] ? "kaleidofin/run-002" : (DATA.runs[0] ? DATA.runs[0].id : null);
  if (defaultRun) select.value = defaultRun;

  function selectedRun() {
    return RUNS_BY_ID[select.value] || null;
  }

  // ---- tabs ----
  var tabButtons = document.querySelectorAll(".tab-btn");
  var tabWorkflow = document.getElementById("tab-workflow");
  var tabRunDetails = document.getElementById("tab-rundetails");
  tabButtons.forEach(function (btn) {
    btn.addEventListener("click", function () {
      tabButtons.forEach(function (b) { b.classList.remove("active"); });
      btn.classList.add("active");
      var tab = btn.getAttribute("data-tab");
      tabWorkflow.classList.toggle("hidden", tab !== "workflow");
      tabRunDetails.classList.toggle("hidden", tab !== "rundetails");
      if (tab === "rundetails") renderRunDetails();
    });
  });

  // ---- pan/zoom ----
  var svg = document.getElementById("workflow-svg");
  var canvasWrap = document.getElementById("tab-workflow");
  var view = { scale: 1, tx: 40, ty: 20 };
  function applyView() {
    svg.style.transform = "translate(" + view.tx + "px," + view.ty + "px) scale(" + view.scale + ")";
  }
  function fitToScreen() {
    var vb = svg.viewBox.baseVal;
    var rect = canvasWrap.getBoundingClientRect();
    var scale = Math.min(rect.width / vb.width, rect.height / vb.height) * 0.92;
    view.scale = scale;
    view.tx = (rect.width - vb.width * scale) / 2 - vb.x * scale;
    view.ty = (rect.height - vb.height * scale) / 2 - vb.y * scale;
    applyView();
  }
  svg.setAttribute("width", "1460");
  svg.setAttribute("height", "400");
  document.getElementById("zoom-in").addEventListener("click", function () { view.scale = Math.min(2.5, view.scale * 1.2); applyView(); });
  document.getElementById("zoom-out").addEventListener("click", function () { view.scale = Math.max(0.35, view.scale / 1.2); applyView(); });
  document.getElementById("zoom-fit").addEventListener("click", fitToScreen);

  var panning = false, panStart = null, viewStart = null;
  canvasWrap.addEventListener("mousedown", function (e) {
    if (e.target.closest(".node")) return;
    panning = true; panStart = { x: e.clientX, y: e.clientY }; viewStart = { tx: view.tx, ty: view.ty };
    canvasWrap.style.cursor = "grabbing";
  });
  window.addEventListener("mousemove", function (e) {
    if (!panning) return;
    view.tx = viewStart.tx + (e.clientX - panStart.x);
    view.ty = viewStart.ty + (e.clientY - panStart.y);
    applyView();
  });
  window.addEventListener("mouseup", function () { panning = false; canvasWrap.style.cursor = "default"; });
  canvasWrap.addEventListener("wheel", function (e) {
    e.preventDefault();
    var factor = e.deltaY < 0 ? 1.1 : 0.9;
    view.scale = Math.max(0.35, Math.min(2.5, view.scale * factor));
    applyView();
  }, { passive: false });

  setTimeout(fitToScreen, 0);
  window.addEventListener("resize", fitToScreen);

  // ---- node click -> side panel ----
  var nodes = document.querySelectorAll(".node");
  var panel = document.getElementById("side-panel");

  function eventBadge(status) {
    var cls = { ok: "badge-ok", error: "badge-error", skipped: "badge-skipped", started: "badge-started" }[status] || "badge-notrecorded";
    return '<span class="badge ' + cls + '">' + esc(status) + "</span>";
  }

  function renderEvents(stageId) {
    var run = selectedRun();
    if (!run) return "<p class=\"placeholder\">No run selected.</p>";
    var events = (run.events || []).filter(function (e) { return e.stage === stageId; });
    if (events.length === 0) {
      return '<p><span class="badge badge-notrecorded">not recorded</span> This run predates or did not log an event for this stage. This is shown honestly rather than assumed.</p>';
    }
    return events.map(function (e) {
      var artifacts = (e.artifacts || []).map(esc).join(", ") || "--";
      return '<div class="event-row">' + eventBadge(e.status) + '<strong>' + esc(e.executor) + '</strong> at ' + esc(e.timestamp) +
        '<br>Artifacts: ' + artifacts + (e.notes ? '<br>Notes: ' + esc(e.notes) : '') + (e.error ? '<br>Error: ' + esc(e.error) : '') + '</div>';
    }).join("");
  }

  function linkOrNone(path, label) {
    if (!path) return '<span class="placeholder">not present in this run</span>';
    return '<a href="' + esc(path) + '" target="_blank" rel="noopener noreferrer">' + esc(label || path) + '</a>';
  }

  function citationExample(run) {
    if (!run || !run.memo) return null;
    var claims = run.memo.claims || [];
    var withSource = claims.filter(function (c) { return c.source_ids && c.source_ids.length > 0; });
    if (withSource.length === 0) return null;
    var claim = withSource[0];
    var sourceId = claim.source_ids[0];
    var source = (run.sources || []).filter(function (s) { return s.id === sourceId; })[0];
    return { claim: claim, source: source };
  }

  function claimCheckExample(run) {
    if (!run || !run.claim_checks) return null;
    var names = Object.keys(run.claim_checks);
    for (var i = 0; i < names.length; i++) {
      if (run.claim_checks[names[i]].status === "ok") return { name: names[i], check: run.claim_checks[names[i]] };
    }
    return names.length ? { name: names[0], check: run.claim_checks[names[0]] } : null;
  }

  function renderStagePanel(stageId) {
    var stage = STAGES_BY_ID[stageId];
    var run = selectedRun();
    var html = "";
    html += "<h2>" + esc(stage.label) + "</h2>";
    html += '<div class="meta-line">Executed by: ' + stage.executors.map(function (e) {
      return '<span class="chip" style="background:var(--' + (e === "python" ? "python-c" : e) + ');display:inline-block;margin-right:4px;">' + esc(e) + "</span>";
    }).join("") + "</div>";
    html += "<p>" + esc(stage.description) + "</p>";
    if (stage.conditional) html += '<p><em>' + esc(stage.conditional) + "</em></p>";
    html += "<h3>Implementation</h3><p><code>" + esc(stage.implementation) + "</code></p>";
    html += "<h3>Inputs</h3><p>" + esc(stage.inputs) + "</p>";
    html += "<h3>Outputs</h3><p>" + esc(stage.outputs) + "</p>";
    html += "<h3>Limitations</h3><p>" + esc(stage.limitations) + "</p>";

    html += "<h3>This run's evidence</h3>";
    if (!run) {
      html += '<p class="placeholder">Select a run above to see real data for this stage.</p>';
    } else if (stageId === "compare_previous_run" && !run.has_previous) {
      html += '<p><span class="badge badge-skipped">not applicable</span> ' + esc(run.company_display) + " " + esc(run.run_id) + " has no previous run to compare against.</p>";
    } else {
      html += renderEvents(stageId);
      if (stageId === "collect_evidence") {
        html += "<p>Sources saved in this run: " + (run.sources || []).length + ". " + linkOrNone(run.paths.sources_json, "sources.json") + "</p>";
      }
      if (stageId === "extract_draft") {
        html += "<p>" + linkOrNone(run.paths.memo_json, "memo.json") + (run.memo_error ? ("<br><span class=\"badge badge-error\">invalid</span> " + esc(run.memo_error)) : "") + "</p>";
      }
      if (stageId === "citation_check") {
        html += "<p>Citation support: " + fmtPct(run.citation.rate) + " (" + run.citation.supported + "/" + run.citation.denominator + ")</p>";
        var ex = citationExample(run);
        if (ex) {
          html += '<p><strong>Example claim:</strong> "' + esc(ex.claim.text) + '" -- ' +
            (ex.claim.supported === true ? '<span class="claim-supported">supported</span>' : ex.claim.supported === false ? '<span class="claim-unsupported">not supported</span>' : "not yet judged") + "</p>";
          if (ex.source) {
            html += '<p><strong>Saved passage (source ' + ex.source.id + '):</strong> <span class="source-passage">"' + esc(ex.source.passage) + '"</span></p>';
          }
        }
      }
      if (stageId === "compute_claims") {
        var cc = claimCheckExample(run);
        if (cc) {
          html += '<p><strong>Example: <code>' + esc(cc.name) + '</code></strong><br>Formula: <code>' + esc(cc.check.formula) + '</code>';
          if (cc.check.status === "ok") {
            html += "<br>Inputs: " + esc((cc.check.inputs || []).join("; ")) + "<br>Result: <strong>" + esc(cc.check.result_display) + "</strong>";
          } else {
            html += "<br>Status: " + esc(cc.check.status) + (cc.check.error ? " (" + esc(cc.check.error) + ")" : "");
          }
          html += "</p>";
        }
        html += "<p>" + linkOrNone(run.paths.claim_checks_json, "claim_checks.json") + "</p>";
      }
      if (stageId === "render_memo") {
        html += "<p>" + linkOrNone(run.paths.memo_html, "Open memo.html") + " &middot; " + linkOrNone(run.paths.memo_md, "memo.md") + "</p>";
      }
      if (stageId === "human_review") {
        var rs = run.review_status;
        var label = rs === "completed" ? '<span class="badge badge-ok">completed</span>' : rs === "blank" ? '<span class="badge badge-started">awaiting review</span>' : '<span class="badge badge-notrecorded">missing</span>';
        html += "<p>" + label + " " + linkOrNone(run.paths.review_md, "review.md");
        if (run.has_previous_review_file) html += " &middot; " + linkOrNone(run.paths.previous_review_md, "previous_review.md (carried forward)");
        html += "</p>";
      }
      if (stageId === "compare_previous_run" && run.has_previous) {
        html += "<p>" + linkOrNone(run.paths.changes_md, "changes.md") + "</p>";
      }
    }
    panel.innerHTML = html;
  }

  nodes.forEach(function (node) {
    node.addEventListener("click", function () {
      nodes.forEach(function (n) { n.classList.remove("selected"); });
      node.classList.add("selected");
      renderStagePanel(node.getAttribute("data-stage"));
    });
    node.addEventListener("keypress", function (e) {
      if (e.key === "Enter" || e.key === " ") node.dispatchEvent(new Event("click"));
    });
  });

  function updateConditionalEdges() {
    var run = selectedRun();
    document.querySelectorAll('.edge[data-conditional="1"]').forEach(function (e) {
      e.classList.toggle("dim", !!(run && !run.has_previous));
    });
    document.querySelectorAll('.node[data-conditional-node="1"]').forEach(function (n) {
      if (n.getAttribute("data-stage") === "compare_previous_run") {
        n.style.opacity = (run && !run.has_previous) ? "0.45" : "1";
      }
    });
  }

  // ---- run details view ----
  function renderRunDetails() {
    var run = selectedRun();
    if (!run) { tabRunDetails.innerHTML = '<p class="placeholder">No runs found under memos/.</p>'; return; }
    var m = run.memo;
    var out = "";
    out += "<h2>" + esc(run.company_display) + " -- " + esc(run.run_id) + "</h2>";
    if (m) {
      var recClass = { Meet: "rec-meet", Track: "rec-track", Pass: "rec-pass" }[m.recommendation] || "rec-track";
      out += '<p><span class="rec-pill ' + recClass + '">' + esc(m.recommendation) + "</span></p>";
      out += "<p>" + esc(m.recommendation_rationale) + "</p>";
    } else {
      out += '<p class="placeholder">No valid memo.json for this run' + (run.memo_error ? ": " + esc(run.memo_error) : "") + ".</p>";
    }

    out += "<h3>Citation support</h3><p>" + fmtPct(run.citation.rate) + " (" + run.citation.supported + "/" + run.citation.denominator + " tracked claims)</p>";

    out += "<h3>Numerical checks</h3><table class=\"data-table\"><tr><th>Check</th><th>Status</th><th>Result</th></tr>";
    Object.keys(run.claim_checks).forEach(function (name) {
      var c = run.claim_checks[name];
      out += "<tr><td><code>" + esc(name) + "</code></td><td>" + esc(c.status) + "</td><td>" + esc(c.status === "ok" ? c.result_display : (c.error || "")) + "</td></tr>";
    });
    out += "</table>";

    out += "<h3>Sources (" + run.sources.length + ")</h3><table class=\"data-table\"><tr><th>ID</th><th>Title</th><th>Evidence type</th><th>Accessed</th></tr>";
    run.sources.forEach(function (s) {
      var link = s.url ? '<a href="' + esc(s.url) + '" target="_blank" rel="noopener noreferrer">' + esc(s.title) + "</a>" : esc(s.title);
      out += "<tr><td>" + s.id + "</td><td>" + link + "</td><td>" + esc(s.evidence_type) + "</td><td>" + esc(s.accessed_date) + "</td></tr>";
    });
    out += "</table>";

    out += "<h3>Event log</h3>";
    if (!run.events || run.events.length === 0) {
      out += '<p><span class="badge badge-notrecorded">not recorded</span> No stage events were logged for this run (it predates event logging, or ran before the logging steps existed for its stages).</p>';
    } else {
      out += "<table class=\"data-table\"><tr><th>Stage</th><th>Executor</th><th>Status</th><th>Timestamp</th><th>Artifacts</th></tr>";
      run.events.forEach(function (e) {
        out += "<tr><td>" + esc(e.stage) + "</td><td>" + esc(e.executor) + "</td><td>" + esc(e.status) + "</td><td>" + esc(e.timestamp) + "</td><td>" + esc((e.artifacts || []).join(", ")) + "</td></tr>";
      });
      out += "</table>";
    }

    out += "<h3>Review status</h3>";
    var rs = run.review_status;
    out += rs === "completed" ? '<p><span class="badge badge-ok">completed</span> ' + linkOrNone(run.paths.review_md, "Open review.md") + "</p>"
      : rs === "blank" ? '<p><span class="badge badge-started">awaiting review</span> ' + linkOrNone(run.paths.review_md, "Open review.md") + "</p>"
      : '<p><span class="badge badge-notrecorded">missing</span> No review.md found.</p>';

    if (run.has_previous) {
      out += "<h3>Changes since " + esc(run.previous_id) + "</h3>";
      out += run.changes_md ? "<pre style=\"white-space:pre-wrap;font-size:0.85rem;background:#f5f1e8;padding:0.8em;border-radius:6px;\">" + esc(run.changes_md) + "</pre>" : '<p><span class="badge badge-notrecorded">not recorded</span> changes.md was not generated for this run.</p>';
    }

    out += "<h3>Open the full memo</h3><p>" + linkOrNone(run.paths.memo_html, "Open memo.html (readable, self-contained)") + "</p>";

    tabRunDetails.innerHTML = out;
  }

  select.addEventListener("change", function () {
    updateConditionalEdges();
    var selectedNode = document.querySelector(".node.selected");
    if (selectedNode) renderStagePanel(selectedNode.getAttribute("data-stage"));
    if (!tabRunDetails.classList.contains("hidden")) renderRunDetails();
  });

  updateConditionalEdges();
})();
</script>
</body>
</html>
"""


def build_viewer_html(runs: list[dict]) -> str:
    stages_json = json.dumps(
        [
            {
                "id": s.id,
                "label": s.label,
                "executors": s.executors,
                "description": s.description,
                "implementation": s.implementation,
                "inputs": s.inputs,
                "outputs": s.outputs,
                "limitations": s.limitations,
                "conditional": s.conditional,
            }
            for s in STAGES
        ]
    )
    data_json = json.dumps({"runs": runs, "generated_at": datetime.now().isoformat(timespec="seconds")})
    svg = _build_svg()

    out = _TEMPLATE
    out = out.replace("__SVG__", svg)
    # JSON blobs go inside a <script type="application/json"> body, which is not
    # parsed as HTML, but "</script" inside a string could still break out of the
    # tag early -- guard against that one sequence.
    out = out.replace("__DATA_JSON__", data_json.replace("</script", "<\\/script"))
    out = out.replace("__STAGES_JSON__", stages_json.replace("</script", "<\\/script"))
    return out


def generate(out_path: Path, root: Path = storage.MEMOS_ROOT) -> tuple[Path, int]:
    runs = discover_runs(root)
    html_doc = build_viewer_html(runs)
    out_path.write_text(html_doc, encoding="utf-8")
    return out_path, len(runs)
