"""EventBus fan-out semantics (live-watch PR).

The bus went from one single-consumer queue per session to per-subscriber
queues + a bounded replay log. These pin the multiplayer contract:
every subscriber sees every event exactly once, late joiners catch up from
the log, a slow watcher's backpressure never touches the driver, and
close_session terminates every subscriber.
"""

from __future__ import annotations

import asyncio

from helixis.common.events import narration_event
from helixis.streaming.bus import EventBus


def _events(n: int, prefix: str = "e") -> list:
    return [narration_event(f"{prefix}{i}") for i in range(n)]


async def _collect(bus: EventBus, session: str, count: int) -> list[str]:
    out: list[str] = []
    async for ev in bus.subscribe(session):
        out.append(ev.data["content"])
        if len(out) >= count:
            break
    return out


def test_two_subscribers_both_receive_every_event() -> None:
    async def run() -> tuple[list[str], list[str]]:
        bus = EventBus()
        bus.create_session("s")
        t1 = asyncio.create_task(_collect(bus, "s", 3))
        t2 = asyncio.create_task(_collect(bus, "s", 3))
        await asyncio.sleep(0)  # let both subscribe
        for ev in _events(3):
            await bus.publish("s", ev)
        return await t1, await t2

    a, b = asyncio.run(run())
    assert a == ["e0", "e1", "e2"]
    assert b == ["e0", "e1", "e2"]  # pre-fan-out, b would have stolen from a


def test_late_subscriber_catches_up_from_the_replay_log() -> None:
    async def run() -> list[str]:
        bus = EventBus()
        bus.create_session("s")
        # Published before ANY subscriber exists — the driver's SSE response
        # attaching a beat after the run starts, or a watcher joining mid-run.
        for ev in _events(2, "early"):
            await bus.publish("s", ev)
        collector = asyncio.create_task(_collect(bus, "s", 3))
        await asyncio.sleep(0)
        await bus.publish("s", narration_event("live0"))
        return await collector

    assert asyncio.run(run()) == ["early0", "early1", "live0"]


def test_no_duplication_across_snapshot_and_live_boundary() -> None:
    async def run() -> list[str]:
        bus = EventBus()
        bus.create_session("s")
        await bus.publish("s", narration_event("a"))
        collected = asyncio.create_task(_collect(bus, "s", 3))
        await asyncio.sleep(0)
        await bus.publish("s", narration_event("b"))
        await bus.publish("s", narration_event("c"))
        return await collected

    # 'a' arrives once (snapshot), 'b'/'c' once (live queue) — never both.
    assert asyncio.run(run()) == ["a", "b", "c"]


def test_slow_watcher_backpressure_never_touches_the_driver() -> None:
    async def run() -> tuple[list[str], list[str]]:
        bus = EventBus(max_queue_size=2)
        bus.create_session("s")
        driver = asyncio.create_task(_collect(bus, "s", 4))
        await asyncio.sleep(0)

        # The watcher subscribes but doesn't consume — its queue (cap 2)
        # overflows and drops ITS oldest; the driver keeps consuming.
        watcher_gen = bus.subscribe("s")
        watcher_task = asyncio.create_task(watcher_gen.__anext__())
        await asyncio.sleep(0)

        for ev in _events(4):
            await bus.publish("s", ev)
            await asyncio.sleep(0)  # let the driver drain between publishes

        driver_seen = await driver
        # The watcher wakes up now: its first buffered event survived the
        # drops or not, but the DRIVER saw everything.
        first = await watcher_task
        await watcher_gen.aclose()
        return driver_seen, [first.data["content"]]

    driver_seen, _ = asyncio.run(run())
    assert driver_seen == ["e0", "e1", "e2", "e3"]


def test_close_session_terminates_every_subscriber() -> None:
    async def run() -> tuple[list[str], list[str]]:
        bus = EventBus()
        bus.create_session("s")

        async def drain() -> list[str]:
            out = []
            async for ev in bus.subscribe("s"):
                out.append(ev.data["content"])
            return out

        t1 = asyncio.create_task(drain())
        t2 = asyncio.create_task(drain())
        await asyncio.sleep(0)
        await bus.publish("s", narration_event("x"))
        await bus.close_session("s")
        return await t1, await t2

    a, b = asyncio.run(run())
    assert a == ["x"]
    assert b == ["x"]


def test_subscriber_count() -> None:
    async def run() -> tuple[int, int, int]:
        bus = EventBus()
        bus.create_session("s")
        before = bus.subscriber_count("s")
        gen = bus.subscribe("s")
        task = asyncio.create_task(gen.__anext__())
        # Yield until the queue is actually registered rather than assuming a
        # fixed number of scheduler ticks. ``subscribe`` delegates to
        # ``subscribe_seq``, so registration is one generator hop further in
        # than it used to be — and a test that encodes the exact tick count
        # breaks on a refactor that changed nothing observable. Registration
        # is still atomic with the log snapshot, which is the property that
        # actually matters (see the bus module docstring).
        for _ in range(10):
            if bus.subscriber_count("s"):
                break
            await asyncio.sleep(0)
        during = bus.subscriber_count("s")
        await bus.publish("s", narration_event("x"))
        await task
        await gen.aclose()
        after = bus.subscriber_count("s")
        return before, during, after

    before, during, after = asyncio.run(run())
    assert (before, during, after) == (0, 1, 0)
    assert EventBus().subscriber_count("missing") == 0
