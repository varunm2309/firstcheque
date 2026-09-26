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

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from .schema import ClaimCheckResult, ClaimCheckStatus, Currency, Fact, MetricKind, Period

# ---------------------------------------------------------------------------
# Heuristic thresholds -- labelled constants, not hidden magic numbers.
# ---------------------------------------------------------------------------

VALUATION_MULTIPLE_LOW_HEURISTIC = Decimal("10")
VALUATION_MULTIPLE_HIGH_HEURISTIC = Decimal("30")
DILUTION_HEURISTIC_THRESHOLD = Decimal("0.25")  # 25%

# Above this gap between two facts' as_of dates, a ratio combining them is
# still computed (the dates are usually the best evidence gives us) but
# gets an explicit staleness note, so a valuation from 2022 quietly divided
# by revenue from 2025 doesn't read as a same-moment comparison.
AS_OF_GAP_WARNING_DAYS = 180

LAKH = Decimal("100000")
CRORE = Decimal("10000000")
USD_THOUSAND = Decimal("1000")
USD_MILLION = Decimal("1000000")
USD_BILLION = Decimal("1000000000")

# Human-readable names for error messages, keyed by the `name` each
# function below reports as. Falls back to name.replace("_", " ") if a
# check isn't listed here.
DISPLAY_NAMES = {
    "annualised_revenue_run_rate": "annualised revenue run rate",
    "valuation_multiple": "valuation multiple",
    "arpu": "ARPU",
    "growth_multiple": "growth multiple",
    "capital_efficiency": "capital efficiency",
    "capital_raised_per_year": "capital raised per year",
    "implied_dilution": "implied dilution",
}


def _display(name: str) -> str:
    return DISPLAY_NAMES.get(name, name.replace("_", " "))


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


_INPUT_DESCRIPTIONS = {
    "monthly_revenue": "sourced monthly revenue figure",
    "annual_revenue": "sourced annual revenue figure",
    "valuation": "sourced valuation",
    "paying_customers": "sourced paying-customer count",
    "current": "sourced current-period revenue figure",
    "earlier": "sourced earlier-period revenue figure",
    "total_capital_raised": "sourced total-capital-raised figure",
    "years_since_founding": "sourced founding date/age",
    "round_size": "sourced round size",
    "post_money_valuation": "sourced post-money valuation",
}


def _missing(name: str, formula: str, missing_labels: list[str]) -> ClaimCheckResult:
    described = [_INPUT_DESCRIPTIONS.get(label, f"sourced value for {label!r}") for label in missing_labels]
    if len(described) == 1:
        missing_clause = f"no {described[0]} was found in the sources reviewed"
    else:
        with_articles = [f"{_article(d)} {d}" for d in described]
        missing_clause = "none of these were found in the sources reviewed: " + "; ".join(with_articles)
    return ClaimCheckResult(
        name=name,
        formula=formula,
        status=ClaimCheckStatus.MISSING_INPUT,
        error=f"Cannot calculate {_display(name)}: {missing_clause}.",
    )


def _require_facts(name: str, formula: str, **facts: Optional[Fact]) -> None:
    missing = [label for label, f in facts.items() if f is None]
    if missing:
        raise _Missing(_missing(name, formula, missing))


def _require_same_currency(name: str, formula: str, *facts: Fact) -> None:
    currencies = {f.currency for f in facts}
    if len(currencies) > 1:
        currency_list = " and ".join(sorted(c.value for c in currencies if c))
        raise _Missing(
            ClaimCheckResult(
                name=name,
                formula=formula,
                status=ClaimCheckStatus.CURRENCY_MISMATCH,
                error=(
                    f"Cannot calculate {_display(name)}: the inputs are reported in different "
                    f"currencies ({currency_list}). No exchange rate was applied -- converting "
                    "would mix a sourced figure with an assumed one."
                ),
            )
        )


def _require_metric_kind(name: str, formula: str, fact: Fact, *expected: MetricKind) -> None:
    if fact.metric_kind not in expected:
        expected_list = " or ".join(e.value.replace("_", " ") for e in expected)
        raise _Missing(
            ClaimCheckResult(
                name=name,
                formula=formula,
                status=ClaimCheckStatus.METRIC_MISMATCH,
                error=(
                    f"Cannot calculate {_display(name)}: {fact.label!r} is recorded as "
                    f"{fact.metric_kind.value.replace('_', ' ')}, but this calculation needs "
                    f"{expected_list}. Using it anyway would compare two different things."
                ),
            )
        )


_PERIOD_DISPLAY = {
    Period.MONTHLY: "monthly",
    Period.QUARTERLY: "quarterly",
    Period.ANNUAL: "annual",
    Period.POINT_IN_TIME: "point-in-time",
}


