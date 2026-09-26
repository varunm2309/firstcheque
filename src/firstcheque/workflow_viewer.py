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
from .stages import STAGE_GROUPS, STAGES, STAGES_BY_ID

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


def _provenance(run_meta) -> str:
    """"example" only when the run's own recorded notes say so (e.g. this
    project's two demo runs, whose execution_notes literally say "demo run"
    / "demo company"). Never inferred otherwise, and never labelled
    "user-requested" -- there is no mechanism here that records that a run
    was specifically asked for, so the honest default is the neutral
    "Saved research", not a claim either way."""
    if run_meta and run_meta.execution_notes and "demo" in run_meta.execution_notes.lower():
        return "example"
    return "unspecified"


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
        "website": memo.website if memo else None,
        "one_liner": memo.one_liner if memo else None,
        "provenance": _provenance(run_meta),
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
# Diagram layout: four horizontal lanes (Research / Evidence Checks /
# Analysis / Analyst Review), stacked top to bottom, each lane's nodes
# left-aligned and flowing left-to-right. This reads top-to-bottom without
# zooming, unlike a single wide snaking row -- zoom/fit are still provided
# for inspecting small text, but the default layout is meant to be legible
# on its own at normal laptop widths.
# ---------------------------------------------------------------------------

NODE_W, NODE_H = 188, 84
LANE_GAP_X = 36
LANE_LABEL_H = 26
LANE_GAP_Y = 46
LEFT_MARGIN = 24

LANE_STAGES: dict[str, list[str]] = {
    "Research": ["company_input", "start_run", "plan_research", "collect_evidence"],
    "Evidence Checks": ["extract_draft", "citation_check"],
    "Analysis": ["follow_up_pass", "compute_claims"],
    "Analyst Review": ["render_memo", "human_review", "compare_previous_run"],
}

_LANE_LABELS_SHORT: dict[str, list[str]] = {
    "company_input": ["Company", "Input"],
    "start_run": ["Identify /", "Start Run"],
    "plan_research": ["Plan Targeted", "Research"],
    "collect_evidence": ["Collect & Save", "Evidence"],
    "extract_draft": ["Structured", "Extraction"],
    "citation_check": ["Citation-Support", "Check"],
    "follow_up_pass": ["Follow-Up Pass", "(if gaps)"],
    "compute_claims": ["Python Claim", "Checks"],
    "render_memo": ["Render", "Memo"],
    "human_review": ["Analyst", "Review"],
    "compare_previous_run": ["Compare With", "Previous Run"],
}


def _compute_layout() -> tuple[dict[str, tuple[int, int, list[str]]], dict[str, tuple[int, int]], int, int]:
    """Returns (node layout, lane label positions, canvas width, canvas height)."""
    layout: dict[str, tuple[int, int, list[str]]] = {}
    lane_labels: dict[str, tuple[int, int]] = {}
    y = 16
    max_x = 0
    for lane in STAGE_GROUPS:
        lane_labels[lane] = (LEFT_MARGIN, y)
        node_y = y + LANE_LABEL_H
        x = LEFT_MARGIN
        for stage_id in LANE_STAGES[lane]:
            layout[stage_id] = (x, node_y, _LANE_LABELS_SHORT[stage_id])
            x += NODE_W + LANE_GAP_X
        max_x = max(max_x, x - LANE_GAP_X)
        y = node_y + NODE_H + LANE_GAP_Y
    height = y - LANE_GAP_Y + 20
    width = max_x + LEFT_MARGIN
    return layout, lane_labels, width, height


LAYOUT, LANE_LABEL_POS, _CANVAS_W, _CANVAS_H = _compute_layout()
VIEWBOX = f"0 0 {_CANVAS_W} {_CANVAS_H}"

