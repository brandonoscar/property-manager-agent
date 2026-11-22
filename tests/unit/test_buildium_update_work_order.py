"""Update-work-order — field-routing (vendor→WO put, priority/due→task put).

``buildium_update_work_order`` splits its writes across two Buildium endpoints
because priority/due-date are task-derived and no-op on ``/workorders`` (gotcha
20): vendor via ``WorkOrders.reassign_vendor``, priority/due-date via
``Tasks.update``. Same write-safety stack (confirm + idempotency + verify).
Harness mirrors ``test_buildium_close_work_order``.
"""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helixis.agents import buildium_tools as bt
from helixis.agents import tools as agent_tools
from helixis.memory.context import memory_context

_WO_ID = 9014
_TASK_ID = 29640


def _wo(
    *, vendor: int = 195537, priority: str = "Normal", due: str | None = None, task: bool = True
) -> Any:
    from helixis.buildium.schemas import WorkOrder, WorkOrderTaskRef

    kwargs: dict[str, Any] = {
        "Id": _WO_ID,
        "Status": "New",
        "Title": "Unjam garbage disposal",
        "Priority": priority,
        "VendorId": vendor,
        "Task": WorkOrderTaskRef(Id=_TASK_ID) if task else None,
    }
    if due is not None:
        kwargs["DueDate"] = due
    return WorkOrder(**kwargs)


def _api(*, before: Any, after: Any) -> tuple[Any, Any, AsyncMock, AsyncMock]:
    get = AsyncMock(side_effect=[before] + [after] * 4)
    reassign = AsyncMock(return_value={"Id": _WO_ID})
    update = AsyncMock(return_value={"Id": _TASK_ID})
    api_mock = MagicMock()
    api_mock.work_orders = MagicMock()
    api_mock.work_orders.get = get
    api_mock.work_orders.reassign_vendor = reassign
    api_mock.tasks = MagicMock()
    # The card derives display text from the RECORD (_gate_label), so the
    # getters must be mocked or every label falls back to the id form.
    api_mock.vendors = MagicMock()
    api_mock.vendors.get = AsyncMock(return_value=MagicMock(company_name="Acme Plumbing"))
    api_mock.tasks.update = update

    client_mock = MagicMock()
    client_mock.__aenter__ = AsyncMock(return_value=client_mock)
    client_mock.__aexit__ = AsyncMock(return_value=False)
    return client_mock, api_mock, reassign, update


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
async def test_vendor_reassign_routes_to_wo_put_only() -> None:
    await agent_tools.drain_pending_confirmations()
    client, api, reassign, update = _api(before=_wo(vendor=195537), after=_wo(vendor=195538))
    with ExitStack() as stack:
        _enter_patches(stack, client, api)
        task = asyncio.create_task(
            bt._update_work_order_impl(
                _WO_ID, vendor_id=195538, vendor_label="Acme Plumbing", work_order_label="Disposal"
            )
        )
        payload = await _next_confirm_payload()
        reassign.assert_not_called()
        await agent_tools.resolve_confirmation(payload["confirm_id"], True)
        result = await asyncio.wait_for(task, timeout=1.0)
    reassign.assert_awaited_once_with(_WO_ID, 195538)
    update.assert_not_called()  # no task-side write for a vendor-only change
    assert "Updated" in result


@pytest.mark.asyncio
async def test_priority_routes_to_task_put_only() -> None:
    await agent_tools.drain_pending_confirmations()
    client, api, reassign, update = _api(before=_wo(priority="Normal"), after=_wo(priority="High"))
    with ExitStack() as stack:
        _enter_patches(stack, client, api)
        task = asyncio.create_task(bt._update_work_order_impl(_WO_ID, priority="High"))
        payload = await _next_confirm_payload()
        await agent_tools.resolve_confirmation(payload["confirm_id"], True)
        await asyncio.wait_for(task, timeout=1.0)
    reassign.assert_not_called()
    update.assert_awaited_once()
    assert update.await_args.kwargs["fields"] == {"Priority": "High"}


@pytest.mark.asyncio
async def test_priority_verify_reads_nested_task_priority() -> None:
    """Regression (prod dogfood 2026-07-26, WO 9006): after ``Tasks.update`` the
    new priority lands on the NESTED ``WorkOrder.Task.Priority``; the WO's
    top-level ``Priority`` stays FROZEN at creation (gotcha 20). The verify
    checked only the top-level, so a real priority change resolved to
    ``blocked_needs_review`` ("couldn't confirm"). Mock the real shape — top-level
    frozen at Normal, nested Task.Priority moved to High — and assert success."""
    from helixis.buildium.schemas import WorkOrder, WorkOrderTaskRef

    await agent_tools.drain_pending_confirmations()
    # after: top-level Priority still "Normal" (frozen), nested task now "High".
    after = WorkOrder(
        Id=_WO_ID,
        Status="New",
        Title="Unjam garbage disposal",
        Priority="Normal",
        VendorId=195537,
        Task=WorkOrderTaskRef(Id=_TASK_ID, Priority="High"),
    )
    client, api, reassign, update = _api(before=_wo(priority="Normal"), after=after)
    with ExitStack() as stack:
        _enter_patches(stack, client, api)
        task = asyncio.create_task(bt._update_work_order_impl(_WO_ID, priority="High"))
        payload = await _next_confirm_payload()
        await agent_tools.resolve_confirmation(payload["confirm_id"], True)
        result = await asyncio.wait_for(task, timeout=1.0)
    update.assert_awaited_once()
    assert "Updated" in result
    assert "couldn't" not in result.lower()


