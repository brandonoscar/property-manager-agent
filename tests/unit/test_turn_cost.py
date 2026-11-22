"""Per-turn cost accounting — what a run actually spent.

Why this exists: the row carried ``stop_reason``, ``call_budget``,
``consults`` and three clocks — everything about a run except its cost. So
gotcha 37's three-fold regression hid for a month behind a daily average taken
on the cheapest day, and the two dark cost levers (``fast_router``,
``lookup_model``) were both documented as "ships dark until calibrated" with
no instrument to calibrate them with.

The two properties that actually matter here, and both have bitten before:

* **A nested specialist sub-run's tokens must land on the parent turn.** The
  sub-runs are Tasks and a Task COPIES the context, so a ``ContextVar.set``
  inside one is invisible outside (gotchas 34/42). Mutating a bound object is
  the mechanism that crosses that boundary — and a test that cannot observe
  the failure is not covering it (gotcha 36), so the sub-run test runs its own
  ``asyncio.run`` rather than trusting an assertion made in the same task.
* **A missing amount must never launder into a real-looking figure.** Gotcha
  25's ``$0.00`` unpaid-bills report is the standing record of what that costs.
  A call LiteLLM cannot price marks the total a FLOOR; it never contributes a
  silent zero.
"""

from __future__ import annotations

import asyncio

import pytest

from helixis.llm.spend import SpendTracker, Usage
from helixis.llm.turn_cost import (
    TurnUsage,
    bind_turn_usage,
    current_turn_usage,
    record_call,
    unbind_turn_usage,
)


class _Resp:
    """Minimal stand-in for a LiteLLM response carrying usage."""

    def __init__(self, prompt: int = 0, completion: int = 0, cached: int = 0, write: int = 0):
        self.usage = type(
            "U",
            (),
            {
                "total_tokens": prompt + completion,
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "prompt_tokens_details": {
                    "cached_tokens": cached,
                    "cache_creation_tokens": write,
                },
            },
        )()


# ── the arithmetic ──────────────────────────────────────────────────────────


def test_cached_and_write_are_subsets_of_prompt() -> None:
    """``spend.Usage`` reads this relationship off LiteLLM's Anthropic
    transform rather than assuming it; getting it backwards double-counts the
    cache. Same relationship, restated where the per-turn figure depends on it."""
    u = TurnUsage(prompt=10_000, cached=8_000, cache_write=1_000)
    assert u.fresh_prompt == 1_000
    assert u.cache_read_pct == 80.0


def test_fresh_prompt_clamps_rather_than_going_negative() -> None:
    """A provider reporting components above the total must not hand out a
    negative token count that reads as a credit."""
    assert TurnUsage(prompt=100, cached=200, cache_write=50).fresh_prompt == 0


def test_no_prompt_tokens_reports_no_cache_rate_not_a_zero_one() -> None:
    """⚠ 0.0%% reads as "the cache is broken" — a much more alarming claim than
    "nothing ran", and the two must not be spelled the same way."""
    assert TurnUsage().cache_read_pct is None
    assert TurnUsage(prompt=10, cached=0).cache_read_pct == 0.0


# ── the floor rule ──────────────────────────────────────────────────────────


