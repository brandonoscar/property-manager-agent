"""Event summarizer — turn an enriched event into a 1-3 sentence
plain-English narrative the Inbox detail pane renders above the
field grid.

The summarizer reads ``EnrichedEvent`` (or a raw payload when
enrichment hasn't run yet) and prompts Gemini for a short
descriptive paragraph: who reported what, where, optionally tone
when clearly present. It does NOT prescribe actions — that's the
planner's job — and it must NOT contain Buildium internal IDs.
"""

from helixis.agent.summarizer.client import Summarizer, SummarizerError
from helixis.agent.summarizer.prompt import SUMMARY_PROMPT_VERSION
from helixis.agent.summarizer.sanitizer import (
    SummarySanitizationError,
    redact_id_references,
    sanitize_summary_text,
)
from helixis.agent.summarizer.types import SummaryResult

__all__ = [
    "SUMMARY_PROMPT_VERSION",
    "SummarizerError",
    "Summarizer",
    "SummaryResult",
    "SummarySanitizationError",
    "redact_id_references",
    "sanitize_summary_text",
]
