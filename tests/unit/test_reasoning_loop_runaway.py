"""Runaway protection for the reasoning loop (W1 slice 4).

A cross-source turn can call several specialist tools, each a nested sub-run —
so the worst case is multiplicative. Two independent bounds contain it:

* per-specialist sub-run turn cap (``.as_tool(max_turns=…)``) — settings knob;
* an absolute per-turn **wall-clock** deadline in the stream watchdog, because
  the stall clock only catches SILENCE and a fan-out loop that keeps emitting
  events could run long without ever going quiet.

These tests cover the wall-clock behavior (fires on a busy-but-long stream,
disabled by default, paused during an approval wait) and the settings defaults.
"""

from __future__ import annotations

import asyncio

import pytest

from helixis.agents.orchestrator import (
    _AgentRunStalledError,
    _SoftDeadlineReachedError,
    _stream_with_stall_watchdog,
)
from helixis.settings import Settings


async def _busy_stream(n: int, gap: float):
    """Yield ``n`` events ``gap`` seconds apart — never silent."""
    for i in range(n):
        await asyncio.sleep(gap)
        yield f"event-{i}"


async def _silent_then_end(silence: float):
    """No events for ``silence`` seconds, then the stream ends — models a tool
    blocked on an approval (the stream goes quiet while the gate is pending)."""
    await asyncio.sleep(silence)
    if False:  # pragma: no cover - makes this an (empty) async generator
        yield


async def _drain(agen) -> list:
    return [ev async for ev in agen]


# ── settings defaults ────────────────────────────────────────────


def test_runaway_settings_have_sane_defaults() -> None:
    s = Settings(
        env="test",
        llm={"default_model": "openai/gpt-4o", "openai_api_key": "k"},
        memory={"enabled": False},
    )
    # Bumped after the 2026-07-25 stress test (turns landed 149-212s; a
    # lease-history fetch needed >6 specialist sub-turns).
    assert s.agent.reasoning_loop_specialist_max_turns == 8
    assert s.agent.reasoning_loop_deadline_s == 240.0


# ── wall-clock cutoff ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wall_clock_fires_on_a_busy_but_long_stream() -> None:
    """A stream that keeps emitting (never silent) is still cut off once the
    absolute deadline passes — the stall clock alone would never fire."""
    stream = _busy_stream(n=100, gap=0.02)  # ~2s worth of steady events
    seen: list = []
    with pytest.raises(_AgentRunStalledError) as ei:
        async for ev in _stream_with_stall_watchdog(
            stream, {"pending": False}, poll=0.02, max_wall_clock=0.1
        ):
            seen.append(ev)
    assert "wall-clock deadline" in str(ei.value)
    # Fired early — nowhere near draining all 100 events.
    assert len(seen) < 100


@pytest.mark.asyncio
async def test_no_deadline_by_default_drains_fully() -> None:
    """max_wall_clock=None (the handoff path) never wall-clock-cuts."""
    out = await _drain(
        _stream_with_stall_watchdog(
            _busy_stream(n=5, gap=0.01), {"pending": False}, poll=0.02, max_wall_clock=None
        )
    )
    assert out == [f"event-{i}" for i in range(5)]


@pytest.mark.asyncio
async def test_wall_clock_paused_while_awaiting_approval() -> None:
    """An approval wait is silent + legitimate up to the 300s confirm timeout —
    both clocks must pause while ``confirm_wait['pending']`` so a gated turn
    isn't killed mid-approval. The stream is quiet during the wait (the tool is
    blocked), so the accrual pauses and the tiny deadline never fires."""
    out = await _drain(
        _stream_with_stall_watchdog(
            _silent_then_end(silence=0.1), {"pending": True}, poll=0.01, max_wall_clock=0.02
        )
    )
    assert out == []  # ended cleanly, never wall-clock-cut despite the deadline


# ── soft cutoff (the 40%-recovery seam) ──────────────────────────


