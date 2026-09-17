"""The canonical list of FirstCheque workflow stages.

This is the single source of truth for stage IDs, used by:
  - `cli.py`'s `log-event` command and its auto-logging wrapper (validates
    that a logged stage name is real, and gives the CLI's own commands a
    stage to log against).
  - `workflow_viewer.py`, which renders this same list as the node graph
    in `workflow.html` and looks up each stage's static description,
    executor(s), implementation pointer, and limitations.

Keeping this in one place is what stops the workflow diagram from
drifting out of sync with what the skill and CLI actually do -- if a
stage's real implementation changes, this is the one file to update, and
both the event log and the diagram follow.

`executors` lists who actually does the work for that stage. A stage with
both "claude" and "python" is a joint step: Claude does the judgment/
research part, a CLI command does the bookkeeping/validation part, and
both may log their own event for the same stage.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class Stage(BaseModel):
    id: str
    label: str
    executors: list[str]  # subset of "claude", "python", "human"
    description: str
    implementation: str
    inputs: str
    outputs: str
    limitations: str
    conditional: Optional[str] = None  # human-readable note on when this stage applies/doesn't


STAGES: list[Stage] = [
    Stage(
        id="company_input",
        label="Company Input",
        executors=["human"],
        description=(
            "You give the workflow a company name, website, or a batch list, optionally with your "
            "own notes/documents as context to investigate."
        ),
        implementation=".claude/skills/company-brief/SKILL.md, step 0 (parse arguments)",
        inputs="A company name/website typed at /company-brief, or a --context file / --batch list.",
        outputs="The raw invocation Claude starts from.",
        limitations=(
            "Your own notes are treated as claims to verify, never as facts copied into the memo directly."
        ),
    ),
    Stage(
        id="start_run",
        label="Identify / Start Run",
        executors=["claude", "python"],
        description=(
            "Claude resolves an ambiguous company name (asking you one focused question if needed, or "
            "using a given website as the anchor), then Python allocates the next run-NNN folder, "
            "carries forward the previous run's sources.json if one exists, and writes a fresh review.md "
            "(plus previous_review.md if the last run's review was actually completed)."
        ),
        implementation="src/firstcheque/cli.py::cmd_new_run (python); SKILL.md step 0-1 (claude)",
        inputs="Company name; existing memos/<slug>/run-*/ directories, if any.",
        outputs="A new run-NNN directory; sources.json (carried forward or empty); review.md.",
        limitations="Disambiguation quality depends on how much the company name search actually turns up.",
    ),
    Stage(
        id="plan_research",
        label="Plan Targeted Research",
        executors=["claude"],
        description=(
            "Claude plans 4-6 searches covering business model/customers, founders/team, funding/"
            "investors, revenue/traction, market/competitors, and negative news/risks."
        ),
        implementation=".claude/skills/company-brief/SKILL.md, step 2",
        inputs="The company identity from the previous stage.",
        outputs="A short list of planned search queries (not persisted as a separate artifact).",
        limitations="Query planning is judgment-based; there is no code that scores query coverage.",
    ),
    Stage(
        id="collect_evidence",
        label="Collect & Save Evidence",
        executors=["claude", "python"],
        description=(
            "Claude runs the searches/fetches and captures title, URL, publisher, date and the actual "
            "retrieved passage for each useful source. Python then merges newly-found sources into the "
            "run's sources.json, deduplicating by URL and preserving stable IDs across runs."
        ),
        implementation="src/firstcheque/cli.py::cmd_merge_sources + src/firstcheque/storage.py::merge_sources (python); SKILL.md step 3 (claude)",
        inputs="Search/fetch results from WebSearch/WebFetch.",
        outputs="sources.json (id, title, url/local_path, publisher, dates, passage, evidence_type).",
        limitations=(
            "Some sites block automated fetches (HTTP 403 seen for Business Standard in these runs); "
            "those sources are recorded as evidence_type=search_snippet and treated as lower confidence."
        ),
    ),
    Stage(
        id="extract_draft",
        label="Structured Extraction",
        executors=["claude", "python"],
        description=(
            "Claude writes memo.json against the Memo schema: prose sections, a Numbers block of typed "
            "Facts (each with currency, metric_kind and source IDs), and a Claims list. Python validates "
            "the result against the schema and checks that every cited source ID actually exists."
        ),
        implementation="src/firstcheque/schema.py::Memo (schema); src/firstcheque/cli.py::cmd_validate_memo (python); SKILL.md step 4 (claude)",
        inputs="Saved sources.json passages.",
        outputs="memo.json (validated).",
        limitations=(
            "Claude extracts values, currencies and metric kinds by reading the source text; a "
            "mislabelled metric_kind would only be caught later, by a claim check refusing to run."
        ),
    ),
    Stage(
        id="citation_check",
        label="Citation-Support Check",
        executors=["claude", "python"],
        description=(
            "Claude re-reads each cited passage and judges, honestly, whether it actually supports the "
            "specific claim -- setting claim.supported true/false. Python checks that no claim cites a "
            "nonexistent source ID, and computes the citation-support rate with an explicit denominator "
            "(excluding opinions/founder questions)."
        ),
        implementation="src/firstcheque/schema.py::citation_support_rate (python); src/firstcheque/cli.py::cmd_citation_check (python); SKILL.md step 5 (claude)",
        inputs="memo.json claims[] and their cited sources.json passages.",
        outputs="Updated memo.json (claims marked supported/unsupported); a printed citation-support rate.",
        limitations=(
            "This is citation SUPPORT, not independent fact verification -- it checks whether the saved "
            "text backs the claim, not whether the underlying fact is true."
        ),
    ),
    Stage(
        id="follow_up_pass",
        label="Targeted Follow-Up Pass",
        executors=["claude"],
        description=(
            "If material gaps remain (missing revenue, valuation, founders, or something flagged in "
            "unresolved_gaps), Claude runs up to 3 more queries once to try to close them, then updates "
            "memo.json and re-merges any new sources. This is a single conditional pass, not an "
            "automated retry loop."
        ),
        implementation=".claude/skills/company-brief/SKILL.md, step 6",
        inputs="unresolved_gaps and missing Numbers fields from the extraction/citation-check stages.",
        outputs="Possibly-updated sources.json and memo.json.",
        limitations="Runs at most once per invocation; gaps that remain after this pass stay as unresolved_gaps in the memo, not hidden.",
        conditional="Only runs when Claude judges there is a material gap worth one more search pass.",
    ),
    Stage(
        id="compute_claims",
        label="Python Claim Checks",
        executors=["python"],
        description=(
            "Pure-Python arithmetic over memo.json's Numbers block: annualised revenue run rate, "
            "valuation multiple, ARPU, growth multiple, capital efficiency, capital raised per year, "
            "implied dilution. Every formula refuses to compute (with a structured reason) on a missing "
            "input, a zero denominator, a currency mismatch, or a metric-kind mismatch, instead of "
            "guessing."
        ),
        implementation="src/firstcheque/claims.py (all 7 functions); src/firstcheque/cli.py::cmd_compute_claims",
        inputs="memo.json's Numbers Facts (value, currency, metric_kind, source_ids).",
        outputs="claim_checks.json (one ClaimCheckResult per formula).",
        limitations="Formulas only catch structural problems (missing/zero/mismatched); they cannot catch a source that is simply wrong.",
    ),
    Stage(
        id="render_memo",
        label="Render Memo",
        executors=["python"],
        description=(
            "Assembles Claude's prose, Python's computed numbers, and the footnoted source list into "
            "memo.md and a self-contained memo.html. No LLM call happens here -- this is purely "
            "mechanical assembly of already-validated data."
        ),
        implementation="src/firstcheque/render.py::render_memo_md / render_memo_html; src/firstcheque/cli.py::cmd_render",
        inputs="memo.json, claim_checks.json, sources.json.",
        outputs="memo.md, memo.html.",
        limitations="Rendering trusts memo.json's prose fields; it does not re-check facts, only formats them.",
    ),
    Stage(
        id="human_review",
        label="Analyst Review",
        executors=["human"],
        description=(
            "You read the draft and record your own corrections, disputed claims, final recommendation "
            "and follow-up questions in review.md. The AI never writes to this file."
        ),
        implementation="src/firstcheque/storage.py::write_review_template / is_review_blank; SKILL.md step 9",
        inputs="memo.md / memo.html and your own judgment.",
        outputs="A completed review.md (or it stays blank until you fill it in).",
        limitations="Status is read directly from review.md's content, never inferred or assumed.",
    ),
    Stage(
        id="compare_previous_run",
        label="Compare With Previous Run",
        executors=["python"],
        description=(
            "For an update run only: diffs the new run's recommendation, claim-check results and "
            "unresolved gaps against the immediately preceding run for the same company, and writes "
            "changes.md. Reports 'unchanged' honestly when nothing material changed, rather than "
            "inventing a delta."
        ),
        implementation="src/firstcheque/render.py::render_changes_md; src/firstcheque/cli.py::cmd_diff; SKILL.md step 10",
        inputs="The current and previous run's memo.json + claim_checks.json + sources.json.",
        outputs="changes.md.",
        limitations="Only compares against the immediately previous run, not the full run history.",
        conditional="Only applicable when a previous run exists for this company (i.e. run-002 or later).",
    ),
]

STAGES_BY_ID: dict[str, Stage] = {s.id: s for s in STAGES}

VALID_EXECUTORS = {"claude", "python", "human"}
VALID_STATUSES = {"started", "ok", "error", "skipped"}
