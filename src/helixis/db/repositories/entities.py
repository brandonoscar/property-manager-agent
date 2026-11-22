"""Upsert functions for Helixis-side entity mirrors.

Called from webhook handlers after a Buildium webhook is received + the
full entity is re-fetched from Buildium's REST API. Upserts keyed on
``buildium_id`` so Buildium redeliveries and repeat syncs don't create
duplicates.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import asyncpg

from helixis.buildium.linkage import task_property_id, work_order_task_id
from helixis.buildium.money import bill_amount, bill_is_paid
from helixis.buildium.schemas import (
    Applicant,
    BankAccount,
    Bill,
    GLAccount,
    Lease,
    LeaseTransaction,
    OutstandingBalance,
    Property,
    RentalOwner,
    Task,
    Tenant,
    Unit,
    Vendor,
    WorkOrder,
)

logger = logging.getLogger(__name__)


def _raw(model: Any) -> str:
    """Serialise a pydantic model to JSON for the ``raw`` jsonb column."""
    return json.dumps(model.model_dump(mode="json", by_alias=True))


# Sensitive leaves stripped from tenant ``raw`` before persist (sandbox-probe
# flag, 2026-07-05). RLS gates reads to company members, but SSN-class data
# (TaxId), DOB, and emergency-contact details don't belong at rest in a
# mirror whose source of truth is Buildium — no Helixis feature reads them.
_TENANT_RAW_STRIP_KEYS = ("TaxId", "DateOfBirth", "EmergencyContact")


def _raw_tenant(tenant: Any) -> str:
    """Tenant ``raw`` with the sensitive leaves removed."""
    dumped = tenant.model_dump(mode="json", by_alias=True)
    for key in _TENANT_RAW_STRIP_KEYS:
        dumped.pop(key, None)
    return json.dumps(dumped)


async def upsert_lease(
    pool: asyncpg.Pool,
    lease: Lease,
    *,
    account_id: int,
    company_id: str,
) -> None:
    """Upsert a lease mirror row keyed on ``buildium_id``.

    ⚠ ``security_deposit`` comes off the nested ``AccountDetails`` block, the
    same place ``rent`` lives (PR #74). It was parsed and stored nowhere for
    two months — the fetched-and-discarded shape of gotchas 41/50/51/53 — so
    "how much deposit is on this lease" was unanswerable while the number sat
    in ``raw``. Migration 0079 backfills what is already mirrored.

    It is the amount on the LEASE AGREEMENT, never a confirmation the money
    was received; the ledger is the authority for what is actually held.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO helixis_leases
                (buildium_id, account_id, company_id, property_id, unit_id, status,
                 lease_from_date, lease_to_date, rent, security_deposit, raw)
            VALUES ($1, $2, $3::uuid, $4, $5, $6, $7, $8, $9, $10, $11::jsonb)
            ON CONFLICT (company_id, buildium_id) DO UPDATE SET
                company_id       = EXCLUDED.company_id,
                property_id      = EXCLUDED.property_id,
                unit_id          = EXCLUDED.unit_id,
                status           = EXCLUDED.status,
                lease_from_date  = EXCLUDED.lease_from_date,
                lease_to_date    = EXCLUDED.lease_to_date,
                rent             = EXCLUDED.rent,
                security_deposit = EXCLUDED.security_deposit,
                raw              = EXCLUDED.raw,
                updated_at       = now()
            """,
            lease.id,
            account_id,
            company_id,
            lease.property_id,
            lease.unit_id,
            lease.status,
            lease.lease_from_date,
            lease.lease_to_date,
            lease.rent,
            lease.security_deposit,
            _raw(lease),
        )


async def upsert_work_order(
    pool: asyncpg.Pool,
    wo: WorkOrder,
    *,
    account_id: int,
    company_id: str,
) -> None:
    """Upsert a work-order mirror row keyed on ``buildium_id``.

    ⚠ **A work order has no property of its own.** ``GET /workorders`` sends
    neither a nested ``Property`` nor a usable flat ``PropertyId`` — both are
    null on every production row, so ``property_id`` landed NULL on all 60
    mirrored work orders while title/status/priority filled in fine. Every
    surface that groups maintenance by building (cockpit, operations, morning
    brief, turnovers, the reconciliation analyst) therefore saw the whole book
    as property-less.

    The linkage runs through the BACKING TASK (``WorkOrder.Task.Id``), which is
    the only portfolio reference the payload carries — so the property is
    resolved from ``helixis_tasks`` in this same statement. That costs no extra
    Buildium call: the backfill mirrors tasks before work orders, and a webhook
    that arrives out of order is repaired from the other side by
    ``upsert_task``. Verified on the live account before building: 60 of 60 work
    orders carry a task id, 60 resolve against the task mirror, 60 gain a
    property.

    ``task_id`` is stored as a real column rather than re-derived from ``raw``
    so both directions are an indexed lookup, and so a query never has to know
    the payload's shape (migration 0076).

    ⚠ The conflict clause COALESCEs ``property_id`` instead of overwriting.
    NULL here means "not resolved yet", not "has no property" — letting an
    unresolved upsert clobber a known property would make the column flicker
    with webhook ordering.

    Unit stays whatever the payload gives — it already flows through
    ``effective_unit_id`` and other companies' rows do carry it. It is absent on
    THIS account specifically (0 of 90 mirrored tasks carry a ``UnitId``, and the
    key is not even present in the raw body), so there is nothing to resolve it
    from: upstream reality, not a second dropped field.
    """
    task_id = work_order_task_id(wo)
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO helixis_work_orders
                (buildium_id, account_id, company_id, task_id, property_id, unit_id,
                 title, status, priority, category_id, assigned_to_user_id, raw)
            VALUES ($1, $2, $3::uuid, $4,
                    COALESCE(
                        $5::bigint,
                        (SELECT t.property_id
                           FROM helixis_tasks t
                          WHERE t.company_id  = $3::uuid
                            AND t.buildium_id = $4::bigint)
                    ),
                    $6, $7, $8, $9, $10, $11, $12::jsonb)
            ON CONFLICT (company_id, buildium_id) DO UPDATE SET
                company_id          = EXCLUDED.company_id,
                task_id             = COALESCE(EXCLUDED.task_id,
                                               helixis_work_orders.task_id),
                property_id         = COALESCE(EXCLUDED.property_id,
                                               helixis_work_orders.property_id),
                unit_id             = EXCLUDED.unit_id,
                title               = EXCLUDED.title,
                status              = EXCLUDED.status,
                priority            = EXCLUDED.priority,
                category_id         = EXCLUDED.category_id,
                assigned_to_user_id = EXCLUDED.assigned_to_user_id,
                raw                 = EXCLUDED.raw,
                updated_at          = now()
            """,
            wo.id,
            account_id,
            company_id,
            task_id,
            wo.property_id,
            # Probe 2026-07-05: top-level UnitId is NULL on real payloads —
            # the live linkage is Task.UnitId (see WorkOrder.effective_unit_id).
            wo.effective_unit_id,
            wo.title,
            wo.status,
            wo.priority,
            wo.category_id,
            wo.assigned_to_user_id,
            _raw(wo),
        )


