"""Work-order create — confirm + idempotency + verify, and the Buildium body.

``buildium_create_work_order`` mirrors the create-task write guarantees (runs
only on approval, single-execution, read-back verified) and additionally builds
Buildium's specific work-order CREATE body (embedded parent Task + VendorId +
EntryAllowed). These tests pin both. Harness mirrors
``test_buildium_write_confirmation`` — the gate awaits a Future resolved
out-of-band; we run the impl as a task, drain its confirm payload, resolve it.
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


def _api_capturing_wo_create() -> tuple[Any, Any, AsyncMock]:
    from helixis.buildium.schemas import WorkOrder

    create = AsyncMock(return_value=WorkOrder(Id=4242, Title="x"))
    api_mock = MagicMock()
    api_mock.work_orders = MagicMock()
    api_mock.work_orders.create = create
    api_mock.work_orders.get = AsyncMock(return_value=WorkOrder(Id=4242))

    client_mock = MagicMock()
    client_mock.__aenter__ = AsyncMock(return_value=client_mock)
    client_mock.__aexit__ = AsyncMock(return_value=False)
    return client_mock, api_mock, create


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


async def _run_create(**overrides: Any) -> Any:
    kwargs: dict[str, Any] = {
        "title": "No hot water - 7C",
        "description": "Tenant reports no hot water",
        "property_id": 47,
        "vendor_id": 4471,
        "assigned_to_user_id": 70271,
    }
    kwargs.update(overrides)
    return await bt._create_work_order_impl(**kwargs)


@pytest.mark.asyncio
async def test_approve_creates_work_order_once_and_verifies() -> None:
    await agent_tools.drain_pending_confirmations()
    client_mock, api_mock, create = _api_capturing_wo_create()
    with ExitStack() as stack:
        _enter_patches(stack, client_mock, api_mock)
        task = asyncio.create_task(_run_create())
        payload = await _next_confirm_payload()
        create.assert_not_called()  # nothing written before approval
        await agent_tools.resolve_confirmation(payload["confirm_id"], True)
        result = await asyncio.wait_for(task, timeout=1.0)
    create.assert_awaited_once()
    assert "Work order created in Buildium" in result
    assert "4242" not in result  # never leak the work-order id


@pytest.mark.asyncio
async def test_create_body_has_buildium_shape() -> None:
    """Approve → the POST body carries the required Buildium fields: embedded
    parent Task, VendorId, EntryAllowed, and PropertyId."""
    await agent_tools.drain_pending_confirmations()
    client_mock, api_mock, create = _api_capturing_wo_create()
    with ExitStack() as stack:
        _enter_patches(stack, client_mock, api_mock)
        task = asyncio.create_task(_run_create())
        payload = await _next_confirm_payload()
        await agent_tools.resolve_confirmation(payload["confirm_id"], True)
        await asyncio.wait_for(task, timeout=1.0)

    body = create.await_args.args[0]
    assert body["PropertyId"] == 47
    assert body["VendorId"] == 4471
    assert body["EntryAllowed"] == "Unknown"
    assert isinstance(body["Task"], dict)
    assert body["Task"]["AssignedToUserId"] == 70271
    assert body["Task"]["Status"] == "New"
    assert body["Task"]["Title"] == "No hot water - 7C"


@pytest.mark.asyncio
async def test_confirm_card_shows_labels_not_ids() -> None:
    """When the agent passes display labels, the user-facing confirm card shows
    names, not raw Buildium ids."""
    await agent_tools.drain_pending_confirmations()
    client_mock, api_mock, create = _api_capturing_wo_create()
    with ExitStack() as stack:
        _enter_patches(stack, client_mock, api_mock)
        task = asyncio.create_task(
            _run_create(
                property_label="123 Oak St",
                vendor_label="Apex Plumbing",
                assignee_label="Vernon Amerson",
            )
        )
        payload = await _next_confirm_payload()
        await agent_tools.resolve_confirmation(payload["confirm_id"], True)
        await asyncio.wait_for(task, timeout=1.0)

    items = " | ".join(payload["items"])
    # ⚠ REWRITTEN 2026-08-24. All three of these — vendor, property, assignee —
    # were model-SUPPLIED labels, and this asserted they appear while the ids
    # do NOT. That is the vulnerability stated as a requirement: the human was
    # shown three names the model chose, next to three ids it also chose, with
    # nothing checking they agreed (the Whitfield misattribution).
    #
    # None of these kinds has a getter yet, so the card renders the ID form.
    # Less friendly, strictly more honest: an id is checkable against Buildium,
    # a confidently wrong name is not. Adding getters later improves the text
    # without changing the guarantee — see test_confirm_label_provenance.
    assert "Vendor: 4471" in items, items
    assert "Assigned to: 70271" in items, items
    for supplied in ("Apex Plumbing", "123 Oak St", "Vernon Amerson"):
        assert supplied not in items, (
            f"the confirm card rendered the model-supplied label {supplied!r}"
        )


@pytest.mark.asyncio
async def test_deny_does_not_create() -> None:
    await agent_tools.drain_pending_confirmations()
    client_mock, api_mock, create = _api_capturing_wo_create()
    with ExitStack() as stack:
        _enter_patches(stack, client_mock, api_mock)
        task = asyncio.create_task(_run_create())
        payload = await _next_confirm_payload()
        await agent_tools.resolve_confirmation(payload["confirm_id"], False)
        result = await asyncio.wait_for(task, timeout=1.0)
    create.assert_not_called()
    assert "didn't create" in result.lower() or "nothing was changed" in result.lower()


@pytest.mark.asyncio
async def test_timeout_does_not_create() -> None:
    await agent_tools.drain_pending_confirmations()
    client_mock, api_mock, create = _api_capturing_wo_create()
    with ExitStack() as stack:
        stack.enter_context(
            patch.object(idem, "confirm_or_abort", new=AsyncMock(return_value=False))
        )
        _enter_patches(stack, client_mock, api_mock)
        result = await _run_create()
    create.assert_not_called()
    assert "nothing was changed" in result.lower()
