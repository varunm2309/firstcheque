"""Command-line helper for the /company-brief skill.

This is the "Python does validation, arithmetic, saved state and
rendering" half of FirstCheque. Claude calls these subcommands with Bash
between its own research/extraction/writing steps -- it never reimplements
JSON bookkeeping or arithmetic inline. Every subcommand is a thin wrapper
over schema.py / claims.py / storage.py / render.py; there is no model
call anywhere in this file.

Usage (see .claude/skills/company-brief/SKILL.md for the full workflow):

    python -m firstcheque.cli new-run "Hisabkitab"
    python -m firstcheque.cli merge-sources memos/hisabkitab/run-001 new_sources.json
    python -m firstcheque.cli validate-memo memos/hisabkitab/run-001
    python -m firstcheque.cli compute-claims memos/hisabkitab/run-001
    python -m firstcheque.cli citation-check memos/hisabkitab/run-001
    python -m firstcheque.cli render memos/hisabkitab/run-001
    python -m firstcheque.cli finalize-run memos/hisabkitab/run-001 --tools-used WebSearch,WebFetch
    python -m firstcheque.cli status "Hisabkitab"
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from pydantic import ValidationError

from . import claims as claims_mod
from . import storage
from .render import render_changes_md, render_memo_html, render_memo_md
from .schema import (
    ClaimCheckResult,
    ClaimCheckStatus,
    Claim,
    Fact,
    Memo,
    MetricKind,
    Period,
    RunMeta,
    Source,
    citation_support_rate,
)
from .stages import STAGES_BY_ID, VALID_EXECUTORS, VALID_STATUSES
from . import workflow_viewer


def _print(*args) -> None:
    print(*args)


# ---------------------------------------------------------------------------
# new-run
# ---------------------------------------------------------------------------


def cmd_new_run(args: argparse.Namespace) -> int:
    company = args.company
    previous = storage.latest_run(company)
    run_dir = storage.new_run_dir(company)
    run_id = run_dir.name

    previous_sources: list[Source] = []
    if previous is not None:
        previous_sources = storage.load_sources(previous)
        storage.write_json(run_dir / "sources.json", [s.model_dump(mode="json") for s in previous_sources])

    storage.write_review_template(run_dir, company, run_id, previous)

    artifacts = ["review.md"] + (["sources.json"] if previous_sources else [])
    storage.append_event(
        run_dir, "start_run", "python", "ok", artifacts=artifacts,
        notes=f"previous_run={previous.name if previous else None}",
    )

    _print(f"run_dir={run_dir}")
    _print(f"run_id={run_id}")
    _print(f"previous_run_dir={previous or ''}")
    if previous_sources:
        _print(f"carried_forward_sources={len(previous_sources)}")
        today = date.today()
        for s in previous_sources:
            age = storage.evidence_age_days(s, today)
            _print(f"  source[{s.id}] age_days={age} title={s.title!r}")
    else:
        _print("carried_forward_sources=0")
    return 0


# ---------------------------------------------------------------------------
# merge-sources
# ---------------------------------------------------------------------------


def cmd_merge_sources(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    current = storage.load_sources(run_dir)
    new_raw = json.loads(Path(args.new_sources_file).read_text(encoding="utf-8"))
    # IDs in the incoming file are placeholders; merge_sources assigns real ones.
    new_sources = [Source.model_validate({**item, "id": item.get("id") or 1}) for item in new_raw]

    result = storage.merge_sources(current, new_sources)
    storage.write_json(run_dir / "sources.json", [s.model_dump(mode="json") for s in result.merged])

    storage.append_event(
        run_dir, "collect_evidence", "python", "ok", artifacts=["sources.json"],
        notes=f"reused={len(result.reused_ids)} new={len(result.new_ids)} total={len(result.merged)}",
    )

    _print(f"reused={len(result.reused_ids)} new={len(result.new_ids)} total={len(result.merged)}")
    if result.new_ids:
        _print("new_source_ids=" + ",".join(str(i) for i in result.new_ids))
    return 0


# ---------------------------------------------------------------------------
# validate-memo
# ---------------------------------------------------------------------------


def cmd_validate_memo(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    data = storage.read_json(run_dir / "memo.json")
    if data is None:
        _print("ERROR: memo.json not found")
        storage.append_event(run_dir, "extract_draft", "python", "error", error="memo.json not found")
        return 1
    try:
        Memo.model_validate(data)
    except ValidationError as e:
        _print("INVALID:")
        _print(str(e))
        storage.append_event(run_dir, "extract_draft", "python", "error", error=str(e)[:500])
        return 1

    sources = {s.id for s in storage.load_sources(run_dir)}
    memo = Memo.model_validate(data)
    bad_refs = []
    for claim in memo.claims:
        for sid in claim.source_ids:
            if sid not in sources:
                bad_refs.append((claim.id, sid))
    if bad_refs:
        _print("INVALID: citations reference unknown source ids:")
        for claim_id, sid in bad_refs:
            _print(f"  claim {claim_id} -> source {sid} (not in sources.json)")
        storage.append_event(
            run_dir, "extract_draft", "python", "error",
            error=f"{len(bad_refs)} claim(s) cite unknown source ids",
        )
        return 1

    storage.append_event(
        run_dir, "extract_draft", "python", "ok", artifacts=["memo.json"],
        notes="Schema and citation-ID validation only; this timestamp is when memo.json was "
        "last checked, not when it was written or researched.",
    )
    _print("OK")
    return 0


# ---------------------------------------------------------------------------
# compute-claims
# ---------------------------------------------------------------------------


def _resolve_annual_revenue(n) -> Optional[Fact]:
    """Prefer a directly disclosed annual revenue figure. Fall back to
    annualising monthly revenue (still pure Python arithmetic, computed
    once here rather than duplicated in both valuation_multiple and
    capital_efficiency)."""
    if n.annual_revenue is not None:
        return n.annual_revenue
    if n.monthly_revenue_current is None:
        return None
    run_rate = claims_mod.annualised_revenue_run_rate(n.monthly_revenue_current)
    if run_rate.status != ClaimCheckStatus.OK:
        return None
    return Fact(
        label="derived_annual_revenue",
        value=run_rate.result,
        currency=n.monthly_revenue_current.currency,
        metric_kind=MetricKind.REVENUE,
        period=Period.ANNUAL,
        definition="Derived by annualising monthly_revenue_current (x12); not a directly disclosed annual figure.",
        as_of=n.monthly_revenue_current.as_of,
        source_ids=n.monthly_revenue_current.source_ids,
    )


def cmd_compute_claims(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    memo = storage.load_memo(run_dir)
    if memo is None:
        _print("ERROR: memo.json not found or invalid")
        return 1
    n = memo.numbers
    annual_revenue = _resolve_annual_revenue(n)

    results: dict[str, ClaimCheckResult] = {
        "annualised_revenue_run_rate": claims_mod.annualised_revenue_run_rate(n.monthly_revenue_current),
        "valuation_multiple": claims_mod.valuation_multiple(n.valuation, annual_revenue),
        "arpu": claims_mod.arpu(n.monthly_revenue_current, n.paying_customers),
        "growth_multiple": claims_mod.growth_multiple(n.monthly_revenue_current, n.monthly_revenue_earlier),
        "capital_efficiency": claims_mod.capital_efficiency(n.total_capital_raised, annual_revenue),
        "capital_raised_per_year": claims_mod.capital_raised_per_year(
            n.total_capital_raised, n.years_since_founding
        ),
        "implied_dilution": claims_mod.implied_dilution(n.latest_round_size, n.post_money_valuation),
    }

    storage.write_json(
        run_dir / "claim_checks.json", {name: r.model_dump(mode="json") for name, r in results.items()}
    )
    ok_count = sum(1 for r in results.values() if r.status == ClaimCheckStatus.OK)
    storage.append_event(
        run_dir, "compute_claims", "python", "ok", artifacts=["claim_checks.json"],
        notes=f"{ok_count}/{len(results)} checks computed",
    )

    for name, r in results.items():
        if r.status.value == "ok":
            _print(f"{name}: {r.result_display}")
        else:
            _print(f"{name}: {r.status.value} ({r.error})")
    return 0


# ---------------------------------------------------------------------------
# citation-check
# ---------------------------------------------------------------------------


def cmd_citation_check(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    memo = storage.load_memo(run_dir)
    if memo is None:
        _print("ERROR: memo.json not found or invalid")
        return 1
    source_ids = {s.id for s in storage.load_sources(run_dir)}

    dangling = [c for c in memo.claims if any(sid not in source_ids for sid in c.source_ids)]
    unverified = [c for c in memo.claims if c.unverified and not c.is_opinion_or_question]
    supported, denominator, rate = citation_support_rate(memo.claims)

    if dangling:
        _print("ERROR: claims cite unknown source ids:")
        for c in dangling:
            _print(f"  {c.id}: {c.text!r} -> {c.source_ids}")
        storage.append_event(
            run_dir, "citation_check", "python", "error",
            error=f"{len(dangling)} claim(s) cite unknown source ids",
        )
        return 1

    rate_str = f"{rate:.0%} ({supported}/{denominator})" if rate is not None else "n/a"
    storage.append_event(
        run_dir, "citation_check", "python", "ok",
        notes=f"citation_support_rate={rate_str}; unverified_claims={len(unverified)}. "
        "Structural check + rate calculation only -- the supported/unsupported judgment on "
        "each claim was made by Claude when it wrote memo.json, not at this timestamp.",
    )

    _print(f"tracked_claims={len(memo.claims)}")
    _print(f"unverified_claims={len(unverified)}")
    for c in unverified:
        _print(f"  unverified: {c.id}: {c.text!r}")
    if rate is None:
        _print("citation_support_rate=n/a (denominator is 0)")
    else:
        _print(f"citation_support_rate={rate:.0%} ({supported}/{denominator})")
    return 0


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------


def cmd_render(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    memo = storage.load_memo(run_dir)
    if memo is None:
        _print("ERROR: memo.json not found or invalid")
        return 1
    sources = storage.load_sources(run_dir)
    checks = storage.load_claim_checks(run_dir)

    memo_md = render_memo_md(memo, sources, checks)
    (run_dir / "memo.md").write_text(memo_md, encoding="utf-8")
    _print(f"wrote {run_dir / 'memo.md'} ({len(memo_md.split())} words)")

    memo_html = render_memo_html(memo, sources, checks)
    (run_dir / "memo.html").write_text(memo_html, encoding="utf-8")
    _print(f"wrote {run_dir / 'memo.html'}")

    storage.append_event(run_dir, "render_memo", "python", "ok", artifacts=["memo.md", "memo.html"])
    return 0


# ---------------------------------------------------------------------------
# diff (changes.md against the previous run for the same company)
# ---------------------------------------------------------------------------


def cmd_diff(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    company = args.company
    runs = storage.list_runs(company)
    try:
        idx = runs.index(run_dir)
    except ValueError:
        _print(f"ERROR: {run_dir} is not a known run for {company!r}")
        return 1
    if idx == 0:
        _print("no previous run to diff against (this is run-001)")
        storage.append_event(
            run_dir, "compare_previous_run", "python", "skipped",
            notes="no previous run exists for this company",
        )
        return 0
    previous_dir = runs[idx - 1]

    previous_memo = storage.load_memo(previous_dir)
    current_memo = storage.load_memo(run_dir)
    if previous_memo is None or current_memo is None:
        _print("ERROR: both runs need a valid memo.json to diff")
        storage.append_event(
            run_dir, "compare_previous_run", "python", "error", error="missing memo.json in one of the two runs"
        )
        return 1

    previous_checks = storage.load_claim_checks(previous_dir)
    current_checks = storage.load_claim_checks(run_dir)

    previous_sources = {s.id for s in storage.load_sources(previous_dir)}
    current_sources = storage.load_sources(run_dir)
    new_source_count = len([s for s in current_sources if s.id not in previous_sources])

    changes_md = render_changes_md(
        company=company,
        previous_run_id=previous_dir.name,
        current_run_id=run_dir.name,
        previous_memo=previous_memo,
        current_memo=current_memo,
        previous_checks=previous_checks,
        current_checks=current_checks,
        new_source_count=new_source_count,
    )
    (run_dir / "changes.md").write_text(changes_md, encoding="utf-8")
    storage.append_event(
        run_dir, "compare_previous_run", "python", "ok", artifacts=["changes.md"],
        notes=f"vs {previous_dir.name}",
    )
    _print(f"wrote {run_dir / 'changes.md'}")
    return 0


# ---------------------------------------------------------------------------
# finalize-run
# ---------------------------------------------------------------------------


def cmd_finalize_run(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    company = args.company
    runs = storage.list_runs(company)
    try:
        idx = runs.index(run_dir)
    except ValueError:
        _print(f"ERROR: {run_dir} is not a known run for {company!r}")
        return 1
    previous_dir = runs[idx - 1] if idx > 0 else None

    existing = storage.load_run_meta(run_dir)
    now = datetime.now()
    meta = RunMeta(
        company=company,
        run_id=run_dir.name,
        created_at=existing.created_at if existing else now,
        updated_at=now if existing else None,
        previous_run_id=previous_dir.name if previous_dir else None,
        evidence_reused_from=previous_dir.name if previous_dir else None,
        tools_used=args.tools_used.split(",") if args.tools_used else [],
        execution_notes=args.notes,
    )
    storage.write_json(run_dir / "run.json", meta)
    if existing:
        _print(f"wrote {run_dir / 'run.json'} (created_at preserved from {existing.created_at.isoformat()})")
    else:
        _print(f"wrote {run_dir / 'run.json'}")
    return 0


# ---------------------------------------------------------------------------
# log-event -- for Claude to record the stages it does itself (planning,
# research, extraction judgment, citation-support judgment, the follow-up
# pass). The python-only stages log themselves automatically above; this
# command exists so the stages that only Claude can attest to still end up
# in the same real, timestamped event log.
# ---------------------------------------------------------------------------


def cmd_log_event(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    if not run_dir.exists():
        _print(f"ERROR: {run_dir} does not exist")
        return 1
    if args.stage not in STAGES_BY_ID:
        _print(f"ERROR: unknown stage {args.stage!r}. Valid stages: {', '.join(sorted(STAGES_BY_ID))}")
        return 1
    if args.executor not in VALID_EXECUTORS:
        _print(f"ERROR: unknown executor {args.executor!r}. Valid: {', '.join(sorted(VALID_EXECUTORS))}")
        return 1
    if args.status not in VALID_STATUSES:
        _print(f"ERROR: unknown status {args.status!r}. Valid: {', '.join(sorted(VALID_STATUSES))}")
        return 1

    artifacts = [a.strip() for a in args.artifacts.split(",") if a.strip()] if args.artifacts else []
    event = storage.append_event(
        run_dir, args.stage, args.executor, args.status,
        artifacts=artifacts, error=args.error, notes=args.notes,
    )
    _print(f"logged: {event['stage']} executor={event['executor']} status={event['status']} at {event['timestamp']}")
    return 0


# ---------------------------------------------------------------------------
# generate-workflow-viewer
# ---------------------------------------------------------------------------


def cmd_generate_workflow_viewer(args: argparse.Namespace) -> int:
    out_path, run_count = workflow_viewer.generate(Path(args.out))
    _print(f"wrote {out_path} ({run_count} run(s) embedded)")
    return 0


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    company = args.company
    runs = storage.list_runs(company)
    if not runs:
        _print(f"no runs yet for {company!r}")
        return 0
    for run_dir in runs:
        memo = storage.load_memo(run_dir)
        review_path = run_dir / "review.md"
        review_status = "no review.md"
        if review_path.exists():
            review_status = (
                "blank"
                if storage.is_review_blank(review_path.read_text(encoding="utf-8"))
                else "completed"
            )
        rec = memo.recommendation.value if memo else "n/a"
        _print(f"{run_dir.name}: recommendation={rec} review={review_status}")
    return 0


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m firstcheque.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("new-run", help="allocate the next run-NNN folder for a company")
    p.add_argument("company")
    p.set_defaults(func=cmd_new_run)

    p = sub.add_parser("merge-sources", help="merge newly-found sources into a run's sources.json")
    p.add_argument("run_dir")
    p.add_argument("new_sources_file")
    p.set_defaults(func=cmd_merge_sources)

    p = sub.add_parser("validate-memo", help="validate memo.json against the Memo schema")
    p.add_argument("run_dir")
    p.set_defaults(func=cmd_validate_memo)

    p = sub.add_parser("compute-claims", help="run all claim-check formulas and write claim_checks.json")
    p.add_argument("run_dir")
    p.set_defaults(func=cmd_compute_claims)

    p = sub.add_parser("citation-check", help="check citation structure and print the support rate")
    p.add_argument("run_dir")
    p.set_defaults(func=cmd_citation_check)

    p = sub.add_parser("render", help="render memo.md from memo.json + claim_checks.json + sources.json")
    p.add_argument("run_dir")
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("diff", help="write changes.md against the previous run")
    p.add_argument("run_dir")
    p.add_argument("company")
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("finalize-run", help="write run.json execution metadata")
    p.add_argument("run_dir")
    p.add_argument("company")
    p.add_argument("--tools-used", default="")
    p.add_argument("--notes", default=None)
    p.set_defaults(func=cmd_finalize_run)

    p = sub.add_parser("status", help="list runs for a company and their review status")
    p.add_argument("company")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("log-event", help="record a real stage-transition event for a run (Claude-executed stages)")
    p.add_argument("run_dir")
    p.add_argument("--stage", required=True, help="one of the ids in stages.py STAGES")
    p.add_argument("--executor", required=True, choices=sorted(VALID_EXECUTORS))
    p.add_argument("--status", required=True, choices=sorted(VALID_STATUSES))
    p.add_argument("--artifacts", default="", help="comma-separated relative paths written/used")
    p.add_argument("--error", default=None)
    p.add_argument("--notes", default=None)
    p.set_defaults(func=cmd_log_event)

    p = sub.add_parser(
        "generate-workflow-viewer",
        help="regenerate workflow.html from all saved runs under memos/",
    )
    p.add_argument("--out", default="workflow.html", help="output path (default: workflow.html at repo root)")
    p.set_defaults(func=cmd_generate_workflow_viewer)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
