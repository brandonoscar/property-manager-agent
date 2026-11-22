"""Pydantic models for the summarizer's output."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# PR #92 — hard cap on summary length AFTER the LLM returns. The
# prompt asks for ~3 sentences; if the model went long anyway we
# truncate to this many chars + an ellipsis. 500 is enough for a
# tight paragraph but short enough that the Inbox detail pane
# doesn't have to scroll the summary.
SUMMARY_TEXT_MAX_CHARS = 500


class SummaryResult(BaseModel):
    """One summarizer call's output. Persisted columns on
    ``buildium_events`` are populated from this object."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(..., min_length=1, max_length=SUMMARY_TEXT_MAX_CHARS)
    """The 1-3 sentence narrative, already sanitized of Buildium
    internal IDs and truncated to SUMMARY_TEXT_MAX_CHARS."""

    prompt_version: str
    """In-code version tag (see ``prompt.SUMMARY_PROMPT_VERSION``)
    stamped on the row so a future PR can re-summarize rows written
    against an old prompt without rebuilding the whole pipeline."""

    model_name: str
    """The LiteLLM model name the call actually used (e.g.
    ``gemini/gemini-2.5-flash``). Useful for audits when we change
    models or the routing picks a different one."""

    duration_ms: int = Field(..., ge=0)
    """Wall-clock duration of the LLM call. The worker logs this
    when it persists the row; Render's log aggregator can spot
    creep from the planner's typical 5-12s baseline."""