@pytest.mark.asyncio
async def test_due_date_routes_to_task_put() -> None:
    await agent_tools.drain_pending_confirmations()
    client, api, reassign, update = _api(before=_wo(), after=_wo(due="2026-08-15"))
    with ExitStack() as stack:
        _enter_patches(stack, client, api)
        task = asyncio.create_task(bt._update_work_order_impl(_WO_ID, due_date="2026-08-15"))
        payload = await _next_confirm_payload()
        await agent_tools.resolve_confirmation(payload["confirm_id"], True)
        await asyncio.wait_for(task, timeout=1.0)
    update.assert_awaited_once()
    assert update.await_args.kwargs["fields"] == {"DueDate": "2026-08-15"}


@pytest.mark.asyncio
async def test_combined_change_hits_both_endpoints() -> None:
    await agent_tools.drain_pending_confirmations()
    client, api, reassign, update = _api(
        before=_wo(vendor=195537, priority="Normal"),
        after=_wo(vendor=195538, priority="High", due="2026-08-15"),
    )
    with ExitStack() as stack:
        _enter_patches(stack, client, api)
        task = asyncio.create_task(
            bt._update_work_order_impl(
                _WO_ID, vendor_id=195538, priority="High", due_date="2026-08-15"
            )
        )
        payload = await _next_confirm_payload()
        await agent_tools.resolve_confirmation(payload["confirm_id"], True)
        await asyncio.wait_for(task, timeout=1.0)
    reassign.assert_awaited_once_with(_WO_ID, 195538)
    assert update.await_args.kwargs["fields"] == {"Priority": "High", "DueDate": "2026-08-15"}


@pytest.mark.asyncio
async def test_no_change_requested_is_a_noop() -> None:
    client, api, reassign, update = _api(before=_wo(), after=_wo())
    with ExitStack() as stack:
        _enter_patches(stack, client, api)
        result = await bt._update_work_order_impl(_WO_ID)
    reassign.assert_not_called()
    update.assert_not_called()
    assert "what to change" in result.lower()


@pytest.mark.asyncio
async def test_invalid_priority_rejected() -> None:
    client, api, reassign, update = _api(before=_wo(), after=_wo())
    with ExitStack() as stack:
        _enter_patches(stack, client, api)
        result = await bt._update_work_order_impl(_WO_ID, priority="Urgent")
    reassign.assert_not_called()
    update.assert_not_called()
    assert "must be one of" in result.lower()


@pytest.mark.asyncio
async def test_bad_due_date_format_rejected() -> None:
    client, api, reassign, update = _api(before=_wo(), after=_wo())
    with ExitStack() as stack:
        _enter_patches(stack, client, api)
        result = await bt._update_work_order_impl(_WO_ID, due_date="Aug 15")
    update.assert_not_called()
    assert "yyyy-mm-dd" in result.lower()


@pytest.mark.asyncio
async def test_no_backing_task_blocks_priority_change() -> None:
    client, api, reassign, update = _api(before=_wo(task=False), after=_wo(task=False))
    with ExitStack() as stack:
        _enter_patches(stack, client, api)
        result = await bt._update_work_order_impl(_WO_ID, priority="High")
    update.assert_not_called()
    assert "couldn't change" in result.lower()


@pytest.mark.asyncio
async def test_deny_changes_nothing() -> None:
    await agent_tools.drain_pending_confirmations()
    client, api, reassign, update = _api(before=_wo(), after=_wo(vendor=195538))
    with ExitStack() as stack:
        _enter_patches(stack, client, api)
        task = asyncio.create_task(bt._update_work_order_impl(_WO_ID, vendor_id=195538))
        payload = await _next_confirm_payload()
        await agent_tools.resolve_confirmation(payload["confirm_id"], False)
        result = await asyncio.wait_for(task, timeout=1.0)
    reassign.assert_not_called()
    update.assert_not_called()
    assert "didn't change" in result.lower() or "nothing was changed" in result.lower()


@pytest.mark.asyncio
async def test_confirm_card_shows_labels_not_ids() -> None:
    await agent_tools.drain_pending_confirmations()
    client, api, reassign, update = _api(before=_wo(vendor=195537), after=_wo(vendor=195538))
    with ExitStack() as stack:
        _enter_patches(stack, client, api)
        task = asyncio.create_task(
            bt._update_work_order_impl(
                _WO_ID, vendor_id=195538, vendor_label="Acme Plumbing", work_order_label="Disposal"
            )
        )
        payload = await _next_confirm_payload()
        await agent_tools.resolve_confirmation(payload["confirm_id"], True)
        await asyncio.wait_for(task, timeout=1.0)
    items = " | ".join(payload["items"])
    # ⚠ REWRITTEN 2026-08-24. Both labels here are model-SUPPLIED, and this
    # asserted they reach the card — the vulnerability as a requirement.
    # The vendor name now comes from the vendor RECORD (same string, but
    # earned), and the work-order title from the WO record the tool already
    # fetched — "Unjam garbage disposal", not the model's "Disposal".
    assert "Acme Plumbing" in items, items
    assert "Unjam garbage disposal" in items, items
    assert "| Disposal" not in items, "the card rendered the model-supplied work_order_label"
    assert "195538" not in items