@pytest.mark.asyncio
async def test_soft_cutoff_fires_before_the_hard_deadline() -> None:
    """The soft cutoff must win the race, or the salvage never gets a chance.

    Measured 2026-08-24: 29 of 134 runs died at the HARD deadline with
    ``final_text`` NULL — 40.2% of all model wall-clock for zero characters.
    Stopping the loop with ~30% of the clock left is what buys the room to
    write an answer from what was already gathered.
    """
    from helixis.agents.orchestrator import _SoftDeadlineReachedError

    with pytest.raises(_SoftDeadlineReachedError) as ei:
        async for _ in _stream_with_stall_watchdog(
            _busy_stream(n=100, gap=0.02),
            {"pending": False},
            poll=0.02,
            max_wall_clock=1.0,
            soft_wall_clock=0.1,
        ):
            pass
    # Keeps the wording every downstream branch keys on.
    assert "wall-clock deadline" in str(ei.value)
    assert "soft cutoff" in str(ei.value)


@pytest.mark.asyncio
async def test_soft_cutoff_subclasses_the_stall_error() -> None:
    """Deliberate: a failed synthesis falls straight through to today's handler.

    The salvage can only ever upgrade a run that was already going to fail —
    it must not be able to turn a normal deadline error into an unhandled
    exception.
    """
    from helixis.agents.orchestrator import _SoftDeadlineReachedError

    assert issubclass(_SoftDeadlineReachedError, _AgentRunStalledError)


@pytest.mark.asyncio
async def test_no_soft_cutoff_keeps_the_hard_deadline_behaviour() -> None:
    """``soft_wall_clock=None`` is byte-for-byte the pre-change path."""
    from helixis.agents.orchestrator import _SoftDeadlineReachedError

    with pytest.raises(_AgentRunStalledError) as ei:
        async for _ in _stream_with_stall_watchdog(
            _busy_stream(n=100, gap=0.02),
            {"pending": False},
            poll=0.02,
            max_wall_clock=0.1,
            soft_wall_clock=None,
        ):
            pass
    assert not isinstance(ei.value, _SoftDeadlineReachedError)


@pytest.mark.asyncio
async def test_soft_cutoff_also_pauses_while_awaiting_approval() -> None:
    """Both clocks read the same ``active`` accumulator, so an approval wait
    cannot be cut short by the soft cutoff either."""
    out = await _drain(
        _stream_with_stall_watchdog(
            _silent_then_end(silence=0.1),
            {"pending": True},
            poll=0.01,
            max_wall_clock=0.05,
            soft_wall_clock=0.01,
        )
    )
    assert out == []


# ── the refusal ceiling is a STOP (battery v7, 2026-08-27) ───────────────
#
# `call_budget` has counted run-wide refusals since #866, and its comment says
# that past `RUN_REFUSAL_CEILING` "the message stops being advice". It did not:
# `_invoke` swapped `_refusal` for the sterner `_LOOPING` string and handed it
# back as another tool result, which the model is free to ignore — and did.
# Battery v7 measured q7 at **26 refusals against a ceiling of 12**, dying at
# 190s having returned nothing, for $0.41.
#
# That was the fifth round of the same argument (#850 docstrings, #860 result
# footer, #863 TRIAGE, #866 refusal escalation). Gotcha 32 had already named
# why refusing harder cannot be the answer: "a refusal is free to the LEDGER
# and expensive on the CLOCK", because each one still costs a model turn. Not
# giving the model another turn is the whole fix.


async def _endless(gap: float = 0.01):
    """A stream that never stops and never goes silent — a fan-out loop."""
    i = 0
    while True:
        await asyncio.sleep(gap)
        yield f"event-{i}"
        i += 1


async def _expect_stop(**kw: object) -> Exception:
    """Drive ``_endless`` and return the exception that stopped it.

    ⚠ BOUNDED, and that is not belt-and-braces. The stream never ends and
    these cases deliberately carry no deadline, so an implementation that
    does NOT stop would run forever — a mutation that HANGS instead of
    failing, which is gotcha 43's exact shape ("a wait for a condition that
    cannot be satisfied"). The first cut of this file did precisely that and
    wedged the suite. A guard has to fail in seconds, or it is not a guard.
    """
    try:
        async with asyncio.timeout(5.0):
            async for _ev in _stream_with_stall_watchdog(
                _endless(), {"pending": False}, poll=0.01, **kw
            ):
                pass
    except _AgentRunStalledError as exc:
        return exc
    except TimeoutError:
        raise AssertionError(
            "the run was NOT stopped — the watchdog let a looping stream continue, "
            "which is the v7 behaviour this exists to prevent"
        ) from None
    raise AssertionError("the stream ended on its own; it should not be able to")


