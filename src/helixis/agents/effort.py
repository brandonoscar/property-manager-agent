"""Adaptive reasoning effort — let easy questions be easy.

The 12-question live battery's median turn was **163 seconds**. "What's the
balance on 4B" costs a property manager about as long as "analyse my whole
portfolio", because every turn thinks with the same budget: we have never set
``reasoning_effort`` anywhere in the fleet.

That is the most-felt problem in the product and the cheapest one to move.
LiteLLM supports the parameter for Anthropic and the Agents SDK forwards it
(``LitellmModel`` reads ``ModelSettings.reasoning.effort`` first), so this
needs a routing decision, not a harness migration.

**Design rule: only ever DOWNGRADE, and only when confident.**

A classifier that guesses "hard" wrong costs nothing — that is today's
behaviour. A classifier that guesses "easy" wrong makes a hard question worse,
and slow-but-right beats fast-but-wrong for a manager acting on the answer. So
this is a narrow allowlist of shapes that are unambiguously single-fact
lookups; everything else — including everything it doesn't recognise — keeps
the current full budget.

It is also deliberately **deterministic**. Asking a model which effort level
to use would spend a model call to save model time, which is the trade this
exists to avoid.
"""

from __future__ import annotations

import re
from typing import Any, Literal

#: The effort levels we will ever emit. Typed so the SDK's Literal matches
#: without a cast — a str here is a mypy error, which is the point.
Effort = Literal["low"]

#: Question shapes that are a single lookup against one source. Each is
#: anchored to the START of the question: "what's the balance on 4B" is a
#: lookup, "what's the balance on 4B and should I file an eviction" is not,
#: and the conjunction guard below catches the difference.
#: ⚠ ``re.VERBOSE`` strips unescaped literal spaces, so every gap between
#: words here MUST be an explicit ``\s+``. Writing "| is" silently compiles to
#: "|is" and the alternative never matches a real sentence.
_LOOKUP_OPENERS = re.compile(
    r"""^\s*(?:
        (?:what|who|when|where)(?:'?s|\s+is|\s+are|\s+does|\s+do)\s+
        (?:the\s+|my\s+)?
        (?:\w+\s+){0,2}?
        (?:balance|rent|status|address|phone|email|owner|tenant|vendor|
           due\s?date|lease|amount|total|number)
      | how\s+much\s+(?:is|does|do)\s+
      | (?:show|list|get|find|look\s?up)\s+(?:me\s+)?(?:the\s+)?
    )""",
    re.IGNORECASE | re.VERBOSE,
)

#: Anything here means the question is NOT a simple lookup, wherever it
#: appears. Judgement, comparison, and portfolio scope all need the full
#: budget — these are the questions the product exists for.
_NEEDS_THOUGHT = re.compile(
    r"""(?:
        \bshould\s+i\b | \bwhy\b | \bcompare\b | \banaly[sz]e\b | \brecommend\b
      | \bacross\b | \bwhich\s+of\b | \beach\b | \bevery\b | \ball\s+(?:my|the)\b
      | \bportfolio\b | \btrend\b | \bpattern\b | \bdupli|\bcross-?reference\b
      | \bdraft\b | \bwrite\b | \bsend\b | \bcreate\b | \bupdate\b | \bclose\b
      | \bthen\b | \band\s+(?:also\s+)?(?:tell|show|check|see|let)\b
    )""",
    re.IGNORECASE | re.VERBOSE,
)

#: Past this, a "simple" question is carrying enough clauses that it probably
#: isn't one. Cheap guard against a lookup opener on a paragraph.
_MAX_LOOKUP_CHARS = 120


def classify_effort(task: str) -> Effort | None:
    """``"low"`` for an unambiguous single-fact lookup, else ``None``.

    ``None`` means "say nothing" — the model keeps whatever budget it has
    today. That is the safe default and the reason this can ship without a
    dogfood tuning pass: the only behaviour that changes is on questions it
    positively recognises as trivial.
    """
    if not task or len(task) > _MAX_LOOKUP_CHARS:
        return None
    if _NEEDS_THOUGHT.search(task):
        return None
    if task.count("?") > 1:
        return None  # more than one question mark = more than one question
    return "low" if _LOOKUP_OPENERS.search(task) else None


def effort_settings(task: str, *, enabled: bool = True) -> Any | None:
    """``ModelSettings`` carrying the effort for this task, or ``None``.

    Returns ``None`` whenever the task isn't confidently trivial, so callers
    can pass it straight through to ``Agent(model_settings=...)`` — the SDK
    treats ``None`` as "use the default", which is exactly today's behaviour.

    Never raises: an import or construction failure here must cost the
    optimisation, never the turn.
    """
    if not enabled:
        return None
    effort = classify_effort(task)
    if effort is None:
        return None
    try:
        from agents import ModelSettings
        from openai.types.shared import Reasoning

        return ModelSettings(reasoning=Reasoning(effort=effort))
    except (Exception, SystemExit):  # noqa: BLE001 — a latency hint, never load-bearing
        return None


__all__ = ["classify_effort", "effort_settings"]