# ── Cockpit entity mirror (migration 0038) ──────────────────────────────
# Same conventions as above: buildium_id conflict key, service-role writes,
# soft Buildium-id references (no hard FKs — upserts arrive out of order).


async def upsert_property(
    pool: asyncpg.Pool,
    prop: Property,
    *,
    account_id: int,
    company_id: str,
) -> None:
    """Upsert a property mirror row keyed on ``buildium_id``."""
    addr = prop.address
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO helixis_properties
                (buildium_id, account_id, company_id, name, rental_sub_type,
                 address_line1, city, state, postal_code, is_active, raw)
            VALUES ($1, $2, $3::uuid, $4, $5, $6, $7, $8, $9, $10, $11::jsonb)
            ON CONFLICT (company_id, buildium_id) DO UPDATE SET
                company_id      = EXCLUDED.company_id,
                name            = EXCLUDED.name,
                rental_sub_type = EXCLUDED.rental_sub_type,
                address_line1   = EXCLUDED.address_line1,
                city            = EXCLUDED.city,
                state           = EXCLUDED.state,
                postal_code     = EXCLUDED.postal_code,
                is_active       = EXCLUDED.is_active,
                raw             = EXCLUDED.raw,
                updated_at      = now()
            """,
            prop.id,
            account_id,
            company_id,
            prop.name,
            prop.rental_sub_type,
            addr.address_line1 if addr else None,
            addr.city if addr else None,
            addr.state if addr else None,
            addr.postal_code if addr else None,
            prop.is_active,
            _raw(prop),
        )


async def upsert_unit(
    pool: asyncpg.Pool,
    unit: Unit,
    *,
    account_id: int,
    company_id: str,
) -> None:
    """Upsert a unit mirror row keyed on ``buildium_id``."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO helixis_units
                (buildium_id, account_id, company_id, property_id, unit_number,
                 market_rent, is_occupied, raw)
            VALUES ($1, $2, $3::uuid, $4, $5, $6, $7, $8::jsonb)
            ON CONFLICT (company_id, buildium_id) DO UPDATE SET
                company_id  = EXCLUDED.company_id,
                property_id = EXCLUDED.property_id,
                unit_number = EXCLUDED.unit_number,
                market_rent = EXCLUDED.market_rent,
                is_occupied = EXCLUDED.is_occupied,
                raw         = EXCLUDED.raw,
                updated_at  = now()
            """,
            unit.id,
            account_id,
            company_id,
            unit.property_id,
            unit.unit_number,
            unit.market_rent,
            unit.is_occupied,
            _raw(unit),
        )


def _primary_phone(tenant: Tenant | Applicant | Vendor) -> str | None:
    """Best phone number on a tenant/applicant payload: prefer the Cell entry
    (probe 2026-07-05: PhoneNumbers is [{Number, Type}]), else the first
    non-empty number."""
    first: str | None = None
    for phone in tenant.phone_numbers or []:
        if not phone.number:
            continue
        if (phone.type or "").lower() == "cell":
            return phone.number
        if first is None:
            first = phone.number
    return first


async def upsert_tenant(
    pool: asyncpg.Pool,
    tenant: Tenant,
    *,
    account_id: int,
    company_id: str,
) -> None:
    """Upsert a tenant mirror row keyed on ``buildium_id``.

    Also refreshes the lease↔tenant join from the tenant payload's embedded
    ``Leases`` summaries (status only — ``move_in_date`` arrives via the
    lease-side sync when available).
    """
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO helixis_tenants
                (buildium_id, account_id, company_id, first_name, last_name,
                 email, phone, raw)
            VALUES ($1, $2, $3::uuid, $4, $5, $6, $7, $8::jsonb)
            ON CONFLICT (company_id, buildium_id) DO UPDATE SET
                company_id = EXCLUDED.company_id,
                first_name = EXCLUDED.first_name,
                last_name  = EXCLUDED.last_name,
                email      = EXCLUDED.email,
                phone      = EXCLUDED.phone,
                raw        = EXCLUDED.raw,
                updated_at = now()
            """,
            tenant.id,
            account_id,
            company_id,
            tenant.first_name,
            tenant.last_name,
            tenant.email,
            _primary_phone(tenant),
            _raw_tenant(tenant),
        )
        for summary in tenant.leases or []:
            await conn.execute(
                """
                INSERT INTO helixis_lease_tenants
                    (company_id, lease_buildium_id, tenant_buildium_id, status)
                VALUES ($1::uuid, $2, $3, $4)
                ON CONFLICT (company_id, lease_buildium_id, tenant_buildium_id)
                DO UPDATE SET
                    status     = EXCLUDED.status,
                    updated_at = now()
                """,
                company_id,
                summary.id,
                tenant.id,
                summary.status,
            )


