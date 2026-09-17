"""Tests for firstcheque.schema: Memo validation, source-id bounds, and
unverified-fact/claim flagging."""

from datetime import date, datetime

import pytest
from pydantic import ValidationError

from firstcheque.schema import (
    Claim,
    Currency,
    EvidenceType,
    Fact,
    Memo,
    MetricKind,
    Numbers,
    RDIFit,
    Recommendation,
    RiskItem,
    Snapshot,
    Source,
    citation_support_rate,
)


def _minimal_memo(**overrides) -> dict:
    base = dict(
        company="Testly Technologies",
        website="https://testly.example",
        one_liner="B2B SaaS for something fictional.",
        recommendation=Recommendation.TRACK,
        recommendation_rationale="Early but growing. Worth a follow-up in a quarter.",
        snapshot=Snapshot(founded="2022", hq="Bengaluru"),
        business_model="Sells seats to SMBs.",
        why_now="GST compliance deadline creates urgency.",
        market_and_competition="Crowded but growing market.",
        traction_and_team="Founders are second-time; revenue growing.",
        numbers=Numbers(),
        risks=[
            RiskItem(risk="Thin moat", mitigant="Ask about switching costs."),
            RiskItem(risk="Customer concentration", mitigant="Ask for logo breakdown."),
            RiskItem(risk="Regulatory exposure", mitigant="Ask about compliance roadmap."),
        ],
        what_would_change_recommendation="Evidence of net-negative churn.",
        founder_questions=[f"Question {i}?" for i in range(1, 6)],
        generated_at=datetime(2026, 9, 17, 12, 0, 0),
    )
    base.update(overrides)
    return base


def test_minimal_memo_is_valid():
    memo = Memo(**_minimal_memo())
    assert memo.company == "Testly Technologies"
    assert memo.recommendation == Recommendation.TRACK


def test_memo_requires_exactly_five_founder_questions():
    with pytest.raises(ValidationError, match="exactly 5"):
        Memo(**_minimal_memo(founder_questions=["Only one?"]))


def test_memo_rejects_more_than_two_rdi_fits():
    three_fits = [
        RDIFit(theme_id="ai", theme_name="AI", rationale="uses ML"),
        RDIFit(theme_id="biotech", theme_name="Biotech", rationale="not really"),
        RDIFit(theme_id="space", theme_name="Space", rationale="not really either"),
    ]
    with pytest.raises(ValidationError, match="at most 2"):
        Memo(**_minimal_memo(rdi_fits=three_fits))


def test_memo_allows_two_rdi_fits():
    two_fits = [
        RDIFit(theme_id="ai", theme_name="AI", rationale="uses ML for underwriting"),
        RDIFit(theme_id="digital_economy", theme_name="Digital economy", rationale="digital lending rails"),
    ]
    memo = Memo(**_minimal_memo(rdi_fits=two_fits))
    assert len(memo.rdi_fits) == 2


def test_memo_requires_at_least_three_risks():
    with pytest.raises(ValidationError, match="at least 3"):
        Memo(**_minimal_memo(risks=[RiskItem(risk="Only one", mitigant="Ask about it.")]))


# ---------------------------------------------------------------------------
# Fact: metric/currency coupling and unverified flagging
# ---------------------------------------------------------------------------


def test_fact_unverified_when_no_sources():
    f = Fact(label="x", value=1, metric_kind=MetricKind.YEARS, source_ids=[])
    assert f.unverified is True


def test_fact_verified_when_sources_present():
    f = Fact(label="x", value=1, metric_kind=MetricKind.YEARS, source_ids=[2])
    assert f.unverified is False


def test_fact_money_kind_requires_currency():
    with pytest.raises(ValidationError, match="currency"):
        Fact(label="revenue", value=100, metric_kind=MetricKind.REVENUE)  # no currency


def test_fact_non_money_kind_does_not_require_currency():
    f = Fact(label="customers", value=100, metric_kind=MetricKind.PAYING_CUSTOMERS)
    assert f.currency is None


# ---------------------------------------------------------------------------
# Source: id bounds and location requirement
# ---------------------------------------------------------------------------


def test_source_id_must_be_positive():
    with pytest.raises(ValidationError):
        Source(
            id=0,
            title="Some article",
            url="https://example.com",
            accessed_date=date(2026, 9, 17),
            passage="text",
            evidence_type=EvidenceType.SEARCH_SNIPPET,
        )


def test_source_requires_url_or_local_path():
    with pytest.raises(ValidationError, match="neither a url nor a local_path"):
        Source(
            id=1,
            title="Some article",
            accessed_date=date(2026, 9, 17),
            passage="text",
            evidence_type=EvidenceType.SEARCH_SNIPPET,
        )


def test_source_with_local_path_only_is_valid():
    s = Source(
        id=1,
        title="Uploaded pitch deck",
        local_path="./evidence/deck.pdf",
        accessed_date=date(2026, 9, 17),
        passage="Slide 4: ARR is INR 3 crore.",
        page="4",
        evidence_type=EvidenceType.DOCUMENT_EXCERPT,
    )
    assert s.local_path == "./evidence/deck.pdf"


# ---------------------------------------------------------------------------
# Claim / citation-support rate
# ---------------------------------------------------------------------------


def test_claim_unverified_when_no_sources():
    c = Claim(id="c1", text="Founded in 2022", section="snapshot", source_ids=[])
    assert c.unverified is True


def test_citation_support_rate_excludes_opinions_from_denominator():
    claims = [
        Claim(id="c1", text="Founded in 2022", section="snapshot", source_ids=[1], supported=True),
        Claim(id="c2", text="Revenue grew fast", section="traction", source_ids=[2], supported=False),
        Claim(id="c3", text="Should raise a Series A", section="risks", is_opinion_or_question=True),
    ]
    supported, denominator, rate = citation_support_rate(claims)
    assert denominator == 2  # opinion excluded
    assert supported == 1
    assert rate == pytest.approx(0.5)


def test_citation_support_rate_handles_empty_denominator():
    supported, denominator, rate = citation_support_rate([])
    assert denominator == 0
    assert rate is None


# ---------------------------------------------------------------------------
# Contradictory source figures: both are preserved, neither is silently
# dropped or averaged away.
# ---------------------------------------------------------------------------


def test_contradictory_figures_are_both_preserved_in_extra():
    numbers = Numbers(
        extra={
            "monthly_revenue_per_entrackr": Fact(
                label="monthly_revenue_per_entrackr",
                value=2_501_000,
                currency=Currency.INR,
                metric_kind=MetricKind.REVENUE,
                as_of=date(2026, 6, 1),
                source_ids=[1],
                notes="Entrackr, June 2026 report",
            ),
            "monthly_revenue_per_inc42": Fact(
                label="monthly_revenue_per_inc42",
                value=3_200_000,
                currency=Currency.INR,
                metric_kind=MetricKind.REVENUE,
                as_of=date(2026, 6, 1),
                source_ids=[2],
                notes="Inc42, same month, different figure -- unresolved",
            ),
        }
    )
    memo = Memo(**_minimal_memo(numbers=numbers, unresolved_gaps=["Entrackr and Inc42 report different June 2026 revenue; unresolved."]))
    assert memo.numbers.extra["monthly_revenue_per_entrackr"].value == 2_501_000
    assert memo.numbers.extra["monthly_revenue_per_inc42"].value == 3_200_000
    assert len(memo.unresolved_gaps) == 1
