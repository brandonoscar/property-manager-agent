"""Read-side queries for the Operations page — tenants, vendors, team.

The Operations hub (frontend OperationsPage) is four sortable tables where
the COLUMNS are the analytics: who's slow, who's expensive, who's behind.
The Properties tab rides the existing cockpit summary
(``repositories/cockpit.py``); this module adds the other three, all
company-scoped, read-only, and answered entirely from the 0038+ entity
mirror — zero live Buildium calls on the hot path.

Grounding notes (probed, not assumed):

* Work orders carry their vendor only inside ``raw`` — the mirror has no
  typed ``vendor_id`` column — so vendor attribution extracts it from the
  PMS's own JSON (digits-guarded before the cast): Buildium's ``VendorId``,
  Rentvine's ``vendorContactID`` (see :data:`_RENTVINE_VENDOR_KEY`).
* Buildium exposes NO work-order created date (schemas.py:214 probe), so
  "response time" uses the mirror's first sighting in
  ``helixis_work_order_status_history`` (0058, append-only) as the start
  and the first ``Completed`` transition as the end. That makes the metric
  forward-looking from when 0058 landed — label it "avg days to complete",
  never "since reported".
* Open-task vocabulary matches cockpit.py: NOT IN Completed/Closed/Cancelled.

⚠ **``None`` means "this company's PMS doesn't send it", never zero or
empty** (same rule as ``cockpit.py``). Rentvine sends no bills, balances,
lease-tenant links or staff, so the tenants and team lists are ``None`` for
it, and a vendor's spend is ``None`` rather than $0.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import asyncpg

from helixis.buildium.maintenance_status import open_status_sql
from helixis.db.pms_coverage import BUILDIUM, RENTVINE, coverage_for
from helixis.db.tenant_links import current_link_sql


@dataclass
class VendorOpsRow:
    """One vendor with the numbers a manager compares vendors by."""

    buildium_id: int
    name: str | None
    category_name: str | None
    is_active: bool | None
    phone: str | None
    email: str | None
    #: ``None`` when work orders can't be attributed to vendors for this PMS.
    open_work_orders: int | None = 0
    total_work_orders: int | None = 0
    jobs_90d: int | None = 0
    avg_days_to_complete: float | None = None
    #: ``None`` when the PMS sends no bills.
    spend_12m: float | None = 0.0
    bill_count_12m: int | None = 0
    last_bill_date: date | None = None


@dataclass
class TenantOpsRow:
    """One tenant on one ACTIVE lease (a tenant with two units = two rows —
    that is the honest operational view, not a modeling accident)."""

    tenant_buildium_id: int
    first_name: str | None
    last_name: str | None
    email: str | None
    phone: str | None
    property_buildium_id: int | None
    property_name: str | None
    unit_number: str | None
    lease_buildium_id: int
    lease_to_date: date | None
    rent: float | None
    #: ``None`` when the PMS sends no lease balances.
    balance: float | None = 0.0
    oldest_bucket: str | None = None  # '0-30' | '31-60' | '61-90' | '90+'


@dataclass
class TeamOpsRow:
    """One Buildium staff member and their open workload."""

    buildium_user_id: int
    first_name: str | None
    last_name: str | None
    email: str | None
    user_role_name: str | None
    is_active: bool | None
    open_work_orders: int = 0
    open_tasks: int = 0
    completed_tasks_7d: int = 0


#: Where a Rentvine work order names its vendor: the vendor's ``contactID``,
#: which is what Rentvine vendors are mirrored under (``mapper.map_vendor``).
#:
#: **Measured on a live row, 2026-09-24** (rule 8): the seed script's smoke run
#: created a work order with ``vendorContactID`` 36 on demoaccount41 and
#: ``GET /maintenance/work-orders/{id}`` read back ``workOrder.vendorContactID``
#: '36'. That was the single-record read. The LIST the sync mirrors is still
#: known only from T1's example (``withVendorAssigned`` carries the key,
#: ``noVendorAssigned`` carries it as ``null``), so :func:`_rentvine_key_present`
#: keeps checking the mirrored rows until Phase 8 syncs one.
_RENTVINE_VENDOR_KEY = "vendorContactID"


#: The ``raw`` key each system keeps a work order's vendor id under. A system
#: not listed keeps none in the mirror (Propertyware's API has no vendor on a
#: work order; Rent Manager's mapper reads one on bills only), so its vendor
#: is unknown, never "none" and never Buildium's key read from its payload.
WORK_ORDER_VENDOR_KEY: dict[str, str] = {
    "buildium": "VendorId",
    "rentvine": _RENTVINE_VENDOR_KEY,
}


def vendor_ref_sql(alias: str) -> str:
    """The vendor id a work order carries, as TEXT, or NULL where its system
    keeps none (:data:`WORK_ORDER_VENDOR_KEY`).

    ONE home for where each system puts it (gotcha 25): the Operations vendor
    rollup and the chat's ``open_work_orders`` both read it. Guard with
    ``~ '^[0-9]+$'`` before casting, or compare as text.
    """
    whens = " ".join(
        f"WHEN {alias}.pms = '{pms}' THEN {alias}.raw->>'{key}'"
        for pms, key in WORK_ORDER_VENDOR_KEY.items()
    )
    return f"CASE {whens} ELSE NULL END"


def vendor_key_present_sql(alias: str) -> str:
    """Does the row carry its PMS's vendor key at all (a null value counts:
    that is "no vendor assigned", a real answer)? False for a system that
    keeps no vendor key: there the vendor is unknown."""
    whens = " ".join(
        f"WHEN {alias}.pms = '{pms}' THEN {alias}.raw ? '{key}'"
        for pms, key in WORK_ORDER_VENDOR_KEY.items()
    )
    return f"CASE {whens} ELSE false END"


_VENDORS_SQL = f"""
WITH wo_vendor AS (
    SELECT wo.buildium_id,
           wo.status,
           {vendor_ref_sql("wo")} AS vendor_ref
      FROM helixis_work_orders wo
     WHERE wo.company_id = $1::uuid
),
vendor_wos AS (
    SELECT vendor_ref::bigint AS vendor_id, buildium_id, status
      FROM wo_vendor
     WHERE vendor_ref ~ '^[0-9]+$'
),
wo_times AS (
    SELECT h.work_order_buildium_id,
           MIN(h.occurred_at) AS first_seen,
           MIN(h.occurred_at) FILTER (WHERE h.status = 'Completed') AS completed_at
      FROM helixis_work_order_status_history h
     WHERE h.company_id = $1::uuid
     GROUP BY 1
)
SELECT
    v.buildium_id,
    v.name,
    v.category_name,
    v.is_active,
    v.phone,
    v.email,
    COALESCE(w.open_wos, 0)::int       AS open_work_orders,
    COALESCE(w.total_wos, 0)::int      AS total_work_orders,
    COALESCE(w.jobs_90d, 0)::int       AS jobs_90d,
    w.avg_days_to_complete,
    COALESCE(b.spend_12m, 0)           AS spend_12m,
    COALESCE(b.bill_count_12m, 0)::int AS bill_count_12m,
    b.last_bill_date
