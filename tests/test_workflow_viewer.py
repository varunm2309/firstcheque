"""Tests for the stage-event log (storage.append_event/load_events) and the
workflow.html generator (workflow_viewer.py): honest discovery of real
runs only, no fabricated events, and safe embedding of untrusted text."""

import json
import re
from datetime import date, datetime

import pytest

from firstcheque import storage, workflow_viewer
from firstcheque.schema import (
    Claim,
    Currency,
    EvidenceType,
    Fact,
    Memo,
    MetricKind,
    Numbers,
    Recommendation,
    RiskItem,
    Snapshot,
    Source,
)


# ---------------------------------------------------------------------------
# Event log
# ---------------------------------------------------------------------------


def test_append_and_load_events(tmp_path):
    run_dir = tmp_path / "run-001"
    run_dir.mkdir()
    storage.append_event(run_dir, "compute_claims", "python", "ok", artifacts=["claim_checks.json"])
    storage.append_event(run_dir, "citation_check", "claude", "ok", notes="83% support")

    events = storage.load_events(run_dir)
    assert len(events) == 2
    assert events[0]["stage"] == "compute_claims"
    assert events[0]["executor"] == "python"
    assert events[0]["artifacts"] == ["claim_checks.json"]
    assert "timestamp" in events[0]


def test_load_events_empty_when_no_file(tmp_path):
    run_dir = tmp_path / "run-001"
    run_dir.mkdir()
    assert storage.load_events(run_dir) == []


def test_events_for_stage_filters(tmp_path):
    run_dir = tmp_path / "run-001"
    run_dir.mkdir()
    storage.append_event(run_dir, "render_memo", "python", "ok")
    storage.append_event(run_dir, "compute_claims", "python", "ok")
    assert len(storage.events_for_stage(run_dir, "render_memo")) == 1
    assert len(storage.events_for_stage(run_dir, "does_not_exist")) == 0


def test_append_is_additive_not_overwriting(tmp_path):
    run_dir = tmp_path / "run-001"
    run_dir.mkdir()
    for i in range(3):
        storage.append_event(run_dir, "render_memo", "python", "ok", notes=f"pass {i}")
    events = storage.load_events(run_dir)
    assert len(events) == 3
    assert [e["notes"] for e in events] == ["pass 0", "pass 1", "pass 2"]


# ---------------------------------------------------------------------------
# discover_runs: only real saved data, nothing inferred
# ---------------------------------------------------------------------------


def _minimal_memo() -> Memo:
    return Memo(
        company="Testly",
        one_liner="A fictional test company.",
        recommendation=Recommendation.TRACK,
        recommendation_rationale="Two sentences of rationale for the test fixture.",
        snapshot=Snapshot(founded="2022"),
        business_model="Sells things.",
        why_now="Now is fine.",
        market_and_competition="Some market.",
        traction_and_team="Some traction.",
        numbers=Numbers(),
        claims=[Claim(id="c1", text="A claim.", section="business_model", source_ids=[1], supported=True)],
        risks=[
            RiskItem(risk="r1", mitigant="m1"),
            RiskItem(risk="r2", mitigant="m2"),
            RiskItem(risk="r3", mitigant="m3"),
        ],
        what_would_change_recommendation="Nothing in particular.",
        founder_questions=[f"Q{i}?" for i in range(1, 6)],
        generated_at=datetime(2026, 1, 1, 12, 0, 0),
    )


def _write_run(root, company_slug, run_id, memo=None, with_events=False):
    run_dir = root / company_slug / run_id
    run_dir.mkdir(parents=True)
    if memo is not None:
        storage.write_json(run_dir / "memo.json", memo)
    src = Source(
        id=1,
        title="A <script>evil()</script> source title",
        url="https://example.com/a",
        accessed_date=date(2026, 1, 1),
        passage="A passage with <script>alert('x')</script> embedded, and a claim & an ampersand.",
        evidence_type=EvidenceType.SEARCH_SNIPPET,
    )
    storage.write_json(run_dir / "sources.json", [src.model_dump(mode="json")])
    (run_dir / "review.md").write_text("# Review\n\n<!-- blank -->\n", encoding="utf-8")
    if with_events:
        storage.append_event(run_dir, "render_memo", "python", "ok", artifacts=["memo.html"])
    return run_dir