async def refresh_lease_tenant_links(
    pool: asyncpg.Pool,
    lease: Lease,
    *,
    company_id: str,
) -> None:
    """Refresh the lease↔tenant join rows from a lease payload's ``Tenants``
    list — the lease side carries ``move_in_date``, which the tenant-side
    summaries don't."""
    async with pool.acquire() as conn:
        for ts in lease.tenants or []:
            await conn.execute(
                """
                INSERT INTO helixis_lease_tenants
                    (company_id, lease_buildium_id, tenant_buildium_id, status,
                     move_in_date)
                VALUES ($1::uuid, $2, $3, $4, $5)
                ON CONFLICT (company_id, lease_buildium_id, tenant_buildium_id)
                DO UPDATE SET
                    status       = EXCLUDED.status,
                    move_in_date = EXCLUDED.move_in_date,
                    updated_at   = now()
                """,
                company_id,
                lease.id,
                ts.id,
                ts.status,
                ts.move_in_date,
            )


async def upsert_lease_transaction(
    pool: asyncpg.Pool,
    txn: LeaseTransaction,
    *,
    account_id: int,
    company_id: str,
) -> None:
    """Upsert a lease-transaction mirror row keyed on ``buildium_id``."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO helixis_lease_transactions
                (buildium_id, account_id, company_id, lease_id,
                 transaction_date, transaction_type, total_amount, raw)
            VALUES ($1, $2, $3::uuid, $4, $5, $6, $7, $8::jsonb)
            ON CONFLICT (company_id, buildium_id) DO UPDATE SET
                company_id       = EXCLUDED.company_id,
                lease_id         = EXCLUDED.lease_id,
                transaction_date = EXCLUDED.transaction_date,
                transaction_type = EXCLUDED.transaction_type,
                total_amount     = EXCLUDED.total_amount,
                raw              = EXCLUDED.raw,
                updated_at       = now()
            """,
            txn.id,
            account_id,
            company_id,
            txn.lease_id,
            txn.date,
            txn.transaction_type,
            txn.total_amount,
            _raw(txn),
        )


