"""FirstCheque: Python side of a Claude-Code-driven startup research workflow.

This package does NOT call any LLM. It only validates data (Pydantic
models), does arithmetic (claims.py), stores run artifacts (storage.py),
and renders markdown (render.py). All research, extraction and writing is
done by Claude Code following the /company-brief skill, which then calls
these functions to check its own numbers and persist its output.
"""