def _article(word: str) -> str:
    return "an" if word[:1].lower() in "aeiou" else "a"


def _period_display(period: Period) -> str:
    return _PERIOD_DISPLAY.get(period, period.value)


def _require_period(name: str, formula: str, fact: Fact, *expected: Period) -> None:
    """Only enforced when the fact actually states a period -- older or
    hand-entered data without one is left alone rather than blocked, but
    once a period IS recorded it must be the one this formula assumes."""
    if fact.period is not None and fact.period not in expected:
        expected_list = " or ".join(f"{_article(_period_display(e))} {_period_display(e)}" for e in expected)
        actual = _period_display(fact.period)
        raise _Missing(
            ClaimCheckResult(
                name=name,
                formula=formula,
                status=ClaimCheckStatus.PERIOD_MISMATCH,
                error=(
                    f"Cannot calculate {_display(name)}: {fact.label!r} covers "
                    f"{_article(actual)} {actual} period, but this calculation needs {expected_list} "
                    "figure. Using it anyway would misstate the rate (e.g. treating an "
                    "already-annual number as if it still needed annualising)."
                ),
            )
        )


def _require_same_period(name: str, formula: str, *facts: Fact) -> None:
    periods = {f.period for f in facts if f.period is not None}
    if len(periods) > 1:
        period_list = " vs ".join(sorted(_period_display(p) for p in periods))
        raise _Missing(
            ClaimCheckResult(
                name=name,
                formula=formula,
                status=ClaimCheckStatus.PERIOD_MISMATCH,
                error=(
                    f"Cannot calculate {_display(name)}: the inputs cover different periods "
                    f"({period_list}), so comparing them directly would not be like-for-like."
                ),
            )
        )


def _zero_denominator(name: str, formula: str, source_ids: list[int], denominator_label: str) -> ClaimCheckResult:
    return ClaimCheckResult(
        name=name,
        formula=formula,
        source_ids=source_ids,
        status=ClaimCheckStatus.ZERO_DENOMINATOR,
        error=f"Cannot calculate {_display(name)}: {denominator_label} is zero, so the ratio is undefined.",
    )