async def replace_lease_balances(
    pool: asyncpg.Pool,
    balances: list[OutstandingBalance],
    *,
    account_id: int,
    company_id: str,
) -> int:
    """Wholesale-replace the company's outstanding-balance snapshot.

    ``/v1/leases/outstandingbalances`` is a point-in-time view — a lease that
    catches up on rent simply disappears from the response — so the mirror
    deletes the old snapshot and inserts the new one in a single transaction.
    Returns the number of rows in the new snapshot.
    """
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "DELETE FROM helixis_lease_balances WHERE company_id = $1::uuid",
            company_id,
        )
        for bal in balances:
            await conn.execute(
                """
                INSERT INTO helixis_lease_balances
                    (company_id, lease_id, account_id, property_id, unit_id,
                     total_balance, balance_0_30, balance_31_60, balance_61_90,
                     balance_over_90, snapshot_at, raw)
                VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10, now(),
                        $11::jsonb)
                ON CONFLICT (company_id, lease_id) DO UPDATE SET
                    property_id     = EXCLUDED.property_id,
                    unit_id         = EXCLUDED.unit_id,
                    total_balance   = EXCLUDED.total_balance,
                    balance_0_30    = EXCLUDED.balance_0_30,
                    balance_31_60   = EXCLUDED.balance_31_60,
                    balance_61_90   = EXCLUDED.balance_61_90,
                    balance_over_90 = EXCLUDED.balance_over_90,
                    snapshot_at     = now(),
                    raw             = EXCLUDED.raw
                """,
                company_id,
                bal.lease_id,
                account_id,
                bal.property_id,
                bal.unit_id,
                bal.total_balance,
                bal.balance_0_to_30_days,
                bal.balance_31_to_60_days,
                bal.balance_61_to_90_days,
                bal.balance_over_90_days,
                _raw(bal),
            )
    return len(balances)


async def upsert_applicant(
    pool: asyncpg.Pool,
    applicant: Applicant,
    *,
    account_id: int,
    company_id: str,
) -> None:
    """Upsert an applicant mirror row keyed on ``buildium_id``.

    The mirrored ``status`` (Buildium's applicant Status enum, verbatim) is
    what the CRM auto-move reads; the full payload rides in ``raw``.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO helixis_applicants
                (buildium_id, account_id, company_id, first_name, last_name,
                 email, phone, status, applicant_group_id, property_id,
                 unit_id, tenant_id, raw)
            VALUES ($1, $2, $3::uuid, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                    $13::jsonb)
            ON CONFLICT (company_id, buildium_id) DO UPDATE SET
                company_id         = EXCLUDED.company_id,
                first_name         = EXCLUDED.first_name,
                last_name          = EXCLUDED.last_name,
                email              = EXCLUDED.email,
                phone              = EXCLUDED.phone,
                status             = EXCLUDED.status,
                applicant_group_id = EXCLUDED.applicant_group_id,
                property_id        = EXCLUDED.property_id,
                unit_id            = EXCLUDED.unit_id,
                tenant_id          = EXCLUDED.tenant_id,
                raw                = EXCLUDED.raw,
                updated_at         = now()
            """,
            applicant.id,
            account_id,
            company_id,
            applicant.first_name,
            applicant.last_name,
            applicant.email,
            _primary_phone(applicant),
            applicant.status,
            applicant.applicant_group_id,
            applicant.property_id,
            applicant.unit_id,
            applicant.tenant_id,
            _raw(applicant),
        )


async def delete_applicant(pool: asyncpg.Pool, buildium_id: int, *, company_id: str) -> None:
    """Drop an applicant mirror row (Applicant.Deleted). Company-scoped so a
    routing mixup can never delete another company's row."""
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM helixis_applicants WHERE buildium_id = $1 AND company_id = $2::uuid",
            buildium_id,
            company_id,
        )


# ── Entity graph, slice 1 (migration 0041): vendors / owners / tasks ────
# Same PII policy as tenants: SSN-class identifiers never land at rest.

_VENDOR_RAW_STRIP_KEYS = ("TaxId", "AccountNumber")
_OWNER_RAW_STRIP_KEYS = ("TaxId", "DateOfBirth", "EmergencyContact")


def _raw_stripped(model: Any, keys: tuple[str, ...]) -> str:
    """Model ``raw`` with the given sensitive top-level leaves removed."""
    dumped = model.model_dump(mode="json", by_alias=True)
    for key in keys:
        dumped.pop(key, None)
    return json.dumps(dumped)


def _primary_phone_from_dump(dumped: dict[str, Any]) -> str | None:
    """``_primary_phone`` for payloads only typed permissively — reads the
    ``PhoneNumbers`` list of ``{Number, Type}`` dicts off a model dump."""
    first: str | None = None
    entries = dumped.get("PhoneNumbers")
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        number = entry.get("Number")
        if not number:
            continue
        if str(entry.get("Type") or "").lower() == "cell":
            return str(number)
        if first is None:
            first = str(number)
    return first