FROM helixis_vendors v
LEFT JOIN (
    SELECT vw.vendor_id,
           COUNT(*) FILTER (WHERE {open_status_sql("vw.status")}) AS open_wos,
           COUNT(*) AS total_wos,
           COUNT(*) FILTER (
               WHERE t.first_seen >= now() - interval '90 days'
           ) AS jobs_90d,
           AVG(EXTRACT(EPOCH FROM (t.completed_at - t.first_seen)) / 86400.0)
               FILTER (
                   WHERE t.completed_at IS NOT NULL
                     AND t.completed_at > t.first_seen
               ) AS avg_days_to_complete
      FROM vendor_wos vw
      LEFT JOIN wo_times t ON t.work_order_buildium_id = vw.buildium_id
     GROUP BY vw.vendor_id
) w ON w.vendor_id = v.buildium_id
LEFT JOIN (
    SELECT vendor_id,
           SUM(total_amount) FILTER (
               WHERE bill_date >= now() - interval '12 months'
           ) AS spend_12m,
           COUNT(*) FILTER (
               WHERE bill_date >= now() - interval '12 months'
           ) AS bill_count_12m,
           MAX(bill_date) AS last_bill_date
      FROM helixis_bills
     WHERE company_id = $1::uuid AND vendor_id IS NOT NULL
     GROUP BY vendor_id
) b ON b.vendor_id = v.buildium_id
WHERE v.company_id = $1::uuid
ORDER BY COALESCE(b.spend_12m, 0) DESC, v.name NULLS LAST
"""


#: The Tenants tab: the people on active leases NOW, so current links only
#: (``db.tenant_links``).
_TENANTS_SQL = f"""
SELECT
    t.buildium_id                    AS tenant_buildium_id,
    t.first_name,
    t.last_name,
    t.email,
    t.phone,
    l.property_id                    AS property_buildium_id,
    p.name                           AS property_name,
    u.unit_number,
    l.buildium_id                    AS lease_buildium_id,
    l.lease_to_date,
    l.rent,
    COALESCE(b.total_balance, 0)     AS balance,
    CASE
        WHEN COALESCE(b.balance_over_90, 0) > 0 THEN '90+'
        WHEN COALESCE(b.balance_61_90, 0)  > 0 THEN '61-90'
        WHEN COALESCE(b.balance_31_60, 0)  > 0 THEN '31-60'
        WHEN COALESCE(b.balance_0_30, 0)   > 0 THEN '0-30'
        ELSE NULL
    END AS oldest_bucket
