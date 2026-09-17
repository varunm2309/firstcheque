"""Pydantic models for FirstCheque.

These models are the contract between Claude (which extracts facts from
research and writes prose) and the Python code (which validates structure
and does arithmetic). Claude fills these in; it never computes ratios,
run-rates or multiples itself -- see claims.py for that.

Design notes worth knowing in an interview:

- `Fact` carries a `metric_kind` alongside its numeric value. This is what
  lets claims.py refuse to divide a GMV figure by a revenue figure, or
  compare registered users to paying customers, instead of silently
  producing a plausible-looking wrong number.
- Every `Fact` and every `Claim` carries `source_ids` pointing into the
  run's `Source` register. A fact with an empty list is not deleted; it is
  marked `unverified` so the memo can say so explicitly.
- `Money` values are stored as plain floats in the *base* unit of their
  currency (e.g. rupees, not lakhs; dollars, not millions). All lakh/crore
  and K/M/B formatting happens at render time in claims.py, so the stored
  number is always unambiguous.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class Currency(str, Enum):
    INR = "INR"
    USD = "USD"


class Period(str, Enum):
    MONTHLY = "monthly"
    ANNUAL = "annual"
    POINT_IN_TIME = "point_in_time"  # e.g. valuation, headcount, a snapshot figure


class MetricKind(str, Enum):
    """What a Fact actually measures. Used to block category-mismatch errors
    such as dividing valuation by GMV, or treating registered users as
    paying customers."""

    REVENUE = "revenue"
    GMV = "gmv"
    VALUATION = "valuation"
    CAPITAL_RAISED = "capital_raised"
    ROUND_SIZE = "round_size"
    PAYING_CUSTOMERS = "paying_customers"
    REGISTERED_USERS = "registered_users"
    YEARS = "years"
    PERCENT = "percent"
    HEADCOUNT = "headcount"
    OTHER = "other"


class EvidenceType(str, Enum):
    FULL_TEXT = "full_text"
    DOCUMENT_EXCERPT = "document_excerpt"
    SEARCH_SNIPPET = "search_snippet"


class Recommendation(str, Enum):
    MEET = "Meet"
    TRACK = "Track"
    PASS = "Pass"


class ClaimCheckStatus(str, Enum):
    OK = "ok"
    MISSING_INPUT = "missing_input"
    ZERO_DENOMINATOR = "zero_denominator"
    CURRENCY_MISMATCH = "currency_mismatch"
    METRIC_MISMATCH = "metric_mismatch"
    NOT_APPLICABLE = "not_applicable"


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


class Source(BaseModel):
    """One entry in a run's source register.

    `id` is stable across re-runs of the same company (storage.py preserves
    it when it merges newly-found sources with a previous run's register),
    so a citation like `source_ids=[3]` keeps meaning the same thing over
    time.
    """

    id: int = Field(ge=1)
    title: str
    url: Optional[str] = None
    local_path: Optional[str] = None
    publisher: Optional[str] = None
    published_date: Optional[date] = None
    accessed_date: date
    passage: str = Field(min_length=1, description="The actual retrieved supporting text.")
    page: Optional[str] = None
    evidence_type: EvidenceType

    @model_validator(mode="after")
    def _must_have_location(self) -> "Source":
        if not self.url and not self.local_path:
            raise ValueError(f"source {self.id} ({self.title!r}) has neither a url nor a local_path")
        return self


class Claim(BaseModel):
    """A qualitative (non-numeric) factual statement in the memo, tracked
    separately from prose so citation support can be measured.

    `supported` is filled in by the citation-support check (verify_citations
    in the skill instructions): does the cited passage actually back the
    claim, per an AI read of the saved text. That is citation *support*,
    not independent fact verification -- the memo and README must not call
    it the latter.
    """

    id: str
    text: str
    section: str
    source_ids: list[int] = Field(default_factory=list)
    supported: Optional[bool] = None
    is_opinion_or_question: bool = False

    @property
    def unverified(self) -> bool:
        return len(self.source_ids) == 0


class Fact(BaseModel):
    """A single reported number, kept with enough metadata that Python can
    safely do arithmetic on it (or safely refuse to)."""

    label: str
    value: float
    currency: Optional[Currency] = None
    metric_kind: MetricKind
    period: Optional[Period] = None
    unit: str = ""  # free text for non-money units, e.g. "customers", "years"
    definition: str = Field(
        default="", description="What this number actually measures, in the source's own terms."
    )
    as_of: Optional[date] = None
    source_ids: list[int] = Field(default_factory=list)
    notes: Optional[str] = None

    @property
    def unverified(self) -> bool:
        return len(self.source_ids) == 0

    @model_validator(mode="after")
    def _money_needs_currency(self) -> "Fact":
        money_kinds = {
            MetricKind.REVENUE,
            MetricKind.GMV,
            MetricKind.VALUATION,
            MetricKind.CAPITAL_RAISED,
            MetricKind.ROUND_SIZE,
        }
        if self.metric_kind in money_kinds and self.currency is None:
            raise ValueError(f"fact {self.label!r} has metric_kind={self.metric_kind} but no currency")
        return self


class Numbers(BaseModel):
    """The specific facts the claim checks in claims.py know how to use.
    Anything extracted that doesn't fit these slots goes in `extra`, still
    validated as a Fact but not run through a formula."""

    monthly_revenue_current: Optional[Fact] = None
    monthly_revenue_earlier: Optional[Fact] = None
    valuation: Optional[Fact] = None
    post_money_valuation: Optional[Fact] = None
    latest_round_size: Optional[Fact] = None
    paying_customers: Optional[Fact] = None
    total_capital_raised: Optional[Fact] = None
    years_since_founding: Optional[Fact] = None
    extra: dict[str, Fact] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Claim-check results (pure-Python arithmetic output, see claims.py)
# ---------------------------------------------------------------------------


class ClaimCheckResult(BaseModel):
    name: str
    formula: str
    inputs: list[str] = Field(default_factory=list)
    source_ids: list[int] = Field(default_factory=list)
    result: Optional[float] = None
    result_display: Optional[str] = None
    unit: Optional[str] = None
    assumptions: list[str] = Field(default_factory=list)
    interpretation: Optional[str] = Field(
        default=None,
        description="A clearly-labelled heuristic band (e.g. 10x/30x), never a recommendation.",
    )
    status: ClaimCheckStatus = ClaimCheckStatus.OK
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Memo body
# ---------------------------------------------------------------------------


class Snapshot(BaseModel):
    founded: Optional[str] = None
    hq: Optional[str] = None
    stage: Optional[str] = None
    total_funding_display: Optional[str] = None
    last_round: Optional[str] = None
    investors: list[str] = Field(default_factory=list)
    founders: list[str] = Field(default_factory=list)


class RiskItem(BaseModel):
    risk: str
    mitigant: str = Field(
        description="A POTENTIAL mitigant / question to press on, not an action the company is confirmed to have taken."
    )


class RDIFit(BaseModel):
    theme_id: str
    theme_name: str
    rationale: str


class Memo(BaseModel):
    company: str
    website: Optional[str] = None
    one_liner: str
    recommendation: Recommendation
    recommendation_rationale: str = Field(description="Two sentences, no more.")
    snapshot: Snapshot
    business_model: str
    why_now: str
    market_and_competition: str
    traction_and_team: str
    numbers: Numbers
    claims: list[Claim] = Field(default_factory=list)
    risks: list[RiskItem] = Field(default_factory=list)
    what_would_change_recommendation: str
    founder_questions: list[str] = Field(default_factory=list)
    rdi_fits: list[RDIFit] = Field(default_factory=list)
    unresolved_gaps: list[str] = Field(default_factory=list)
    generated_at: datetime

    @field_validator("founder_questions")
    @classmethod
    def _exactly_five_questions(cls, v: list[str]) -> list[str]:
        if len(v) != 5:
            raise ValueError(f"expected exactly 5 founder questions, got {len(v)}")
        return v

    @field_validator("rdi_fits")
    @classmethod
    def _at_most_two_rdi_fits(cls, v: list[RDIFit]) -> list[RDIFit]:
        if len(v) > 2:
            raise ValueError(f"at most 2 RDI theme fits allowed, got {len(v)}")
        return v

    @field_validator("risks")
    @classmethod
    def _at_least_three_risks(cls, v: list[RiskItem]) -> list[RiskItem]:
        if len(v) < 3:
            raise ValueError(f"expected at least 3 risks with mitigants, got {len(v)}")
        return v


class RunMeta(BaseModel):
    """Execution metadata for one run. Only records what actually happened;
    never invents timing, cost or usage figures."""

    company: str
    run_id: str
    created_at: datetime
    previous_run_id: Optional[str] = None
    evidence_reused_from: Optional[str] = None
    tools_used: list[str] = Field(default_factory=list)
    execution_notes: Optional[str] = None


def citation_support_rate(claims: list[Claim]) -> tuple[int, int, Optional[float]]:
    """Return (supported_count, denominator, rate_or_None).

    Denominator excludes opinions/founder questions, per the brief: this is
    a citation-*support* rate, not a fact-verification rate, and the
    denominator must be explicit whenever the number is reported.
    """
    countable = [c for c in claims if not c.is_opinion_or_question]
    denominator = len(countable)
    if denominator == 0:
        return 0, 0, None
    supported = sum(1 for c in countable if c.supported is True)
    return supported, denominator, supported / denominator
