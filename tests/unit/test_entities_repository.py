"""Coverage for the entity-mirror upserts (db/repositories/entities.py).

These run from webhook handlers after a Buildium entity is re-fetched, and the
bug-prone part is the column→positional-param mapping in the big INSERT … ON
CONFLICT statements. This pins that mapping deterministically: the asyncpg pool
is mocked, so no database — we assert the exact args each upsert sends to
``conn.execute``.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from helixis.db.repositories import entities


class _AcquireCM:
    """Stand-in for ``pool.acquire()`` — an async context manager yielding conn."""

    def __init__(self, conn: object) -> None:
        self._conn = conn

    async def __aenter__(self) -> object:
        return self._conn

    async def __aexit__(self, *_a: object) -> bool:
        return False


def _pool_and_conn() -> tuple[MagicMock, MagicMock]:
    conn = MagicMock()
    conn.execute = AsyncMock()
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AcquireCM(conn))
    return pool, conn


def test_raw_serializes_with_by_alias() -> None:
    model = MagicMock()
    model.model_dump = MagicMock(return_value={"Id": 7, "Status": "Active"})
    assert entities._raw(model) == json.dumps({"Id": 7, "Status": "Active"})
    model.model_dump.assert_called_once_with(mode="json", by_alias=True)


@pytest.mark.asyncio
async def test_upsert_lease_maps_columns_to_params_in_order() -> None:
    pool, conn = _pool_and_conn()
    lease = MagicMock()
    lease.id = 101
    lease.property_id = 5
    lease.unit_id = 9
    lease.status = "Active"
    lease.lease_from_date = "2026-01-01"
    lease.lease_to_date = "2026-12-31"
    lease.rent = 2400
    lease.security_deposit = 1800
    lease.model_dump = MagicMock(return_value={"Id": 101})

    await entities.upsert_lease(pool, lease, account_id=7, company_id="co-1")

    conn.execute.assert_awaited_once()
    args = conn.execute.await_args.args
    sql = args[0]
    assert "helixis_leases" in sql
    assert "ON CONFLICT (company_id, buildium_id)" in sql
    # $1..$11 in declared column order: buildium_id, account_id, company_id,
    # property_id, unit_id, status, from, to, rent, security_deposit, raw.
    assert args[1:] == (
        101,
        7,
        "co-1",
        5,
        9,
        "Active",
        "2026-01-01",
        "2026-12-31",
        2400,
        1800,
        json.dumps({"Id": 101}),
    )


@pytest.mark.asyncio
async def test_upsert_work_order_maps_columns_to_params_in_order() -> None:
    pool, conn = _pool_and_conn()
    wo = MagicMock()
    wo.id = 5501
    wo.property_id = 5
    wo.unit_id = 9
    wo.title = "Leaky faucet"
    wo.status = "New"
    wo.priority = "High"
    wo.category_id = 3
    wo.assigned_to_user_id = 88
    wo.effective_unit_id = 9  # derived accessor (top-level or Task.UnitId)
    wo.task = MagicMock(id=39544)
    wo.model_dump = MagicMock(return_value={"Id": 5501})

    await entities.upsert_work_order(pool, wo, account_id=7, company_id="co-1")

    conn.execute.assert_awaited_once()
    args = conn.execute.await_args.args
    sql = args[0]
    assert "helixis_work_orders" in sql
    assert "ON CONFLICT (company_id, buildium_id)" in sql
    # $1..$12: buildium_id, account_id, company_id, task_id, property_id,
    # unit_id, title, status, priority, category_id, assigned_to_user_id, raw.
    assert args[1:] == (
        5501,
        7,
        "co-1",
        39544,
        5,
        9,
        "Leaky faucet",
        "New",
        "High",
        3,
        88,
        json.dumps({"Id": 5501}),
    )


# ── Cockpit mirror upserts (migration 0038) ─────────────────────────────


class _TxnCM:
    """Stand-in for ``conn.transaction()``."""

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_a: object) -> bool:
        return False


@pytest.mark.asyncio
async def test_upsert_property_maps_columns_and_flattens_address() -> None:
    pool, conn = _pool_and_conn()
    prop = MagicMock()
    prop.id = 700
    prop.name = "Maple Court"
    prop.rental_sub_type = "MultiFamily"
    prop.address = MagicMock(
        address_line1="12 Maple St", city="Boston", state="MA", postal_code="02101"
    )
    prop.is_active = True
    prop.model_dump = MagicMock(return_value={"Id": 700})

    await entities.upsert_property(pool, prop, account_id=7, company_id="co-1")

    args = conn.execute.await_args.args
    assert "helixis_properties" in args[0]
    assert "ON CONFLICT (company_id, buildium_id)" in args[0]
    assert args[1:] == (
        700,
        7,
        "co-1",
        "Maple Court",
        "MultiFamily",
        "12 Maple St",
        "Boston",
        "MA",
        "02101",
        True,
        json.dumps({"Id": 700}),
    )


@pytest.mark.asyncio
async def test_upsert_property_tolerates_missing_address() -> None:
    pool, conn = _pool_and_conn()
    prop = MagicMock()
    prop.id = 701
    prop.name = "No Address Yet"
    prop.rental_sub_type = None
    prop.address = None
    prop.is_active = None
    prop.model_dump = MagicMock(return_value={"Id": 701})

    await entities.upsert_property(pool, prop, account_id=7, company_id="co-1")

    args = conn.execute.await_args.args
    assert args[6:10] == (None, None, None, None)


@pytest.mark.asyncio
async def test_upsert_unit_maps_columns_in_order() -> None:
    pool, conn = _pool_and_conn()
    unit = MagicMock()
    unit.id = 801
    unit.property_id = 700
    unit.unit_number = "4B"
    unit.market_rent = 2100.0
    unit.is_occupied = True
    unit.model_dump = MagicMock(return_value={"Id": 801})

    await entities.upsert_unit(pool, unit, account_id=7, company_id="co-1")

    args = conn.execute.await_args.args
    assert "helixis_units" in args[0]
    assert args[1:] == (801, 7, "co-1", 700, "4B", 2100.0, True, json.dumps({"Id": 801}))


@pytest.mark.asyncio
async def test_upsert_tenant_writes_row_and_lease_join() -> None:
    pool, conn = _pool_and_conn()
    tenant = MagicMock()
    tenant.id = 901
    tenant.first_name = "Ada"
    tenant.last_name = "Lovelace"
    tenant.email = "ada@example.com"
    phone = MagicMock()
    phone.number = "555-0100"
    tenant.phone_numbers = [phone]
    summary = MagicMock()
    summary.id = 101
    summary.status = "Active"
    tenant.leases = [summary]
    tenant.model_dump = MagicMock(return_value={"Id": 901})

    await entities.upsert_tenant(pool, tenant, account_id=7, company_id="co-1")

    # Two executes: the tenant row, then one join row per embedded lease.
    assert conn.execute.await_count == 2
    tenant_args = conn.execute.await_args_list[0].args
    assert "helixis_tenants" in tenant_args[0]
    assert tenant_args[1:] == (
        901,
        7,
        "co-1",
        "Ada",
        "Lovelace",
        "ada@example.com",
        "555-0100",
        json.dumps({"Id": 901}),
    )
    join_args = conn.execute.await_args_list[1].args
    assert "helixis_lease_tenants" in join_args[0]
    assert join_args[1:] == ("co-1", 101, 901, "Active")


@pytest.mark.asyncio
async def test_upsert_tenant_no_phone_no_leases() -> None:
    pool, conn = _pool_and_conn()
    tenant = MagicMock()
    tenant.id = 902
    tenant.first_name = "No"
    tenant.last_name = "Phone"
    tenant.email = None
    tenant.phone_numbers = None
    tenant.leases = None
    tenant.model_dump = MagicMock(return_value={"Id": 902})

    await entities.upsert_tenant(pool, tenant, account_id=7, company_id="co-1")

    assert conn.execute.await_count == 1
    assert conn.execute.await_args.args[7] is None  # phone param


@pytest.mark.asyncio
async def test_upsert_lease_transaction_maps_columns_in_order() -> None:
    pool, conn = _pool_and_conn()
    txn = MagicMock()
    txn.id = 3001
    txn.lease_id = 101
    txn.date = "2026-07-01"
    txn.transaction_type = "Payment"
    txn.total_amount = 2400.0
    txn.model_dump = MagicMock(return_value={"Id": 3001})

    await entities.upsert_lease_transaction(pool, txn, account_id=7, company_id="co-1")

    args = conn.execute.await_args.args
    assert "helixis_lease_transactions" in args[0]
    assert args[1:] == (
        3001,
        7,
        "co-1",
        101,
        "2026-07-01",
        "Payment",
        2400.0,
        json.dumps({"Id": 3001}),
    )


@pytest.mark.asyncio
async def test_replace_lease_balances_deletes_then_inserts_snapshot() -> None:
    pool, conn = _pool_and_conn()
    conn.transaction = MagicMock(return_value=_TxnCM())
    bal = MagicMock()
    bal.lease_id = 101
    bal.property_id = 700
    bal.unit_id = 801
    bal.total_balance = 350.0
    bal.balance_0_to_30_days = 200.0
    bal.balance_31_to_60_days = 150.0
    bal.balance_61_to_90_days = 0.0
    bal.balance_over_90_days = 0.0
    bal.model_dump = MagicMock(return_value={"LeaseId": 101})

    count = await entities.replace_lease_balances(pool, [bal], account_id=7, company_id="co-1")

    assert count == 1
    assert conn.execute.await_count == 2
    delete_sql = conn.execute.await_args_list[0].args[0]
    assert "DELETE FROM helixis_lease_balances" in delete_sql
    insert_args = conn.execute.await_args_list[1].args
    assert "helixis_lease_balances" in insert_args[0]
    assert insert_args[1:] == (
        "co-1",
        101,
        7,
        700,
        801,
        350.0,
        200.0,
        150.0,
        0.0,
        0.0,
        json.dumps({"LeaseId": 101}),
    )


@pytest.mark.asyncio
async def test_replace_lease_balances_empty_clears_snapshot() -> None:
    pool, conn = _pool_and_conn()
    conn.transaction = MagicMock(return_value=_TxnCM())

    count = await entities.replace_lease_balances(pool, [], account_id=7, company_id="co-1")

    assert count == 0
    assert conn.execute.await_count == 1  # DELETE only — everyone paid up


# ── Probe-driven hardening (2026-07-05 sandbox verification) ────────────


def test_tenant_raw_strips_sensitive_leaves() -> None:
    """TaxId / DateOfBirth / EmergencyContact never reach the raw column."""
    tenant = MagicMock()
    tenant.model_dump = MagicMock(
        return_value={
            "Id": 901,
            "FirstName": "Ada",
            "TaxId": "123-45-6789",
            "DateOfBirth": "1990-01-01",
            "EmergencyContact": {"Name": "X", "Phone": "555"},
            "Email": "ada@example.com",
        }
    )
    raw = entities._raw_tenant(tenant)
    assert "TaxId" not in raw
    assert "DateOfBirth" not in raw
    assert "EmergencyContact" not in raw
    # Non-sensitive leaves survive.
    assert "FirstName" in raw and "Email" in raw


def test_primary_phone_prefers_cell() -> None:
    home = MagicMock()
    home.number = "555-0001"
    home.type = "Home"
    cell = MagicMock()
    cell.number = "555-0002"
    cell.type = "Cell"
    tenant = MagicMock()
    tenant.phone_numbers = [home, cell]
    assert entities._primary_phone(tenant) == "555-0002"


def test_primary_phone_falls_back_to_first() -> None:
    home = MagicMock()
    home.number = "555-0001"
    home.type = "Home"
    tenant = MagicMock()
    tenant.phone_numbers = [home]
    assert entities._primary_phone(tenant) == "555-0001"


def test_work_order_effective_unit_id_falls_back_to_task() -> None:
    """Real payloads (probe 2026-07-05): top-level UnitId is null; Task.UnitId
    carries the linkage."""
    from helixis.buildium.schemas import WorkOrder

    wo = WorkOrder.model_validate({"Id": 1, "UnitId": None, "Task": {"Id": 9, "UnitId": 42}})
    assert wo.effective_unit_id == 42
    top = WorkOrder.model_validate({"Id": 2, "UnitId": 7, "Task": {"UnitId": 42}})
    assert top.effective_unit_id == 7
    neither = WorkOrder.model_validate({"Id": 3})
    assert neither.effective_unit_id is None