async def upsert_vendor(
    pool: asyncpg.Pool,
    vendor: Vendor,
    *,
    account_id: int,
    company_id: str,
) -> None:
    """Upsert a vendor mirror row keyed on ``buildium_id``."""
    category = vendor.category
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO helixis_vendors
                (buildium_id, account_id, company_id, name, is_company,
                 first_name, last_name, company_name, is_active, category_id,
                 category_name, phone, email, raw)
            VALUES ($1, $2, $3::uuid, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                    $13, $14::jsonb)
            ON CONFLICT (company_id, buildium_id) DO UPDATE SET
                company_id    = EXCLUDED.company_id,
                name          = EXCLUDED.name,
                is_company    = EXCLUDED.is_company,
                first_name    = EXCLUDED.first_name,
                last_name     = EXCLUDED.last_name,
                company_name  = EXCLUDED.company_name,
                is_active     = EXCLUDED.is_active,
                category_id   = EXCLUDED.category_id,
                category_name = EXCLUDED.category_name,
                phone         = EXCLUDED.phone,
                email         = EXCLUDED.email,
                raw           = EXCLUDED.raw,
                updated_at    = now()
            """,
            vendor.id,
            account_id,
            company_id,
            vendor.name or vendor.company_name,
            vendor.is_company,
            vendor.first_name,
            vendor.last_name,
            vendor.company_name,
            vendor.is_active,
            category.id if category else None,
            category.name if category else None,
            _primary_phone(vendor),
            vendor.primary_email,
            _raw_stripped(vendor, _VENDOR_RAW_STRIP_KEYS),
        )


async def delete_vendor(pool: asyncpg.Pool, buildium_id: int, *, company_id: str) -> None:
    """Drop a vendor mirror row (Vendor.Deleted). Company-scoped."""
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM helixis_vendors WHERE buildium_id = $1 AND company_id = $2::uuid",
            buildium_id,
            company_id,
        )


async def upsert_owner(
    pool: asyncpg.Pool,
    owner: RentalOwner,
    *,
    account_id: int,
    company_id: str,
) -> None:
    """Upsert a rental-owner mirror row keyed on ``buildium_id``.

    Owners have NO webhook events in Buildium's catalog — this is reached
    only by the backfill / daily resync, so owner data is at most a day
    stale (the hybrid-policy footer already communicates that).
    """
    dumped = owner.model_dump(mode="json", by_alias=True)
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO helixis_owners
                (buildium_id, account_id, company_id, first_name, last_name,
                 company_name, email, phone, property_ids, raw)
            VALUES ($1, $2, $3::uuid, $4, $5, $6, $7, $8, $9, $10::jsonb)
            ON CONFLICT (company_id, buildium_id) DO UPDATE SET
                company_id   = EXCLUDED.company_id,
                first_name   = EXCLUDED.first_name,
                last_name    = EXCLUDED.last_name,
                company_name = EXCLUDED.company_name,
                email        = EXCLUDED.email,
                phone        = EXCLUDED.phone,
                property_ids = EXCLUDED.property_ids,
                raw          = EXCLUDED.raw,
                updated_at   = now()
            """,
            owner.id,
            account_id,
            company_id,
            owner.first_name,
            owner.last_name,
            owner.company_name,
            owner.email,
            _primary_phone_from_dump(dumped),
            owner.property_ids,
            _raw_stripped(owner, _OWNER_RAW_STRIP_KEYS),
        )


def _task_due_date(dumped: dict[str, Any]) -> Any:
    """Parse ``DueDate`` (date or datetime string) into a date, else None."""
    from datetime import date

    value = dumped.get("DueDate")
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


