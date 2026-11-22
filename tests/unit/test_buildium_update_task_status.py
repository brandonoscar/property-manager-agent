"""Update-task-status — confirm + idempotency + verify, partial PATCH.

``buildium_update_task_status`` reuses the write safety stack (runs only on
approval, single-execution, read-back verified) and sends Buildium's partial
to-do PATCH ``{"TaskStatus": ...}`` — no read-modify-write. Harness mirrors
``test_buildium_work_order_create``.
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


def _api_capturing_update(new_status: str = "Completed") -> tuple[Any, Any, AsyncMock]:
    from helixis.buildium.schemas import Task

    update = AsyncMock(return_value={"Id": 555, "Status": new_status})
    api_mock = MagicMock()
    api_mock.tasks = MagicMock()
    api_mock.tasks.update = update
    # read-back verify sees the new status
    api_mock.tasks.get = AsyncMock(
        return_value=Task(Id=555, Status=new_status, Title="Fix sink in 4B")
    )

    client_mock = MagicMock()
    client_mock.__aenter__ = AsyncMock(return_value=client_mock)
    client_mock.__aexit__ = AsyncMock(return_value=False)
    return client_mock, api_mock, update


def _enter_patches(stack: ExitStack, client_mock: Any, api_mock: Any) -> None:
    stack.enter_context(memory_context(company_id="company-1", user_id="u1", session_id="s1"))
    stack.enter_context(
        patch.object(bt, "load_buildium_settings_for_company", new=AsyncMock(return_value=object()))
    )
    stack.enter_context(patch.object(bt, "BuildiumClient", return_value=client_mock))
    stack.enter_context(patch.object(bt, "BuildiumApi", return_value=api_mock))


async def _next_confirm_payload() -> dict:
    for _ in range(50):
        await asyncio.sleep(0)
        drained = await agent_tools.drain_pending_confirmations()
        if drained:
            return drained[0]
    raise AssertionError("no confirm payload was emitted by the write tool")


@pytest.mark.asyncio
async def test_approve_updates_status_once_and_verifies() -> None:
    await agent_tools.drain_pending_confirmations()
    client_mock, api_mock, update = _api_capturing_update("Completed")
    with ExitStack() as stack:
        _enter_patches(stack, client_mock, api_mock)
        task = asyncio.create_task(
            bt._update_task_status_impl(555, "Completed", task_label="Fix sink in 4B")
        )
        payload = await _next_confirm_payload()
        update.assert_not_called()
        await agent_tools.resolve_confirmation(payload["confirm_id"], True)
        result = await asyncio.wait_for(task, timeout=1.0)
    update.assert_awaited_once()
    # Partial PATCH carries exactly the TaskStatus delta.
    assert update.await_args.kwargs["fields"] == {"TaskStatus": "Completed"}
    assert "Completed" in result
    assert "555" not in result  # never leak the id


@pytest.mark.asyncio
async def test_verify_reads_taskstatus_not_status() -> None:
    """Regression (prod dogfood 2026-07-26): ``GET /tasks/{id}`` returns the
    status under ``TaskStatus`` (→ ``.task_status``), NOT ``Status``. The verify
    checked only ``.status`` (None on the real shape), so every successful status
    change wrongly resolved to ``blocked_needs_review`` ("couldn't confirm"). Mock
    the REAL wire shape — a Task with TaskStatus set and Status absent — and assert
    the tool confirms success instead of the can't-confirm message."""
    from helixis.buildium.schemas import Task

    await agent_tools.drain_pending_confirmations()
    client_mock, api_mock, _update = _api_capturing_update("Completed")
    # The live shape: TaskStatus populated, Status None (the bug's trigger).
    api_mock.tasks.get = AsyncMock(
        return_value=Task(Id=555, TaskStatus="Completed", Title="Fix sink in 4B")
    )
    with ExitStack() as stack:
        _enter_patches(stack, client_mock, api_mock)
        task = asyncio.create_task(bt._update_task_status_impl(555, "Completed"))
        payload = await _next_confirm_payload()
        await agent_tools.resolve_confirmation(payload["confirm_id"], True)
        result = await asyncio.wait_for(task, timeout=1.0)
    # Verify passed → success message, NOT the "couldn't confirm" degrade.
    assert "Completed" in result
    assert "couldn't confirm" not in result.lower()
    assert "check" not in result.lower() or "buildium" not in result.lower()


@pytest.mark.asyncio
async def test_confirm_card_shows_label_not_id() -> None:
    await agent_tools.drain_pending_confirmations()
    client_mock, api_mock, _update = _api_capturing_update("InProgress")
    with ExitStack() as stack:
        _enter_patches(stack, client_mock, api_mock)
        task = asyncio.create_task(
            bt._update_task_status_impl(555, "InProgress", task_label="Fix sink in 4B")
        )
        payload = await _next_confirm_payload()
        await agent_tools.resolve_confirmation(payload["confirm_id"], True)
        await asyncio.wait_for(task, timeout=1.0)
    items = " | ".join(payload["items"])
    assert "Fix sink in 4B" in items
    assert "555" not in items


@pytest.mark.asyncio
async def test_invalid_status_no_call() -> None:
    client_mock, api_mock, update = _api_capturing_update()
    with ExitStack() as stack:
        _enter_patches(stack, client_mock, api_mock)
        result = await bt._update_task_status_impl(555, "Bogus")
    update.assert_not_called()
    assert "must be one of" in result.lower()


@pytest.mark.asyncio
async def test_deny_does_not_update() -> None:
    await agent_tools.drain_pending_confirmations()
    client_mock, api_mock, update = _api_capturing_update()
    with ExitStack() as stack:
        _enter_patches(stack, client_mock, api_mock)
        task = asyncio.create_task(bt._update_task_status_impl(555, "Completed"))
        payload = await _next_confirm_payload()
        await agent_tools.resolve_confirmation(payload["confirm_id"], False)
        result = await asyncio.wait_for(task, timeout=1.0)
    update.assert_not_called()
    assert "didn't change" in result.lower() or "nothing was changed" in result.lower()


@pytest.mark.asyncio
async def test_timeout_does_not_update() -> None:
    await agent_tools.drain_pending_confirmations()
    client_mock, api_mock, update = _api_capturing_update()
    with ExitStack() as stack:
        stack.enter_context(
            patch.object(idem, "confirm_or_abort", new=AsyncMock(return_value=False))
        )
        _enter_patches(stack, client_mock, api_mock)
        result = await bt._update_task_status_impl(555, "Completed")
    update.assert_not_called()
    assert "nothing was changed" in result.lower()