EDGES: list[dict] = [
    {"from": "company_input", "to": "start_run"},
    {"from": "start_run", "to": "plan_research"},
    {"from": "plan_research", "to": "collect_evidence"},
    {"from": "collect_evidence", "to": "extract_draft", "port": "lane_transition"},
    {"from": "extract_draft", "to": "citation_check"},
    {"from": "citation_check", "to": "follow_up_pass", "port": "lane_transition", "label": "if gaps found"},
    {
        "from": "citation_check",
        "to": "compute_claims",
        "port": "bypass",
        "label": "if no gaps, skip follow-up",
        "dashed": True,
    },
    {"from": "follow_up_pass", "to": "compute_claims"},
    {"from": "compute_claims", "to": "render_memo", "port": "lane_transition"},
    {"from": "render_memo", "to": "human_review"},
    {
        "from": "human_review",
        "to": "compare_previous_run",
        "label": "on update runs only",
        "conditional": True,
        "dashed": True,
    },
]

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

    if kind == "lane_transition":
        # down from the source node, left to the destination column, down into it
        x1, y1 = _port(fx, fy, "bottom")
        x2, y2 = _port(tx, ty, "top")
        mid_y = y1 + (LANE_GAP_Y // 2)
        return (
            f"M{x1},{y1} L{x1},{mid_y} L{x2},{mid_y} L{x2},{y2}",
            (min(x1, x2) + abs(x1 - x2) // 2, mid_y - 6),
        )

    if kind == "bypass":
        # a dashed line to the right of both boxes, clearing whatever sits
        # between the two lanes at this column (here: follow_up_pass)
        x1, y1 = _port(fx, fy, "right")
        x2, y2 = _port(tx, ty, "right")
        clear_x = max(x1, x2) + 56
        return (
            f"M{x1},{y1} L{clear_x},{y1} L{clear_x},{y2} L{x2},{y2}",
            (clear_x + 6, (y1 + y2) // 2),
        )

    # horizontal: same lane, left to right
    x1, y1 = _port(fx, fy, "right")
    x2, y2 = _port(tx, ty, "left")
    return f"M{x1},{y1} L{x2},{y2}", ((x1 + x2) // 2, y1 - 10)


def _e(text) -> str:
    return html.escape(str(text), quote=True)


def _build_svg() -> str:
    parts = [
        f'<svg id="workflow-svg" viewBox="{VIEWBOX}" xmlns="http://www.w3.org/2000/svg">',
        "<defs>",
        '<marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">',
        '<path d="M0,0 L10,5 L0,10 z" class="arrowhead"/>',
        "</marker>",
        "</defs>",
    ]

    # Lane backgrounds + labels, drawn first so nodes/edges sit on top.
    parts.append('<g id="lanes">')
    for lane in STAGE_GROUPS:
        lx, ly = LANE_LABEL_POS[lane]
        band_top = ly + LANE_LABEL_H - 6
        parts.append(
            f'<rect x="0" y="{band_top}" width="{_CANVAS_W}" height="{NODE_H + 12}" class="lane-band"></rect>'
        )
        parts.append(f'<text x="{lx}" y="{ly + 12}" class="lane-label">{_e(lane.upper())}</text>')
    parts.append("</g>")

    parts.append('<g id="edges">')
    for edge in EDGES:
        d, (lx, ly) = _edge_path(edge)
        dash_cls = " edge-dashed" if edge.get("dashed") else ""
        cond_attr = ' data-conditional="1"' if edge.get("conditional") else ""
        parts.append(f'<path d="{d}" class="edge{dash_cls}" marker-end="url(#arrow)"{cond_attr}></path>')
        if edge.get("label"):
            parts.append(f'<text x="{lx}" y="{ly}" class="edge-label">{_e(edge["label"])}</text>')
    parts.append("</g>")

    parts.append('<g id="nodes">')
    for stage in STAGES:
        if stage.id not in LAYOUT:
            continue
        x, y, lines = LAYOUT[stage.id]
        executor_text = " + ".join(EXECUTOR_LABEL[e] for e in stage.executors)
        text_lines = "".join(
            f'<tspan x="{x + NODE_W // 2}" dy="{0 if i == 0 else 16}">{_e(line)}</tspan>'
            for i, line in enumerate(lines)
        )
        conditional_mark = ' data-conditional-node="1"' if stage.conditional else ""
        parts.append(
            f'<g class="node" data-stage="{stage.id}" tabindex="0" role="button" '
            f'aria-label="{_e(stage.label)}"{conditional_mark}>'
            f'<rect x="{x}" y="{y}" width="{NODE_W}" height="{NODE_H}" rx="8" class="node-box"></rect>'
            f'<text x="{x + 12}" y="{y + 16}" class="node-executor">{_e(executor_text)}</text>'
            f'<circle cx="{x + NODE_W - 14}" cy="{y + 12}" r="4" class="status-dot" data-status-dot="1"></circle>'
            f'<text x="{x + NODE_W // 2}" y="{y + 42}" text-anchor="middle" class="node-label">{text_lines}</text>'
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
<title>FirstCheque -- Workflow Map &amp; Saved-Research Inspector</title>
<style>
  :root {
    --paper: #faf7f2;
    --paper-alt: #f1ece1;
    --ink: #29251f;
    --ink-soft: #4d473d;
    --muted: #837a6a;
    --line: #e2dac9;
    --panel-bg: #ffffff;
    --emerald: #1f6b4a;
    --emerald-dark: #17513838;
    --emerald-soft: #e7f1ea;
    --status-completed: #1f6b4a;
    --status-failed: #9c4a3c;
    --status-notrecorded: #a29a8a;
    --status-insufficient: #a87f3a;
    --status-notapplicable: #b7b0a1;
    --status-awaiting: #a3781f;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; }
  body {
    background: var(--paper); color: var(--ink);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    font-size: 15px; line-height: 1.5;
  }
  h1, h2, h3 { font-weight: 600; margin: 0; }
  a { color: var(--emerald); }
  code, .mono { font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace; }

  /* ---------- Header ---------- */
  .topbar {
    display: flex; align-items: center; gap: 1.5rem; flex-wrap: wrap;
    padding: 1rem 1.75rem; background: var(--panel-bg); border-bottom: 1px solid var(--line);
  }
  .brand { display: flex; flex-direction: column; gap: 0.1rem; }
  .brand .wordmark { font-family: Georgia, "Iowan Old Style", serif; font-size: 1.3rem; font-weight: 700; }
  .brand .tagline { font-size: 0.78rem; color: var(--muted); }
  .header-run { display: flex; flex-direction: column; gap: 0.25rem; min-width: 220px; }
  .header-run .company-line { display: flex; align-items: baseline; gap: 0.6rem; flex-wrap: wrap; }
  .header-run .company-name { font-size: 1.1rem; font-weight: 700; }
  .header-run .company-website { font-size: 0.82rem; color: var(--muted); }
  .pill {
    display: inline-flex; align-items: center; gap: 0.3em; font-size: 0.7rem; font-weight: 600;
    text-transform: uppercase; letter-spacing: 0.04em; padding: 0.18em 0.65em; border-radius: 999px;
    background: var(--paper-alt); color: var(--ink-soft); border: 1px solid var(--line);
  }
  .pill-saved { background: var(--emerald-soft); color: var(--emerald); border-color: #c9e2d4; }
  .header-meta-line { font-size: 0.78rem; color: var(--muted); }
  .header-controls { display: flex; align-items: center; gap: 0.7rem; margin-left: auto; flex-wrap: wrap; }
  select {
    font: inherit; padding: 0.5em 0.7em; border: 1px solid var(--line); border-radius: 7px;
    background: var(--panel-bg); color: var(--ink); min-width: 220px;
  }
  .btn {
    font: inherit; font-size: 0.88rem; padding: 0.5em 1.1em; border: 1px solid var(--line);
    border-radius: 7px; background: var(--panel-bg); color: var(--ink); cursor: pointer; text-decoration: none;
    display: inline-flex; align-items: center; gap: 0.4em; white-space: nowrap;
  }
  .btn:hover { border-color: var(--muted); }
  .btn-primary { background: var(--emerald); border-color: var(--emerald); color: #fff; font-weight: 600; }
  .btn-primary:hover { background: #185a3c; }
  .btn[aria-disabled="true"] { opacity: 0.45; pointer-events: none; }

  .disclaimer-bar {
    background: var(--paper-alt); border-bottom: 1px solid var(--line); color: var(--ink-soft);
    font-size: 0.85rem; padding: 0.6em 1.75rem; text-align: center;
  }

  /* ---------- Page layout ---------- */
  .page { max-width: 1280px; margin: 0 auto; padding: 1.75rem 1.75rem 3rem; }
  .section-title {
    font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted);
    margin-bottom: 0.9rem; padding-bottom: 0.4rem; border-bottom: 1px solid var(--line);
  }
  section { margin-bottom: 2.75rem; }

  /* ---------- Company selection ---------- */
  .selection-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem; }
  @media (max-width: 900px) { .selection-grid { grid-template-columns: 1fr; } }
  .selection-card {
    background: var(--panel-bg); border: 1px solid var(--line); border-radius: 10px; padding: 1.3rem 1.4rem;
  }
  .selection-card h3 { font-size: 1rem; margin-bottom: 0.3rem; }
  .card-hint { color: var(--muted); font-size: 0.85rem; margin: 0 0 1rem; }
  .company-card-list { display: flex; flex-direction: column; gap: 0.6rem; }
  .company-card {
    display: flex; align-items: center; justify-content: space-between; gap: 0.8rem;
    padding: 0.7em 0.9em; border: 1px solid var(--line); border-radius: 8px; cursor: pointer; background: var(--paper);
  }
  .company-card:hover { border-color: var(--emerald); }
  .company-card.active { border-color: var(--emerald); background: var(--emerald-soft); }
  .company-card .cc-name { font-weight: 600; }
  .company-card .cc-meta { font-size: 0.76rem; color: var(--muted); }
  .rec-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 0.4em; }
  .rec-Meet { background: var(--status-completed); }
  .rec-Track { background: var(--status-awaiting); }
  .rec-Pass { background: var(--status-failed); }

  .command-builder input[type="text"] {
    width: 100%; font: inherit; padding: 0.6em 0.8em; border: 1px solid var(--line); border-radius: 7px;
    margin-bottom: 0.8rem; background: var(--paper);
  }
  .command-row { display: flex; gap: 0.5rem; }
  .command-row input {
    flex: 1 1 auto; font-family: "SFMono-Regular", Consolas, monospace; font-size: 0.85rem;
    padding: 0.6em 0.8em; border: 1px solid var(--line); border-radius: 7px; background: var(--paper-alt); color: var(--ink);
  }
  .copy-feedback { font-size: 0.78rem; color: var(--emerald); margin-top: 0.5rem; min-height: 1.1em; }
  .paste-note { font-size: 0.82rem; color: var(--ink-soft); margin-top: 0.9rem; padding-top: 0.7rem; border-top: 1px dashed var(--line); }

  /* ---------- Workflow canvas ---------- */
  .canvas-layout { display: flex; gap: 1.25rem; align-items: stretch; }
  .canvas-wrap {
    position: relative; flex: 1 1 auto; min-width: 0; height: 640px; overflow: hidden;
    background: var(--panel-bg); border: 1px solid var(--line); border-radius: 10px;
  }
  #workflow-svg { position: absolute; top: 0; left: 0; transform-origin: 0 0; }
  .lane-band { fill: var(--paper-alt); opacity: 0.55; }
  .lane-label { font-size: 11px; font-weight: 700; letter-spacing: 0.08em; fill: var(--muted); }
  .node { cursor: pointer; }
  .node-box { fill: #fff; stroke: var(--line); stroke-width: 1.4; transition: stroke 0.1s; }
  .node:hover .node-box { stroke: var(--ink-soft); }
  .node.selected .node-box { stroke: var(--emerald); stroke-width: 2.2; }
  .node-executor {
    font-size: 9px; font-weight: 600; letter-spacing: 0.05em; text-transform: uppercase; fill: var(--muted);
  }
  .node-label { font-size: 12.5px; font-weight: 600; fill: var(--ink); }
  .status-dot { fill: var(--status-notrecorded); }
  .edge { fill: none; stroke: #b6ac97; stroke-width: 1.4; }
  .edge-dashed { stroke-dasharray: 5,4; }
  .edge[data-conditional="1"].dim { stroke: #ddd6c5; }
  .arrowhead { fill: #b6ac97; }
  .edge-label { font-size: 10.5px; fill: var(--muted); font-style: italic; }
  .zoom-controls { position: absolute; right: 12px; bottom: 12px; display: flex; gap: 6px; z-index: 5; }
  .zoom-controls .btn { padding: 0.35em 0.75em; }

  .side-panel {
    width: 380px; flex: 0 0 auto; background: var(--panel-bg); border: 1px solid var(--line); border-radius: 10px;
    padding: 1.25rem 1.35rem; overflow-y: auto; height: 640px;
  }
  .side-panel .placeholder { color: var(--muted); font-size: 0.9rem; padding-top: 2rem; text-align: center; }
  .side-panel h3.stage-title { font-size: 1.05rem; margin-bottom: 0.5rem; }
  .side-panel .meta-row { display: flex; align-items: center; gap: 0.6rem; flex-wrap: wrap; margin-bottom: 0.9rem; font-size: 0.8rem; }
  .status-chip {
    display: inline-flex; align-items: center; gap: 0.4em; font-size: 0.72rem; font-weight: 600;
    padding: 0.2em 0.6em; border-radius: 999px; border: 1px solid transparent;
  }
  .status-chip .dot { width: 7px; height: 7px; border-radius: 50%; }
  .status-chip.st-completed { background: var(--emerald-soft); color: var(--status-completed); }
  .status-chip.st-failed { background: #f6e9e6; color: var(--status-failed); }
  .status-chip.st-notrecorded { background: #f0ede6; color: var(--status-notrecorded); }
  .status-chip.st-insufficient { background: #f5ecdc; color: var(--status-insufficient); }
  .status-chip.st-notapplicable { background: #f0eee9; color: var(--status-notapplicable); }
  .status-chip.st-awaiting { background: #f7edd8; color: var(--status-awaiting); }
  .executor-line { color: var(--muted); font-size: 0.8rem; }
  .side-panel h4 {
    font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted);
    border-bottom: 1px solid var(--line); padding-bottom: 0.25em; margin: 1rem 0 0.5em;
  }
  .side-panel p { margin: 0.4em 0; font-size: 0.88rem; }
  .fact-table { width: 100%; border-collapse: collapse; font-size: 0.82rem; margin: 0.3em 0; }
  .fact-table th, .fact-table td { text-align: left; padding: 0.3em 0.45em; border-bottom: 1px solid var(--line); }
  .fact-table th { color: var(--muted); font-weight: 600; font-size: 0.72rem; text-transform: uppercase; }
  .evidence-quote {
    background: var(--paper-alt); border-left: 3px solid var(--emerald); padding: 0.6em 0.8em;
    font-size: 0.85rem; font-style: italic; margin: 0.4em 0; border-radius: 0 6px 6px 0;
  }
  .formula-box {
    background: var(--paper-alt); border-radius: 6px; padding: 0.5em 0.7em; font-size: 0.85rem; margin: 0.4em 0;
  }
  .result-line { font-size: 1rem; font-weight: 700; margin: 0.5em 0; }
  .limitation-box { background: #f6f2e9; border-radius: 6px; padding: 0.6em 0.8em; font-size: 0.82rem; }

  /* ---------- Legend ---------- */
  .legend-bar {
    display: flex; flex-wrap: wrap; gap: 1.4rem; align-items: center; margin-top: 0.9rem;
    font-size: 0.78rem; color: var(--muted);
  }
  .legend-item { display: flex; align-items: center; gap: 0.4em; }
  .legend-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }

  /* ---------- Research quality summary ---------- */
  .quality-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 1.2rem; }
  .quality-card { background: var(--panel-bg); border: 1px solid var(--line); border-radius: 10px; padding: 1.1rem 1.25rem; }
  .quality-card h3 { font-size: 0.85rem; margin-bottom: 0.6rem; }
  .quality-headline { font-size: 1.5rem; font-weight: 700; margin-bottom: 0.2rem; }
  .quality-sub { font-size: 0.78rem; color: var(--muted); margin-bottom: 0.7rem; }
  .quality-card ul { margin: 0; padding-left: 1.1rem; font-size: 0.85rem; }
  .quality-card li { margin-bottom: 0.4rem; }
  .quality-card .none { color: var(--muted); font-style: italic; font-size: 0.85rem; }
  .quality-note {
    font-size: 0.76rem; color: var(--muted); margin-top: 0.8rem; padding-top: 0.6rem; border-top: 1px dashed var(--line);
  }

  /* ---------- Full data (collapsible) ---------- */
  details.full-data { background: var(--panel-bg); border: 1px solid var(--line); border-radius: 10px; padding: 0; }
  details.full-data > summary {
    cursor: pointer; padding: 1rem 1.25rem; font-weight: 600; font-size: 0.9rem; list-style: none;
  }
  details.full-data > summary::-webkit-details-marker { display: none; }
  details.full-data > summary::before { content: "+ "; color: var(--emerald); }
  details.full-data[open] > summary::before { content: "- "; }
  .full-data-body { padding: 0 1.25rem 1.25rem; }
  table.data-table { border-collapse: collapse; width: 100%; margin: 0.6em 0 1.2em; font-size: 0.85rem; }
  table.data-table th, table.data-table td { border: 1px solid var(--line); padding: 0.4em 0.6em; text-align: left; vertical-align: top; }
  table.data-table th { background: var(--paper-alt); font-weight: 600; }
  .hidden { display: none !important; }
</style>
</head>
<body>
<header class="topbar">
  <div class="brand">
    <div class="wordmark">FirstCheque</div>
    <div class="tagline">Workflow map &amp; saved-research inspector</div>
  </div>
  <div class="header-run">
    <div class="company-line">
      <span class="company-name" id="hdr-company">--</span>
      <a class="company-website" id="hdr-website" href="#" target="_blank" rel="noopener noreferrer"></a>
      <span class="pill pill-saved">Saved research</span>
      <span class="pill hidden" id="hdr-provenance-pill">Saved example</span>
    </div>
    <div class="header-meta-line" id="hdr-meta">Select a run to see details.</div>
  </div>
  <div class="header-controls">
    <select id="run-select" aria-label="Select a saved run"></select>
    <a class="btn btn-primary" id="open-memo-btn" target="_blank" rel="noopener noreferrer" aria-disabled="true">Open memo</a>
  </div>
</header>

<div class="disclaimer-bar">
  This page displays saved research and does not perform research itself. New research happens in a Claude Code
  session via <code>/company-brief</code> -- see "Research another company" below.
</div>

<main class="page">

  <section id="selection-section">
    <div class="section-title">Company selection</div>
    <div class="selection-grid">
      <div class="selection-card">
        <h3>View saved research</h3>
        <p class="card-hint">Pick from the companies already researched and saved in this project.</p>
        <div class="company-card-list" id="company-card-list"></div>
      </div>
      <div class="selection-card command-builder">
        <h3>Research another company</h3>
        <p class="card-hint">Type a company name or website, copy the command, and paste it into your Claude Code session to start real research. This page cannot run it for you.</p>
        <input type="text" id="company-name-input" placeholder="e.g. Zerodha, or https://zerodha.com" autocomplete="off">
        <div class="command-row">
          <input type="text" id="command-output" readonly aria-label="Command to paste into Claude Code" value="/company-brief ">
          <button class="btn" id="copy-command-btn">Copy</button>
        </div>
        <div class="copy-feedback" id="copy-feedback"></div>
        <div class="paste-note">
          If your browser blocks the copy button, the text above is a plain input field -- click into it,
          select all (Ctrl/Cmd+A), and copy manually.
        </div>
      </div>
    </div>
  </section>

  <section id="canvas-section">
    <div class="section-title">Workflow</div>
    <div class="canvas-layout">
      <div class="canvas-wrap" id="canvas-wrap">
        __SVG__
        <div class="zoom-controls">
          <button class="btn" id="zoom-out">-</button>
          <button class="btn" id="zoom-fit">Fit</button>
          <button class="btn" id="zoom-in">+</button>
        </div>
      </div>
      <aside class="side-panel" id="side-panel">
        <div class="placeholder">Click a stage to see a plain-English explanation, who runs it, and (for the selected run) the real recorded status and evidence.</div>
      </aside>
    </div>
    <div class="legend-bar">
      <span class="legend-item"><strong>Executor</strong> (shown on each stage): Claude Code, Python, or You</span>
      <span class="legend-item"><span class="legend-dot" style="background:var(--status-completed)"></span>Completed</span>
      <span class="legend-item"><span class="legend-dot" style="background:var(--status-failed)"></span>Failed</span>
      <span class="legend-item"><span class="legend-dot" style="background:var(--status-insufficient)"></span>Insufficient data</span>
      <span class="legend-item"><span class="legend-dot" style="background:var(--status-notapplicable)"></span>Not applicable</span>
      <span class="legend-item"><span class="legend-dot" style="background:var(--status-awaiting)"></span>Awaiting review</span>
      <span class="legend-item"><span class="legend-dot" style="background:var(--status-notrecorded)"></span>Not recorded</span>
    </div>
  </section>

  <section id="quality-section">
    <div class="section-title">Research quality summary</div>
    <div class="quality-grid" id="quality-grid"></div>
    <div class="quality-note">These are reported separately on purpose. Citation support measures whether a saved passage backs a claim -- it is not proof the claim is true, and none of these figures are combined into a single confidence or investment score.</div>
  </section>

  <details class="full-data">
    <summary>Full run data (all sources, event log, claim checks)</summary>
    <div class="full-data-body" id="full-data-body"></div>
  </details>

</main>

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

  var NUMBERS_FIELD_FOR_CHECK = {
    annualised_revenue_run_rate: ["monthly_revenue_current"],
    valuation_multiple: ["valuation", "annual_revenue"],
    arpu: ["monthly_revenue_current", "paying_customers"],
    growth_multiple: ["monthly_revenue_current", "monthly_revenue_earlier"],
    capital_efficiency: ["total_capital_raised", "annual_revenue"],
    capital_raised_per_year: ["total_capital_raised", "years_since_founding"],
    implied_dilution: ["latest_round_size", "post_money_valuation"]
  };

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

  function fmtDate(iso) {
    if (!iso) return null;
    var d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    var months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
    return d.getDate() + " " + months[d.getMonth()] + " " + d.getFullYear();
  }

  // ---------------------------------------------------------------------
  // Status resolution: every raw status (event status, claim-check status,
  // review status) maps into ONE of six display states, so "why is this
  // grey vs amber vs red" means the same thing everywhere on the page.
  // ---------------------------------------------------------------------
  var STATUS = {
    COMPLETED: { key: "completed", cls: "st-completed", label: "Completed" },
    FAILED: { key: "failed", cls: "st-failed", label: "Failed" },
    NOT_RECORDED: { key: "notrecorded", cls: "st-notrecorded", label: "Not recorded" },
    INSUFFICIENT: { key: "insufficient", cls: "st-insufficient", label: "Insufficient data" },
    NOT_APPLICABLE: { key: "notapplicable", cls: "st-notapplicable", label: "Not applicable" },
    AWAITING: { key: "awaiting", cls: "st-awaiting", label: "Awaiting review" }
  };

  function latestEvent(run, stageId) {
    var events = (run.events || []).filter(function (e) { return e.stage === stageId; });
    return events.length ? events[events.length - 1] : null;
  }

  function eventStatusToDisplay(ev) {
    if (!ev) return STATUS.NOT_RECORDED;
    if (ev.status === "ok") return STATUS.COMPLETED;
    if (ev.status === "error") return STATUS.FAILED;
    if (ev.status === "skipped") return STATUS.NOT_APPLICABLE;
    return STATUS.NOT_RECORDED;
  }

  function claimCheckStatusToDisplay(status) {
    if (status === "ok") return STATUS.COMPLETED;
    if (status === "not_applicable") return STATUS.NOT_APPLICABLE;
    return STATUS.INSUFFICIENT; // missing_input / zero_denominator / *_mismatch
  }

  function nodeStatus(stageId, run) {
    if (!run) return STATUS.NOT_RECORDED;
    if (stageId === "company_input") return null; // no recorded-status concept for a human typing a name
    if (stageId === "human_review") {
      if (run.review_status === "completed") return STATUS.COMPLETED;
      if (run.review_status === "blank") return STATUS.AWAITING;
      return STATUS.NOT_RECORDED;
    }
    if (stageId === "compare_previous_run" && !run.has_previous) return STATUS.NOT_APPLICABLE;
    return eventStatusToDisplay(latestEvent(run, stageId));
  }

  function statusChipHtml(status) {
    if (!status) return "";
    return '<span class="status-chip ' + status.cls + '"><span class="dot" style="background:var(--status-' + status.key + ')"></span>' + esc(status.label) + "</span>";
  }

  // ---------------------------------------------------------------------
  // Header + run selector
  // ---------------------------------------------------------------------
  var select = document.getElementById("run-select");
  var companiesOrder = [];
  var runsByCompany = {};
  DATA.runs.forEach(function (r) {
    if (!runsByCompany[r.company_slug]) { runsByCompany[r.company_slug] = []; companiesOrder.push(r.company_slug); }
    runsByCompany[r.company_slug].push(r);
  });
  companiesOrder.forEach(function (slug) {
    var group = document.createElement("optgroup");
    group.label = runsByCompany[slug][0].company_display;
    runsByCompany[slug].forEach(function (r) {
      var opt = document.createElement("option");
      opt.value = r.id;
      opt.textContent = r.run_id + (r.memo ? " -- " + r.memo.recommendation : "");
      group.appendChild(opt);
    });
    select.appendChild(group);
  });

  var defaultRun = RUNS_BY_ID["kaleidofin/run-002"] ? "kaleidofin/run-002" : (DATA.runs[0] ? DATA.runs[0].id : null);
  if (defaultRun) select.value = defaultRun;

  function selectedRun() { return RUNS_BY_ID[select.value] || null; }

  function renderHeader() {
    var run = selectedRun();
    var nameEl = document.getElementById("hdr-company");
    var siteEl = document.getElementById("hdr-website");
    var metaEl = document.getElementById("hdr-meta");
    var provPill = document.getElementById("hdr-provenance-pill");
    var openBtn = document.getElementById("open-memo-btn");

    if (!run) {
      nameEl.textContent = "No saved runs found";
      siteEl.classList.add("hidden");
      metaEl.textContent = "Nothing under memos/ yet.";
      provPill.classList.add("hidden");
      openBtn.setAttribute("aria-disabled", "true");
      return;
    }

    nameEl.textContent = run.company_display;
    if (run.website) {
      siteEl.textContent = run.website.replace(/^https?:\/\//, "");
      siteEl.href = run.website;
      siteEl.classList.remove("hidden");
    } else {
      siteEl.classList.add("hidden");
    }

    var researchedOn = run.run_meta ? fmtDate(run.run_meta.created_at) : null;
    var updatedOn = run.run_meta && run.run_meta.updated_at ? fmtDate(run.run_meta.updated_at) : null;
    var metaParts = [run.run_id];
    if (researchedOn) metaParts.push("researched " + researchedOn);
    if (updatedOn) metaParts.push("recomputed " + updatedOn + " (no new research)");
    metaEl.textContent = metaParts.join(" · ");

    if (run.provenance === "example") {
      provPill.textContent = "Saved example -- not requested by you";
      provPill.classList.remove("hidden");
    } else {
      provPill.classList.add("hidden");
    }

    if (run.paths.memo_html) {
      openBtn.href = run.paths.memo_html;
      openBtn.removeAttribute("aria-disabled");
    } else {
      openBtn.removeAttribute("href");
      openBtn.setAttribute("aria-disabled", "true");
    }
  }

  // ---------------------------------------------------------------------
  // Company selection: saved-research cards
  // ---------------------------------------------------------------------
  function renderCompanyCards() {
    var list = document.getElementById("company-card-list");
    list.innerHTML = "";
    if (companiesOrder.length === 0) {
      list.innerHTML = '<p class="none">No saved runs found under memos/.</p>';
      return;
    }
    companiesOrder.forEach(function (slug) {
      var runs = runsByCompany[slug];
      var latest = runs[runs.length - 1];
      var card = document.createElement("div");
      card.className = "company-card" + (select.value.indexOf(slug + "/") === 0 ? " active" : "");
      var rec = latest.memo ? latest.memo.recommendation : null;
      var dot = rec ? '<span class="rec-dot rec-' + rec + '"></span>' : "";
      var provTag = latest.provenance === "example" ? " (saved example)" : "";
      card.innerHTML =
        '<div><div class="cc-name">' + dot + esc(latest.company_display) + '</div>' +
        '<div class="cc-meta">' + runs.length + ' saved run' + (runs.length > 1 ? "s" : "") +
        provTag + " · latest: " + esc(latest.run_id) + '</div></div>' +
        '<div class="cc-meta">' + (rec ? esc(rec) : "") + '</div>';
      card.addEventListener("click", function () {
        select.value = latest.id;
        onRunChanged();
      });
      list.appendChild(card);
    });
  }

  // ---------------------------------------------------------------------
  // Command builder: this is the ONLY way this page can start research --
  // by handing you the exact command to paste into Claude Code yourself.
  // ---------------------------------------------------------------------
  var nameInput = document.getElementById("company-name-input");
  var commandOutput = document.getElementById("command-output");
  var copyBtn = document.getElementById("copy-command-btn");
  var copyFeedback = document.getElementById("copy-feedback");

  function updateCommand() {
    var val = nameInput.value.trim();
    commandOutput.value = "/company-brief " + val;
  }
  nameInput.addEventListener("input", updateCommand);

  function announceCopySuccess() {
    copyFeedback.textContent = "Copied. Paste it into your Claude Code session to start research.";
  }
  function tryExecCommandFallback() {
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    copyFeedback.textContent = ok
      ? "Copied. Paste it into your Claude Code session to start research."
      : "Could not copy automatically -- the command above is selected; press Ctrl/Cmd+C, or just retype it.";
  }

  copyBtn.addEventListener("click", function () {
    commandOutput.select();
    commandOutput.setSelectionRange(0, commandOutput.value.length);
    // navigator.clipboard.writeText() returns a promise that can reject
    // asynchronously (e.g. NotAllowedError on a file:// origin, or in a
    // sandboxed context) -- a plain try/catch does NOT catch that, so the
    // fallback has to live in .catch(), not after a synchronous call.
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(commandOutput.value).then(announceCopySuccess).catch(tryExecCommandFallback);
    } else {
      tryExecCommandFallback();
    }
  });

  // ---------------------------------------------------------------------
  // Pan/zoom (not required to read the diagram -- default view fits)
  // ---------------------------------------------------------------------
  var svg = document.getElementById("workflow-svg");
  var canvasWrap = document.getElementById("canvas-wrap");
  var view = { scale: 1, tx: 0, ty: 0 };
  function applyView() {
    svg.style.transform = "translate(" + view.tx + "px," + view.ty + "px) scale(" + view.scale + ")";
  }
  function fitToScreen() {
    var vb = svg.viewBox.baseVal;
    var rect = canvasWrap.getBoundingClientRect();
    var scale = Math.min(rect.width / vb.width, rect.height / vb.height) * 0.94;
    view.scale = scale;
    view.tx = (rect.width - vb.width * scale) / 2 - vb.x * scale;
    view.ty = (rect.height - vb.height * scale) / 2 - vb.y * scale;
    applyView();
  }
  document.getElementById("zoom-in").addEventListener("click", function () { view.scale = Math.min(2.5, view.scale * 1.15); applyView(); });
  document.getElementById("zoom-out").addEventListener("click", function () { view.scale = Math.max(0.4, view.scale / 1.15); applyView(); });
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
    view.scale = Math.max(0.4, Math.min(2.5, view.scale * factor));
    applyView();
  }, { passive: false });
  setTimeout(fitToScreen, 0);
  window.addEventListener("resize", fitToScreen);

  // ---------------------------------------------------------------------
  // Node status dots + click -> side panel
  // ---------------------------------------------------------------------
  function updateNodeDots() {
    var run = selectedRun();
    document.querySelectorAll(".node").forEach(function (node) {
      var stageId = node.getAttribute("data-stage");
      var dot = node.querySelector(".status-dot");
      var status = nodeStatus(stageId, run);
      dot.style.fill = status ? "var(--status-" + status.key + ")" : "transparent";
      dot.style.opacity = status ? "1" : "0";
    });
    document.querySelectorAll('.edge[data-conditional="1"]').forEach(function (e) {
      e.classList.toggle("dim", !!(run && !run.has_previous));
    });
  }

  function factRow(label, fact) {
    if (!fact) return "<tr><td>" + esc(label) + "</td><td colspan=\"4\" class=\"none\">not provided</td></tr>";
    var value = fact.currency ? (fact.value.toLocaleString ? fact.value.toLocaleString() : fact.value) + " " + fact.currency : fact.value;
    var src = (fact.source_ids || []).map(function (id) { return "[" + id + "]"; }).join(" ");
    return "<tr><td>" + esc(label) + "</td><td>" + esc(value) + "</td><td>" + esc(fact.period || "—") + "</td>" +
      "<td>" + esc(fact.as_of || "—") + "</td><td>" + esc(src || "—") + "</td></tr>";
  }

  function renderComputeClaimsEvidence(run) {
    var checks = run.claim_checks || {};
    var names = Object.keys(checks);
    if (names.length === 0) return '<p class="none">No numerical checks computed for this run.</p>';
    var okName = names.filter(function (n) { return checks[n].status === "ok"; })[0];
    var featured = okName || names[0];
    var check = checks[featured];
    var fields = NUMBERS_FIELD_FOR_CHECK[featured] || [];
    var numbers = (run.memo && run.memo.numbers) || {};

    var html = '<p><strong>Example: <code>' + esc(featured) + "</code></strong></p>";
    html += '<div class="formula-box"><code>' + esc(check.formula) + "</code></div>";
    if (check.status === "ok") {
      html += '<table class="fact-table"><tr><th>Input</th><th>Value</th><th>Period</th><th>As of</th><th>Source</th></tr>';
      fields.forEach(function (f) { html += factRow(f, numbers[f]); });
      html += "</table>";
      html += '<div class="result-line">' + esc(check.result_display) + "</div>";
      (check.assumptions || []).forEach(function (a) { html += "<p>" + esc(a) + "</p>"; });
    } else {
      html += "<p>" + esc(check.error) + "</p>";
    }
    html += "<h4>All numerical checks in this run</h4><table class=\"fact-table\"><tr><th>Check</th><th>Result</th></tr>";
    names.forEach(function (n) {
      html += "<tr><td><code>" + esc(n) + "</code></td><td>" + esc(checks[n].status === "ok" ? checks[n].result_display : "Insufficient data") + "</td></tr>";
    });
    html += "</table>";
    return html;
  }

  function renderCitationEvidence(run) {
    var claims = (run.memo && run.memo.claims) || [];
    var withSource = claims.filter(function (c) { return c.source_ids && c.source_ids.length > 0; });
    var html = "<p>Citation support: <strong>" + fmtPct(run.citation.rate) + "</strong> (" + run.citation.supported + " of " + run.citation.denominator + " tracked claims)</p>";
    if (withSource.length === 0) return html + '<p class="none">No sourced claims recorded.</p>';
    var claim = withSource[0];
    var sourceId = claim.source_ids[0];
    var source = (run.sources || []).filter(function (s) { return s.id === sourceId; })[0];
    html += '<p><strong>Example claim:</strong> "' + esc(claim.text) + '"</p>';
    html += "<p>" + statusChipHtml(claim.supported === true ? STATUS.COMPLETED : claim.supported === false ? STATUS.FAILED : STATUS.NOT_RECORDED) + "</p>";
    if (source) html += '<div class="evidence-quote">"' + esc(source.passage) + '" &mdash; source ' + source.id + " (" + esc(source.evidence_type) + ")</div>";
    return html;
  }

  function linkOrNone(path, label) {
    if (!path) return '<span class="none">not present in this run</span>';
    return '<a href="' + esc(path) + '" target="_blank" rel="noopener noreferrer">' + esc(label || path) + "</a>";
  }

  function renderEventsList(run, stageId) {
    var events = (run.events || []).filter(function (e) { return e.stage === stageId; });
    if (events.length === 0) return "";
    return events.map(function (e) {
      var st = eventStatusToDisplay(e);
      return '<p>' + statusChipHtml(st) + " <strong>" + esc(e.executor) + "</strong> at " + esc(e.timestamp) +
        (e.notes ? "<br><span style=\"color:var(--muted);font-size:0.82rem\">" + esc(e.notes) + "</span>" : "") +
        (e.error ? "<br><span style=\"color:var(--status-failed);font-size:0.82rem\">" + esc(e.error) + "</span>" : "") + "</p>";
    }).join("");
  }

  function renderStagePanel(stageId) {
    var stage = STAGES_BY_ID[stageId];
    var run = selectedRun();
    var status = nodeStatus(stageId, run);
    var executorText = stage.executors.map(function (e) { return e === "claude" ? "Claude Code" : e === "python" ? "Python" : "You"; }).join(" + ");

    var out = "";
    out += '<h3 class="stage-title">' + esc(stage.label) + "</h3>";
    out += '<div class="meta-row">' + statusChipHtml(status || STATUS.NOT_RECORDED) + '<span class="executor-line">Executed by ' + esc(executorText) + "</span></div>";
    out += "<p>" + esc(stage.description) + "</p>";
    if (stage.conditional) out += "<p><em>" + esc(stage.conditional) + "</em></p>";

    out += "<h4>Inputs</h4><p>" + esc(stage.inputs) + "</p>";
    out += "<h4>Outputs</h4><p>" + esc(stage.outputs) + "</p>";
    out += "<h4>Implementation</h4><p><code>" + esc(stage.implementation) + "</code></p>";

    out += "<h4>This run's evidence</h4>";
    if (!run) {
      out += '<p class="none">Select a run above.</p>';
    } else if (stageId === "compare_previous_run" && !run.has_previous) {
      out += "<p>" + esc(run.company_display) + " " + esc(run.run_id) + " has no previous run for the same company, so this stage is not applicable here.</p>";
    } else if (stageId === "citation_check") {
      out += renderCitationEvidence(run);
      out += renderEventsList(run, stageId);
    } else if (stageId === "compute_claims") {
      out += renderComputeClaimsEvidence(run);
      out += renderEventsList(run, stageId);
      out += "<p>" + linkOrNone(run.paths.claim_checks_json, "claim_checks.json") + "</p>";
    } else if (stageId === "collect_evidence") {
      out += "<p>" + (run.sources || []).length + " source(s) saved. " + linkOrNone(run.paths.sources_json, "sources.json") + "</p>";
      out += renderEventsList(run, stageId);
    } else if (stageId === "extract_draft") {
      out += "<p>" + linkOrNone(run.paths.memo_json, "memo.json") + (run.memo_error ? "<br>" + statusChipHtml(STATUS.FAILED) + " " + esc(run.memo_error) : "") + "</p>";
      out += renderEventsList(run, stageId);
    } else if (stageId === "render_memo") {
      out += "<p>" + linkOrNone(run.paths.memo_html, "Open memo.html") + " &middot; " + linkOrNone(run.paths.memo_md, "memo.md") + "</p>";
      out += renderEventsList(run, stageId);
    } else if (stageId === "human_review") {
      out += "<p>" + linkOrNone(run.paths.review_md, "Open review.md") + "</p>";
      if (run.has_previous_review_file) out += "<p>" + linkOrNone(run.paths.previous_review_md, "Previous review, carried forward") + "</p>";
    } else if (stageId === "compare_previous_run") {
      out += "<p>" + linkOrNone(run.paths.changes_md, "changes.md") + "</p>";
      out += renderEventsList(run, stageId);
    } else {
      var ev = renderEventsList(run, stageId);
      out += ev || '<p class="none">No event recorded for this stage in this run.</p>';
    }

    out += '<h4>Limitation</h4><div class="limitation-box">' + esc(stage.limitations) + "</div>";
    document.getElementById("side-panel").innerHTML = out;
  }

  document.querySelectorAll(".node").forEach(function (node) {
    node.addEventListener("click", function () {
      document.querySelectorAll(".node").forEach(function (n) { n.classList.remove("selected"); });
      node.classList.add("selected");
      renderStagePanel(node.getAttribute("data-stage"));
    });
    node.addEventListener("keypress", function (e) {
      if (e.key === "Enter" || e.key === " ") node.dispatchEvent(new Event("click"));
    });
  });

  // ---------------------------------------------------------------------
  // Research quality summary
  // ---------------------------------------------------------------------
  function renderQualitySummary() {
    var run = selectedRun();
    var grid = document.getElementById("quality-grid");
    if (!run) { grid.innerHTML = '<p class="none">No run selected.</p>'; return; }
    var memo = run.memo;

    var missing = Object.keys(run.claim_checks || {}).filter(function (n) { return run.claim_checks[n].status !== "ok"; });
    var gaps = (memo && memo.unresolved_gaps) || [];

    var citationCard =
      '<div class="quality-card"><h3>Citation support</h3>' +
      '<div class="quality-headline">' + fmtPct(run.citation.rate) + "</div>" +
      '<div class="quality-sub">' + run.citation.supported + " of " + run.citation.denominator + " tracked factual claims have a saved passage judged to support them.</div>" +
      "<p style=\"font-size:0.8rem;color:var(--muted)\">Excludes opinions and founder questions from the denominator. This is citation support, not independent fact verification.</p></div>";

    var missingCard =
      '<div class="quality-card"><h3>Material missing information</h3>' +
      (missing.length
        ? "<ul>" + missing.map(function (n) {
            var c = run.claim_checks[n];
            return "<li><code>" + esc(n) + "</code>: " + esc(c.error || c.status) + "</li>";
          }).join("") + "</ul>"
        : '<p class="none">No numerical checks are missing data in this run.</p>') +
      "</div>";

    var gapsCard =
      '<div class="quality-card"><h3>Unresolved gaps &amp; conflicting figures</h3>' +
      (gaps.length ? "<ul>" + gaps.map(function (g) { return "<li>" + esc(g) + "</li>"; }).join("") + "</ul>" : '<p class="none">None recorded for this run.</p>') +
      "</div>";

    var reviewStatus = run.review_status === "completed" ? STATUS.COMPLETED : run.review_status === "blank" ? STATUS.AWAITING : STATUS.NOT_RECORDED;
    var reviewCard =
      '<div class="quality-card"><h3>Human review status</h3>' +
      "<p>" + statusChipHtml(reviewStatus) + "</p>" +
      "<p>" + linkOrNone(run.paths.review_md, "Open review.md") + "</p></div>";

    grid.innerHTML = citationCard + missingCard + gapsCard + reviewCard;
  }

  // ---------------------------------------------------------------------
  // Full data (collapsible): sources + event log + claim checks table
  // ---------------------------------------------------------------------
  function renderFullData() {
    var run = selectedRun();
    var body = document.getElementById("full-data-body");
    if (!run) { body.innerHTML = '<p class="none">No run selected.</p>'; return; }

    var out = "<h4>Sources (" + (run.sources || []).length + ")</h4>";
    out += '<table class="data-table"><tr><th>ID</th><th>Title</th><th>Evidence type</th><th>Accessed</th></tr>';
    (run.sources || []).forEach(function (s) {
      var link = s.url ? '<a href="' + esc(s.url) + '" target="_blank" rel="noopener noreferrer">' + esc(s.title) + "</a>" : esc(s.title);
      out += "<tr><td>" + s.id + "</td><td>" + link + "</td><td>" + esc(s.evidence_type) + "</td><td>" + esc(s.accessed_date) + "</td></tr>";
    });
    out += "</table>";

    out += "<h4>Event log</h4>";
    if (!run.events || run.events.length === 0) {
      out += "<p>" + statusChipHtml(STATUS.NOT_RECORDED) + " No stage events were logged for this run.</p>";
    } else {
      out += '<table class="data-table"><tr><th>Stage</th><th>Executor</th><th>Status</th><th>Timestamp</th><th>Notes</th></tr>';
      run.events.forEach(function (e) {
        out += "<tr><td>" + esc(e.stage) + "</td><td>" + esc(e.executor) + "</td><td>" + statusChipHtml(eventStatusToDisplay(e)) + "</td><td>" + esc(e.timestamp) + "</td><td style=\"font-size:0.8rem\">" + esc(e.notes || "") + "</td></tr>";
      });
      out += "</table>";
    }

    if (run.has_previous) {
      out += "<h4>Changes since " + esc(run.previous_id) + "</h4>";
      out += run.changes_md
        ? "<pre style=\"white-space:pre-wrap;font-size:0.85rem;background:var(--paper-alt);padding:0.8em;border-radius:6px;\">" + esc(run.changes_md) + "</pre>"
        : "<p>" + statusChipHtml(STATUS.NOT_RECORDED) + " changes.md was not generated for this run.</p>";
    }

    document.getElementById("full-data-body").innerHTML = out;
  }

  // ---------------------------------------------------------------------
  // Wire it together
  // ---------------------------------------------------------------------
  function onRunChanged() {
    renderHeader();
    renderCompanyCards();
    updateNodeDots();
    var selectedNode = document.querySelector(".node.selected");
    if (selectedNode) renderStagePanel(selectedNode.getAttribute("data-stage"));
    renderQualitySummary();
    renderFullData();
  }

  select.addEventListener("change", onRunChanged);
  updateCommand();
  onRunChanged();
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
                "group": s.group,
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
