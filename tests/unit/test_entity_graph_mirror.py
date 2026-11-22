"""Entity graph, slice 1 (migration 0041) — vendors / owners / tasks mirror.

Pins: the PII strip on vendor/owner raw (TaxId-class leaves never land at
rest), the task hot-column extraction from the permissive dump (TaskStatus
with a Status fallback, Property.Id, DueDate parse), and the task-handler
mirror wiring (upsert on the refetch, delete on Task.Deleted).
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helixis.buildium.schemas import RentalOwner, Task, Vendor
from helixis.db.repositories import entities


class _CapturingConn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, sql: str, *args: Any) -> None:
        self.calls.append((sql, args))


def _pool(conn: _CapturingConn) -> MagicMock:
    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    return pool


@pytest.mark.asyncio
async def test_vendor_upsert_strips_tax_id_and_extracts_hot_columns() -> None:
    vendor = Vendor.model_validate(
        {
            "Id": 70239,
            "Name": "Apex Plumbing",
            "IsCompany": True,
            "CompanyName": "Apex Plumbing LLC",
            "IsActive": True,
            "Category": {"Id": 4, "Name": "Plumbing"},
            "PhoneNumbers": [{"Number": "+15550001111", "Type": "Cell"}],
            "PrimaryEmail": "office@apex.example",
            "TaxId": "12-3456789",
            "AccountNumber": "ACCT-9",
        }
    )
    conn = _CapturingConn()
    await entities.upsert_vendor(_pool(conn), vendor, account_id=1, company_id="co-1")

    sql, args = conn.calls[0]
    assert "INSERT INTO helixis_vendors" in sql
    raw = json.loads(args[-1])
    # SSN-class leaves never land at rest (same policy as tenants).
    assert "TaxId" not in raw and "AccountNumber" not in raw
    # Hot columns: category + cell phone + email extracted.
    assert 4 in args and "Plumbing" in args
    assert "+15550001111" in args and "office@apex.example" in args


@pytest.mark.asyncio
async def test_owner_upsert_strips_tax_id_and_reads_phone_from_dump() -> None:
    owner = RentalOwner.model_validate(
        {
            "Id": 88,
            "FirstName": "Dana",
            "LastName": "Owner",
            "Email": "dana@example.com",
            "PropertyIds": [10282, 10283],
            "PhoneNumbers": [{"Number": "+15552223333", "Type": "Cell"}],
            "TaxId": "98-7654321",
        }
    )
    conn = _CapturingConn()
    await entities.upsert_owner(_pool(conn), owner, account_id=1, company_id="co-1")

    sql, args = conn.calls[0]
    assert "INSERT INTO helixis_owners" in sql
    raw = json.loads(args[-1])
    assert "TaxId" not in raw
    # Phone comes off the permissive dump (RentalOwner has no typed field).
    assert "+15552223333" in args
    assert [10282, 10283] in args


@pytest.mark.asyncio
async def test_task_upsert_extracts_spec_fields_with_status_fallback() -> None:
    task = Task.model_validate(
        {
            "Id": 21856,
            "TaskType": "Todo",
            "Title": "Fix the door",
            "TaskStatus": "New",
            "Priority": "Normal",
            "DueDate": "2026-07-20T00:00:00",
            "Property": {"Id": 10282, "Type": "Rental"},
            "UnitId": 55,
            "AssignedToUserId": 70237,
            "Category": {"Id": 3, "Name": "Maintenance"},
        }
    )
    conn = _CapturingConn()
    await entities.upsert_task(_pool(conn), task, account_id=1, company_id="co-1")

    sql, args = conn.calls[0]
    assert "INSERT INTO helixis_tasks" in sql
    assert "Todo" in args and "New" in args and "Normal" in args
    assert 10282 in args and 55 in args and 70237 in args and "Maintenance" in args
    from datetime import date

    assert date(2026, 7, 20) in args

    # Subtype payloads that use "Status" instead of "TaskStatus" still map.
    task2 = Task.model_validate({"Id": 2, "Title": "x", "Status": "InProgress"})
    conn2 = _CapturingConn()
    await entities.upsert_task(_pool(conn2), task2, account_id=1, company_id="co-1")
    assert "InProgress" in conn2.calls[0][1]


@pytest.mark.asyncio
async def test_task_handler_mirrors_on_refetch_and_deletes_on_deleted() -> None:
    from helixis.webhooks.handlers import maintenance as maintenance_h

    @asynccontextmanager
    async def _yield_api(api):
        yield api

    api = MagicMock()
    fetched = MagicMock()
    fetched.title = "Fix the door"
    api.tasks.get = AsyncMock(return_value=fetched)

    envelope: dict[str, Any] = {
        "EventName": "Task.Updated",
        "AccountId": 42,
        "TaskId": 21856,
    }
    with (
        patch.object(maintenance_h, "open_api_for_company", return_value=_yield_api(api)),
        patch.object(maintenance_h, "get_pool", return_value=object()),
        patch.object(maintenance_h, "write_activity", new=AsyncMock()),
        patch.object(maintenance_h.entities_repo, "upsert_task", new=AsyncMock()) as upsert,
        patch.object(maintenance_h.entities_repo, "delete_task", new=AsyncMock()) as delete,
    ):
        await maintenance_h.handle_task_changed("co-1", envelope)
    upsert.assert_awaited_once()
    assert upsert.await_args.args[1] is fetched
    delete.assert_not_awaited()

    envelope["EventName"] = "Task.Deleted"
    with (
        patch.object(maintenance_h, "get_pool", return_value=object()),
        patch.object(maintenance_h, "write_activity", new=AsyncMock()),
        patch.object(maintenance_h.entities_repo, "upsert_task", new=AsyncMock()) as upsert,
        patch.object(maintenance_h.entities_repo, "delete_task", new=AsyncMock()) as delete,
    ):
        await maintenance_h.handle_task_changed("co-1", envelope)
    upsert.assert_not_awaited()
    delete.assert_awaited_once()
    assert delete.await_args.args[1] == 21856
