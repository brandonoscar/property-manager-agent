"""Add-note — confirm + idempotency (append-only, no read-back verify).

``buildium_add_task_note`` / ``buildium_add_work_order_note`` post a staff note
through the write safety stack. A note is append-only, so it runs the confirm
gate + idempotency claim but skips verify. Harness mirrors the other write tests.
"""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helixis.agents import buildium_tools as bt
from helixis.agents import idempotency as idem
from helixis.agents import tools as agent_tools
from helixis.memory.context import memory_context


def _api() -> tuple[Any, Any, AsyncMock, AsyncMock]:
    task_note = AsyncMock(return_value={"Id": 1})
    wo_note = AsyncMock(return_value={"Id": 2})
    api_mock = MagicMock()
    api_mock.tasks = MagicMock()
    api_mock.tasks.add_note = task_note
    # The card derives display text from the RECORD (_gate_label), so the
    # getters must be mocked or every label falls back to the id form.
    api_mock.tasks.get = AsyncMock(return_value=MagicMock(title="Fix sink"))
    api_mock.work_orders = MagicMock()
    api_mock.work_orders.add_note = wo_note

    client_mock = MagicMock()
    client_mock.__aenter__ = AsyncMock(return_value=client_mock)
    client_mock.__aexit__ = AsyncMock(return_value=False)
    return client_mock, api_mock, task_note, wo_note


def _enter(stack: ExitStack, client_mock: Any, api_mock: Any) -> None:
    stack.enter_context(memory_context(company_id="company-1", user_id="u1", session_id="s1"))
    stack.enter_context(
        patch.object(bt, "load_buildium_settings_for_company", new=AsyncMock(return_value=object()))
    )
    stack.enter_context(patch.object(bt, "BuildiumClient", return_value=client_mock))
    stack.enter_context(patch.object(bt, "BuildiumApi", return_value=api_mock))


async def _next_payload() -> dict:
    for _ in range(50):
        await asyncio.sleep(0)
        drained = await agent_tools.drain_pending_confirmations()
        if drained:
            return drained[0]
    raise AssertionError("no confirm payload emitted")


@pytest.mark.asyncio
async def test_approve_adds_task_note_once() -> None:
    await agent_tools.drain_pending_confirmations()
    client_mock, api_mock, task_note, _ = _api()
    with ExitStack() as stack:
        _enter(stack, client_mock, api_mock)
        task = asyncio.create_task(
            bt._add_note_impl(
                kind="task", target_id=555, note="Tenant home after 3pm", label="Fix sink"
            )
        )
        payload = await _next_payload()
        task_note.assert_not_called()
        await agent_tools.resolve_confirmation(payload["confirm_id"], True)
        result = await asyncio.wait_for(task, timeout=1.0)
    task_note.assert_awaited_once()
    assert task_note.await_args.kwargs["note"] == "Tenant home after 3pm"
    assert "Added your note" in result
    # confirm card shows the title + note, never the id
    items = " | ".join(payload["items"])
    assert "Fix sink" in items and "Tenant home after 3pm" in items and "555" not in items


@pytest.mark.asyncio
async def test_approve_adds_work_order_note_once() -> None:
    await agent_tools.drain_pending_confirmations()
    client_mock, api_mock, _task_note, wo_note = _api()
    with ExitStack() as stack:
        _enter(stack, client_mock, api_mock)
        task = asyncio.create_task(
            bt._add_note_impl(
                kind="work_order", target_id=42, note="Vendor en route", label="Roof leak"
            )
        )
        payload = await _next_payload()
        await agent_tools.resolve_confirmation(payload["confirm_id"], True)
        result = await asyncio.wait_for(task, timeout=1.0)
    wo_note.assert_awaited_once()
    assert wo_note.await_args.kwargs["note"] == "Vendor en route"
    assert "Added your note" in result


@pytest.mark.asyncio
async def test_empty_note_no_call() -> None:
    client_mock, api_mock, task_note, _ = _api()
    with ExitStack() as stack:
        _enter(stack, client_mock, api_mock)
        result = await bt._add_note_impl(kind="task", target_id=555, note="   ", label="x")
    task_note.assert_not_called()
    assert "note" in result.lower()


@pytest.mark.asyncio
async def test_deny_does_not_add_note() -> None:
    await agent_tools.drain_pending_confirmations()
    client_mock, api_mock, task_note, _ = _api()
    with ExitStack() as stack:
        _enter(stack, client_mock, api_mock)
        task = asyncio.create_task(
            bt._add_note_impl(kind="task", target_id=555, note="hello", label="x")
        )
        payload = await _next_payload()
        await agent_tools.resolve_confirmation(payload["confirm_id"], False)
        result = await asyncio.wait_for(task, timeout=1.0)
    task_note.assert_not_called()
    assert "didn't add" in result.lower() or "nothing was changed" in result.lower()


@pytest.mark.asyncio
async def test_timeout_does_not_add_note() -> None:
    await agent_tools.drain_pending_confirmations()
    client_mock, api_mock, task_note, _ = _api()
    with ExitStack() as stack:
        stack.enter_context(
            patch.object(idem, "confirm_or_abort", new=AsyncMock(return_value=False))
        )
        _enter(stack, client_mock, api_mock)
        result = await bt._add_note_impl(kind="task", target_id=555, note="hi", label="x")
    task_note.assert_not_called()
    assert "nothing was changed" in result.lower()
