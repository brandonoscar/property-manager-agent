"""#7 (2026-07-13) — the agent-run stall watchdog.

The deployed app intermittently hung a turn right after the orchestrator
handoff: the SDK stream went fully silent (no tokens / tool calls / done /
[DONE]) and the client spun forever. ``_stream_with_stall_watchdog`` turns
that silence into a terminal ``_AgentRunStalledError`` so the run can surface
a recoverable error — WITHOUT killing a legitimately slow turn (any event
resets the clock) or a confirm-gate approval wait (silent by design).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from helixis.agents.orchestrator import (
    _AgentRunStalledError,
    _stream_with_stall_watchdog,
)

pytestmark = pytest.mark.asyncio

# Fast timings so the tests run in milliseconds, not the 120s prod budget.
_STALL = 0.05
_POLL = 0.01


async def _drain(stream: AsyncIterator[Any]) -> list[Any]:
    return [ev async for ev in stream]


async def test_passes_events_through_without_stalling() -> None:
    async def gen() -> AsyncIterator[str]:
        for tok in ("a", "b", "c"):
            yield tok
            await asyncio.sleep(0)  # progress — resets the clock

    wait = {"pending": False}
    got = await _drain(_stream_with_stall_watchdog(gen(), wait, stall_timeout=_STALL, poll=_POLL))
    assert got == ["a", "b", "c"]


async def test_raises_on_total_silence() -> None:
    async def gen() -> AsyncIterator[str]:
        yield "first"
        await asyncio.sleep(1.0)  # go silent well past the stall budget
        yield "never"  # pragma: no cover

    wait = {"pending": False}
    with pytest.raises(_AgentRunStalledError):
        await _drain(_stream_with_stall_watchdog(gen(), wait, stall_timeout=_STALL, poll=_POLL))


async def test_pending_confirm_pauses_the_clock() -> None:
    # A confirm gate is pending (set by the emitter, before the stream goes
    # silent) and the stream stays silent far longer than the stall budget —
    # the approval wait. It must NOT raise; when the user approves, the tool
    # output flows and is yielded.
    async def gen() -> AsyncIterator[str]:
        await asyncio.sleep(_STALL * 4)  # user "thinking" at the gate
        yield "tool_output"

    wait = {"pending": True}
    got = await _drain(_stream_with_stall_watchdog(gen(), wait, stall_timeout=_STALL, poll=_POLL))
    assert got == ["tool_output"]


async def test_forwards_stream_exceptions_verbatim() -> None:
    class BoomError(RuntimeError):
        pass

    async def gen() -> AsyncIterator[str]:
        yield "ok"
        raise BoomError("upstream failed")

    wait = {"pending": False}
    with pytest.raises(BoomError, match="upstream failed"):
        await _drain(_stream_with_stall_watchdog(gen(), wait, stall_timeout=_STALL, poll=_POLL))


async def test_completes_cleanly_on_empty_stream() -> None:
    async def gen() -> AsyncIterator[str]:
        return
        yield  # pragma: no cover — makes this an async generator

    wait = {"pending": False}
    assert (
        await _drain(_stream_with_stall_watchdog(gen(), wait, stall_timeout=_STALL, poll=_POLL))
        == []
    )


class _Ev:
    """Minimal stand-in for the SDK's RunItemStreamEvent."""

    def __init__(self, name: str) -> None:
        self.name = name


async def test_a_running_tool_is_not_a_stalled_stream() -> None:
    """Battery v3 killed two HEALTHY runs this way. The SDK emits nothing
    between ``tool_called`` and ``tool_output``, so a specialist doing 60s of
    real work is indistinguishable from a dead stream on the silence clock —
    Q11 was 196k tokens deep and still generating when the watchdog fired."""

    async def gen():
        yield _Ev("tool_called")
        await asyncio.sleep(_STALL * 1.8)  # past the plain budget, under the tool one
        yield _Ev("tool_output")
        yield "done"

    wait = {"pending": False}
    got = await _drain(_stream_with_stall_watchdog(gen(), wait, stall_timeout=_STALL, poll=_POLL))
    assert got[-1] == "done"


async def test_a_hung_tool_still_dies() -> None:
    """Stretched, not removed. A tool that never returns must not hang the
    turn forever — that was the original bug the watchdog exists for."""

    async def gen():
        yield _Ev("tool_called")
        await asyncio.sleep(5.0)  # pragma: no cover
        yield _Ev("tool_output")  # pragma: no cover

    wait = {"pending": False}
    with pytest.raises(_AgentRunStalledError) as exc:
        await _drain(_stream_with_stall_watchdog(gen(), wait, stall_timeout=_STALL, poll=_POLL))
    assert "tool ran" in str(exc.value)


async def test_silence_after_the_tool_returns_is_still_a_stall() -> None:
    """The allowance is scoped to the in-flight window, not to the rest of
    the run — otherwise one tool call would disarm the watchdog for good."""

    async def gen():
        yield _Ev("tool_called")
        yield _Ev("tool_output")
        await asyncio.sleep(5.0)  # pragma: no cover
        yield "never"  # pragma: no cover

    wait = {"pending": False}
    with pytest.raises(_AgentRunStalledError) as exc:
        await _drain(_stream_with_stall_watchdog(gen(), wait, stall_timeout=_STALL, poll=_POLL))
    assert "no stream event" in str(exc.value)
