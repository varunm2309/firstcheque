"""Render a Memo + its claim checks + its source register into memo.md, and
diff two runs into changes.md.

This module does no writing of its own prose -- it assembles fields Claude
already wrote (under the /company-brief skill) into one document, in a
fixed layout, with footnote-style citations. Keeping this mechanical (no
LLM call here) is what makes the numbers in the appendix trustworthy: they
come straight out of claim_checks.json, not out of a second model pass.
"""

from __future__ import annotations

import html
import re

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


# ---------------------------------------------------------------------------
# HTML rendering: a single self-contained file (no CDN scripts/styles, no
# server) so it opens directly in a browser from disk. Same underlying data
# as render_memo_md -- this is a second view of it, not a second source of
# truth.
# ---------------------------------------------------------------------------

_FOOTNOTE_RE = re.compile(r"\[\^(\d+)\]")


def _e(text) -> str:
    """HTML-escape anything we interpolate. Memo prose is analyst/LLM
    written free text, not trusted markup, so this always runs first."""
    return html.escape(str(text), quote=True)


def _linkify_footnotes(escaped_text: str) -> str:
    """Turn already-escaped '[^3]' markers into jump links to the source
    list. Must run AFTER escaping -- '[', ']', '^' aren't HTML-special, so
    this is safe to do as a second pass."""
    return _FOOTNOTE_RE.sub(
        lambda m: f'<sup><a href="#src-{m.group(1)}">{m.group(1)}</a></sup>', escaped_text
    )


def _html_prose(text: str) -> str:
    return _linkify_footnotes(_e(text))


_RECOMMENDATION_CLASS = {"Meet": "rec-meet", "Track": "rec-track", "Pass": "rec-pass"}


def _html_snapshot(memo: Memo) -> str:
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
    cells = "".join(
        f'<div class="snap-item"><div class="snap-label">{_e(label)}</div>'
        f'<div class="snap-value">{_html_prose(value)}</div></div>'
        for label, value in rows
        if value
    )
    return cells or '<p class="muted">Snapshot incomplete.</p>'


def _html_risks(memo: Memo) -> str:
    items = "".join(
        f"<li><strong>{_html_prose(item.risk)}</strong>"
        f'<div class="mitigant">Possible mitigant / question to press on: {_html_prose(item.mitigant)}</div></li>'
        for item in memo.risks
    )
    return f"<ol class=\"risks\">{items}</ol>"


def _html_founder_questions(memo: Memo) -> str:
    items = "".join(f"<li>{_html_prose(q)}</li>" for q in memo.founder_questions)
    return f"<ol>{items}</ol>"


def _html_rdi(memo: Memo) -> str:
    if not memo.rdi_fits:
        return (
            '<p class="muted">No RDI theme assigned. Theme alignment, where present, '
            "is not confirmation of funding eligibility.</p>"
        )
    items = "".join(
        f"<li><strong>{_e(fit.theme_name)}</strong>: {_html_prose(fit.rationale)}</li>"
        for fit in memo.rdi_fits
    )
    return f'<ul>{items}</ul><p class="muted">Theme alignment is not confirmation of RDI funding eligibility.</p>'


def _html_gaps(memo: Memo) -> str:
    if not memo.unresolved_gaps:
        return ""
    items = "".join(f"<li>{_html_prose(g)}</li>" for g in memo.unresolved_gaps)
    return f'<section class="card gaps"><h2>Unresolved gaps</h2><ul>{items}</ul></section>'


def _html_claim_check(name: str, result: ClaimCheckResult) -> str:
    status_class = "status-ok" if result.status == ClaimCheckStatus.OK else "status-flag"
    header = (
        f'<div class="check-head"><code>{_e(name)}</code>'
        f'<span class="badge {status_class}">{_e(result.status.value)}</span></div>'
        f'<div class="formula">{_e(result.formula)}</div>'
    )
    if result.status != ClaimCheckStatus.OK:
        return f'<div class="check">{header}<div class="check-error">{_e(result.error)}</div></div>'

    body = [f'<div class="check-result">{_e(result.result_display)}</div>']
    if result.inputs:
        body.append(f'<div class="check-line"><strong>Inputs:</strong> {_e("; ".join(result.inputs))}</div>')
    if result.interpretation:
        body.append(
            f'<div class="check-line"><strong>Interpretation (heuristic):</strong> {_e(result.interpretation)}</div>'
        )
    for a in result.assumptions:
        body.append(f'<div class="check-line"><strong>Assumption:</strong> {_e(a)}</div>')
    if result.source_ids:
        refs = " ".join(
            f'<a href="#src-{sid}">[{sid}]</a>' for sid in sorted(result.source_ids)
        )
        body.append(f'<div class="check-line"><strong>Sources:</strong> {refs}</div>')
    return f'<div class="check">{header}{"".join(body)}</div>'


