"""Tests for the CLI's annual-revenue resolution helper: it must prefer a
directly disclosed annual figure, and only fall back to annualising a
monthly one (still via claims.py, not ad hoc arithmetic) when no annual
figure was given. Also covers the run.json created_at/updated_at fix:
re-running finalize-run must never overwrite when a run was ORIGINALLY
created, since that timestamp is what tells a later reader "the research
happened around here," separate from any later recomputation."""

import time
from datetime import date

from firstcheque import claims as claims_mod
from firstcheque import storage
from firstcheque.cli import _resolve_annual_revenue, main
from firstcheque.schema import Currency, Fact, MetricKind, Numbers, Period


def test_prefers_directly_disclosed_annual_revenue():
    annual = Fact(
        label="annual_revenue",
        value=290_000_000,
        currency=Currency.INR,
        metric_kind=MetricKind.REVENUE,
        period=Period.ANNUAL,
        source_ids=[1],
    )
    monthly = Fact(
        label="monthly_revenue_current",
        value=1_000_000,
        currency=Currency.INR,
        metric_kind=MetricKind.REVENUE,
        period=Period.MONTHLY,
        source_ids=[2],
    )
    numbers = Numbers(annual_revenue=annual, monthly_revenue_current=monthly)
    resolved = _resolve_annual_revenue(numbers)
    assert resolved is annual


def test_derives_annual_revenue_from_monthly_when_no_annual_given():
    monthly = Fact(
        label="monthly_revenue_current",
        value=2_501_000,
        currency=Currency.INR,
        metric_kind=MetricKind.REVENUE,
        period=Period.MONTHLY,
        as_of=date(2026, 6, 30),
        source_ids=[1],
    )
    numbers = Numbers(monthly_revenue_current=monthly)
    resolved = _resolve_annual_revenue(numbers)
    assert resolved is not None
    assert resolved.value == 30_012_000
    assert resolved.currency == Currency.INR
    assert resolved.metric_kind == MetricKind.REVENUE
    assert resolved.source_ids == [1]


def test_returns_none_when_neither_disclosed():
    numbers = Numbers()
    assert _resolve_annual_revenue(numbers) is None


# ---------------------------------------------------------------------------
# finalize-run: created_at must survive a second call (the real bug: it was
# being overwritten with datetime.now() every time, which would have made
# a later Python-only re-run look like the research itself happened then).
# ---------------------------------------------------------------------------


def test_finalize_run_preserves_created_at_on_second_call(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run_dir = storage.new_run_dir("Acme")

    assert main(["finalize-run", str(run_dir), "Acme", "--notes", "first pass"]) == 0
    first_meta = storage.load_run_meta(run_dir)
    assert first_meta.updated_at is None

    time.sleep(0.01)
    assert main(["finalize-run", str(run_dir), "Acme", "--notes", "second pass, e.g. after a bugfix"]) == 0
    second_meta = storage.load_run_meta(run_dir)

    assert second_meta.created_at == first_meta.created_at
    assert second_meta.updated_at is not None
    assert second_meta.updated_at > second_meta.created_at
    assert second_meta.execution_notes == "second pass, e.g. after a bugfix"
