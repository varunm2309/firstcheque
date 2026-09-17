"""Unit tests for every claim-check formula in firstcheque.claims.

The primary fixture is the fictional company from the project brief:
    monthly revenue (current): INR 25.01 L  = 2,501,000
    monthly revenue (earlier): INR 4.18 L   =   418,000
    valuation:                 INR 20 Cr    = 200,000,000
    paying customers:          2,700

Expected (rounded) results:
    annualised revenue   ~= INR 3 crore
    valuation multiple   ~= 6.7x
    monthly ARPU         ~= INR 926
    growth multiple      ~= 6.0x
"""

from datetime import date

import pytest

from firstcheque import claims
from firstcheque.schema import ClaimCheckStatus, Currency, Fact, MetricKind, Period


def fact(**kwargs) -> Fact:
    defaults = dict(source_ids=[1])
    defaults.update(kwargs)
    return Fact(**defaults)


@pytest.fixture
def monthly_revenue_current() -> Fact:
    return fact(
        label="monthly_revenue_current",
        value=2_501_000,
        currency=Currency.INR,
        metric_kind=MetricKind.REVENUE,
        period=Period.MONTHLY,
        as_of=date(2026, 6, 30),
    )


@pytest.fixture
def monthly_revenue_earlier() -> Fact:
    return fact(
        label="monthly_revenue_earlier",
        value=418_000,
        currency=Currency.INR,
        metric_kind=MetricKind.REVENUE,
        period=Period.MONTHLY,
        as_of=date(2025, 6, 30),
    )


@pytest.fixture
def valuation() -> Fact:
    return fact(
        label="valuation",
        value=200_000_000,
        currency=Currency.INR,
        metric_kind=MetricKind.VALUATION,
        period=Period.POINT_IN_TIME,
    )


@pytest.fixture
def paying_customers() -> Fact:
    return fact(
        label="paying_customers",
        value=2_700,
        metric_kind=MetricKind.PAYING_CUSTOMERS,
        unit="customers",
    )


# ---------------------------------------------------------------------------
# 1. Annualised revenue run rate
# ---------------------------------------------------------------------------


def test_annualised_revenue_run_rate(monthly_revenue_current):
    result = claims.annualised_revenue_run_rate(monthly_revenue_current)
    assert result.status == ClaimCheckStatus.OK
    assert result.result == pytest.approx(30_012_000)
    assert result.result_display == "INR 3.00 Cr"
    assert result.source_ids == [1]


def test_annualised_revenue_run_rate_missing_input():
    result = claims.annualised_revenue_run_rate(None)
    assert result.status == ClaimCheckStatus.MISSING_INPUT
    assert result.result is None


def test_annualised_revenue_run_rate_rejects_gmv():
    gmv = fact(label="gmv", value=1_000_000, currency=Currency.INR, metric_kind=MetricKind.GMV)
    result = claims.annualised_revenue_run_rate(gmv)
    assert result.status == ClaimCheckStatus.METRIC_MISMATCH


# ---------------------------------------------------------------------------
# 2. Valuation multiple
# ---------------------------------------------------------------------------


def test_valuation_multiple(valuation, monthly_revenue_current):
    result = claims.valuation_multiple(valuation, monthly_revenue_current)
    assert result.status == ClaimCheckStatus.OK
    # exact calculation, not the rounded display value
    assert result.result == pytest.approx(200_000_000 / 30_012_000)
    assert result.result_display == "6.7x"
    assert "heuristic" in result.interpretation.lower()


def test_valuation_multiple_currency_mismatch(monthly_revenue_current):
    usd_valuation = fact(
        label="valuation", value=25_000_000, currency=Currency.USD, metric_kind=MetricKind.VALUATION
    )
    result = claims.valuation_multiple(usd_valuation, monthly_revenue_current)
    assert result.status == ClaimCheckStatus.CURRENCY_MISMATCH
    assert result.result is None


def test_valuation_multiple_zero_revenue(valuation):
    zero_revenue = fact(
        label="monthly_revenue_current", value=0, currency=Currency.INR, metric_kind=MetricKind.REVENUE
    )
    result = claims.valuation_multiple(valuation, zero_revenue)
    assert result.status == ClaimCheckStatus.ZERO_DENOMINATOR


# ---------------------------------------------------------------------------
# 3. ARPU
# ---------------------------------------------------------------------------


def test_arpu(monthly_revenue_current, paying_customers):
    result = claims.arpu(monthly_revenue_current, paying_customers)
    assert result.status == ClaimCheckStatus.OK
    assert result.result == pytest.approx(2_501_000 / 2_700)
    assert result.result_display == "INR 926 per month"


def test_arpu_rejects_registered_users(monthly_revenue_current):
    registered = fact(
        label="registered_users", value=50_000, metric_kind=MetricKind.REGISTERED_USERS
    )
    result = claims.arpu(monthly_revenue_current, registered)
    assert result.status == ClaimCheckStatus.METRIC_MISMATCH


def test_arpu_zero_customers(monthly_revenue_current):
    zero_customers = fact(label="paying_customers", value=0, metric_kind=MetricKind.PAYING_CUSTOMERS)
    result = claims.arpu(monthly_revenue_current, zero_customers)
    assert result.status == ClaimCheckStatus.ZERO_DENOMINATOR


