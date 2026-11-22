"""Resumable SSE — a dropped connection shouldn't cost a 240-second answer.

The server half was already built: the bus keeps a bounded replay log and
``subscribe`` replays it before going live, so a reconnecting client has always
received the whole turn. What was missing is the CURSOR — without one a
reattach replays from event zero and the user sees the turn render twice,
which is why "just reconnect" was never good enough.

Our turns run to the 240s deadline, so a connection blip is a routine event
rather than an edge case. That is what makes this reliability rather than
polish.
"""

from __future__ import annotations

import asyncio

from helixis.common.events import narration_event
from helixis.streaming.bus import EventBus
from helixis.streaming.sse import sse_event_generator


def _run(coro):
    return asyncio.run(coro)


def test_sequences_start_at_one_and_increment() -> None:
    async def go() -> list[int]:
        bus = EventBus()
        bus.create_session("s")
        for i in range(3):
            await bus.publish("s", narration_event(f"e{i}"))
        seqs = []
        gen = bus.subscribe_seq("s")
        try:
            for _ in range(3):
                seq, _event = await gen.__anext__()
                seqs.append(seq)
        finally:
            await gen.aclose()
        return seqs

    assert _run(go()) == [1, 2, 3]


def test_after_resumes_instead_of_replaying() -> None:
    """The whole point. A client that rendered through event 2 gets 3 onward,
    not the turn from the beginning."""

    async def go() -> list[str]:
        bus = EventBus()
        bus.create_session("s")
        for i in range(4):
            await bus.publish("s", narration_event(f"e{i}"))
        seen = []
        gen = bus.subscribe_seq("s", after=2)
        try:
            for _ in range(2):
                _seq, event = await gen.__anext__()
                seen.append(event.data["text"] if "text" in event.data else event.data)
        finally:
            await gen.aclose()
        return seen

    out = _run(go())
    assert len(out) == 2  # e2 and e3 — not e0/e1 again


def test_a_nonsense_cursor_still_delivers_live_events() -> None:
    """A cursor AHEAD of the session (a stale client, a copied url) must not
    swallow the live stream. My first cut filtered live events against
    ``after`` too, so such a client hung forever with no output and no error —
    strictly worse than the problem resume exists to solve."""

    async def go() -> bool:
        bus = EventBus()
        bus.create_session("s")
        await bus.publish("s", narration_event("only"))
        gen = bus.subscribe_seq("s", after=99)
        task = asyncio.create_task(gen.__anext__())
        for _ in range(10):
            if bus.subscriber_count("s"):
                break
            await asyncio.sleep(0)
        await bus.publish("s", narration_event("live"))
        seq, event = await task
        await gen.aclose()
        # The logged event (seq 1) was correctly skipped by the cursor; the
        # live one (seq 2) arrives regardless of the bogus cursor.
        return seq == 2 and event.data.get("content") == "live"

    assert _run(go())


def test_the_sequence_survives_a_dropped_event() -> None:
    """A subscriber counting locally would desync the moment backpressure shed
    one of its events, and a wrong cursor silently skips or duplicates. The
    sequence therefore travels WITH the event."""

    async def go() -> list[int]:
        bus = EventBus(max_queue_size=2)
        bus.create_session("s")
        gen = bus.subscribe_seq("s")
        task = asyncio.create_task(gen.__anext__())
        for _ in range(10):
            if bus.subscriber_count("s"):
                break
            await asyncio.sleep(0)

        # Overflow the subscriber's queue: 6 events into a maxsize of 2, so
        # ``_put_dropping_oldest`` sheds several. A locally-counted cursor
        # would now be behind by exactly the number dropped.
        for i in range(6):
            await bus.publish("s", narration_event(f"e{i}"))

        # Only ``max_queue_size`` events survive the shedding, so read exactly
        # that many — asking for more blocks forever on a queue that is
        # legitimately empty.
        seqs = [(await task)[0]]
        seqs.append((await gen.__anext__())[0])
        await gen.aclose()
        return seqs

    seqs = _run(go())
    # The surviving numbers are the TRUE publish order. A locally-counted
    # cursor would have restarted at 1 and 2 here, quietly mislabelling the
    # last two events as the first two — and a client resuming from that
    # cursor would then re-receive most of the turn.
    assert seqs == [5, 6]


def test_closing_the_plain_subscribe_unregisters_the_subscriber() -> None:
    """``async for`` does NOT propagate GeneratorExit into a delegated async
    generator. Without an explicit ``aclose`` the inner one stays suspended,
    its ``finally`` never runs, and ``publish`` keeps filling a queue nobody
    reads — a leak per closed stream."""

    async def go() -> int:
        bus = EventBus()
        bus.create_session("s")
        gen = bus.subscribe("s")
        task = asyncio.create_task(gen.__anext__())
        for _ in range(10):
            if bus.subscriber_count("s"):
                break
            await asyncio.sleep(0)
        await bus.publish("s", narration_event("x"))
        await task
        await gen.aclose()
        return bus.subscriber_count("s")

    assert _run(go()) == 0


def test_the_wire_carries_an_id_line_before_each_event() -> None:
    """The cursor has to reach the client to be usable, and the SSE ``id``
    field is the standard place for it."""

    async def go() -> str:
        bus = EventBus()
        bus.create_session("s")
        await bus.publish("s", narration_event("hello"))
        gen = sse_event_generator(bus, "s")
        try:
            chunk = await gen.__anext__()
        finally:
            await gen.aclose()
        return chunk

    chunk = _run(go())
    assert chunk.startswith("id: 1\ndata: ")
    assert chunk.endswith("\n\n")
