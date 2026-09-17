"""Pure-Python claim checks: every ratio in the memo is computed here, never
by the LLM. Claude (via the /company-brief skill) extracts raw reported
numbers into `Fact` objects; this module turns them into `ClaimCheckResult`
objects with a visible formula, inputs, sources and assumptions.

Every function in here is defensive about the three ways a "quick ratio"
goes wrong in a real memo:

1. Missing input        -> status=MISSING_INPUT, result=None.
2. Zero denominator      -> status=ZERO_DENOMINATOR, result=None.
3. Category mismatch     -> status=CURRENCY_MISMATCH or METRIC_MISMATCH,
   result=None. This is what stops "valuation / GMV" or "revenue INR
   divided by revenue USD" from silently producing a number that looks
   fine but means nothing.

Interpretation bands (10x/30x revenue multiple, 25% dilution) are kept as
clearly labelled heuristics on the result. They are informational only and
must never be read as, or feed into, an automated recommendation.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from .schema import ClaimCheckResult, ClaimCheckStatus, Currency, Fact, MetricKind

# ---------------------------------------------------------------------------
# Heuristic thresholds -- labelled constants, not hidden magic numbers.
# ---------------------------------------------------------------------------

VALUATION_MULTIPLE_LOW_HEURISTIC = Decimal("10")
VALUATION_MULTIPLE_HIGH_HEURISTIC = Decimal("30")
DILUTION_HEURISTIC_THRESHOLD = Decimal("0.25")  # 25%

LAKH = Decimal("100000")
CRORE = Decimal("10000000")
USD_THOUSAND = Decimal("1000")
USD_MILLION = Decimal("1000000")
USD_BILLION = Decimal("1000000000")


# ---------------------------------------------------------------------------
# Formatting -- full precision is kept internally; this is display-only.
# ---------------------------------------------------------------------------


def _dec(value: float) -> Decimal:
    """Convert a float to Decimal via its string repr, avoiding binary
    float artifacts (e.g. 0.1 + 0.2)."""
    return Decimal(str(value))


def _round(value: Decimal, places: int) -> Decimal:
    quant = Decimal("1").scaleb(-places) if places > 0 else Decimal("1")
    return value.quantize(quant, rounding=ROUND_HALF_UP)


def format_inr(value: float) -> str:
    """Format a rupee amount using India's lakh/crore convention.

    >= 1 crore (1,00,00,000)  -> "INR X.XX Cr"
    >= 1 lakh (1,00,000)      -> "INR X.XX L"
    else                      -> "INR N,NNN" (Indian-style grouping)
    """
    d = _dec(value)
    sign = "-" if d < 0 else ""
    d = abs(d)
    if d >= CRORE:
        return f"{sign}INR {_round(d / CRORE, 2)} Cr"
    if d >= LAKH:
        return f"{sign}INR {_round(d / LAKH, 2)} L"
    return f"{sign}INR {_round(d, 0):,.0f}"


def format_usd(value: float) -> str:
    """Format a dollar amount using K/M/B."""
    d = _dec(value)
    sign = "-" if d < 0 else ""
    d = abs(d)
    if d >= USD_BILLION:
        return f"{sign}USD {_round(d / USD_BILLION, 2)}B"
    if d >= USD_MILLION:
        return f"{sign}USD {_round(d / USD_MILLION, 2)}M"
    if d >= USD_THOUSAND:
        return f"{sign}USD {_round(d / USD_THOUSAND, 2)}K"
    return f"{sign}USD {_round(d, 0):,.0f}"


def format_money(value: float, currency: Currency) -> str:
    if currency == Currency.INR:
        return format_inr(value)
    if currency == Currency.USD:
        return format_usd(value)
    raise ValueError(f"unknown currency: {currency}")  # pragma: no cover


# ---------------------------------------------------------------------------
# Shared guards
# ---------------------------------------------------------------------------


class _Missing(Exception):
    def __init__(self, result: ClaimCheckResult):
        self.result = result


def _missing(name: str, formula: str, missing_labels: list[str]) -> ClaimCheckResult:
    return ClaimCheckResult(
        name=name,
        formula=formula,
        status=ClaimCheckStatus.MISSING_INPUT,
        error=f"missing input(s): {', '.join(missing_labels)}",
    )


def _require_facts(name: str, formula: str, **facts: Optional[Fact]) -> None:
    missing = [label for label, f in facts.items() if f is None]
    if missing:
        raise _Missing(_missing(name, formula, missing))


def _require_same_currency(name: str, formula: str, *facts: Fact) -> None:
    currencies = {f.currency for f in facts}
    if len(currencies) > 1:
        raise _Missing(
            ClaimCheckResult(
                name=name,
                formula=formula,
                status=ClaimCheckStatus.CURRENCY_MISMATCH,
                error=f"cannot mix currencies: {sorted(c.value for c in currencies if c)}",
            )
        )


def _require_metric_kind(name: str, formula: str, fact: Fact, *expected: MetricKind) -> None:
    if fact.metric_kind not in expected:
        raise _Missing(
            ClaimCheckResult(
                name=name,
                formula=formula,
                status=ClaimCheckStatus.METRIC_MISMATCH,
                error=(
                    f"{fact.label!r} is measured as {fact.metric_kind.value!r}, "
                    f"expected one of {[e.value for e in expected]}"
                ),
            )
        )


def _zero_denominator(name: str, formula: str, source_ids: list[int]) -> ClaimCheckResult:
    return ClaimCheckResult(
        name=name,
        formula=formula,
        source_ids=source_ids,
        status=ClaimCheckStatus.ZERO_DENOMINATOR,
        error="denominator is zero",
    )


# ---------------------------------------------------------------------------
# 1. Annualised revenue run rate (explicitly NOT called "ARR" here -- that
#    label is only earned in the memo prose if evidence shows recurring
#    revenue; this function just multiplies by 12).
# ---------------------------------------------------------------------------


def annualised_revenue_run_rate(monthly_revenue: Optional[Fact]) -> ClaimCheckResult:
    name = "annualised_revenue_run_rate"
    formula = "monthly_revenue * 12"
    try:
        _require_facts(name, formula, monthly_revenue=monthly_revenue)
        assert monthly_revenue is not None
        _require_metric_kind(name, formula, monthly_revenue, MetricKind.REVENUE)
    except _Missing as m:
        return m.result

    value = _dec(monthly_revenue.value) * 12
    return ClaimCheckResult(
        name=name,
        formula=formula,
        inputs=[f"monthly_revenue={monthly_revenue.value} {monthly_revenue.currency.value}"],
        source_ids=list(monthly_revenue.source_ids),
        result=float(value),
        result_display=format_money(float(value), monthly_revenue.currency),
        unit=monthly_revenue.currency.value if monthly_revenue.currency else None,
        assumptions=[
            "This is an annualised revenue run rate (last reported month x 12), "
            "not confirmed recurring revenue. Call it ARR only where evidence "
            "establishes subscription/recurring revenue."
        ],
        status=ClaimCheckStatus.OK,
    )


# ---------------------------------------------------------------------------
# 2. Valuation / annualised revenue multiple
# ---------------------------------------------------------------------------


def valuation_multiple(valuation: Optional[Fact], monthly_revenue: Optional[Fact]) -> ClaimCheckResult:
    name = "valuation_multiple"
    formula = "valuation / (monthly_revenue * 12)"
    try:
        _require_facts(name, formula, valuation=valuation, monthly_revenue=monthly_revenue)
        assert valuation is not None and monthly_revenue is not None
        _require_metric_kind(name, formula, valuation, MetricKind.VALUATION)
        _require_metric_kind(name, formula, monthly_revenue, MetricKind.REVENUE)
        _require_same_currency(name, formula, valuation, monthly_revenue)
    except _Missing as m:
        return m.result

    annual_revenue = _dec(monthly_revenue.value) * 12
    if annual_revenue == 0:
        return _zero_denominator(name, formula, valuation.source_ids + monthly_revenue.source_ids)

    multiple = _dec(valuation.value) / annual_revenue
    if multiple < VALUATION_MULTIPLE_LOW_HEURISTIC:
        interpretation = (
            f"Below the commonly cited {VALUATION_MULTIPLE_LOW_HEURISTIC}x-"
            f"{VALUATION_MULTIPLE_HIGH_HEURISTIC}x growth-stage revenue-multiple "
            "heuristic band. Heuristic only, not a valuation opinion."
        )
    elif multiple > VALUATION_MULTIPLE_HIGH_HEURISTIC:
        interpretation = (
            f"Above the commonly cited {VALUATION_MULTIPLE_LOW_HEURISTIC}x-"
            f"{VALUATION_MULTIPLE_HIGH_HEURISTIC}x growth-stage revenue-multiple "
            "heuristic band. Heuristic only, not a valuation opinion."
        )
    else:
        interpretation = (
            f"Within the commonly cited {VALUATION_MULTIPLE_LOW_HEURISTIC}x-"
            f"{VALUATION_MULTIPLE_HIGH_HEURISTIC}x growth-stage revenue-multiple "
            "heuristic band. Heuristic only, not a valuation opinion."
        )

    return ClaimCheckResult(
        name=name,
        formula=formula,
        inputs=[
            f"valuation={valuation.value} {valuation.currency.value}",
            f"monthly_revenue={monthly_revenue.value} {monthly_revenue.currency.value}",
        ],
        source_ids=sorted(set(valuation.source_ids) | set(monthly_revenue.source_ids)),
        result=float(multiple),
        result_display=f"{_round(multiple, 1)}x",
        unit="x",
        assumptions=[
            "Uses annualised revenue run rate (monthly x 12) as the revenue base, "
            "not audited annual revenue.",
        ],
        interpretation=interpretation,
        status=ClaimCheckStatus.OK,
    )


# ---------------------------------------------------------------------------
# 3. ARPU (monthly revenue per paying customer)
# ---------------------------------------------------------------------------


def arpu(monthly_revenue: Optional[Fact], paying_customers: Optional[Fact]) -> ClaimCheckResult:
    name = "arpu"
    formula = "monthly_revenue / paying_customers"
    try:
        _require_facts(name, formula, monthly_revenue=monthly_revenue, paying_customers=paying_customers)
        assert monthly_revenue is not None and paying_customers is not None
        _require_metric_kind(name, formula, monthly_revenue, MetricKind.REVENUE)
        _require_metric_kind(name, formula, paying_customers, MetricKind.PAYING_CUSTOMERS)
    except _Missing as m:
        return m.result

    customers = _dec(paying_customers.value)
    if customers == 0:
        return _zero_denominator(name, formula, monthly_revenue.source_ids + paying_customers.source_ids)

    value = _dec(monthly_revenue.value) / customers
    return ClaimCheckResult(
        name=name,
        formula=formula,
        inputs=[
            f"monthly_revenue={monthly_revenue.value} {monthly_revenue.currency.value}",
            f"paying_customers={paying_customers.value}",
        ],
        source_ids=sorted(set(monthly_revenue.source_ids) | set(paying_customers.source_ids)),
        result=float(value),
        result_display=f"{format_money(float(value), monthly_revenue.currency)} per month",
        unit=f"{monthly_revenue.currency.value}/customer/month" if monthly_revenue.currency else None,
        assumptions=[
            "Uses PAYING customers only; registered/free users are a different "
            "metric and are never substituted here."
        ],
        status=ClaimCheckStatus.OK,
    )


# ---------------------------------------------------------------------------
# 4. Growth multiple (current revenue vs an earlier reported revenue)
# ---------------------------------------------------------------------------


def growth_multiple(current: Optional[Fact], earlier: Optional[Fact]) -> ClaimCheckResult:
    name = "growth_multiple"
    formula = "current_monthly_revenue / earlier_monthly_revenue"
    try:
        _require_facts(name, formula, current=current, earlier=earlier)
        assert current is not None and earlier is not None
        _require_metric_kind(name, formula, current, MetricKind.REVENUE)
        _require_metric_kind(name, formula, earlier, MetricKind.REVENUE)
        _require_same_currency(name, formula, current, earlier)
    except _Missing as m:
        return m.result

    earlier_value = _dec(earlier.value)
    if earlier_value == 0:
        return _zero_denominator(name, formula, current.source_ids + earlier.source_ids)

    multiple = _dec(current.value) / earlier_value
    assumptions = [
        "Compares two reported points in time; not a compounded annual growth rate."
    ]
    if current.as_of and earlier.as_of:
        assumptions.append(f"Period: {earlier.as_of.isoformat()} to {current.as_of.isoformat()}.")

    return ClaimCheckResult(
        name=name,
        formula=formula,
        inputs=[
            f"current={current.value} {current.currency.value} (as_of={current.as_of})",
            f"earlier={earlier.value} {earlier.currency.value} (as_of={earlier.as_of})",
        ],
        source_ids=sorted(set(current.source_ids) | set(earlier.source_ids)),
        result=float(multiple),
        result_display=f"{_round(multiple, 1)}x",
        unit="x",
        assumptions=assumptions,
        status=ClaimCheckStatus.OK,
    )


# ---------------------------------------------------------------------------
# 5. Capital efficiency (annualised revenue generated per unit of capital
#    raised). Defined explicitly because "capital efficiency" has no single
#    universal formula.
# ---------------------------------------------------------------------------


def capital_efficiency(
    total_capital_raised: Optional[Fact], monthly_revenue: Optional[Fact]
) -> ClaimCheckResult:
    name = "capital_efficiency"
    formula = "(monthly_revenue * 12) / total_capital_raised"
    try:
        _require_facts(
            name, formula, total_capital_raised=total_capital_raised, monthly_revenue=monthly_revenue
        )
        assert total_capital_raised is not None and monthly_revenue is not None
        _require_metric_kind(name, formula, total_capital_raised, MetricKind.CAPITAL_RAISED)
        _require_metric_kind(name, formula, monthly_revenue, MetricKind.REVENUE)
        _require_same_currency(name, formula, total_capital_raised, monthly_revenue)
    except _Missing as m:
        return m.result

    raised = _dec(total_capital_raised.value)
    if raised == 0:
        return _zero_denominator(name, formula, total_capital_raised.source_ids + monthly_revenue.source_ids)

    annual_revenue = _dec(monthly_revenue.value) * 12
    ratio = annual_revenue / raised
    return ClaimCheckResult(
        name=name,
        formula=formula,
        inputs=[
            f"monthly_revenue={monthly_revenue.value} {monthly_revenue.currency.value}",
            f"total_capital_raised={total_capital_raised.value} {total_capital_raised.currency.value}",
        ],
        source_ids=sorted(set(total_capital_raised.source_ids) | set(monthly_revenue.source_ids)),
        result=float(ratio),
        result_display=f"{_round(ratio, 2)}x annualised revenue per unit raised",
        unit="x",
        assumptions=[
            "Defined here as annualised revenue run rate divided by total capital "
            "raised to date. This is a rough efficiency signal, not a margin or "
            "burn-multiple calculation."
        ],
        status=ClaimCheckStatus.OK,
    )


# ---------------------------------------------------------------------------
# 6. Capital raised per year since founding
# ---------------------------------------------------------------------------


def capital_raised_per_year(
    total_capital_raised: Optional[Fact], years_since_founding: Optional[Fact]
) -> ClaimCheckResult:
    name = "capital_raised_per_year"
    formula = "total_capital_raised / years_since_founding"
    try:
        _require_facts(
            name,
            formula,
            total_capital_raised=total_capital_raised,
            years_since_founding=years_since_founding,
        )
        assert total_capital_raised is not None and years_since_founding is not None
        _require_metric_kind(name, formula, total_capital_raised, MetricKind.CAPITAL_RAISED)
        _require_metric_kind(name, formula, years_since_founding, MetricKind.YEARS)
    except _Missing as m:
        return m.result

    years = _dec(years_since_founding.value)
    if years == 0:
        return _zero_denominator(
            name, formula, total_capital_raised.source_ids + years_since_founding.source_ids
        )

    value = _dec(total_capital_raised.value) / years
    return ClaimCheckResult(
        name=name,
        formula=formula,
        inputs=[
            f"total_capital_raised={total_capital_raised.value} {total_capital_raised.currency.value}",
            f"years_since_founding={years_since_founding.value}",
        ],
        source_ids=sorted(set(total_capital_raised.source_ids) | set(years_since_founding.source_ids)),
        result=float(value),
        result_display=f"{format_money(float(value), total_capital_raised.currency)} per year",
        unit=f"{total_capital_raised.currency.value}/year" if total_capital_raised.currency else None,
        status=ClaimCheckStatus.OK,
    )


# ---------------------------------------------------------------------------
# 7. Implied dilution from the latest round, assuming post-money valuation
# ---------------------------------------------------------------------------


def implied_dilution(
    round_size: Optional[Fact], post_money_valuation: Optional[Fact]
) -> ClaimCheckResult:
    name = "implied_dilution"
    formula = "round_size / post_money_valuation"
    try:
        _require_facts(name, formula, round_size=round_size, post_money_valuation=post_money_valuation)
        assert round_size is not None and post_money_valuation is not None
        _require_metric_kind(name, formula, round_size, MetricKind.ROUND_SIZE)
        _require_metric_kind(name, formula, post_money_valuation, MetricKind.VALUATION)
        _require_same_currency(name, formula, round_size, post_money_valuation)
    except _Missing as m:
        return m.result

    post_money = _dec(post_money_valuation.value)
    if post_money == 0:
        return _zero_denominator(name, formula, round_size.source_ids + post_money_valuation.source_ids)

    dilution = _dec(round_size.value) / post_money
    interpretation = (
        f"At or above the {DILUTION_HEURISTIC_THRESHOLD * 100}% heuristic "
        "threshold commonly flagged as a large dilution event."
        if dilution >= DILUTION_HEURISTIC_THRESHOLD
        else f"Below the {DILUTION_HEURISTIC_THRESHOLD * 100}% heuristic dilution threshold."
    )

    return ClaimCheckResult(
        name=name,
        formula=formula,
        inputs=[
            f"round_size={round_size.value} {round_size.currency.value}",
            f"post_money_valuation={post_money_valuation.value} {post_money_valuation.currency.value}",
        ],
        source_ids=sorted(set(round_size.source_ids) | set(post_money_valuation.source_ids)),
        result=float(dilution),
        result_display=f"{_round(dilution * 100, 1)}%",
        unit="percent",
        assumptions=[
            "Assumes the stated valuation is POST-money and already includes this "
            "round. If a source reports a pre-money valuation instead, this "
            "result will be wrong and should not be used."
        ],
        interpretation=interpretation,
        status=ClaimCheckStatus.OK,
    )


ALL_CHECKS = (
    "annualised_revenue_run_rate",
    "valuation_multiple",
    "arpu",
    "growth_multiple",
    "capital_efficiency",
    "capital_raised_per_year",
    "implied_dilution",
)
