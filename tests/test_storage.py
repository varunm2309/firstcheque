"""Tests for run-folder bookkeeping, evidence reuse/merge, review
carry-forward, and an end-to-end pass from saved sources through claim
checks to a rendered memo.md."""

import re
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from firstcheque import storage
from firstcheque.render import render_memo_html, render_memo_md
from firstcheque import claims as claims_mod
from firstcheque.schema import (
    Claim,
    Currency,
    EvidenceType,
    Fact,
    Memo,
    MetricKind,
    Numbers,
    Period,
    Recommendation,
    RiskItem,
    Snapshot,
    Source,
)


@pytest.fixture
def tmp_root(tmp_path) -> Path:
    return tmp_path / "memos"


# ---------------------------------------------------------------------------
# slugify / run directory allocation
# ---------------------------------------------------------------------------


def test_slugify():
    assert storage.slugify("Hisabkitab") == "hisabkitab"
    assert storage.slugify("  Foo & Bar, Inc. ") == "foo-bar-inc"


def test_new_run_dir_increments(tmp_root):
    r1 = storage.new_run_dir("Acme", root=tmp_root)
    r2 = storage.new_run_dir("Acme", root=tmp_root)
    assert r1.name == "run-001"
    assert r2.name == "run-002"
    assert storage.list_runs("Acme", root=tmp_root) == [r1, r2]
    assert storage.latest_run("Acme", root=tmp_root) == r2


def test_new_run_dir_isolated_per_company(tmp_root):
    a = storage.new_run_dir("Acme", root=tmp_root)
    b = storage.new_run_dir("Beta", root=tmp_root)
    assert a.name == "run-001"
    assert b.name == "run-001"


# ---------------------------------------------------------------------------
# Source merge: dedupe by URL, preserve IDs, assign new IDs to new sources
# ---------------------------------------------------------------------------


def _src(id_: int, url: str, accessed: date) -> Source:
    return Source(
        id=id_,
        title=f"Article at {url}",
        url=url,
        accessed_date=accessed,
        passage="some retrieved text",
        evidence_type=EvidenceType.SEARCH_SNIPPET,
    )


def test_merge_sources_preserves_ids_for_reused_urls():
    previous = [_src(1, "https://a.example", date(2026, 1, 1)), _src(2, "https://b.example", date(2026, 1, 1))]
    newly_found = [
        _src(99, "https://a.example", date(2026, 9, 1)),  # re-fetched, same URL -> keeps id 1
        _src(99, "https://c.example", date(2026, 9, 1)),  # genuinely new -> gets id 3
    ]
    result = storage.merge_sources(previous, newly_found)
    ids = sorted(s.id for s in result.merged)
    assert ids == [1, 2, 3]
    assert result.reused_ids == [1]
    assert result.new_ids == [3]


def test_evidence_age_days():
    old = _src(1, "https://a.example", date(2026, 1, 1))
    assert storage.evidence_age_days(old, as_of=date(2026, 1, 31)) == 30


# ---------------------------------------------------------------------------
# Review carry-forward
# ---------------------------------------------------------------------------


def test_is_review_blank_true_for_template():
    assert storage.is_review_blank(storage.REVIEW_TEMPLATE.format(company="X", run_id="run-001"))


def test_is_review_blank_false_once_filled_in():
    text = storage.REVIEW_TEMPLATE.format(company="X", run_id="run-001") + "\nActually I think Pass.\n"
    assert storage.is_review_blank(text) is False


def test_completed_review_is_carried_forward(tmp_root):
    run1 = storage.new_run_dir("Acme", root=tmp_root)
    storage.write_review_template(run1, "Acme", run1.name, previous_run_dir=None)
    (run1 / "review.md").write_text(
        (run1 / "review.md").read_text(encoding="utf-8") + "\nMy call: Track, revisit in Q1.\n",
        encoding="utf-8",
    )

    run2 = storage.new_run_dir("Acme", root=tmp_root)
    storage.write_review_template(run2, "Acme", run2.name, previous_run_dir=run1)

    assert (run2 / "previous_review.md").exists()
    assert "Track, revisit in Q1" in (run2 / "previous_review.md").read_text(encoding="utf-8")
    # the new run still gets its own blank review.md to fill in
    assert storage.is_review_blank((run2 / "review.md").read_text(encoding="utf-8"))


def test_blank_review_is_not_carried_forward(tmp_root):
    run1 = storage.new_run_dir("Acme", root=tmp_root)
    storage.write_review_template(run1, "Acme", run1.name, previous_run_dir=None)

    run2 = storage.new_run_dir("Acme", root=tmp_root)
    storage.write_review_template(run2, "Acme", run2.name, previous_run_dir=run1)

    assert not (run2 / "previous_review.md").exists()


