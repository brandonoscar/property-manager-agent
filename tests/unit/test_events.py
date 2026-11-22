"""Tests for event schema and factory functions."""

import json

from helixis.common.events import (
    EventType,
    HelixisEvent,
    confirm_event,
    done_event,
    error_event,
    thinking_event,
    token_event,
    tool_result_event,
    tool_start_event,
)


def test_token_event():
    event = token_event("hello")
    assert event.type == EventType.TOKEN
    assert event.data["content"] == "hello"
    assert event.timestamp > 0


def test_tool_start_event():
    event = tool_start_event("browse_website", "Navigating to Buildium...")
    assert event.type == EventType.TOOL_START
    assert event.data["tool"] == "browse_website"
    assert event.data["description"] == "Navigating to Buildium..."


def test_tool_result_event():
    event = tool_result_event("browse_website", "Found 3 overdue tenants", 8200)
    assert event.data["duration_ms"] == 8200


def test_confirm_event():
    event = confirm_event(
        confirm_id="abc-123",
        action="send_email",
        title="Send 3 collection emails?",
        details="Draft collection emails for overdue rent",
        items=["sarah@email.com", "mike@email.com"],
    )
    assert event.type == EventType.CONFIRM
    assert event.data["confirm_id"] == "abc-123"
    assert len(event.data["items"]) == 2


def test_done_event():
    event = done_event("All tasks completed successfully")
    assert event.type == EventType.DONE
    assert event.data["summary"] == "All tasks completed successfully"


def test_error_event():
    event = error_event("Connection timeout", recoverable=True)
    assert event.data["recoverable"] is True


def test_event_sse_serialization():
    event = thinking_event("Planning next steps...")
    sse = event.to_sse()
    assert sse.startswith("data: ")
    assert sse.endswith("\n\n")

    # Parse the JSON
    json_str = sse.replace("data: ", "").strip()
    parsed = json.loads(json_str)
    assert parsed["type"] == "thinking"
    assert parsed["data"]["content"] == "Planning next steps..."


def test_event_model_dump():
    event = HelixisEvent(
        type=EventType.TOKEN,
        data={"content": "test"},
    )
    dumped = event.model_dump()
    assert dumped["type"] == "token"
    assert dumped["data"]["content"] == "test"
    assert "timestamp" in dumped