FROM helixis_lease_tenants lt
JOIN helixis_leases l
  ON l.buildium_id = lt.lease_buildium_id AND l.company_id = lt.company_id
JOIN helixis_tenants t
  ON t.buildium_id = lt.tenant_buildium_id AND t.company_id = lt.company_id
LEFT JOIN helixis_properties p
  ON p.buildium_id = l.property_id AND p.company_id = l.company_id
LEFT JOIN helixis_units u
  ON u.buildium_id = l.unit_id AND u.company_id = l.company_id
LEFT JOIN helixis_lease_balances b
  ON b.lease_id = l.buildium_id AND b.company_id = l.company_id
WHERE lt.company_id = $1::uuid AND l.status = 'Active'
  AND {current_link_sql()}
ORDER BY COALESCE(b.total_balance, 0) DESC, t.last_name NULLS LAST, t.buildium_id
"""


_TEAM_SQL = f"""
SELECT
    s.buildium_user_id,
    s.first_name,
    s.last_name,
    s.email,
    s.user_role_name,
    s.is_active,
    COALESCE(w.open_wos, 0)::int    AS open_work_orders,
    COALESCE(tk.open_tasks, 0)::int AS open_tasks,
    COALESCE(tk.done_7d, 0)::int    AS completed_tasks_7d
FROM buildium_staff s
LEFT JOIN (
    SELECT assigned_to_user_id,
           COUNT(*) FILTER (WHERE {open_status_sql("status")}) AS open_wos
      FROM helixis_work_orders
     WHERE company_id = $1::uuid AND assigned_to_user_id IS NOT NULL
     GROUP BY 1
) w ON w.assigned_to_user_id = s.buildium_user_id
LEFT JOIN (
    SELECT assigned_to_user_id,
           COUNT(*) FILTER (
               WHERE {open_status_sql("status")}
           ) AS open_tasks,
           COUNT(*) FILTER (
               WHERE status = 'Completed'
                 AND updated_at >= now() - interval '7 days'
           ) AS done_7d
      FROM helixis_tasks
     WHERE company_id = $1::uuid AND assigned_to_user_id IS NOT NULL
     GROUP BY 1
) tk ON tk.assigned_to_user_id = s.buildium_user_id
WHERE s.company_id = $1::uuid AND 'Staff' = ANY(s.user_types)
ORDER BY s.is_active DESC,
         (COALESCE(w.open_wos, 0) + COALESCE(tk.open_tasks, 0)) DESC,
         s.last_name NULLS LAST
"""


def _f(value: Any) -> float:
    """numeric → float (asyncpg yields Decimal for numeric columns)."""
    return float(value) if value is not None else 0.0


_RENTVINE_KEY_SQL = f"""
SELECT COUNT(*) AS work_orders,
       COUNT(*) FILTER (WHERE raw ? '{_RENTVINE_VENDOR_KEY}') AS keyed
  FROM helixis_work_orders
 WHERE company_id = $1::uuid AND pms = 'rentvine'