# ---------------------------------------------------------------------------
# End-to-end: saved sources -> claim checks -> rendered memo.md
# ---------------------------------------------------------------------------


def _minimal_memo_with_numbers() -> Memo:
    revenue_now = Fact(
        label="monthly_revenue_current",
        value=2_501_000,
        currency=Currency.INR,
        metric_kind=MetricKind.REVENUE,
        period=Period.MONTHLY,
        as_of=date(2026, 6, 30),
        source_ids=[1],
    )
    valuation = Fact(
        label="valuation",
        value=200_000_000,
        currency=Currency.INR,
        metric_kind=MetricKind.VALUATION,
        source_ids=[2],
    )
    return Memo(
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
        numbers=Numbers(monthly_revenue_current=revenue_now, valuation=valuation),
        claims=[
            Claim(id="c1", text="Founded in 2022", section="snapshot", source_ids=[1], supported=True),
        ],
        risks=[
            RiskItem(risk="Thin moat", mitigant="Ask about switching costs."),
            RiskItem(risk="Customer concentration", mitigant="Ask for logo breakdown."),
            RiskItem(risk="Regulatory exposure", mitigant="Ask about compliance roadmap."),
        ],
        what_would_change_recommendation="Evidence of net-negative churn.",
        founder_questions=[f"Question {i}?" for i in range(1, 6)],
        generated_at=datetime(2026, 9, 17, 12, 0, 0),
    )


def test_end_to_end_saved_evidence_through_render(tmp_root):
    run_dir = storage.new_run_dir("Testly", root=tmp_root)

    sources = [
        _src(1, "https://entrackr.example/testly-revenue", date(2026, 7, 1)),
        _src(2, "https://inc42.example/testly-valuation", date(2026, 7, 1)),
    ]
    storage.write_json(run_dir / "sources.json", [s.model_dump(mode="json") for s in sources])

    memo = _minimal_memo_with_numbers()
    storage.write_json(run_dir / "memo.json", memo)

    loaded_memo = storage.load_memo(run_dir)
    assert loaded_memo == memo

    from firstcheque.cli import _resolve_annual_revenue

    annual_revenue = _resolve_annual_revenue(loaded_memo.numbers)
    result = claims_mod.valuation_multiple(loaded_memo.numbers.valuation, annual_revenue)
    checks = {"valuation_multiple": result}
    storage.write_json(
        run_dir / "claim_checks.json", {k: v.model_dump(mode="json") for k, v in checks.items()}
    )

    loaded_checks = storage.load_claim_checks(run_dir)
    assert loaded_checks["valuation_multiple"].result_display == "6.7x"

    memo_md = render_memo_md(loaded_memo, storage.load_sources(run_dir), loaded_checks)
    assert "Testly Technologies" in memo_md
    assert "6.7x" in memo_md
    assert "AI-drafted from public sources" in memo_md
    assert "[^1]:" in memo_md and "[^2]:" in memo_md


def test_html_render_is_self_contained_and_has_no_dangling_footnote_links():
    """The HTML view must open with no server/network: no external
    stylesheet/script tags, and every in-prose [^N] marker must resolve to
    an anchor that actually exists in the rendered sources list."""
    run_dir_memo = _minimal_memo_with_numbers()
    sources = [
        Source(
            id=1,
            title="Entrackr piece",
            url="https://entrackr.example/testly",
            accessed_date=date(2026, 7, 1),
            passage="Testly reports monthly revenue of INR 25.01 lakh.",
            evidence_type=EvidenceType.SEARCH_SNIPPET,
        ),
        Source(
            id=2,
            title="Inc42 piece",
            url="https://inc42.example/testly",
            accessed_date=date(2026, 7, 1),
            passage="Testly is valued at INR 20 crore.",
            evidence_type=EvidenceType.SEARCH_SNIPPET,
        ),
    ]
    from firstcheque.cli import _resolve_annual_revenue

    checks = {
        "valuation_multiple": claims_mod.valuation_multiple(
            run_dir_memo.numbers.valuation, _resolve_annual_revenue(run_dir_memo.numbers)
        )
    }

    html_doc = render_memo_html(run_dir_memo, sources, checks)

    assert html_doc.startswith("<!DOCTYPE html>")
    assert "<script" not in html_doc
    assert "http://" not in html_doc.split("<style>")[0]  # no external stylesheet before the inline one
    assert 'rel="stylesheet"' not in html_doc
    assert "Testly Technologies" in html_doc
    assert 'id="src-1"' in html_doc and 'id="src-2"' in html_doc

    referenced_ids = set(int(n) for n in re.findall(r'href="#src-(\d+)"', html_doc))
    anchored_ids = set(int(n) for n in re.findall(r'id="src-(\d+)"', html_doc))
    assert referenced_ids <= anchored_ids
