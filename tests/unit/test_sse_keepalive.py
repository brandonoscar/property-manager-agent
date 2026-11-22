"""The keepalive was documented, never written to the wire, and never tested.

WHAT HAPPENED
-------------
``sse_event_generator``'s docstring says "Sends keepalive comments every 15
seconds to prevent proxy timeouts." It did not send any, ever. The old shape
built a nested ``keepalive()`` async generator and drove it with
``asyncio.create_task(gen.__anext__())`` — so the yielded string landed in the
TASK'S RESULT and was discarded. The function's only ``yield``s were the event
and the terminator.

Measured against the real generator at a 0.2s interval with 3s of silence
(15 intervals): one event chunk, **zero** keepalive chunks.

The restart was dead twice over: ``keepalive_task.cancel()`` threw
``CancelledError`` into the generator at its ``sleep``, ending it, so every
later ``__anext__()`` raised ``StopAsyncIteration`` immediately.

⚠ WHY IT MATTERED
------------------
The stream emits nothing during a publish-free window, and a specialist consult
is one uninterrupted silent window (gotcha 37 measured 195 of 232 tool calls as
nested ``buildium_agent`` runs). A confirm gate is worse — the wait is up to
300s. Battery v10-100 q63 (172.5s) and q96 (392s wall against 81.5s
watchdog-active, i.e. a ~310s gate wait) both came back BLANK at the client
while ``orchestrator_runs`` held the finished answer — 331 and 1075 characters.

⚠ AND WHY NOTHING CAUGHT IT
----------------------------
``grep -rn keepalive`` over ``src/``, ``tests/`` and ``frontend/`` hit only
``sse.py`` itself. A docstring cannot fail (gotcha 48), so the claim stood for
as long as nobody measured it. That is the whole reason this file exists: the
assertion has to be that a byte reached the wire, not that the code looks
right.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from helixis.common.events import thinking_event
from helixis.streaming import sse
from helixis.streaming.bus import EventBus


async def _drain(bus: EventBus, session: str, *, silence: float, interval: float) -> list[str]:
    """Run the real generator across a silent window and return every chunk."""
    original = sse.KEEPALIVE_INTERVAL
    sse.KEEPALIVE_INTERVAL = interval
    chunks: list[str] = []
    try:
        gen = sse.sse_event_generator(bus, session)

        async def pump() -> None:
            async for chunk in gen:
                chunks.append(chunk)

        task = asyncio.create_task(pump())
        await asyncio.sleep(interval / 4)
        await bus.publish(session, thinking_event("first"))
        await asyncio.sleep(silence)
        await bus.publish(session, thinking_event("second"))
        await asyncio.sleep(interval / 2)
        await bus.close_session(session)
        await asyncio.wait_for(task, timeout=5)
    finally:
        sse.KEEPALIVE_INTERVAL = original
    return chunks


@pytest.mark.asyncio
async def test_a_silent_window_still_writes_to_the_wire() -> None:
    """⚠ THE bug. Asserts BYTES, not structure — the old code was structurally
    plausible and emitted nothing."""
    chunks = await _drain(EventBus(), "s-keepalive", silence=1.2, interval=0.2)
    keepalives = [c for c in chunks if c.startswith(": keepalive")]
    assert len(keepalives) >= 4, (
        f"1.2s of silence at a 0.2s interval produced {len(keepalives)} keepalive(s). "
        f"Zero means the generator writes nothing while the agent is thinking, and a "
        f"proxy is free to close the connection mid-turn — which is how a finished "
        f"answer reaches nobody."
    )


@pytest.mark.asyncio
async def test_the_keepalive_repeats_rather_than_firing_once() -> None:
    """⚠ The bug MY OWN first fix had, caught by running the probe rather than
    reading the diff.

    The first draft rebuilt the pending future inside the loop and awaited it
    after a timeout, so a silent window produced exactly ONE keepalive and then
    blocked — 1 instead of 5. A keepalive that fires once is not a keepalive;
    the repeat is the entire mechanism. A test asserting merely ``>= 1`` would
    have passed against it.
    """
    chunks = await _drain(EventBus(), "s-repeat", silence=1.0, interval=0.2)
    keepalives = [c for c in chunks if c.startswith(": keepalive")]
    assert len(keepalives) >= 3, (
        f"got {len(keepalives)} — a single keepalive followed by silence is the "
        f"first-draft bug, and it looks identical to a working one at >= 1"
    )


@pytest.mark.asyncio
async def test_events_still_arrive_in_order_around_the_keepalives() -> None:
    """⚠ The canary. Every assertion above is about ABSENCE of silence, and a
    generator that yielded keepalives and dropped events would satisfy them
    all. Cancelling the pending future on each timeout is the obvious way to
    write this loop and it drops whichever event arrives next."""
    chunks = await _drain(EventBus(), "s-order", silence=0.9, interval=0.2)
    events = [c for c in chunks if c.startswith("id: ")]
    assert len(events) == 2, f"both events must survive the keepalive loop: {chunks!r}"
    assert chunks[-1] == "data: [DONE]\n\n", "the terminator still closes the stream"


@pytest.mark.asyncio
async def test_a_client_disconnect_does_not_leak_the_subscriber() -> None:
    """⚠ gotcha 31, re-introduced at this call site — and the FIRST version of
    this test could not see it.

    ``async for`` does not close a delegated async generator, so the inner
    ``subscribe_seq`` stays suspended, its ``finally`` never pops the
    subscriber, and ``publish`` goes on filling a queue nobody reads. One leak
    per closed stream, on the path every chat turn uses.

    The mistake worth keeping: the first draft closed the SESSION and then
    asserted the count was zero. That passes with or without ``aclose`` —
    ``close_session`` ends the inner generator on its own, so the cleanup is a
    no-op on that path. Verified by mutation: deleting ``aclose`` left all four
    tests green.

    The leak only exists when the OUTER generator is abandoned while the inner
    one is still suspended — i.e. a client disconnect mid-turn, which gotcha 31
    records as routine now that a reattach closes the old stream by design. So
    this test disconnects instead of finishing.
    """
    bus = EventBus()
    session = "s-disconnect"
    original = sse.KEEPALIVE_INTERVAL
    sse.KEEPALIVE_INTERVAL = 0.2
    try:
        gen = sse.sse_event_generator(bus, session)
        await bus.publish(session, thinking_event("first"))
        # Consume one event, then walk away with the stream still open.
        first = await gen.__anext__()
        assert first.startswith("id: ")
        assert bus.subscriber_count(session) == 1, "the subscriber should be live here"
        await gen.aclose()
    finally:
        sse.KEEPALIVE_INTERVAL = original

    assert bus.subscriber_count(session) == 0, (
        "the subscriber survived a client disconnect. publish() will fill its queue "
        "for the life of the process, one leak per dropped connection — and a reattach "
        "drops a connection every time it happens."
    )


@pytest.mark.asyncio
async def test_a_stream_cannot_outlive_its_run() -> None:
    """⚠ THE REGRESSION THE KEEPALIVE INTRODUCED.

    Before keepalives, a stream that stopped delivering died at the client's
    read timeout. After them, every 15s comment resets that timer — so a
    stream delivering NOTHING can hold a connection open forever. Battery v11
    q96 measured it: the agent finished in 176.1s with a 758-character answer,
    and the client sat on the socket for **2,948 seconds** receiving
    keepalives and no events, then died on ReadTimeout with nothing captured.
    It ate 49 minutes of a 60-minute token and took three unrun questions with
    it, including the release-blocking fair-housing one.

    ⚠ This is CONTAINMENT. Why that stream delivered nothing is still
    unexplained — the ``done`` event carrying 776 characters was published and
    never arrived. The cap does not fix that. It makes the failure cost a
    connection instead of an hour, and end with a terminator so the client can
    tell a timed-out stream from a severed one.
    """
    original_cap = sse.MAX_STREAM_SECONDS
    original_interval = sse.KEEPALIVE_INTERVAL
    sse.MAX_STREAM_SECONDS = 0.6
    sse.KEEPALIVE_INTERVAL = 0.1
    try:
        bus = EventBus()
        chunks: list[str] = []
        # A session that never publishes and never closes — exactly the shape
        # that hung. Without the cap this generator runs until the heat death
        # of the process, so the timeout below IS the assertion.
        gen = sse.sse_event_generator(bus, "s-runaway")

        async def pump() -> None:
            async for chunk in gen:
                chunks.append(chunk)

        await asyncio.wait_for(asyncio.create_task(pump()), timeout=10)
    finally:
        sse.MAX_STREAM_SECONDS = original_cap
        sse.KEEPALIVE_INTERVAL = original_interval

    assert any(c.startswith(": keepalive") for c in chunks), "it should have kept alive first"
    assert chunks[-1] == "data: [DONE]\n\n", (
        "a capped stream must still close with the terminator — otherwise the client "
        "cannot distinguish 'we gave up' from 'the pipe was cut', which is the exact "
        "ambiguity that made q96 unreadable for two days"
    )


@pytest.mark.asyncio
async def test_a_disconnect_during_a_keepalive_wait_closes_cleanly() -> None:
    """⚠ THE BUG THE CAP TEST UNCOVERED, and the likelier q96 root cause.

    ``aclose()`` raises ``RuntimeError: asynchronous generator is already
    running`` when an ``__anext__`` is in flight — and the keepalive branch
    leaves one pending on EVERY iteration. So any teardown while waiting
    (client disconnect, the stream cap, cancellation) raised out of the
    ``finally``, killed the generator abnormally, and skipped the ``[DONE]``
    terminator entirely.

    That is a client left holding a socket with no events and no close, which
    is exactly what q96 observed. Nothing exercised a teardown from inside the
    keepalive branch until the cap test did — the earlier disconnect test
    consumes an EVENT first, which leaves no pending read.
    """
    original = sse.KEEPALIVE_INTERVAL
    sse.KEEPALIVE_INTERVAL = 0.05
    try:
        bus = EventBus()
        gen = sse.sse_event_generator(bus, "s-mid-wait")
        # Start it and let it settle INTO the keepalive wait — no event is
        # ever published, so the pending __anext__ is what we tear down on.
        pump = asyncio.create_task(gen.asend(None))
        await asyncio.sleep(0.2)
        pump.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pump
        # The assertion: this must not raise RuntimeError.
        await gen.aclose()
    finally:
        sse.KEEPALIVE_INTERVAL = original

    assert bus.subscriber_count("s-mid-wait") == 0, "and the subscriber must still be released"