async def upsert_task(
    pool: asyncpg.Pool,
    task: Task,
    *,
    account_id: int,
    company_id: str,
) -> None:
    """Upsert a task mirror row keyed on ``buildium_id``.

    The generic ``/tasks`` row (``AllTasksMessage``, spec-verified
    2026-07-05) carries TaskType / TaskStatus / Priority / DueDate /
    Property.Id / UnitId / AssignedToUserId / Category.Name. The typed
    ``Task`` schema is thin, so the hot columns read the permissive dump
    (with a ``Status`` fallback for subtype payloads that use it).
    """
    dumped = task.model_dump(mode="json", by_alias=True)
    category = dumped.get("Category")
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO helixis_tasks
                (buildium_id, account_id, company_id, task_type, title, status,
                 priority, due_date, property_id, unit_id, assigned_to_user_id,
                 category_name, raw)
            VALUES ($1, $2, $3::uuid, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                    $13::jsonb)
            ON CONFLICT (company_id, buildium_id) DO UPDATE SET
                company_id          = EXCLUDED.company_id,
                task_type           = EXCLUDED.task_type,
                title               = EXCLUDED.title,
                status              = EXCLUDED.status,
                priority            = EXCLUDED.priority,
                due_date            = EXCLUDED.due_date,
                property_id         = EXCLUDED.property_id,
                unit_id             = EXCLUDED.unit_id,
                assigned_to_user_id = EXCLUDED.assigned_to_user_id,
                category_name       = EXCLUDED.category_name,
                raw                 = EXCLUDED.raw,
                updated_at          = now()
            """,
            task.id,
            account_id,
            company_id,
            dumped.get("TaskType"),
            task.title,
            dumped.get("TaskStatus") or dumped.get("Status"),
            dumped.get("Priority"),
            _task_due_date(dumped),
            task_property_id(dumped),
            dumped.get("UnitId"),
            dumped.get("AssignedToUserId"),
            category.get("Name") if isinstance(category, dict) else None,
            _raw(task),
        )

        # The other half of the work-order property linkage. ``upsert_work_order``
        # resolves through the task mirror, which only works if the task landed
        # first — true in the backfill, not guaranteed for webhooks, where a
        # WorkOrder.Created can beat its own Task.Created. Repairing from this
        # side means arrival order stops mattering in either direction.
        #
        # Scoped to rows that would actually change, so the common case (task
        # touched, work orders already correct) writes nothing. A task with no
        # property resolves to NULL and is skipped by the IS NOT NULL guard,
        # for the same reason the upsert COALESCEs: unresolved must never
        # overwrite resolved.
        resolved_property = task_property_id(dumped)
        if resolved_property is not None:
            await conn.execute(
                """
                UPDATE helixis_work_orders
                   SET property_id = $3::bigint,
                       updated_at  = now()
                 WHERE company_id  = $1::uuid
                   AND task_id     = $2::bigint
                   AND property_id IS DISTINCT FROM $3::bigint
                """,
                company_id,
                task.id,
                resolved_property,
            )


async def delete_task(pool: asyncpg.Pool, buildium_id: int, *, company_id: str) -> None:
    """Drop a task mirror row (Task.Deleted). Company-scoped."""
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM helixis_tasks WHERE buildium_id = $1 AND company_id = $2::uuid",
            buildium_id,
            company_id,
        )


# ── Money mirror, slice 2 (migration 0042): GL accounts + bills ─────────


async def upsert_gl_account(
    pool: asyncpg.Pool,
    gl: GLAccount,
    *,
    account_id: int,
    company_id: str,
) -> None:
    """Upsert a chart-of-accounts row keyed on ``buildium_id``.

    GLAccount.* webhook events are acknowledged-only (rarely-changing
    reference data) — this is reached by the backfill / daily resync.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO helixis_gl_accounts
                (buildium_id, account_id, company_id, account_number, name,
                 type, sub_type, is_active, is_bank_account,
                 parent_gl_account_id, raw)
            VALUES ($1, $2, $3::uuid, $4, $5, $6, $7, $8, $9, $10, $11::jsonb)
            ON CONFLICT (company_id, buildium_id) DO UPDATE SET
                company_id           = EXCLUDED.company_id,
                account_number       = EXCLUDED.account_number,
                name                 = EXCLUDED.name,
                type                 = EXCLUDED.type,
                sub_type             = EXCLUDED.sub_type,
                is_active            = EXCLUDED.is_active,
                is_bank_account      = EXCLUDED.is_bank_account,
                parent_gl_account_id = EXCLUDED.parent_gl_account_id,
                raw                  = EXCLUDED.raw,
                updated_at           = now()
            """,
            gl.id,
            account_id,
            company_id,
            gl.account_number,
            gl.name,
            gl.type,
            gl.sub_type,
            gl.is_active,
            gl.is_bank_account,
            gl.parent_gl_account_id,
            _raw(gl),
        )


async def prune_gl_accounts(
    pool: asyncpg.Pool,
    keep_buildium_ids: list[int],
    *,
    company_id: str,
) -> None:
    """Drop chart rows whose buildium_id is no longer in the live chart.

    The GLAccount.* webhook envelope's id field isn't publicly documented,
    so the handler refreshes the WHOLE chart from the verified list
    endpoint and reconciles deletes here — exact without guessing keys.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            """
            DELETE FROM helixis_gl_accounts
            WHERE company_id = $1::uuid AND NOT (buildium_id = ANY($2::bigint[]))
            """,
            company_id,
            keep_buildium_ids,
        )


# Bank credentials never land at rest (0043): the wire carries account and
# routing numbers plus check-printing/e-payment config — all stripped.
_BANK_RAW_STRIP_KEYS = (
    "AccountNumber",
    "AccountNumberUnmasked",
    "RoutingNumber",
    "CheckPrintingInfo",
    "ElectronicPayments",
)


