"""Run-folder persistence for FirstCheque.

Every company gets `memos/<slug>/`, and every time the workflow runs for
that company it gets a new subfolder `run-001`, `run-002`, ... This module
never overwrites a previous run: that's what lets `review.md` (the
analyst's own judgment) survive later runs, and what makes `changes.md`
possible (it just diffs run N against run N-1).

Nothing here calls an LLM. It reads/writes JSON and markdown and does the
bookkeeping (slugs, run ids, source-id continuity, evidence age) that the
/company-brief skill relies on so Claude doesn't have to reimplement file
handling by hand on every invocation.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from .schema import ClaimCheckResult, Memo, RunMeta, Source

MEMOS_ROOT = Path("memos")

_RUN_DIR_RE = re.compile(r"^run-(\d{3,})$")


def slugify(company: str) -> str:
    slug = company.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-") or "unnamed-company"


# ---------------------------------------------------------------------------
# Run directory bookkeeping
# ---------------------------------------------------------------------------


def company_dir(company: str, root: Path = MEMOS_ROOT) -> Path:
    return root / slugify(company)


def list_runs(company: str, root: Path = MEMOS_ROOT) -> list[Path]:
    """Existing run-NNN directories, oldest first."""
    cdir = company_dir(company, root)
    if not cdir.exists():
        return []
    runs = []
    for child in cdir.iterdir():
        if child.is_dir() and _RUN_DIR_RE.match(child.name):
            runs.append(child)
    return sorted(runs, key=lambda p: int(_RUN_DIR_RE.match(p.name).group(1)))


def latest_run(company: str, root: Path = MEMOS_ROOT) -> Optional[Path]:
    runs = list_runs(company, root)
    return runs[-1] if runs else None


def new_run_dir(company: str, root: Path = MEMOS_ROOT) -> Path:
    """Allocate (but do not populate) the next run-NNN directory."""
    existing = list_runs(company, root)
    next_n = 1
    if existing:
        next_n = int(_RUN_DIR_RE.match(existing[-1].name).group(1)) + 1
    run_dir = company_dir(company, root) / f"run-{next_n:03d}"
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


# ---------------------------------------------------------------------------
# JSON read/write helpers
# ---------------------------------------------------------------------------


def _default(obj):
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    raise TypeError(f"not JSON serialisable: {type(obj)}")


def write_json(path: Path, data) -> None:
    if hasattr(data, "model_dump"):
        data = data.model_dump(mode="json")
    path.write_text(json.dumps(data, indent=2, default=_default, ensure_ascii=False), encoding="utf-8")


def read_json(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_sources(run_dir: Path) -> list[Source]:
    data = read_json(run_dir / "sources.json") or []
    return [Source.model_validate(item) for item in data]


def load_memo(run_dir: Path) -> Optional[Memo]:
    data = read_json(run_dir / "memo.json")
    return Memo.model_validate(data) if data else None


def load_claim_checks(run_dir: Path) -> dict[str, ClaimCheckResult]:
    data = read_json(run_dir / "claim_checks.json") or {}
    return {name: ClaimCheckResult.model_validate(v) for name, v in data.items()}


def load_run_meta(run_dir: Path) -> Optional[RunMeta]:
    data = read_json(run_dir / "run.json")
    return RunMeta.model_validate(data) if data else None


# ---------------------------------------------------------------------------
# Evidence reuse: merge a previous source register with newly-found sources,
# keeping stable IDs and letting the caller judge staleness.
# ---------------------------------------------------------------------------


@dataclass
class SourceMergeResult:
    merged: list[Source]
    reused_ids: list[int]
    new_ids: list[int]


def merge_sources(previous: list[Source], newly_found: list[Source]) -> SourceMergeResult:
    """Dedupe `newly_found` against `previous` by URL (falling back to
    local_path). A source already in `previous` keeps its original ID even
    if it was fetched again; a genuinely new URL gets the next free ID.

    This is what lets a citation like `source_ids=[3]` mean the same source
    across runs -- important, because the memo's prose and the source
    register are written in separate steps and must not drift apart.
    """
    by_key: dict[str, Source] = {}
    for s in previous:
        key = s.url or s.local_path
        by_key[key] = s

    next_id = max([s.id for s in previous], default=0) + 1
    merged = list(previous)
    reused_ids: list[int] = []
    new_ids: list[int] = []

    for candidate in newly_found:
        key = candidate.url or candidate.local_path
        existing = by_key.get(key)
        if existing is not None:
            reused_ids.append(existing.id)
            continue
        candidate = candidate.model_copy(update={"id": next_id})
        by_key[key] = candidate
        merged.append(candidate)
        new_ids.append(next_id)
        next_id += 1

    return SourceMergeResult(merged=merged, reused_ids=reused_ids, new_ids=new_ids)


def evidence_age_days(source: Source, as_of: Optional[date] = None) -> int:
    as_of = as_of or date.today()
    return (as_of - source.accessed_date).days


# ---------------------------------------------------------------------------
# Review carry-forward
# ---------------------------------------------------------------------------

REVIEW_TEMPLATE = """\
# Review: {company} (run {run_id})

<!--
  Fill this in yourself. Nothing below is written by the AI. Leave a field
  blank if you have not reached a judgment yet; a blank field is not the
  same as "no corrections needed."
-->

## Corrections to the draft

<!-- Anything the draft got wrong, oversimplified, or missed. -->

## Disputed claims

<!-- Claims you don't accept even though a source was cited, and why. -->

## Final recommendation

<!-- Your own Meet / Track / Pass, which may differ from the draft's. -->

## Follow-up questions

<!-- Questions you'd actually ask the founders, beyond the draft's five. -->
"""


def is_review_blank(review_text: str) -> bool:
    """A review counts as "completed" once the analyst has typed anything
    into it beyond the template's own headings/comments (including
    multi-line HTML comments)."""
    stripped_lines = []
    in_comment = False
    for line in review_text.splitlines():
        line = line.strip()
        if not line:
            continue
        if in_comment:
            if line.endswith("-->"):
                in_comment = False
            continue
        if line.startswith("<!--"):
            if not line.endswith("-->"):
                in_comment = True
            continue
        if line.startswith("#"):
            continue
        stripped_lines.append(line)
    return len(stripped_lines) == 0


def write_review_template(run_dir: Path, company: str, run_id: str, previous_run_dir: Optional[Path]) -> None:
    """Write a fresh review.md for this run. If the previous run has a
    completed review, copy it alongside as `previous_review.md` so the
    analyst can see their own last judgment while reviewing the update --
    the previous run's own review.md is left untouched where it is."""
    (run_dir / "review.md").write_text(
        REVIEW_TEMPLATE.format(company=company, run_id=run_id), encoding="utf-8"
    )
    if previous_run_dir is None:
        return
    previous_review_path = previous_run_dir / "review.md"
    if not previous_review_path.exists():
        return
    previous_text = previous_review_path.read_text(encoding="utf-8")
    if is_review_blank(previous_text):
        return
    (run_dir / "previous_review.md").write_text(previous_text, encoding="utf-8")