def test_an_unpriced_call_marks_the_total_a_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gotcha 25's rule. A call we cannot price contributes its TOKENS and
    withholds its dollars — it never silently adds zero and lets a partial
    total be read as a total."""
    monkeypatch.setattr("helixis.llm.turn_cost._cost_of", lambda *_a, **_k: None)
    ledger, token = bind_turn_usage()
    try:
        record_call(
            _Resp(prompt=500, completion=50), "some/unknown-model", Usage(prompt=500, completion=50)
        )
    finally:
        unbind_turn_usage(token)

    summary = ledger.summary()
    assert summary["unpriced_calls"] == 1
    assert summary["cost_is_floor"] is True
    assert summary["prompt_tokens"] == 500, "tokens are known even when dollars are not"


def test_a_zero_cost_is_treated_as_unpriced(monkeypatch: pytest.MonkeyPatch) -> None:
    """LiteLLM returns 0.0 for a model it has no rate for, which is
    indistinguishable from a genuinely free call. Marking the total a floor is
    the conservative read; asserting "this turn cost nothing" is not."""
    monkeypatch.setattr("litellm.completion_cost", lambda **_k: 0.0)
    ledger, token = bind_turn_usage()
    try:
        record_call(_Resp(prompt=10), "x", Usage(prompt=10))
    finally:
        unbind_turn_usage(token)
    assert ledger.unpriced_calls == 1
    assert ledger.summary()["cost_is_floor"] is True


def test_priced_calls_accumulate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("litellm.completion_cost", lambda **_k: 0.004)
    ledger, token = bind_turn_usage()
    try:
        for _ in range(3):
            record_call(
                _Resp(prompt=100, completion=10),
                "anthropic/claude-sonnet-5",
                Usage(prompt=100, completion=10),
            )
    finally:
        unbind_turn_usage(token)
    assert ledger.calls == 3
    assert ledger.summary()["cost_usd"] == 0.012
    assert ledger.summary()["cost_is_floor"] is False
    assert ledger.by_model == {"anthropic/claude-sonnet-5": 3}


def test_by_model_separates_the_tiers(monkeypatch: pytest.MonkeyPatch) -> None:
    """The number that says whether a cheaper-tier lever is doing anything.
    Without it, ``fast_router`` and ``lookup_model`` cannot be calibrated —
    which is why both shipped dark."""
    monkeypatch.setattr("litellm.completion_cost", lambda **_k: 0.001)
    ledger, token = bind_turn_usage()
    try:
        record_call(_Resp(prompt=1), "anthropic/claude-sonnet-5", Usage(prompt=1))
        record_call(_Resp(prompt=1), "anthropic/claude-haiku-4-5", Usage(prompt=1))
        record_call(_Resp(prompt=1), "anthropic/claude-haiku-4-5", Usage(prompt=1))
    finally:
        unbind_turn_usage(token)
    assert ledger.by_model == {
        "anthropic/claude-sonnet-5": 1,
        "anthropic/claude-haiku-4-5": 2,
    }


# ── the boundary that eats accumulators ─────────────────────────────────────


def test_a_nested_task_lands_on_the_parent_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠ The property specialist consults depend on, and the one that has been
    got wrong twice (gotchas 34, 42).

    Every specialist consult is a nested ``.as_tool()`` sub-run — a Task. A
    Task COPIES the context, so ``ContextVar.set`` inside it is invisible to
    the parent. If the tokens a consult spends do not cross back, a turn that
    made 34 model calls reports the handful the orchestrator made itself, and
    the expensive half of the bill is simply missing from the row.

    Runs its OWN ``asyncio.run`` and asserts from OUTSIDE the task, because an
    assertion made inside the child would pass against the broken version.
    """
    monkeypatch.setattr("litellm.completion_cost", lambda **_k: 0.002)

    async def _child() -> None:
        # What a nested sub-run's completions do: mutate the bound object.
        record_call(
            _Resp(prompt=900, cached=800),
            "anthropic/claude-sonnet-5",
            Usage(prompt=900, cached=800),
        )

    async def _parent() -> TurnUsage:
        ledger, token = bind_turn_usage()
        try:
            record_call(_Resp(prompt=100), "anthropic/claude-sonnet-5", Usage(prompt=100))
            await asyncio.create_task(_child())  # the boundary
        finally:
            unbind_turn_usage(token)
        return ledger

    ledger = asyncio.run(_parent())
    assert ledger.calls == 2, "the nested sub-run's completion never reached the parent tally"
    assert ledger.prompt == 1_000
    assert ledger.cached == 800
    assert ledger.summary()["cost_usd"] == 0.004


def test_recording_outside_a_run_is_a_no_op() -> None:
    """The worker's planner/summarizer calls, boot warmups and evals all go
    through the same callback and must not accumulate onto anything."""
    assert current_turn_usage() is None
    record_call(_Resp(prompt=5), "x", Usage(prompt=5))  # must not raise
    assert current_turn_usage() is None


# ── the seam ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_spend_callback_feeds_the_turn_ledger(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠ Reads BOTH sides (gotcha 28): the per-DAY ledger's callback is where
    the per-TURN tally is fed, deliberately, so the two horizons cannot
    disagree about what one completion spent. A test of ``record_call`` alone
    would pass with the callback never wired.
    """
    monkeypatch.setattr("litellm.completion_cost", lambda **_k: 0.003)
    # No trace context bound → the per-company write returns early. The
    # per-turn record must happen ANYWAY: a turn's cost is worth having even
    # on a path with no company attribution.
    ledger, token = bind_turn_usage()
    try:
        await SpendTracker().async_log_success_event(
            {"model": "anthropic/claude-sonnet-5"},
            _Resp(prompt=2_000, completion=120, cached=1_800),
            None,
            None,
        )
    finally:
        unbind_turn_usage(token)

    assert ledger.calls == 1, "the spend callback did not feed the per-turn ledger"
    assert ledger.prompt == 2_000
    assert ledger.cached == 1_800
    assert ledger.summary()["cache_read_pct"] == 90.0


@pytest.mark.asyncio
async def test_a_broken_cost_lookup_never_fails_the_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Accounting has the same posture as the daily ledger and prompt caching:
    it is a hint, and a hint must never be able to fail a model call."""

    def _boom(**_k: object) -> float:
        raise RuntimeError("pricing map exploded")

    monkeypatch.setattr("litellm.completion_cost", _boom)
    ledger, token = bind_turn_usage()
    try:
        await SpendTracker().async_log_success_event(
            {"model": "anthropic/claude-sonnet-5"}, _Resp(prompt=10), None, None
        )
    finally:
        unbind_turn_usage(token)
    assert ledger.calls == 1
    assert ledger.unpriced_calls == 1
    assert ledger.summary()["cost_is_floor"] is True