"""  # noqa: S608 — a module constant, never caller input


async def _rentvine_key_present(conn: Any, company_id: str) -> bool:
    """Can this company's Rentvine work orders be attributed to vendors?

    True when the rows carry the vendor key (null means "no vendor assigned",
    a real answer), or when there are no work orders at all (every vendor
    really has zero). False when there are rows and NONE carry it: the key is
    wrong or Rentvine changed it, and counting would show every vendor as
    never used.
    """
    row = await conn.fetchrow(_RENTVINE_KEY_SQL, company_id)
    if row is None:
        return False
    return bool(row["work_orders"] == 0 or row["keyed"] > 0)


async def list_vendor_ops(pool: asyncpg.Pool, company_id: str) -> list[VendorOpsRow]:
    cov = await coverage_for(pool, company_id)
    bills = cov.sees("helixis_bills")
    # Jobs-in-90-days and days-to-complete are measured from the status
    # history (0058), which only Buildium's webhooks write.
    history = cov.sees("helixis_work_order_status_history")
    async with pool.acquire() as conn:
        # ⚠ Unknown PMS attributes nothing: counting on a guessed key shows
        # every vendor as never used.
        if cov.pms == BUILDIUM:
            attributed = True
        elif cov.pms == RENTVINE:
            attributed = await _rentvine_key_present(conn, company_id)
        else:
            attributed = False
        rows = await conn.fetch(_VENDORS_SQL, company_id)
    return [
        VendorOpsRow(
            buildium_id=r["buildium_id"],
            name=r["name"],
            category_name=r["category_name"],
            is_active=r["is_active"],
            phone=r["phone"],
            email=r["email"],
            open_work_orders=r["open_work_orders"] if attributed else None,
            total_work_orders=r["total_work_orders"] if attributed else None,
            jobs_90d=r["jobs_90d"] if attributed and history else None,
            avg_days_to_complete=(
                float(r["avg_days_to_complete"])
                if attributed and history and r["avg_days_to_complete"] is not None
                else None
            ),
            spend_12m=_f(r["spend_12m"]) if bills else None,
            bill_count_12m=r["bill_count_12m"] if bills else None,
            last_bill_date=r["last_bill_date"] if bills else None,
        )
        for r in rows
    ]


async def list_tenant_ops(pool: asyncpg.Pool, company_id: str) -> list[TenantOpsRow] | None:
    """Tenants on active leases, or ``None`` when the PMS sends no lease links.

    The rows are reached through ``helixis_lease_tenants``; without it the
    query returns nothing, which would read as "no tenants".
    """
    cov = await coverage_for(pool, company_id)
    if not cov.sees("helixis_lease_tenants"):
        return None
    balances = cov.sees("helixis_lease_balances")
    async with pool.acquire() as conn:
        rows = await conn.fetch(_TENANTS_SQL, company_id)
    return [
        TenantOpsRow(
            tenant_buildium_id=r["tenant_buildium_id"],
            first_name=r["first_name"],
            last_name=r["last_name"],
            email=r["email"],
            phone=r["phone"],
            property_buildium_id=r["property_buildium_id"],
            property_name=r["property_name"],
            unit_number=r["unit_number"],
            lease_buildium_id=r["lease_buildium_id"],
            lease_to_date=r["lease_to_date"],
            rent=(float(r["rent"]) if r["rent"] is not None else None),
            balance=_f(r["balance"]) if balances else None,
            oldest_bucket=r["oldest_bucket"] if balances else None,
        )
        for r in rows
    ]


async def list_team_ops(pool: asyncpg.Pool, company_id: str) -> list[TeamOpsRow] | None:
    """Staff with their workload, or ``None`` when the PMS sends no staff list."""
    if not (await coverage_for(pool, company_id)).sees("buildium_staff"):
        return None
    async with pool.acquire() as conn:
        rows = await conn.fetch(_TEAM_SQL, company_id)
    return [
        TeamOpsRow(
            buildium_user_id=r["buildium_user_id"],
            first_name=r["first_name"],
            last_name=r["last_name"],
            email=r["email"],
            user_role_name=r["user_role_name"],
            is_active=r["is_active"],
            open_work_orders=r["open_work_orders"],
            open_tasks=r["open_tasks"],
            completed_tasks_7d=r["completed_tasks_7d"],
        )
        for r in rows
    ]