async def upsert_bank_account(
    pool: asyncpg.Pool,
    account: BankAccount,
    *,
    account_id: int,
    company_id: str,
) -> None:
    """Upsert a bank-account mirror row keyed on ``buildium_id``.

    Balance freshness rides BankAccount.Transaction.* webhooks (the handler
    refetches the account) + backfill/daily resync.
    """
    dumped = account.model_dump(mode="json", by_alias=True)
    gl_ref = dumped.get("GLAccount")
    gl_account_id = gl_ref.get("Id") if isinstance(gl_ref, dict) else None
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO helixis_bank_accounts
                (buildium_id, account_id, company_id, name, description,
                 bank_account_type, country, is_active, balance,
                 gl_account_id, raw)
            VALUES ($1, $2, $3::uuid, $4, $5, $6, $7, $8, $9, $10, $11::jsonb)
            ON CONFLICT (company_id, buildium_id) DO UPDATE SET
                company_id        = EXCLUDED.company_id,
                name              = EXCLUDED.name,
                description       = EXCLUDED.description,
                bank_account_type = EXCLUDED.bank_account_type,
                country           = EXCLUDED.country,
                is_active         = EXCLUDED.is_active,
                balance           = EXCLUDED.balance,
                gl_account_id     = EXCLUDED.gl_account_id,
                raw               = EXCLUDED.raw,
                updated_at        = now()
            """,
            account.id,
            account_id,
            company_id,
            account.name,
            account.description,
            account.bank_account_type,
            account.country,
            account.is_active,
            account.balance,
            gl_account_id,
            _raw_stripped(account, _BANK_RAW_STRIP_KEYS),
        )


async def prune_bank_accounts(
    pool: asyncpg.Pool,
    keep_buildium_ids: list[int],
    *,
    company_id: str,
) -> None:
    """Drop bank-account rows no longer present in the live list.

    BankAccount.* (table) events stay acknowledged-only (undocumented
    envelope id key), so deletes reconcile on backfill/daily resync.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            """
            DELETE FROM helixis_bank_accounts
            WHERE company_id = $1::uuid AND NOT (buildium_id = ANY($2::bigint[]))
            """,
            company_id,
            keep_buildium_ids,
        )


def _as_date(value: Any) -> Any:
    """datetime → date passthrough for date columns (Buildium ships bill
    dates as datetimes)."""
    from datetime import date
    from datetime import datetime as _dt

    if isinstance(value, _dt):
        return value.date()
    if isinstance(value, date):
        return value
    return None


