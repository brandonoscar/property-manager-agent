"""Close-work-order — confirm + idempotency + verify, via the backing task.

``buildium_close_work_order`` resolves the WO's backing ``Task.Id`` and closes
it through ``Tasks.update({"TaskStatus": "Completed"})`` (the WO update endpoint
has no status field — gotcha 20), then read-back-verifies on the WORK ORDER
(where the visible status lives). Harness mirrors
``test_buildium_update_task_status``.
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

_WO_ID = 9001
_TASK_ID = 29640


def _wo(status: str, *, task_id: int | None = _TASK_ID) -> Any:
    from helixis.buildium.schemas import WorkOrder, WorkOrderTaskRef

    return WorkOrder(
        Id=_WO_ID,
        Status=status,
        Title="Repair package room latch",
        Task=WorkOrderTaskRef(Id=task_id) if task_id is not None else None,
    )


def _api_for_close(
    *, initial: str = "New", after: str = "Completed", task_id: int | None = _TASK_ID
) -> tuple[Any, Any, AsyncMock, AsyncMock]:
    # get() is called once to resolve the task id, then again to verify — so the
    # first call returns the pre-close WO and subsequent calls the completed one.
    get = AsyncMock(side_effect=[_wo(initial, task_id=task_id)] + [_wo(after, task_id=task_id)] * 4)
    update = AsyncMock(return_value={"Id": _TASK_ID, "TaskStatus": after})
    api_mock = MagicMock()
    api_mock.work_orders = MagicMock()
    api_mock.work_orders.get = get
    api_mock.tasks = MagicMock()
    api_mock.tasks.update = update

    client_mock = MagicMock()
    client_mock.__aenter__ = AsyncMock(return_value=client_mock)
    client_mock.__aexit__ = AsyncMock(return_value=False)
    return client_mock, api_mock, update, get


def _enter_patches(
    stack: ExitStack, client_mock: Any, api_mock: Any, *, role: str = "admin"
) -> None:
    stack.enter_context(memory_context(company_id="company-1", user_id="u1", session_id="s1"))
    stack.enter_context(
        patch.object(bt, "load_buildium_settings_for_company", new=AsyncMock(return_value=object()))
    )
    stack.enter_context(patch.object(bt, "BuildiumClient", return_value=client_mock))
    stack.enter_context(patch.object(bt, "BuildiumApi", return_value=api_mock))
    # Closing a WO is a one-way latch, so it carries a Manager+ role floor
    # (fail-closed). No pool in unit tests → stub the caller's role.
    stack.enter_context(patch.object(bt, "_caller_role", new=AsyncMock(return_value=role)))


async def _next_confirm_payload() -> dict:
    for _ in range(50):
        await asyncio.sleep(0)
        drained = await agent_tools.drain_pending_confirmations()
        if drained:
            return drained[0]
    raise AssertionError("no confirm payload was emitted by the write tool")


@pytest.mark.asyncio
async def test_approve_closes_via_backing_task_and_verifies() -> None:
    await agent_tools.drain_pending_confirmations()
    client_mock, api_mock, update, _get = _api_for_close()
    with ExitStack() as stack:
        _enter_patches(stack, client_mock, api_mock)
        task = asyncio.create_task(
            bt._close_work_order_impl(_WO_ID, work_order_label="Repair package room latch")
        )
        payload = await _next_confirm_payload()
        update.assert_not_called()  # nothing before approval
        await agent_tools.resolve_confirmation(payload["confirm_id"], True)
        result = await asyncio.wait_for(task, timeout=1.0)
    # The write is the backing task's status flip, not a WO-side status.
    update.assert_awaited_once()
    assert update.await_args.args[0] == _TASK_ID
    assert update.await_args.kwargs["fields"] == {"TaskStatus": "Completed"}
    assert "Completed" in result
    assert str(_WO_ID) not in result and str(_TASK_ID) not in result  # never leak ids


@pytest.mark.asyncio
async def test_confirm_card_shows_label_not_id() -> None:
    await agent_tools.drain_pending_confirmations()
    client_mock, api_mock, _update, _get = _api_for_close()
    with ExitStack() as stack:
        _enter_patches(stack, client_mock, api_mock)
        task = asyncio.create_task(
            bt._close_work_order_impl(_WO_ID, work_order_label="Repair package room latch")
        )
        payload = await _next_confirm_payload()
        await agent_tools.resolve_confirmation(payload["confirm_id"], True)
        await asyncio.wait_for(task, timeout=1.0)
    items = " | ".join(payload["items"])
    assert "Repair package room latch" in items
    assert str(_WO_ID) not in items and str(_TASK_ID) not in items


@pytest.mark.asyncio
async def test_deny_does_not_close() -> None:
    await agent_tools.drain_pending_confirmations()
    client_mock, api_mock, update, _get = _api_for_close()
    with ExitStack() as stack:
        _enter_patches(stack, client_mock, api_mock)
        task = asyncio.create_task(bt._close_work_order_impl(_WO_ID))
        payload = await _next_confirm_payload()
        await agent_tools.resolve_confirmation(payload["confirm_id"], False)
        result = await asyncio.wait_for(task, timeout=1.0)
    update.assert_not_called()
    assert "didn't close" in result.lower() or "nothing was changed" in result.lower()


@pytest.mark.asyncio
async def test_already_completed_is_a_noop() -> None:
    # No confirm gate, no write — the WO is already Completed.
    client_mock, api_mock, update, _get = _api_for_close(initial="Completed")
    with ExitStack() as stack:
        _enter_patches(stack, client_mock, api_mock)
        result = await bt._close_work_order_impl(_WO_ID)
    update.assert_not_called()
    assert "already completed" in result.lower()


@pytest.mark.asyncio
async def test_no_backing_task_is_graceful() -> None:
    client_mock, api_mock, update, _get = _api_for_close(task_id=None)
    with ExitStack() as stack:
        _enter_patches(stack, client_mock, api_mock)
        result = await bt._close_work_order_impl(_WO_ID)
    update.assert_not_called()
    assert "couldn't close" in result.lower() and "buildium" in result.lower()


@pytest.mark.asyncio
async def test_timeout_does_not_close() -> None:
    await agent_tools.drain_pending_confirmations()
    client_mock, api_mock, update, _get = _api_for_close()
    with ExitStack() as stack:
        stack.enter_context(
            patch.object(idem, "confirm_or_abort", new=AsyncMock(return_value=False))
        )
        _enter_patches(stack, client_mock, api_mock)
        result = await bt._close_work_order_impl(_WO_ID)
    update.assert_not_called()
    assert "nothing was changed" in result.lower()


@pytest.mark.asyncio
async def test_close_role_floor_denies_member() -> None:
    """A one-way latch (no API reopen) — a Member is refused BEFORE any fetch,
    confirm gate, or task update."""
    client_mock, api_mock, update, get = _api_for_close()
    with ExitStack() as stack:
        _enter_patches(stack, client_mock, api_mock, role="member")
        result = await bt._close_work_order_impl(_WO_ID)
    update.assert_not_called()
    get.assert_not_called()  # floored before any Buildium I/O
    assert "manager or admin" in result.lower()


@pytest.mark.asyncio
async def test_close_role_floor_fails_closed_when_role_unknown() -> None:
    """Unverifiable role (no pool / no row → None) refuses the close."""
    client_mock, api_mock, update, get = _api_for_close()
    with ExitStack() as stack:
        _enter_patches(stack, client_mock, api_mock)
        stack.enter_context(patch.object(bt, "_caller_role", new=AsyncMock(return_value=None)))
        result = await bt._close_work_order_impl(_WO_ID)
    update.assert_not_called()
    get.assert_not_called()
    assert "manager or admin" in result.lower()