# ---------------------------------------------------------------------------
# 4. Growth multiple
# ---------------------------------------------------------------------------


def test_growth_multiple(monthly_revenue_current, monthly_revenue_earlier):
    result = claims.growth_multiple(monthly_revenue_current, monthly_revenue_earlier)
    assert result.status == ClaimCheckStatus.OK
    assert result.result == pytest.approx(2_501_000 / 418_000)
    assert result.result_display == "6.0x"
    assert any("2025-06-30" in a and "2026-06-30" in a for a in result.assumptions)


def test_growth_multiple_missing_earlier(monthly_revenue_current):
    result = claims.growth_multiple(monthly_revenue_current, None)
    assert result.status == ClaimCheckStatus.MISSING_INPUT


# ---------------------------------------------------------------------------
# 5. Capital efficiency
# ---------------------------------------------------------------------------


def test_capital_efficiency(monthly_revenue_current):
    raised = fact(
        label="total_capital_raised", value=50_000_000, currency=Currency.INR, metric_kind=MetricKind.CAPITAL_RAISED
    )
    result = claims.capital_efficiency(raised, monthly_revenue_current)
    assert result.status == ClaimCheckStatus.OK
    assert result.result == pytest.approx(30_012_000 / 50_000_000)


def test_capital_efficiency_zero_raised(monthly_revenue_current):
    raised = fact(
        label="total_capital_raised", value=0, currency=Currency.INR, metric_kind=MetricKind.CAPITAL_RAISED
    )
    result = claims.capital_efficiency(raised, monthly_revenue_current)
    assert result.status == ClaimCheckStatus.ZERO_DENOMINATOR


# ---------------------------------------------------------------------------
# 6. Capital raised per year
# ---------------------------------------------------------------------------


def test_capital_raised_per_year():
    raised = fact(
        label="total_capital_raised", value=40_000_000, currency=Currency.INR, metric_kind=MetricKind.CAPITAL_RAISED
    )
    years = fact(label="years_since_founding", value=4, metric_kind=MetricKind.YEARS)
    result = claims.capital_raised_per_year(raised, years)
    assert result.status == ClaimCheckStatus.OK
    assert result.result == pytest.approx(10_000_000)
    assert result.result_display == "INR 1.00 Cr per year"


def test_capital_raised_per_year_zero_years():
    raised = fact(
        label="total_capital_raised", value=40_000_000, currency=Currency.INR, metric_kind=MetricKind.CAPITAL_RAISED
    )
    years = fact(label="years_since_founding", value=0, metric_kind=MetricKind.YEARS)
    result = claims.capital_raised_per_year(raised, years)
    assert result.status == ClaimCheckStatus.ZERO_DENOMINATOR


# ---------------------------------------------------------------------------
# 7. Implied dilution (post-money assumption)
# ---------------------------------------------------------------------------


def test_implied_dilution_below_threshold():
    round_size = fact(
        label="latest_round_size", value=10_000_000, currency=Currency.INR, metric_kind=MetricKind.ROUND_SIZE
    )
    post_money = fact(
        label="post_money_valuation", value=100_000_000, currency=Currency.INR, metric_kind=MetricKind.VALUATION
    )
    result = claims.implied_dilution(round_size, post_money)
    assert result.status == ClaimCheckStatus.OK
    assert result.result == pytest.approx(0.10)
    assert result.result_display == "10.0%"
    assert "below" in result.interpretation.lower()
    assert "post-money" in result.assumptions[0].lower()


def test_implied_dilution_at_threshold_flagged():
    round_size = fact(
        label="latest_round_size", value=25_000_000, currency=Currency.INR, metric_kind=MetricKind.ROUND_SIZE
    )
    post_money = fact(
        label="post_money_valuation", value=100_000_000, currency=Currency.INR, metric_kind=MetricKind.VALUATION
    )
    result = claims.implied_dilution(round_size, post_money)
    assert result.result == pytest.approx(0.25)
    assert "heuristic threshold" in result.interpretation.lower()


def test_implied_dilution_currency_mismatch():
    round_size = fact(
        label="latest_round_size", value=2_000_000, currency=Currency.USD, metric_kind=MetricKind.ROUND_SIZE
    )
    post_money = fact(
        label="post_money_valuation", value=100_000_000, currency=Currency.INR, metric_kind=MetricKind.VALUATION
    )
    result = claims.implied_dilution(round_size, post_money)
    assert result.status == ClaimCheckStatus.CURRENCY_MISMATCH


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        (2_501_000, "INR 25.01 L"),
        (200_000_000, "INR 20.00 Cr"),
        (30_012_000, "INR 3.00 Cr"),
        (99_999, "INR 99,999"),
        (926.296296, "INR 926"),
        (-500_000, "-INR 5.00 L"),
    ],
)
def test_format_inr(value, expected):
    assert claims.format_inr(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        (2_000_000_000, "USD 2.00B"),
        (25_000_000, "USD 25.00M"),
        (4_500, "USD 4.50K"),
        (999, "USD 999"),
    ],
)
def test_format_usd(value, expected):
    assert claims.format_usd(value) == expected


def test_format_money_unknown_currency_raises(monthly_revenue_current):
    with pytest.raises(ValueError):
        claims.format_money(100, "GBP")  # type: ignore[arg-type]