async def upsert_bill(
    pool: asyncpg.Pool,
    bill: Bill,
    *,
    account_id: int,
    company_id: str,
) -> None:
    """Upsert a bill mirror row + replace its per-line detail.

    Lines carry the GL/property attribution that makes "spend on plumbing
    at Garden Row" a local SQL question; they're replace-on-upsert so a
    line edit in Buildium can't leave stale rows behind.
    """
    dumped = bill.model_dump(mode="json", by_alias=True)
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO helixis_bills
                (buildium_id, account_id, company_id, vendor_id, bill_date,
                 due_date, total_amount, is_paid, reference_number,
                 work_order_id, raw)
            VALUES ($1, $2, $3::uuid, $4, $5, $6, $7, $8, $9, $10, $11::jsonb)
            ON CONFLICT (company_id, buildium_id) DO UPDATE SET
                company_id       = EXCLUDED.company_id,
                vendor_id        = EXCLUDED.vendor_id,
                bill_date        = EXCLUDED.bill_date,
                due_date         = EXCLUDED.due_date,
                total_amount     = EXCLUDED.total_amount,
                is_paid          = EXCLUDED.is_paid,
                reference_number = EXCLUDED.reference_number,
                work_order_id    = EXCLUDED.work_order_id,
                raw              = EXCLUDED.raw,
                updated_at       = now()
            """,
            bill.id,
            account_id,
            company_id,
            bill.vendor_id,
            _as_date(bill.date),
            _as_date(bill.due_date),
            # NOT ``bill.total_amount`` — production bills don't carry one, so
            # every mirrored row landed NULL and ``unpaid_bills`` rendered the
            # whole payables book as $0.00 (battery v2, Q2). ``bill_amount``
            # falls back to the line items; it still returns None when there is
            # genuinely no number, so NULL keeps meaning "unknown".
            bill_amount(bill),
            # NOT ``bill.is_paid`` — production sends it NULL on every bill,
            # so the mirror could not tell paid from unpaid and reported all
            # 131 as outstanding. PaidDate is the real signal.
            bill_is_paid(bill),
            bill.reference_number,
            dumped.get("WorkOrderId"),
            _raw(bill),
        )
        await conn.execute(
            "DELETE FROM helixis_bill_lines WHERE company_id = $1::uuid AND bill_id = $2",
            company_id,
            bill.id,
        )
        for index, line in enumerate(bill.lines or []):
            await conn.execute(
                """
                INSERT INTO helixis_bill_lines
                    (company_id, bill_id, line_index, property_id, unit_id,
                     gl_account_id, amount, memo)
                VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8)
                """,
                company_id,
                bill.id,
                index,
                # ⚠ resolved_property_id, NOT line.property_id. Buildium sends
                # ``PropertyId`` as a key on every production line and a value
                # on none of them (131/131 present, 0/131 populated); the
                # association lives on ``AccountingEntity.Id``. Writing the raw
                # field here is why this column was NULL on 100% of rows and
                # why "which property costs the most in repairs" could not be
                # answered from a mirror that held the answer.
                line.resolved_property_id,
                line.unit_id,
                # ⚠ resolved_gl_account_id, NOT line.gl_account_id — the SAME
                # trap as the property association one line up, on the very
                # next column. ``GLAccountId`` is present on 131/131 production
                # lines and populated on 0; the category lives on the nested
                # ``GLAccount.Id`` (populated 131/131). Writing the raw field
                # is why every mirrored line has a NULL category and why
                # "where is my money going" answers "Uncategorized" for the
                # entire book.
                line.resolved_gl_account_id,
                line.amount,
                line.memo,
            )


async def delete_bill(pool: asyncpg.Pool, buildium_id: int, *, company_id: str) -> None:
    """Drop a bill mirror row + its lines (Bill.Deleted). Company-scoped."""
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM helixis_bill_lines WHERE company_id = $1::uuid AND bill_id = $2",
            company_id,
            buildium_id,
        )
        await conn.execute(
            "DELETE FROM helixis_bills WHERE buildium_id = $1 AND company_id = $2::uuid",
            buildium_id,
            company_id,
        )


# ── Mirror deletions (2026-08-29) ──────────────────────────────────────
#
# ⚠ **The mirror had no way to forget.** Sixteen ``*.Deleted`` webhook events
# are routed and only five removed anything: applicant, vendor, task, bill,
# plus the GL/bank prunes. Six entity types with a real mirror table wrote an
# activity line saying the record was deleted and left the row in place —
# including two handlers NAMED for it, ``handle_lease_deleted`` and
# ``handle_workorder_deleted``.
#
# Every mirror rollup reads these tables, so a deleted property stayed in
# ``portfolio_overview``, a deleted lease stayed in ``delinquency_report``, a
# deleted work order stayed in the maintenance counts. That is a confident
# FALSE PRESENCE — the mirror image of gotcha 26's false absence, and worse in
# one respect: an absence makes the manager go and look, while a presence makes
# them act on a record that is not there. Buildium is the source of truth
# (CLAUDE.md §2) and the mirror is a cache; a cache that cannot evict is not a
# cache.
#
# All are company-scoped (gotcha 47) and keyed the same way their upsert is, so
# a delete can never reach another tenant's row.


async def delete_property(pool: asyncpg.Pool, buildium_id: int, *, company_id: str) -> None:
    """Drop a property mirror row (Rental.Deleted). Company-scoped."""
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM helixis_properties WHERE buildium_id = $1 AND company_id = $2::uuid",
            buildium_id,
            company_id,
        )


async def delete_unit(pool: asyncpg.Pool, buildium_id: int, *, company_id: str) -> None:
    """Drop a unit mirror row (RentalUnit.Deleted). Company-scoped."""
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM helixis_units WHERE buildium_id = $1 AND company_id = $2::uuid",
            buildium_id,
            company_id,
        )


async def delete_lease(pool: asyncpg.Pool, buildium_id: int, *, company_id: str) -> None:
    """Drop a lease mirror row AND its tenant links (Lease.Deleted).

    ⚠ The link rows go too, in the same transaction. ``helixis_lease_tenants``
    keys on ``lease_buildium_id`` with no FK cascade, so leaving them behind
    would strand a join row pointing at a lease that no longer exists — and
    ``contact_lookup`` reads exactly that join to decide whether someone is a
    current tenant. Gotcha 51 is the same defect one field over: a person
    described as a resident on the strength of a lease record that is not
    what the reader thinks it is.
    """
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "DELETE FROM helixis_lease_tenants "
            "WHERE lease_buildium_id = $1 AND company_id = $2::uuid",
            buildium_id,
            company_id,
        )
        await conn.execute(
            "DELETE FROM helixis_leases WHERE buildium_id = $1 AND company_id = $2::uuid",
            buildium_id,
            company_id,
        )


async def delete_work_order(pool: asyncpg.Pool, buildium_id: int, *, company_id: str) -> None:
    """Drop a work-order mirror row (WorkOrder.Deleted). Company-scoped."""
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM helixis_work_orders WHERE buildium_id = $1 AND company_id = $2::uuid",
            buildium_id,
            company_id,
        )


async def delete_lease_transaction(
    pool: asyncpg.Pool, buildium_id: int, *, company_id: str
) -> None:
    """Drop a lease-ledger row (LeaseTransaction.Deleted). Company-scoped.

    A stale ledger row is money: it feeds the balance arithmetic behind
    ``delinquency_report``, so keeping a deleted charge means telling a
    manager a resident owes something they do not.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM helixis_lease_transactions "
            "WHERE buildium_id = $1 AND company_id = $2::uuid",
            buildium_id,
            company_id,
        )


async def delete_lease_tenant_link(
    pool: asyncpg.Pool,
    *,
    lease_buildium_id: int,
    tenant_buildium_id: int,
    company_id: str,
) -> None:
    """Drop ONE lease↔tenant join row (LeaseTenant.Deleted). Company-scoped.

    Deliberately keyed on both ids: a tenant can hold several leases and a
    lease several tenants, so deleting by either alone would remove links the
    event said nothing about.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            """
            DELETE FROM helixis_lease_tenants
             WHERE lease_buildium_id = $1
               AND tenant_buildium_id = $2
               AND company_id = $3::uuid
            """,
            lease_buildium_id,
            tenant_buildium_id,
            company_id,
        )
