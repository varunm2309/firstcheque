"""Render a Memo + its claim checks + its source register into memo.md, and
diff two runs into changes.md.

This module does no writing of its own prose -- it assembles fields Claude
already wrote (under the /company-brief skill) into one document, in a
fixed layout, with footnote-style citations. Keeping this mechanical (no
LLM call here) is what makes the numbers in the appendix trustworthy: they
come straight out of claim_checks.json, not out of a second model pass.
"""

from __future__ import annotations

from .schema import ClaimCheckResult, ClaimCheckStatus, Memo, RunMeta, Source, citation_support_rate

FOOTER = "AI-drafted from public sources, numbers re-checked in code. Verify before relying on it."


def _footnote_ref(source: Source) -> str:
    bits = [source.title]
    if source.publisher:
        bits.append(source.publisher)
    if source.published_date:
        bits.append(source.published_date.isoformat())
    location = source.url or source.local_path
    bits.append(f"{location} (accessed {source.accessed_date.isoformat()})")
    if source.page:
        bits.append(f"p. {source.page}")
    return f"[^{source.id}]: " + ". ".join(bits) + "."


def _sources_block(sources: list[Source]) -> str:
    if not sources:
        return "_No sources recorded._"
    ordered = sorted(sources, key=lambda s: s.id)
    return "\n".join(_footnote_ref(s) for s in ordered)


def _risk_block(memo: Memo) -> str:
    lines = []
    for i, item in enumerate(memo.risks, start=1):
        lines.append(f"{i}. **{item.risk}** -- possible mitigant / question to press on: {item.mitigant}")
    return "\n".join(lines)


def _founder_questions_block(memo: Memo) -> str:
    return "\n".join(f"{i}. {q}" for i, q in enumerate(memo.founder_questions, start=1))


def _claim_check_block(name: str, result: ClaimCheckResult) -> str:
    lines = [f"**{name}** -- `{result.formula}`"]
    if result.status != ClaimCheckStatus.OK:
        lines.append(f"- Status: {result.status.value} ({result.error})")
        return "\n".join(lines)
    lines.append(f"- Inputs: {'; '.join(result.inputs)}")
    lines.append(f"- Result: {result.result_display}")
    if result.interpretation:
        lines.append(f"- Interpretation (heuristic): {result.interpretation}")
    for a in result.assumptions:
        lines.append(f"- Assumption: {a}")
    if result.source_ids:
        refs = " ".join(f"[^{sid}]" for sid in sorted(result.source_ids))
        lines.append(f"- Sources: {refs}")
    return "\n".join(lines)


def _numbers_appendix(claim_checks: dict[str, ClaimCheckResult]) -> str:
    if not claim_checks:
        return "_No numerical checks were computed (insufficient reported numbers)._"
    return "\n\n".join(_claim_check_block(name, result) for name, result in claim_checks.items())


def _rdi_block(memo: Memo) -> str:
    if not memo.rdi_fits:
        return "No RDI theme assigned. Theme alignment, where present, is not confirmation of funding eligibility."
    lines = []
    for fit in memo.rdi_fits:
        lines.append(f"- **{fit.theme_name}**: {fit.rationale}")
    lines.append("\n_Theme alignment is not confirmation of RDI funding eligibility._")
    return "\n".join(lines)


def _snapshot_block(memo: Memo) -> str:
    s = memo.snapshot
    rows = [
        ("Founded", s.founded),
        ("HQ", s.hq),
        ("Stage", s.stage),
        ("Total funding", s.total_funding_display),
        ("Last round", s.last_round),
        ("Investors", ", ".join(s.investors) if s.investors else None),
        ("Founders", ", ".join(s.founders) if s.founders else None),
    ]
    lines = [f"- **{label}:** {value}" for label, value in rows if value]
    return "\n".join(lines) if lines else "_Snapshot incomplete._"