def _as_of_gap_note(label_a: str, fact_a: Fact, label_b: str, fact_b: Fact) -> Optional[str]:
    """A visible staleness warning when two inputs to the same ratio were
    measured on dates far enough apart that the ratio mixes an old figure
    with a newer one -- e.g. a 2022 valuation over 2025 revenue. Returns
    None when either date is missing or the gap is small; the calculation
    still runs either way, this only makes the mismatch visible instead of
    silently blending two different moments in time."""
    if not fact_a.as_of or not fact_b.as_of:
        return None
    gap_days = abs((fact_a.as_of - fact_b.as_of).days)
    if gap_days <= AS_OF_GAP_WARNING_DAYS:
        return None
    return (
        f"{label_a} is as of {fact_a.as_of.isoformat()} and {label_b} is as of "
        f"{fact_b.as_of.isoformat()} -- roughly {gap_days // 30} months apart. This ratio "
        "mixes those two moments rather than comparing figures from the same date."
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
        _require_period(name, formula, monthly_revenue, Period.MONTHLY)
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


def valuation_multiple(valuation: Optional[Fact], annual_revenue: Optional[Fact]) -> ClaimCheckResult:
    """valuation / annual revenue.

    `annual_revenue` must already BE an annual figure -- either a directly
    disclosed one (common for rated NBFCs and larger companies that report
    FY totals, not monthly revenue), or one derived by calling
    `annualised_revenue_run_rate` first and passing its result in as a Fact
    (what the CLI does for companies that only disclose monthly revenue).
    This function does not multiply by 12 itself, so it must never be
    handed a monthly figure directly.
    """
    name = "valuation_multiple"
    formula = "valuation / annual_revenue"
    try:
        _require_facts(name, formula, valuation=valuation, annual_revenue=annual_revenue)
        assert valuation is not None and annual_revenue is not None
        _require_metric_kind(name, formula, valuation, MetricKind.VALUATION)
        _require_metric_kind(name, formula, annual_revenue, MetricKind.REVENUE)
        _require_period(name, formula, annual_revenue, Period.ANNUAL)
        _require_period(name, formula, valuation, Period.POINT_IN_TIME)
        _require_same_currency(name, formula, valuation, annual_revenue)
    except _Missing as m:
        return m.result

    annual_revenue_value = _dec(annual_revenue.value)
    if annual_revenue_value == 0:
        return _zero_denominator(name, formula, valuation.source_ids + annual_revenue.source_ids, "annual_revenue")

    multiple = _dec(valuation.value) / annual_revenue_value
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
            f"annual_revenue={annual_revenue.value} {annual_revenue.currency.value}",
        ],
        source_ids=sorted(set(valuation.source_ids) | set(annual_revenue.source_ids)),
        result=float(multiple),
        result_display=f"{_round(multiple, 1)}x",
        unit="x",
        assumptions=[
            a
            for a in [
                f"Revenue base: {annual_revenue.definition or annual_revenue.label} "
                f"(as_of={annual_revenue.as_of}).",
                _as_of_gap_note("valuation", valuation, "annual_revenue", annual_revenue),
            ]
            if a
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
        _require_period(name, formula, monthly_revenue, Period.MONTHLY)
    except _Missing as m:
        return m.result

    customers = _dec(paying_customers.value)
    if customers == 0:
        return _zero_denominator(
            name, formula, monthly_revenue.source_ids + paying_customers.source_ids, "paying_customers"
        )

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
        _require_same_period(name, formula, current, earlier)
    except _Missing as m:
        return m.result

    earlier_value = _dec(earlier.value)
    if earlier_value == 0:
        return _zero_denominator(name, formula, current.source_ids + earlier.source_ids, "earlier")

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
    total_capital_raised: Optional[Fact], annual_revenue: Optional[Fact]
) -> ClaimCheckResult:
    """annual_revenue / total_capital_raised. See valuation_multiple's
    docstring: `annual_revenue` must already be annual (disclosed directly,
    or derived from monthly via annualised_revenue_run_rate)."""
    name = "capital_efficiency"
    formula = "annual_revenue / total_capital_raised"
    try:
        _require_facts(
            name, formula, total_capital_raised=total_capital_raised, annual_revenue=annual_revenue
        )
        assert total_capital_raised is not None and annual_revenue is not None
        _require_metric_kind(name, formula, total_capital_raised, MetricKind.CAPITAL_RAISED)
        _require_metric_kind(name, formula, annual_revenue, MetricKind.REVENUE)
        _require_period(name, formula, annual_revenue, Period.ANNUAL)
        _require_same_currency(name, formula, total_capital_raised, annual_revenue)
    except _Missing as m:
        return m.result

    raised = _dec(total_capital_raised.value)
    if raised == 0:
        return _zero_denominator(
            name, formula, total_capital_raised.source_ids + annual_revenue.source_ids, "total_capital_raised"
        )

    ratio = _dec(annual_revenue.value) / raised
    return ClaimCheckResult(
        name=name,
        formula=formula,
        inputs=[
            f"annual_revenue={annual_revenue.value} {annual_revenue.currency.value}",
            f"total_capital_raised={total_capital_raised.value} {total_capital_raised.currency.value}",
        ],
        source_ids=sorted(set(total_capital_raised.source_ids) | set(annual_revenue.source_ids)),
        result=float(ratio),
        result_display=f"{_round(ratio, 2)}x annual revenue per unit raised",
        unit="x",
        assumptions=[
            a
            for a in [
                f"Revenue base: {annual_revenue.definition or annual_revenue.label} "
                f"(as_of={annual_revenue.as_of}). This is a rough efficiency signal, "
                "not a margin or burn-multiple calculation.",
                _as_of_gap_note("total_capital_raised", total_capital_raised, "annual_revenue", annual_revenue),
            ]
            if a
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
            name, formula, total_capital_raised.source_ids + years_since_founding.source_ids,
            "years_since_founding",
        )

    value = _dec(total_capital_raised.value) / years
    gap_note = _as_of_gap_note("total_capital_raised", total_capital_raised, "years_since_founding", years_since_founding)
    return ClaimCheckResult(
        name=name,
        formula=formula,
        inputs=[
            f"total_capital_raised={total_capital_raised.value} {total_capital_raised.currency.value} (as_of={total_capital_raised.as_of})",
            f"years_since_founding={years_since_founding.value} (as_of={years_since_founding.as_of})",
        ],
        source_ids=sorted(set(total_capital_raised.source_ids) | set(years_since_founding.source_ids)),
        result=float(value),
        result_display=f"{format_money(float(value), total_capital_raised.currency)} per year",
        unit=f"{total_capital_raised.currency.value}/year" if total_capital_raised.currency else None,
        assumptions=(
            [
                "years_since_founding should be measured as of the same date as "
                "total_capital_raised (not as of today), or this rate understates/overstates "
                "how fast the company actually raised money."
            ]
            + ([gap_note] if gap_note else [])
        ),
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
        return _zero_denominator(
            name, formula, round_size.source_ids + post_money_valuation.source_ids, "post_money_valuation"
        )

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