def test_discover_runs_finds_all_runs_across_companies(tmp_path):
    root = tmp_path / "memos"
    _write_run(root, "acme", "run-001", memo=_minimal_memo())
    _write_run(root, "acme", "run-002", memo=_minimal_memo())
    _write_run(root, "beta", "run-001", memo=_minimal_memo())

    runs = workflow_viewer.discover_runs(root)
    ids = sorted(r["id"] for r in runs)
    assert ids == ["acme/run-001", "acme/run-002", "beta/run-001"]


def test_discover_runs_tracks_previous_run_linkage(tmp_path):
    root = tmp_path / "memos"
    _write_run(root, "acme", "run-001", memo=_minimal_memo())
    _write_run(root, "acme", "run-002", memo=_minimal_memo())

    runs = {r["id"]: r for r in workflow_viewer.discover_runs(root)}
    assert runs["acme/run-001"]["has_previous"] is False
    assert runs["acme/run-002"]["has_previous"] is True
    assert runs["acme/run-002"]["previous_id"] == "acme/run-001"


def test_discover_runs_no_memo_json_is_not_invented(tmp_path):
    root = tmp_path / "memos"
    _write_run(root, "acme", "run-001", memo=None)  # no memo.json at all

    runs = workflow_viewer.discover_runs(root)
    assert len(runs) == 1
    assert runs[0]["memo"] is None
    assert runs[0]["citation"]["denominator"] == 0


def test_discover_runs_missing_events_reported_as_empty_not_fabricated(tmp_path):
    root = tmp_path / "memos"
    _write_run(root, "acme", "run-001", memo=_minimal_memo(), with_events=False)
    runs = workflow_viewer.discover_runs(root)
    assert runs[0]["events"] == []


def test_discover_runs_empty_root_returns_empty_list(tmp_path):
    assert workflow_viewer.discover_runs(tmp_path / "does-not-exist") == []


# ---------------------------------------------------------------------------
# HTML generation: safe embedding, self-contained, no external assets
# ---------------------------------------------------------------------------


def test_build_viewer_html_is_self_contained_and_escapes_script_tags(tmp_path):
    root = tmp_path / "memos"
    _write_run(root, "acme", "run-001", memo=_minimal_memo(), with_events=True)
    runs = workflow_viewer.discover_runs(root)

    doc = workflow_viewer.build_viewer_html(runs)

    assert doc.startswith("<!DOCTYPE html>")
    assert 'rel="stylesheet"' not in doc  # no external CSS
    assert "cdn." not in doc.lower()
    # The malicious source title/passage must never produce a literal closing
    # </script> that would break out of the embedded JSON <script> block.
    for block_start in [m.start() for m in re.finditer(r'<script id="firstcheque-data"[^>]*>', doc)]:
        block_end = doc.index("</script>", block_start)
        block = doc[block_start:block_end]
        assert "</script" not in block or "<\\/script" in block

    # And the underlying data must still round-trip losslessly once parsed.
    m = re.search(r'<script id="firstcheque-data" type="application/json">(.*?)</script>', doc, re.S)
    data = json.loads(m.group(1).replace("<\\/script", "</script"))
    passage = data["runs"][0]["sources"][0]["passage"]
    assert "<script>alert('x')</script>" in passage


def test_build_viewer_html_contains_all_stage_nodes(tmp_path):
    root = tmp_path / "memos"
    _write_run(root, "acme", "run-001", memo=_minimal_memo())
    runs = workflow_viewer.discover_runs(root)
    doc = workflow_viewer.build_viewer_html(runs)

    for stage_id in workflow_viewer.LAYOUT:
        assert f'data-stage="{stage_id}"' in doc


def test_build_viewer_html_handles_zero_runs():
    doc = workflow_viewer.build_viewer_html([])
    assert doc.startswith("<!DOCTYPE html>")
    assert '"runs": []' in doc.replace(" ", "") or '"runs":[]' in doc.replace(" ", "")


def test_generate_writes_file_and_returns_run_count(tmp_path):
    root = tmp_path / "memos"
    _write_run(root, "acme", "run-001", memo=_minimal_memo())
    out_path = tmp_path / "workflow.html"

    written_path, count = workflow_viewer.generate(out_path, root=root)

    assert written_path == out_path
    assert count == 1
    assert out_path.exists()
    assert out_path.read_text(encoding="utf-8").startswith("<!DOCTYPE html>")