@pytest.mark.asyncio
async def test_a_looping_run_is_stopped_even_with_clock_to_spare() -> None:
    """⚠ THE point. The run is busy, well inside its deadline, and would
    otherwise continue — but it has demonstrably been refused past the
    ceiling, so it is cut off rather than handed another sterner string."""
    exc = await _expect_stop(
        max_wall_clock=1000.0,  # nowhere near it
        soft_wall_clock=700.0,  # nor this
        is_looping=lambda: True,
    )
    assert "refusal ceiling" in str(exc), "the run row must name the real cause"


@pytest.mark.asyncio
async def test_the_stop_routes_into_the_rescue_not_the_error_path() -> None:
    """⚠ LOAD-BEARING WORDING, not prose.

    `is_deadline` downstream is literally `"wall-clock" in str(e)`, and it
    gates the salvage fallback and the `wall_clock_deadline` reason code;
    `_stalled_run_user_message` keys on the full "wall-clock deadline" phrase
    to choose split-it-up advice over retry advice. Drop the phrase and the
    rescue silently disables itself while every other test still passes —
    which is exactly the class of failure v7 spent two runs on.
    """
    exc = await _expect_stop(is_looping=lambda: True)
    msg = str(exc)
    assert isinstance(exc, _SoftDeadlineReachedError), (
        "must raise the SOFT deadline subclass — that is the only exception the "
        "handler runs deadline_synthesis for; the base class returns nothing"
    )
    assert "wall-clock deadline" in msg, (
        "_stalled_run_user_message keys on this exact phrase; without it the "
        "user gets retry advice for a run that must not be retried identically"
    )
    assert "wall-clock" in msg, "is_deadline gates the salvage fallback on this"


@pytest.mark.asyncio
async def test_a_healthy_run_is_untouched() -> None:
    """No false positives. Twelve refusals cannot happen on a healthy run, so
    a run that is not looping must drain exactly as before."""
    out = await _drain(
        _stream_with_stall_watchdog(
            _busy_stream(n=5, gap=0.01),
            {"pending": False},
            poll=0.01,
            is_looping=lambda: False,
        )
    )
    assert out == [f"event-{i}" for i in range(5)]


@pytest.mark.asyncio
async def test_the_check_is_absent_by_default() -> None:
    """``is_looping=None`` (every existing caller, and the tests above) behaves
    byte-for-byte as before."""
    out = await _drain(
        _stream_with_stall_watchdog(_busy_stream(n=5, gap=0.01), {"pending": False}, poll=0.01)
    )
    assert out == [f"event-{i}" for i in range(5)]


@pytest.mark.asyncio
async def test_it_stops_on_the_handoff_path_too() -> None:
    """⚠ Deliberately OUTSIDE the ``max_wall_clock`` branch. The ceiling is a
    COUNTER, not a clock — twelve refused reads is unambiguous evidence of a
    loop — so it is worth stopping on the handoff path, where no deadline
    exists to catch it at all."""
    await _expect_stop(max_wall_clock=None, is_looping=lambda: True)


def test_the_ceiling_the_watchdog_names_is_the_one_that_counts() -> None:
    """⚠ BOTH sides (gotcha 28). The message quotes a number; the ledger
    enforces one. If they drift, the run row tells the reader a figure the
    code never used."""
    import inspect

    from helixis.agents.call_budget import RUN_REFUSAL_CEILING

    src = inspect.getsource(_stream_with_stall_watchdog)
    assert "RUN_REFUSAL_CEILING" in src, (
        "the watchdog hardcoded a ceiling instead of importing the ledger's — "
        "the two would drift and the run row would misreport it"
    )
    assert RUN_REFUSAL_CEILING > 0
