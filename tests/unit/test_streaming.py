"""Tests for the event bus and SSE streaming."""

import asyncio

import pytest

from helixis.common.events import done_event, token_event, tool_start_event
from helixis.streaming.bus import EventBus


@pytest.mark.asyncio
async def test_publish_subscribe():
    bus = EventBus()
    bus.create_session("s1")

    event = token_event("hello")
    await bus.publish("s1", event)

    received = []
    async for e in bus.subscribe("s1"):
        received.append(e)
        if len(received) >= 1:
            await bus.close_session("s1")

    assert len(received) == 1
    assert received[0].type.value == "token"
    assert received[0].data["content"] == "hello"


@pytest.mark.asyncio
async def test_multiple_events():
    bus = EventBus()
    bus.create_session("s2")

    await bus.publish("s2", token_event("a"))
    await bus.publish("s2", token_event("b"))
    await bus.publish("s2", tool_start_event("browse", "Browsing..."))
    await bus.publish("s2", done_event("All done"))

    received = []
    async for e in bus.subscribe("s2"):
        received.append(e)
        if e.type.value == "done":
            await bus.close_session("s2")

    assert len(received) == 4
    assert received[0].data["content"] == "a"
    assert received[1].data["content"] == "b"
    assert received[2].type.value == "tool_start"
    assert received[3].type.value == "done"


@pytest.mark.asyncio
async def test_close_session():
    bus = EventBus()
    bus.create_session("s3")

    assert bus.has_session("s3")
    await bus.close_session("s3")
    await asyncio.sleep(0.2)
    assert not bus.has_session("s3")


@pytest.mark.asyncio
async def test_publish_to_unknown_session():
    bus = EventBus()
    # Should create the session automatically
    await bus.publish("unknown", token_event("test"))
    assert bus.has_session("unknown")
    await bus.close_session("unknown")