def render_memo_md(memo: Memo, sources: list[Source], claim_checks: dict[str, ClaimCheckResult]) -> str:
    supported, denominator, rate = citation_support_rate(memo.claims)
    rate_str = f"{rate:.0%} ({supported}/{denominator})" if rate is not None else "n/a (no factual claims tracked)"

    parts = [
        f"# {memo.company}",
        f"{memo.website or ''}".strip(),
        f"\n> {memo.one_liner}",
        f"\n## Recommendation: {memo.recommendation.value}",
        memo.recommendation_rationale,
        "\n## Snapshot",
        _snapshot_block(memo),
        "\n## Business model",
        memo.business_model,
        "\n## Why now",
        memo.why_now,
        "\n## Market and competition",
        memo.market_and_competition,
        "\n## Traction and team",
        memo.traction_and_team,
        "\n## Three risks and possible mitigants",
        _risk_block(memo),
        "\n## What would change this recommendation",
        memo.what_would_change_recommendation,
        "\n## Five questions for the founders",
        _founder_questions_block(memo),
        "\n## RDI relevance",
        _rdi_block(memo),
    ]

    if memo.unresolved_gaps:
        parts.append("\n## Unresolved gaps")
        parts.append("\n".join(f"- {g}" for g in memo.unresolved_gaps))

    parts += [
        "\n---",
        f"\nCitation support: {rate_str} of tracked factual claims are backed by a saved, "
        "matching passage. This measures whether a cited source's saved text supports the "
        "claim -- it is not independent fact verification.",
        "\n## Appendix: numerical checks",
        _numbers_appendix(claim_checks),
        "\n## Appendix: sources",
        _sources_block(sources),
        "\n---",
        f"\n_{FOOTER}_",
    ]
    return "\n".join(p for p in parts if p is not None) + "\n"


def render_changes_md(
    company: str,
    previous_run_id: str,
    current_run_id: str,
    previous_memo: Memo,
    current_memo: Memo,
    previous_checks: dict[str, ClaimCheckResult],
    current_checks: dict[str, ClaimCheckResult],
    new_source_count: int,
) -> str:
    lines = [f"# Changes for {company}: {previous_run_id} -> {current_run_id}", ""]

    if previous_memo.recommendation != current_memo.recommendation:
        lines.append(
            f"- Recommendation changed: **{previous_memo.recommendation.value}** -> "
            f"**{current_memo.recommendation.value}**"
        )
    else:
        lines.append(f"- Recommendation unchanged: {current_memo.recommendation.value}")

    if new_source_count:
        lines.append(f"- {new_source_count} new source(s) added to the evidence register.")
    else:
        lines.append("- No new sources added; this run reused the existing evidence register.")

    lines.append("")
    lines.append("## Numerical checks")
    all_names = sorted(set(previous_checks) | set(current_checks))
    if not all_names:
        lines.append("_No numerical checks in either run._")
    for name in all_names:
        prev = previous_checks.get(name)
        curr = current_checks.get(name)
        if prev is None:
            lines.append(f"- `{name}`: newly computed -> {curr.result_display or curr.status.value}")
        elif curr is None:
            lines.append(f"- `{name}`: no longer computed (was {prev.result_display or prev.status.value})")
        elif prev.result_display != curr.result_display or prev.status != curr.status:
            lines.append(
                f"- `{name}`: {prev.result_display or prev.status.value} -> "
                f"{curr.result_display or curr.status.value}"
            )
        else:
            lines.append(f"- `{name}`: unchanged ({curr.result_display or curr.status.value})")

    prev_gaps = set(previous_memo.unresolved_gaps)
    curr_gaps = set(current_memo.unresolved_gaps)
    closed = prev_gaps - curr_gaps
    opened = curr_gaps - prev_gaps
    if closed or opened:
        lines.append("")
        lines.append("## Gaps")
        for g in sorted(closed):
            lines.append(f"- Closed: {g}")
        for g in sorted(opened):
            lines.append(f"- New: {g}")

    return "\n".join(lines) + "\n"