def _html_checks(claim_checks: dict[str, ClaimCheckResult]) -> str:
    if not claim_checks:
        return '<p class="muted">No numerical checks were computed (insufficient reported numbers).</p>'
    return "".join(_html_claim_check(name, result) for name, result in claim_checks.items())


def _html_source(source: Source) -> str:
    bits = [f"<strong>{_e(source.title)}</strong>"]
    meta = []
    if source.publisher:
        meta.append(_e(source.publisher))
    if source.published_date:
        meta.append(_e(source.published_date.isoformat()))
    if meta:
        bits.append(" &middot; ".join(meta))
    location = source.url or source.local_path
    if source.url:
        link = f'<a href="{_e(source.url)}" target="_blank" rel="noopener noreferrer">{_e(source.url)}</a>'
    else:
        link = _e(location)
    bits.append(f"{link} (accessed {_e(source.accessed_date.isoformat())})")
    if source.page:
        bits.append(f"p. {_e(source.page)}")
    evidence_badge = f'<span class="badge badge-evidence">{_e(source.evidence_type.value)}</span>'
    body = "<br>".join(bits)
    return f'<li id="src-{source.id}"><span class="src-id">[{source.id}]</span> {evidence_badge}<div>{body}</div></li>'


def _html_sources(sources: list[Source]) -> str:
    if not sources:
        return '<p class="muted">No sources recorded.</p>'
    ordered = sorted(sources, key=lambda s: s.id)
    return f'<ol class="sources">{"".join(_html_source(s) for s in ordered)}</ol>'


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{
    --paper: #faf8f4; --ink: #1f1c18; --muted: #6b6459; --line: #e4ddd0;
    --accent: #2f5233; --meet: #1f7a3d; --track: #b8860b; --pass: #a33a3a;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 2.5rem 1.25rem 4rem; background: var(--paper); color: var(--ink);
    font-family: Georgia, 'Iowan Old Style', 'Palatino Linotype', serif;
    line-height: 1.55; font-size: 17px;
  }}
  .page {{ max-width: 800px; margin: 0 auto; }}
  h1 {{ font-size: 2rem; margin-bottom: 0.15em; }}
  h1 + .website {{ font-family: 'Courier New', monospace; font-size: 0.85rem; }}
  h1 + .website a {{ color: var(--accent); }}
  .oneliner {{ font-style: italic; color: var(--muted); margin: 0.6em 0 1.4em; font-size: 1.05rem; }}
  h2 {{
    font-family: -apple-system, Helvetica, Arial, sans-serif; font-size: 0.78rem;
    letter-spacing: 0.09em; text-transform: uppercase; color: var(--muted);
    border-bottom: 1px solid var(--line); padding-bottom: 0.35em; margin: 2em 0 0.8em;
  }}
  .recommendation {{
    display: flex; align-items: center; gap: 0.75rem; margin: 1em 0 0.5em;
  }}
  .rec-pill {{
    font-family: -apple-system, Helvetica, Arial, sans-serif; font-weight: 700;
    font-size: 0.95rem; padding: 0.3em 0.9em; border-radius: 999px; color: #fff;
  }}
  .rec-meet {{ background: var(--meet); }}
  .rec-track {{ background: var(--track); }}
  .rec-pass {{ background: var(--pass); }}
  .rationale {{ margin: 0.6em 0 0; }}
  .snapshot-grid {{
    display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
    gap: 0.9rem; margin-top: 0.5em;
  }}
  .snap-item {{ background: #fff; border: 1px solid var(--line); border-radius: 8px; padding: 0.6em 0.8em; }}
  .snap-label {{
    font-family: -apple-system, Helvetica, Arial, sans-serif; font-size: 0.72rem;
    text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); margin-bottom: 0.2em;
  }}
  .snap-value {{ font-size: 0.95rem; }}
  p {{ margin: 0.7em 0; }}
  ol, ul {{ padding-left: 1.4em; }}
  .risks li {{ margin-bottom: 0.9em; }}
  .mitigant {{ color: var(--muted); font-size: 0.93rem; margin-top: 0.2em; }}
  .muted {{ color: var(--muted); font-style: italic; }}
  .card {{ background: #fff5e6; border: 1px solid #e8d5ab; border-radius: 8px; padding: 1em 1.2em; margin-top: 0.6em; }}
  .card h2 {{ border: none; margin-top: 0; }}
  .card.gaps {{ background: #fdf3f0; border-color: #eccbc2; }}
  .citation-line {{
    font-family: -apple-system, Helvetica, Arial, sans-serif; font-size: 0.9rem; color: var(--muted);
    border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); padding: 0.9em 0; margin: 1.6em 0;
  }}
  .check {{ border: 1px solid var(--line); border-radius: 8px; padding: 0.8em 1em; margin-bottom: 0.7em; background: #fff; }}
  .check-head {{ display: flex; justify-content: space-between; align-items: center; font-family: 'Courier New', monospace; }}
  .badge {{
    font-family: -apple-system, Helvetica, Arial, sans-serif; font-size: 0.68rem; font-weight: 700;
    text-transform: uppercase; letter-spacing: 0.04em; padding: 0.15em 0.6em; border-radius: 999px;
  }}
  .status-ok {{ background: #e3f2e6; color: var(--meet); }}
  .status-flag {{ background: #f5e9e3; color: #8a4b1f; }}
  .badge-evidence {{ background: #eee; color: #555; margin-left: 0.4em; }}
  .formula {{ font-family: 'Courier New', monospace; font-size: 0.85rem; color: var(--muted); margin: 0.3em 0 0.5em; }}
  .check-result {{ font-size: 1.15rem; font-weight: 700; margin-bottom: 0.3em; }}
  .check-error {{ color: #8a4b1f; }}
  .check-line {{ font-size: 0.9rem; margin: 0.25em 0; }}
  ol.sources {{ list-style: none; padding-left: 0; }}
  ol.sources li {{ padding: 0.6em 0; border-bottom: 1px solid var(--line); font-size: 0.92rem; }}
  .src-id {{ font-family: 'Courier New', monospace; font-weight: 700; margin-right: 0.4em; }}
  a {{ color: var(--accent); word-break: break-word; }}
  footer {{
    margin-top: 3em; padding-top: 1em; border-top: 1px solid var(--line);
    font-size: 0.85rem; color: var(--muted); font-style: italic; text-align: center;
  }}
  sup a {{ text-decoration: none; }}
  @media print {{ body {{ background: #fff; }} .card {{ break-inside: avoid; }} .check {{ break-inside: avoid; }} }}
</style>
</head>
<body>
<div class="page">
  <h1>{company}</h1>
  {website_line}
  <p class="oneliner">{one_liner}</p>

  <div class="recommendation">
    <span class="rec-pill {rec_class}">{recommendation}</span>
  </div>
  <p class="rationale">{recommendation_rationale}</p>

  <h2>Snapshot</h2>
  <div class="snapshot-grid">{snapshot}</div>

  <h2>Business model</h2>
  <p>{business_model}</p>

  <h2>Why now</h2>
  <p>{why_now}</p>

  <h2>Market and competition</h2>
  <p>{market_and_competition}</p>

  <h2>Traction and team</h2>
  <p>{traction_and_team}</p>

  <h2>Three risks and possible mitigants</h2>
  {risks}

  <h2>What would change this recommendation</h2>
  <p>{what_would_change_recommendation}</p>

  <h2>Five questions for the founders</h2>
  {founder_questions}

  <h2>RDI relevance</h2>
  {rdi}

  {gaps}

  <div class="citation-line">Citation support: {citation_rate} of tracked factual claims are backed by a
  saved, matching passage. This measures whether a cited source's saved text supports the claim -- it is
  not independent fact verification.</div>

  <h2>Appendix: numerical checks</h2>
  {checks}

  <h2>Appendix: sources</h2>
  {sources}

  <footer>{footer}</footer>
</div>
</body>
</html>
"""


def render_memo_html(memo: Memo, sources: list[Source], claim_checks: dict[str, ClaimCheckResult]) -> str:
    """A single, self-contained HTML file: no external stylesheets, fonts
    or scripts, so it opens straight from disk in any browser with no
    server and no network access (other than following a source's own
    external link, which is expected to need one)."""
    supported, denominator, rate = citation_support_rate(memo.claims)
    rate_str = f"{rate:.0%} ({supported}/{denominator})" if rate is not None else "n/a (no factual claims tracked)"

    website_line = ""
    if memo.website:
        website_line = f'<p class="website"><a href="{_e(memo.website)}" target="_blank" rel="noopener noreferrer">{_e(memo.website)}</a></p>'

    return _HTML_TEMPLATE.format(
        title=f"{_e(memo.company)} -- FirstCheque memo",
        company=_e(memo.company),
        website_line=website_line,
        one_liner=_html_prose(memo.one_liner),
        rec_class=_RECOMMENDATION_CLASS.get(memo.recommendation.value, "rec-track"),
        recommendation=_e(memo.recommendation.value),
        recommendation_rationale=_html_prose(memo.recommendation_rationale),
        snapshot=_html_snapshot(memo),
        business_model=_html_prose(memo.business_model),
        why_now=_html_prose(memo.why_now),
        market_and_competition=_html_prose(memo.market_and_competition),
        traction_and_team=_html_prose(memo.traction_and_team),
        risks=_html_risks(memo),
        what_would_change_recommendation=_html_prose(memo.what_would_change_recommendation),
        founder_questions=_html_founder_questions(memo),
        rdi=_html_rdi(memo),
        gaps=_html_gaps(memo),
        citation_rate=_e(rate_str),
        checks=_html_checks(claim_checks),
        sources=_html_sources(sources),
        footer=_e(FOOTER),
    )
