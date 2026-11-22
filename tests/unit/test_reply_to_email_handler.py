"""Tests for the reply_to_email handler (Composio Gmail send)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from helixis.agent.handlers import HANDLERS, get_handler
from helixis.agent.handlers.base import HandlerError
from helixis.agent.handlers.reply_to_email import ReplyToEmailHandler
from helixis.agent.planner.tool_catalog import TOOL_IDS


def test_handler_registered_and_keys_match_catalog() -> None:
    # The integration invariant: every catalog tool has a handler.
    assert set(HANDLERS.keys()) == set(TOOL_IDS)
    assert isinstance(get_handler("reply_to_email"), ReplyToEmailHandler)


def _ctx(composio, **over):
    base = {
        "composio": composio,
        "composio_user_id": "u1",
        "email_thread_id": "t1",
        "email_to": "sarah@example.com",
    }
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_reply_sends_via_composio() -> None:
    composio = MagicMock()
    composio.available = True
    composio.execute_tool.return_value = {"ok": True}
    h = ReplyToEmailHandler()
    result = await h.execute(None, {"body": "Hi Sarah, sounds good."}, context=_ctx(composio))
    assert result["buildium_id"] is None
    assert "sarah@example.com" in result["summary"]
    _, kwargs = composio.execute_tool.call_args
    assert kwargs["user_id"] == "u1"
    assert kwargs["arguments"]["thread_id"] == "t1"
    assert kwargs["arguments"]["message_body"] == "Hi Sarah, sounds good."


@pytest.mark.asyncio
async def test_reply_requires_body() -> None:
    composio = MagicMock()
    composio.available = True
    h = ReplyToEmailHandler()
    with pytest.raises(HandlerError) as exc:
        await h.execute(None, {"body": "  "}, context=_ctx(composio))
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_reply_errors_when_gmail_unavailable() -> None:
    composio = MagicMock()
    composio.available = False
    h = ReplyToEmailHandler()
    with pytest.raises(HandlerError) as exc:
        await h.execute(None, {"body": "hi"}, context=_ctx(composio))
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_reply_errors_without_thread() -> None:
    composio = MagicMock()
    composio.available = True
    h = ReplyToEmailHandler()
    with pytest.raises(HandlerError) as exc:
        await h.execute(None, {"body": "hi"}, context=_ctx(composio, email_thread_id=None))
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_reply_surfaces_send_failure() -> None:
    composio = MagicMock()
    composio.available = True
    composio.execute_tool.return_value = None  # composio degraded / failed
    h = ReplyToEmailHandler()
    with pytest.raises(HandlerError) as exc:
        await h.execute(None, {"body": "hi"}, context=_ctx(composio))
    assert exc.value.status_code == 502
