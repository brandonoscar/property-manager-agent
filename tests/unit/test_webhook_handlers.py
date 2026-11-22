"""Tests for Buildium webhook handlers.

Focus on the handler orchestration: refetch from Buildium → upsert the
Helixis mirror row → write an activity-log entry. We mock the
``upsert_*`` and ``write_activity`` repo functions so the tests don't
need a running Postgres.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from helixis.buildium.errors import BuildiumApiError
from helixis.buildium.schemas import Lease, WorkOrder
from helixis.db.repositories import ActivityEntry
from helixis.webhooks.handlers.lease import (
    handle_lease_created,
    handle_lease_updated,
)
from helixis.webhooks.handlers.work_order import (
    handle_work_order_created,
    handle_work_order_updated,
)
from helixis.webhooks.queue import ClaimedEvent


def _event(event_name: str, **ids: int) -> ClaimedEvent:
    return ClaimedEvent(
        id=uuid4(),
        event_name=event_name,
        event_time=datetime(2026, 4, 21, 12, tzinfo=UTC),
        account_id=42,
        entity_ids=ids,
        payload={"EventName": event_name, **ids},
        attempts=1,
    )


def _lease() -> Lease:
    return Lease.model_validate(
        {
            "Id": 99,
            "PropertyId": 7,
            "UnitId": 14,
            "LeaseStatus": "Active",
            "LeaseFromDate": "2025-03-01T00:00:00Z",
            "LeaseToDate": "2026-05-31T00:00:00Z",
            # PR #74 — Buildium nests Rent inside AccountDetails. The
            # top-level "Rent" alias was a no-op against real responses;
            # the fixture matches the real shape now.
            "AccountDetails": {"Rent": 2150.0},
        }
    )


def _work_order() -> WorkOrder:
    return WorkOrder.model_validate(
        {
            "Id": 2041,
            "PropertyId": 7,
            "UnitId": 14,
            "Title": "HVAC not cooling",
            "Status": "New",
            "Priority": "High",
        }
    )


# ── Lease handler ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_lease_created_refetches_upserts_and_logs_activity() -> None:
    api = MagicMock()
    api.leases.get = AsyncMock(return_value=_lease())

    with (
        patch("helixis.webhooks.handlers.lease.get_pool", return_value="POOL") as gp,
        patch("helixis.webhooks.handlers.lease.upsert_lease", new_callable=AsyncMock) as upsert,
        patch("helixis.webhooks.handlers.lease.write_activity", new_callable=AsyncMock) as log,
    ):
        await handle_lease_created(_event("Lease.Created", LeaseId=99), api)

    api.leases.get.assert_awaited_once_with(99)
    gp.assert_called()
    upsert.assert_awaited_once()
    assert upsert.await_args.kwargs["account_id"] == 42

    log.assert_awaited_once()
    entry: ActivityEntry = log.await_args.args[1]
    assert entry.source == "webhook"
    assert entry.account_id == 42
    assert entry.entity_type == "lease"
    assert entry.entity_id == 99
    assert "created" in entry.description
    assert "$2,150.00/mo" in entry.description
    assert entry.action_type == "lease_created"


@pytest.mark.asyncio
async def test_lease_updated_uses_updated_action_type() -> None:
    api = MagicMock()
    api.leases.get = AsyncMock(return_value=_lease())

    with (
        patch("helixis.webhooks.handlers.lease.get_pool", return_value="POOL"),
        patch("helixis.webhooks.handlers.lease.upsert_lease", new_callable=AsyncMock),
        patch("helixis.webhooks.handlers.lease.write_activity", new_callable=AsyncMock) as log,
    ):
        await handle_lease_updated(_event("Lease.Updated", LeaseId=99), api)

    entry: ActivityEntry = log.await_args.args[1]
    assert entry.action_type == "lease_updated"
    assert "updated" in entry.description


@pytest.mark.asyncio
async def test_lease_handler_skips_on_missing_lease_id() -> None:
    api = MagicMock()
    api.leases.get = AsyncMock()

    with (
        patch("helixis.webhooks.handlers.lease.upsert_lease", new_callable=AsyncMock) as upsert,
        patch("helixis.webhooks.handlers.lease.write_activity", new_callable=AsyncMock) as log,
    ):
        await handle_lease_created(_event("Lease.Created"), api)  # no LeaseId

    api.leases.get.assert_not_awaited()
    upsert.assert_not_awaited()
    log.assert_not_awaited()


@pytest.mark.asyncio
async def test_lease_handler_swallows_404_on_race_with_delete() -> None:
    api = MagicMock()
    api.leases.get = AsyncMock(side_effect=BuildiumApiError(status=404, message="not found"))

    with (
        patch("helixis.webhooks.handlers.lease.upsert_lease", new_callable=AsyncMock) as upsert,
        patch("helixis.webhooks.handlers.lease.write_activity", new_callable=AsyncMock) as log,
    ):
        # Must not raise — the lease may have been deleted between webhook
        # delivery and our refetch.
        await handle_lease_created(_event("Lease.Created", LeaseId=99), api)

    upsert.assert_not_awaited()
    log.assert_not_awaited()


@pytest.mark.asyncio
async def test_lease_handler_propagates_non_404_api_errors() -> None:
    api = MagicMock()
    api.leases.get = AsyncMock(side_effect=BuildiumApiError(status=500, message="server down"))

    with (
        patch("helixis.webhooks.handlers.lease.upsert_lease", new_callable=AsyncMock),
        patch("helixis.webhooks.handlers.lease.write_activity", new_callable=AsyncMock),
    ):
        with pytest.raises(BuildiumApiError):
            await handle_lease_created(_event("Lease.Created", LeaseId=99), api)


# ── Work-order handler ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_work_order_created_refetches_upserts_and_logs() -> None:
    api = MagicMock()
    api.work_orders.get = AsyncMock(return_value=_work_order())

    with (
        patch("helixis.webhooks.handlers.work_order.get_pool", return_value="POOL"),
        patch(
            "helixis.webhooks.handlers.work_order.upsert_work_order",
            new_callable=AsyncMock,
        ) as upsert,
        patch(
            "helixis.webhooks.handlers.work_order.write_activity",
            new_callable=AsyncMock,
        ) as log,
    ):
        await handle_work_order_created(_event("WorkOrder.Created", WorkOrderId=2041), api)

    api.work_orders.get.assert_awaited_once_with(2041)
    upsert.assert_awaited_once()
    assert upsert.await_args.kwargs["account_id"] == 42

    entry: ActivityEntry = log.await_args.args[1]
    assert entry.action_type == "work_order_created"
    assert entry.entity_type == "work_order"
    assert entry.entity_id == 2041
    # Priority surfaced in the description for operational context.
    assert "[High]" in entry.description


@pytest.mark.asyncio
async def test_work_order_created_runs_photo_triage() -> None:
    # A CREATED work order fires #533-entry-point-2 photo triage; an UPDATED one
    # does not (bounds cost + avoids re-assessing on every status flip).
    api = MagicMock()
    api.work_orders.get = AsyncMock(return_value=_work_order())

    with (
        patch("helixis.webhooks.handlers.work_order.get_pool", return_value="POOL"),
        patch("helixis.webhooks.handlers.work_order.upsert_work_order", new_callable=AsyncMock),
        patch("helixis.webhooks.handlers.work_order.write_activity", new_callable=AsyncMock),
        patch(
            "helixis.webhooks.handlers.work_order.triage_work_order_photos",
            new_callable=AsyncMock,
        ) as triage,
    ):
        await handle_work_order_created(_event("WorkOrder.Created", WorkOrderId=2041), api)
        assert triage.await_count == 1
        assert triage.await_args.kwargs["company_id"] == "42"
        assert triage.await_args.kwargs["account_id"] == 42

        triage.reset_mock()
        await handle_work_order_updated(_event("WorkOrder.Updated", WorkOrderId=2041), api)
        triage.assert_not_awaited()


@pytest.mark.asyncio
async def test_work_order_updated_uses_updated_action_type() -> None:
    api = MagicMock()
    api.work_orders.get = AsyncMock(return_value=_work_order())

    with (
        patch("helixis.webhooks.handlers.work_order.get_pool", return_value="POOL"),
        patch(
            "helixis.webhooks.handlers.work_order.upsert_work_order",
            new_callable=AsyncMock,
        ),
        patch(
            "helixis.webhooks.handlers.work_order.write_activity",
            new_callable=AsyncMock,
        ) as log,
    ):
        await handle_work_order_updated(_event("WorkOrder.Updated", WorkOrderId=2041), api)

    entry: ActivityEntry = log.await_args.args[1]
    assert entry.action_type == "work_order_updated"


@pytest.mark.asyncio
async def test_work_order_handler_skips_on_missing_id() -> None:
    api = MagicMock()
    api.work_orders.get = AsyncMock()

    with (
        patch(
            "helixis.webhooks.handlers.work_order.upsert_work_order",
            new_callable=AsyncMock,
        ) as upsert,
    ):
        await handle_work_order_created(_event("WorkOrder.Created"), api)

    api.work_orders.get.assert_not_awaited()
    upsert.assert_not_awaited()


# ── Repository layer ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_write_activity_swallows_db_errors() -> None:
    """Activity logging is best-effort — a DB hiccup must not bubble up."""
    from helixis.db.repositories import write_activity

    # Pool.acquire() raises — we still shouldn't propagate.
    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=RuntimeError("db gone"))

    # Should not raise.
    await write_activity(
        pool,
        ActivityEntry(
            account_id=1,
            company_id="5ca1ab1e-0000-4000-8000-000000000004",
            source="webhook",
            action_type="x",
            description="y",
        ),
    )
