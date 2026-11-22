"""What did THIS turn cost? Per-run token and dollar accounting.

We have a cost crisis and, until this module, no way to answer the question a
cost crisis is made of: *which turns are expensive, and why?*

What existed:

* ``llm/spend.py`` — a per-company-per-DAY ledger. Correct, and the right
  shape for a budget gate, but a daily total cannot tell one question from
  another. Gotcha 37 is the record of what that costs: the headline figure
  ``$0.274/turn`` was computed on 2026-08-23, which happened to run 11.5 model
  calls per turn, while 08-20/22/24 ran 38.5/21.3/34.0. A three-fold cost
  regression hid for a month behind an average taken on the cheapest day.
* ``orchestrator_runs.raw`` — ``stop_reason``, ``call_budget``, ``consults``,
  ``clocks``. Everything about a run EXCEPT what it spent.

So the two dark cost levers (``fast_router``, ``lookup_model``) are both
documented as "ships dark until calibrated", and there was no instrument to
calibrate them with. Every structural fix in gotchas 25/32/34/37 was verified
by counting tool calls, because tokens were not on the row.

This module puts the number on the row.

**Design**

* The accumulator rides the SAME LiteLLM success callback as the daily ledger
  (``spend.SpendTracker``), so a turn's cost and a company's day cannot drift
  into disagreeing about what was spent. One seam, two horizons.
* Cost comes from ``litellm.completion_cost``, never from prices written here.
  LiteLLM's map carries cache-aware per-model rates and is maintained against
  the providers; a constant in this file would be a number nobody re-checks.
  (Confirmed while writing this: direct-Anthropic ``claude-sonnet-5`` is
  \\$2/M input, not the \\$3/M a reasonable person would recall — gotcha 25's
  "confirm it on a live row, never from memory", applied to pricing.)
* A call LiteLLM cannot price increments ``unpriced_calls`` rather than
  contributing zero. A partial total presented as a total is exactly the
  ``$0.00`` bill of gotcha 25 — **a missing amount must never launder into a
  real-looking figure**, so consumers get the count and can treat the dollar
  figure as the floor it is.

⚠ **The accumulator is MUTATED, never re-``set``.** Specialist consults run as
nested ``.as_tool()`` sub-runs, and a Task COPIES the context — a
``ContextVar.set`` inside one is invisible outside (gotchas 34/42). Binding a
mutable object once and mutating it is the mechanism ``LoopStats`` and
``specialist_stream_capture`` already use to cross that boundary, and it is
why a sub-run's tokens land on the parent turn's total instead of vanishing.
"""

from __future__ import annotations

import contextvars
import dataclasses
import logging
from typing import Any

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class TurnUsage:
    """One run's model spend, accumulated across every completion it caused —
    the orchestrator's own turns AND every nested specialist sub-run."""

    calls: int = 0
    prompt: int = 0
    completion: int = 0
    cached: int = 0
    cache_write: int = 0
    cost_usd: float = 0.0
    #: Completions LiteLLM could not price (unknown model, missing rate).
    #: Their TOKENS are still counted; only their dollars are missing, so a
    #: non-zero value means ``cost_usd`` is a floor and not a total.
    unpriced_calls: int = 0
    #: calls per model — which tier actually did the work. This is the number
    #: that says whether a cheaper-tier lever is doing anything.
    by_model: dict[str, int] = dataclasses.field(default_factory=dict)

    @property
    def fresh_prompt(self) -> int:
        """Prompt tokens that were neither a cache read nor a cache write.

        ``cached`` and ``cache_write`` are SUBSETS of ``prompt`` — see
        ``spend.Usage``, where that relationship is read off LiteLLM's
        Anthropic transform rather than assumed. Clamped, so a provider
        reporting components above the total cannot go negative.
        """
        return max(0, self.prompt - self.cached - self.cache_write)

    @property
    def cache_read_pct(self) -> float | None:
        """Share of prompt tokens served from cache. ``None`` when there were
        no prompt tokens — 0.0 would read as "the cache is broken", which is a
        different and much more alarming statement than "nothing ran"."""
        if self.prompt <= 0:
            return None
        return round(100.0 * self.cached / self.prompt, 1)

    def summary(self) -> dict[str, Any]:
        """The shape written to ``orchestrator_runs.raw.usage``."""
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt,
            "completion_tokens": self.completion,
            "cached_tokens": self.cached,
            "cache_write_tokens": self.cache_write,
            "fresh_prompt_tokens": self.fresh_prompt,
            "cache_read_pct": self.cache_read_pct,
            # Rounded to a tenth of a cent: a turn costs cents, and more
            # precision than that invites reading noise as signal.
            "cost_usd": round(self.cost_usd, 4),
            "unpriced_calls": self.unpriced_calls,
            "cost_is_floor": self.unpriced_calls > 0,
            "by_model": dict(self.by_model),
        }


