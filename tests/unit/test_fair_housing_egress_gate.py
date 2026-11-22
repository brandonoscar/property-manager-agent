"""#49 — the fair-housing gate wired into the communication egress points.

The pure gate (``check_draft``) is tested in test_fair_housing_gate.py; here we
pin that it actually BLOCKS an outbound send in the two comms handlers, and
that a clean message still goes through.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from helixis.agent.handlers.base import HandlerError, guard_fair_housing
from helixis.agent.handlers.reply_to_email import ReplyToEmailHandler
from helixis.agent.handlers.send_tenant_message import SendTenantMessageHandler

# Guaranteed-flagged / guaranteed-clean per test_fair_housing_gate.py.
_STEERING = "It's a predominantly white neighborhood, very quiet."
_CLEAN = "Hi Sarah — your maintenance request is scheduled for Tuesday 9am. Thanks!"


# ── the shared guard ────────────────────────────────────────────────


def test_guard_raises_on_steering() -> None:
    with pytest.raises(HandlerError) as exc:
        guard_fair_housing(_STEERING, tool_id="send_tenant_message")
    assert exc.value.status_code == 422
    assert "fair-housing" in str(exc.value).lower()


def test_guard_passes_clean_text() -> None:
    # No raise = pass.
    guard_fair_housing(_CLEAN, tool_id="send_tenant_message")


# ── send_tenant_message egress ──────────────────────────────────────


@pytest.mark.asyncio
async def test_send_tenant_message_blocks_steering_before_buildium() -> None:
    client = MagicMock()
    client.request = AsyncMock()
    h = SendTenantMessageHandler()
    with pytest.raises(HandlerError) as exc:
        await h.execute(
            client,
            {"tenant_id": 42, "subject": "Your area", "body": _STEERING},
        )
    assert exc.value.status_code == 422
    # The message never reached Buildium.
    client.request.assert_not_awaited()


# ── reply_to_email egress ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_reply_to_email_blocks_steering_before_gmail() -> None:
    composio = MagicMock()
    composio.available = True
    composio.execute_tool = MagicMock()
    h = ReplyToEmailHandler()
    ctx = {
        "composio": composio,
        "composio_user_id": "u1",
        "email_thread_id": "t1",
        "email_to": "sarah@example.com",
    }
    with pytest.raises(HandlerError) as exc:
        await h.execute(None, {"body": _STEERING}, context=ctx)
    assert exc.value.status_code == 422
    # The reply never went out via Gmail.
    composio.execute_tool.assert_not_called()


# ── the deflection half now fires (the wiring-bug fix) ──────────────
# Before: guard_fair_housing was called with no `prompt`, so
# protected_class_question("") was always None and Part 2 (the
# protected-class-question -> required-deflection check) was silently
# dead on every live send. The handlers now thread the inbound request
# via context["source_request"], so a non-deflecting reply to a
# protected-class screening question is blocked; a deflecting one passes.

# Proven to classify as a protected-class question in test_fair_housing_gate.py.
_PCQ = "Am I allowed to deny someone because of their disability?"
_NON_DEFLECTING = "Sure, whatever works best for you."
_DEFLECTING = (
    "You should apply the same criteria to every applicant regardless of their "
    "background — you can't ask about that."
)


@pytest.mark.asyncio
async def test_send_tenant_message_flags_protected_class_without_deflection() -> None:
    client = MagicMock()
    client.request = AsyncMock()
    h = SendTenantMessageHandler()
    with pytest.raises(HandlerError) as exc:
        await h.execute(
            client,
            {"tenant_id": 7, "subject": "Re: your question", "body": _NON_DEFLECTING},
            context={"source_request": _PCQ},
        )
    assert exc.value.status_code == 422
    assert "missing_deflection" in str(exc.value)
    client.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_tenant_message_allows_protected_class_with_deflection() -> None:
    client = MagicMock()
    client.request = AsyncMock(return_value={"Id": 99})
    h = SendTenantMessageHandler()
    result = await h.execute(
        client,
        {"tenant_id": 7, "subject": "Re: your question", "body": _DEFLECTING},
        context={"source_request": _PCQ},
    )
    assert result["buildium_id"] == 99
    client.request.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_tenant_message_no_source_request_does_not_overblock() -> None:
    # Without a source_request the deflection half is correctly dormant, so a
    # non-steering, non-deflecting reply still passes (no false positive).
    client = MagicMock()
    client.request = AsyncMock(return_value={"Id": 1})
    h = SendTenantMessageHandler()
    result = await h.execute(
        client,
        {"tenant_id": 7, "subject": "Hi", "body": _NON_DEFLECTING},
    )
    assert result["buildium_id"] == 1
    client.request.assert_awaited_once()


@pytest.mark.asyncio
async def test_reply_to_email_flags_protected_class_without_deflection() -> None:
    composio = MagicMock()
    composio.available = True
    composio.execute_tool = MagicMock()
    h = ReplyToEmailHandler()
    ctx = {
        "composio": composio,
        "composio_user_id": "u1",
        "email_thread_id": "t1",
        "email_to": "prospect@example.com",
        "source_request": _PCQ,
    }
    with pytest.raises(HandlerError) as exc:
        await h.execute(None, {"body": _NON_DEFLECTING}, context=ctx)
    assert exc.value.status_code == 422
    assert "missing_deflection" in str(exc.value)
    composio.execute_tool.assert_not_called()


@pytest.mark.asyncio
async def test_reply_to_email_allows_protected_class_with_deflection() -> None:
    composio = MagicMock()
    composio.available = True
    composio.execute_tool = MagicMock(return_value={"ok": True})
    h = ReplyToEmailHandler()
    ctx = {
        "composio": composio,
        "composio_user_id": "u1",
        "email_thread_id": "t1",
        "email_to": "prospect@example.com",
        "source_request": _PCQ,
    }
    result = await h.execute(None, {"body": _DEFLECTING}, context=ctx)
    assert result["thread_id"] == "t1"
    composio.execute_tool.assert_called_once()