_turn_usage: contextvars.ContextVar[TurnUsage | None] = contextvars.ContextVar(
    "helixis_turn_usage", default=None
)


def bind_turn_usage() -> tuple[TurnUsage, contextvars.Token[TurnUsage | None]]:
    """Start accounting for one run. Bind beside the call ledger in
    ``run_agent_streaming``; unbind with :func:`unbind_turn_usage`."""
    ledger = TurnUsage()
    return ledger, _turn_usage.set(ledger)


def unbind_turn_usage(token: contextvars.Token[TurnUsage | None]) -> None:
    try:
        _turn_usage.reset(token)
    except ValueError:  # pragma: no cover — reset from a different context
        logger.debug("turn-usage token reset out of context", exc_info=True)


def current_turn_usage() -> TurnUsage | None:
    return _turn_usage.get()


def _cost_of(response_obj: Any, model: str | None) -> float | None:
    """LiteLLM's own cost for one completion, or ``None`` if it cannot price it.

    Never raises: accounting must not be able to fail a model call, the same
    posture ``spend.SpendTracker`` takes.
    """
    try:
        import litellm

        cost = litellm.completion_cost(completion_response=response_obj, model=model or "")
    except (Exception, SystemExit):  # noqa: BLE001
        logger.debug("completion_cost unavailable for one call", exc_info=True)
        return None
    try:
        value = float(cost)
    except (TypeError, ValueError):
        return None
    # 0.0 is what LiteLLM returns for a model it has no rate for, and it is
    # indistinguishable from a genuinely free call. Treating it as unpriced is
    # the conservative read: it marks the total a floor rather than asserting
    # a turn cost nothing.
    return value if value > 0 else None


def record_call(response_obj: Any, model: str | None, usage: Any) -> None:
    """Add one completion to the bound run's tally. No-op when unbound (the
    worker's planner/summarizer calls, boot warmups, evals).

    ``usage`` is a ``spend.Usage`` — passed in rather than re-parsed so the
    per-turn and per-day ledgers cannot disagree about the same completion.
    """
    ledger = _turn_usage.get()
    if ledger is None:
        return
    try:
        ledger.calls += 1
        ledger.prompt += int(getattr(usage, "prompt", 0) or 0)
        ledger.completion += int(getattr(usage, "completion", 0) or 0)
        ledger.cached += int(getattr(usage, "cached", 0) or 0)
        ledger.cache_write += int(getattr(usage, "cache_write", 0) or 0)
        name = str(model or "unknown")
        ledger.by_model[name] = ledger.by_model.get(name, 0) + 1
        cost = _cost_of(response_obj, model)
        if cost is None:
            ledger.unpriced_calls += 1
        else:
            ledger.cost_usd += cost
    except (Exception, SystemExit):  # noqa: BLE001 — accounting never breaks a call
        logger.debug("turn-usage accounting skipped for one call", exc_info=True)


__all__ = [
    "TurnUsage",
    "bind_turn_usage",
    "current_turn_usage",
    "record_call",
    "unbind_turn_usage",
]
