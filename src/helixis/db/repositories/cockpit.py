"""Read-side queries for the property cockpit (Workstream A, master #5).

Computes the starter-six metrics from the 0038 entity mirror — instant local
SQL instead of multi-call Buildium orchestration under the 10 req/s limit:

  vacancy · open work orders · lease expirations (90d) · delinquency ·
  rent roll (scheduled vs collected) · tenants + property basics

All queries are company-scoped and read-only. "Occupied" means a unit with at
least one Active lease in the mirror; "collected" sums Payment transactions in
the current calendar month.

⚠ **``None`` means "this company's PMS doesn't send it", never zero.** Rentvine
fills no lease balances, ledger, tasks or lease-tenant links
(``db/pms_coverage.py``), so for a Rentvine company those tables are empty and
a plain query answers "$0 delinquent", "nothing collected", "no tenants".
Every field read from such a table is ``None`` when the company's PMS doesn't
fill it, and ``pms_name`` lets the page say which system to check. A failed
PMS lookup declines the same way (gotcha 55).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import asyncpg

from helixis.buildium.maintenance_status import open_status_sql
from helixis.buildium.money import payments_received_sql
from helixis.db.pms_coverage import Coverage, coverage_for
from helixis.db.tenant_links import current_link_sql

EXPIRING_WINDOW_DAYS = 90


@dataclass
class PropertySummary:
    """One cockpit-list row."""

    buildium_id: int
    name: str | None
    address_line1: str | None
    city: str | None
    state: str | None
    unit_count: int = 0
    occupied_count: int = 0
    open_work_orders: int = 0
    expiring_leases_90d: int = 0
    #: ``None`` when the company's PMS sends no lease balances.
    delinquent_leases: int | None = 0
    delinquent_total: float | None = 0.0
    #: ⚠ LAST, after every older field: tests build this positionally.
    #: Units with no Active lease whose occupancy the PMS never stated
    #: (``is_occupied IS NULL``: Rentvine's units completed from the units
    #: export carry no lease link, gotcha 169) or that the PMS flags OCCUPIED
    #: while our mirror holds no Active lease for them (4 Buildium units,
    #: 2026-10-02). Neither occupied NOR vacant —
    #: folding them into vacant called a 120-unit property "fully vacant" with
    #: 100 unknown (sandbox, 2026-10-02; rule 14).
    occupancy_unknown_count: int = 0

    @property
    def vacant_count(self) -> int:
        """Units KNOWN to be vacant: never the unknown ones. The one home for
        this subtraction; every reader (chat tools, API) uses it."""
        return max(0, self.unit_count - self.occupied_count - self.occupancy_unknown_count)


@dataclass
class PropertyDetail:
    """The full starter-six picture for one property."""

    summary: PropertySummary
    #: ``None`` when Active leases exist and NONE has a known rent: a rent roll
    #: nobody knows is not a rent roll of zero (rule 14). ``0.0`` only when the
    #: property has no Active lease at all.
    rent_scheduled: float | None = 0.0
    #: Active leases whose rent is unknown, so a partly-known roll says it is.
    rent_unknown_leases: int = 0
    #: ``None`` when the PMS sends no ledger.
    rent_collected_mtd: float | None = 0.0
    #: ``None`` when the PMS sends no lease-tenant links.
    tenants: list[dict[str, Any]] | None = field(default_factory=list)
    open_work_order_list: list[dict[str, Any]] = field(default_factory=list)
    expiring_lease_list: list[dict[str, Any]] = field(default_factory=list)
    #: ``None`` when the PMS sends no lease balances.
    delinquency_list: list[dict[str, Any]] | None = field(default_factory=list)
    pms_name: str | None = None


async def list_units_for_property(
    pool: asyncpg.Pool, company_id: str, property_buildium_id: int
) -> list[dict[str, Any]]:
    """All mirrored units for one property, ordered by unit number.

    Feeds property_snapshot's unit roster so "what units are at X?" is a
    mirror read — before this, the agent's only unit-listing path was the
    live API (the Riverside Townhomes transcript, 2026-07-24)."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT buildium_id, unit_number, market_rent, is_occupied
              FROM helixis_units
             WHERE company_id = $1::uuid AND property_id = $2
             ORDER BY unit_number NULLS LAST, buildium_id
            """,
            company_id,
            property_buildium_id,
        )
    return [dict(r) for r in rows]


@dataclass
class UnitDetail:
    """One unit's operational picture: lease, tenants, money, maintenance."""

    unit: dict[str, Any]
    current_lease: dict[str, Any] | None = None
    #: ``None`` when the PMS sends no lease-tenant links.
    tenants: list[dict[str, Any]] | None = field(default_factory=list)
    #: ``None`` either way when there is no balance row, so read
    #: ``balances_sent`` to tell "owes nothing" from "the PMS doesn't say".
    balance: dict[str, Any] | None = None
    balances_sent: bool = True
    open_work_orders: list[dict[str, Any]] = field(default_factory=list)
    recent_work_orders: list[dict[str, Any]] = field(default_factory=list)
    #: ``None`` when the PMS sends no tasks.
    open_tasks: list[dict[str, Any]] | None = field(default_factory=list)
    pms_name: str | None = None


@dataclass
class TenantDetail:
    """One tenant's picture: leases (with balances), payments, maintenance."""

    tenant: dict[str, Any]
    #: ``None`` when the PMS sends no lease-tenant links, which every list
    #: here is reached through.
    leases: list[dict[str, Any]] | None = field(default_factory=list)
    #: ``None`` when the PMS sends no ledger (or no lease-tenant links).
    recent_transactions: list[dict[str, Any]] | None = field(default_factory=list)
    open_work_orders: list[dict[str, Any]] | None = field(default_factory=list)
    #: ``False`` when each lease's ``total_balance`` is unknown, not zero.
    balances_sent: bool = True
    pms_name: str | None = None


_SUMMARY_SQL = f"""
SELECT
    p.buildium_id,
    p.name,
    p.address_line1,
    p.city,
    p.state,
    COALESCE(u.unit_count, 0)        AS unit_count,
    COALESCE(o.occupied_count, 0)    AS occupied_count,
    COALESCE(q.unknown_count, 0)     AS occupancy_unknown_count,
    COALESCE(w.open_wos, 0)          AS open_work_orders,
    COALESCE(e.expiring, 0)          AS expiring_leases_90d,
    COALESCE(b.delinquent_count, 0)  AS delinquent_leases,
    COALESCE(b.delinquent_total, 0)  AS delinquent_total
FROM helixis_properties p
LEFT JOIN (
    SELECT property_id, COUNT(*) AS unit_count
      FROM helixis_units WHERE company_id = $1::uuid GROUP BY property_id
) u ON u.property_id = p.buildium_id
LEFT JOIN (
    SELECT property_id, COUNT(DISTINCT unit_id) AS occupied_count
      FROM helixis_leases
     WHERE company_id = $1::uuid AND status = 'Active' AND unit_id IS NOT NULL
     GROUP BY property_id
) o ON o.property_id = p.buildium_id
LEFT JOIN (
    -- Occupancy UNKNOWN: no Active lease, and the PMS either never stated the
    -- flag (NULL) or says OCCUPIED (TRUE) — our lease data and the PMS then
    -- disagree, so the unit is neither known occupied nor known vacant (rule 14).
    -- Only a FALSE flag with no Active lease is vacant. A unit with an Active
    -- lease is occupied whatever the flag says, so it is counted above.
    -- ⚠ Measured 2026-10-02 (read-only, helixis-test, every company): 4 units
    -- flagged occupied with no Active lease, all Buildium (2 + 2 in two
    -- companies); they had been counted vacant. Rentvine: 0.
    SELECT qu.property_id, COUNT(*) AS unknown_count
      FROM helixis_units qu
     WHERE qu.company_id = $1::uuid AND qu.is_occupied IS NOT FALSE
       AND NOT EXISTS (
             SELECT 1 FROM helixis_leases ql
              WHERE ql.company_id = qu.company_id AND ql.unit_id = qu.buildium_id
                AND ql.status = 'Active')
     GROUP BY qu.property_id
) q ON q.property_id = p.buildium_id
LEFT JOIN (
    -- Probe 2026-07-05: real WO payloads carry NULL top-level PropertyId;
    -- the mirror stores Task.UnitId in unit_id, so attribute WOs to a
    -- property through the unit when the direct column is null.
    SELECT COALESCE(wo.property_id, wu.property_id) AS property_id,
           COUNT(*) AS open_wos
      FROM helixis_work_orders wo
      LEFT JOIN helixis_units wu
        ON wu.buildium_id = wo.unit_id AND wu.company_id = wo.company_id
     WHERE wo.company_id = $1::uuid AND {open_status_sql("wo.status")}
     GROUP BY 1
) w ON w.property_id = p.buildium_id
LEFT JOIN (
    SELECT property_id, COUNT(*) AS expiring
      FROM helixis_leases
     WHERE company_id = $1::uuid AND status = 'Active'
       AND lease_to_date BETWEEN now() AND now() + make_interval(days => $2)
     GROUP BY property_id
) e ON e.property_id = p.buildium_id
LEFT JOIN (
    SELECT property_id,
           COUNT(*) AS delinquent_count,
           SUM(COALESCE(total_balance, 0)) AS delinquent_total
      FROM helixis_lease_balances
     WHERE company_id = $1::uuid
     GROUP BY property_id
) b ON b.property_id = p.buildium_id
WHERE p.company_id = $1::uuid
"""


def _summary_from_record(r: asyncpg.Record, cov: Coverage) -> PropertySummary:
    balances = cov.sees("helixis_lease_balances")
    return PropertySummary(
        buildium_id=r["buildium_id"],
        name=r["name"],
        address_line1=r["address_line1"],
        city=r["city"],
        state=r["state"],
        unit_count=r["unit_count"],
        occupied_count=r["occupied_count"],
        occupancy_unknown_count=r["occupancy_unknown_count"],
        open_work_orders=r["open_work_orders"],
        expiring_leases_90d=r["expiring_leases_90d"],
        delinquent_leases=r["delinquent_leases"] if balances else None,
        delinquent_total=float(r["delinquent_total"] or 0) if balances else None,
    )


async def list_property_summaries(pool: asyncpg.Pool, company_id: str) -> list[PropertySummary]:
    """All properties with per-property metric rollups, name-ordered."""
    cov = await coverage_for(pool, company_id)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            _SUMMARY_SQL + " ORDER BY p.name NULLS LAST",
            company_id,
            EXPIRING_WINDOW_DAYS,
        )
    return [_summary_from_record(r, cov) for r in rows]


async def get_property_detail(
    pool: asyncpg.Pool, company_id: str, buildium_property_id: int
) -> PropertyDetail | None:
    """The starter-six detail for one property, or None if not mirrored."""
    cov = await coverage_for(pool, company_id)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            _SUMMARY_SQL + " AND p.buildium_id = $3",
            company_id,
            EXPIRING_WINDOW_DAYS,
            buildium_property_id,
        )
        if row is None:
            return None
        detail = PropertyDetail(summary=_summary_from_record(row, cov), pms_name=cov.name)

        # ⚠ No COALESCE to 0. It made a property whose Active leases carried no
        # rent (every Rentvine lease, before the sync read `rentAmount`) show
        # "Rent roll $0" — an unknown rendered as zero (rule 14, 2026-10-01).
        rent = await conn.fetchrow(
            """
            SELECT SUM(rent) AS scheduled, COUNT(*) AS active, COUNT(rent) AS priced
              FROM helixis_leases
             WHERE company_id = $1::uuid AND property_id = $2 AND status = 'Active'
            """,
            company_id,
            buildium_property_id,
        )
        active = int(rent["active"] or 0)
        priced = int(rent["priced"] or 0)
        detail.rent_unknown_leases = active - priced
        if active and not priced:
            detail.rent_scheduled = None
        else:
            detail.rent_scheduled = float(rent["scheduled"] or 0)

        # ⚠ The sign is load-bearing and this query had it backwards. A ledger
        # row is signed by its effect on the BALANCE, so a payment is negative
        # (measured: Payment 144/144 negative, Charge 619/619 positive) — a
        # bare SUM here rendered "$X collected this month" as a NEGATIVE
        # figure, in the property cockpit and in the agent's property
        # snapshot. ``reconciliation/analyst.py`` already negated and
        # explained why; the rule now lives once in ``buildium/money.py`` so
        # a third caller cannot re-derive it wrongly.
        if not cov.sees("helixis_lease_transactions"):
            detail.rent_collected_mtd = None
        else:
            collected = await conn.fetchrow(
                f"""
                SELECT {payments_received_sql()} AS collected
                  FROM helixis_lease_transactions t
                  JOIN helixis_leases l
                    ON l.buildium_id = t.lease_id AND l.company_id = t.company_id
                 WHERE t.company_id = $1::uuid
                   AND l.property_id = $2
                   AND t.transaction_type = 'Payment'
                   AND t.transaction_date >= date_trunc('month', now())
                """,  # noqa: S608 — no interpolation of caller input; see money.py
                company_id,
                buildium_property_id,
            )
            detail.rent_collected_mtd = float(collected["collected"] or 0)

        if not cov.sees("helixis_lease_tenants"):
            detail.tenants = None
        else:
            # The people living at the property now: current links only
            # (``db.tenant_links``), never a co-tenant who moved out of a lease
            # the others still hold.
            detail.tenants = [
                dict(r)
                for r in await conn.fetch(
                    f"""
                    SELECT DISTINCT t.buildium_id, t.first_name, t.last_name,
                           l.unit_id, u.unit_number
                      FROM helixis_tenants t
                      JOIN helixis_lease_tenants lt
                        ON lt.tenant_buildium_id = t.buildium_id
                       AND lt.company_id = t.company_id
                      JOIN helixis_leases l
                        ON l.buildium_id = lt.lease_buildium_id
                       AND l.company_id = lt.company_id
                      LEFT JOIN helixis_units u
                        ON u.buildium_id = l.unit_id AND u.company_id = l.company_id
                     WHERE t.company_id = $1::uuid
                       AND l.property_id = $2
                       AND l.status = 'Active'
                       AND {current_link_sql()}
                     ORDER BY u.unit_number NULLS LAST, t.last_name NULLS LAST
                    """,
                    company_id,
                    buildium_property_id,
                )
            ]

        detail.open_work_order_list = [
            dict(r)
            for r in await conn.fetch(
                f"""
                SELECT wo.buildium_id, wo.title, wo.status, wo.priority,
                       wo.unit_id, wo.updated_at
                  FROM helixis_work_orders wo
                  LEFT JOIN helixis_units wu
                    ON wu.buildium_id = wo.unit_id
                   AND wu.company_id = wo.company_id
                 WHERE wo.company_id = $1::uuid
                   AND COALESCE(wo.property_id, wu.property_id) = $2
                   AND {open_status_sql("wo.status")}
                 ORDER BY wo.updated_at DESC
                 LIMIT 25
                """,
                company_id,
                buildium_property_id,
            )
        ]

        detail.expiring_lease_list = [
            dict(r)
            for r in await conn.fetch(
                """
                SELECT l.buildium_id, l.unit_id, u.unit_number, l.lease_to_date, l.rent
                  FROM helixis_leases l
                  LEFT JOIN helixis_units u
                    ON u.buildium_id = l.unit_id AND u.company_id = l.company_id
                 WHERE l.company_id = $1::uuid AND l.property_id = $2
                   AND l.status = 'Active'
                   AND l.lease_to_date BETWEEN now()
                       AND now() + make_interval(days => $3)
                 ORDER BY l.lease_to_date
                """,
                company_id,
                buildium_property_id,
                EXPIRING_WINDOW_DAYS,
            )
        ]

        if not cov.sees("helixis_lease_balances"):
            detail.delinquency_list = None
        else:
            detail.delinquency_list = [
                dict(r)
                for r in await conn.fetch(
                    """
                    SELECT b.lease_id, b.unit_id, u.unit_number, b.total_balance,
                           b.snapshot_at
                      FROM helixis_lease_balances b
                      LEFT JOIN helixis_units u
                        ON u.buildium_id = b.unit_id AND u.company_id = b.company_id
                     WHERE b.company_id = $1::uuid AND b.property_id = $2
                     ORDER BY b.total_balance DESC NULLS LAST
                    """,
                    company_id,
                    buildium_property_id,
                )
            ]

    return detail


async def get_unit_detail(
    pool: asyncpg.Pool, company_id: str, unit_buildium_id: int
) -> UnitDetail | None:
    """One unit's detail from the mirror, or None if not mirrored."""
    cov = await coverage_for(pool, company_id)
    async with pool.acquire() as conn:
        unit_row = await conn.fetchrow(
            """
            SELECT u.buildium_id, u.unit_number, u.market_rent, u.is_occupied,
                   u.property_id, p.name AS property_name
              FROM helixis_units u
              LEFT JOIN helixis_properties p
                ON p.buildium_id = u.property_id AND p.company_id = u.company_id
             WHERE u.company_id = $1::uuid AND u.buildium_id = $2
            """,
            company_id,
            unit_buildium_id,
        )
        if unit_row is None:
            return None
        detail = UnitDetail(
            unit=dict(unit_row),
            balances_sent=cov.sees("helixis_lease_balances"),
            pms_name=cov.name,
        )
        if not cov.sees("helixis_lease_tenants"):
            detail.tenants = None

        lease_row = await conn.fetchrow(
            """
            SELECT buildium_id, status, lease_from_date, lease_to_date, rent
              FROM helixis_leases
             WHERE company_id = $1::uuid AND unit_id = $2 AND status = 'Active'
             ORDER BY lease_from_date DESC
             LIMIT 1
            """,
            company_id,
            unit_buildium_id,
        )
        if lease_row is not None:
            detail.current_lease = dict(lease_row)
            lease_id = lease_row["buildium_id"]

            if detail.tenants is not None:
                # The unit's tenants now: current links only.
                detail.tenants = [
                    dict(r)
                    for r in await conn.fetch(
                        f"""
                    SELECT t.buildium_id, t.first_name, t.last_name,
                           t.email, t.phone, lt.move_in_date
                      FROM helixis_tenants t
                      JOIN helixis_lease_tenants lt
                        ON lt.tenant_buildium_id = t.buildium_id
                       AND lt.company_id = t.company_id
                     WHERE lt.lease_buildium_id = $2
                       AND t.company_id = $1::uuid
                       AND {current_link_sql()}
                     ORDER BY t.last_name NULLS LAST
                    """,
                        company_id,
                        lease_id,
                    )
                ]

            if detail.balances_sent:
                balance_row = await conn.fetchrow(
                    """
                    SELECT total_balance, balance_0_30, balance_31_60,
                           balance_61_90, balance_over_90, snapshot_at
                      FROM helixis_lease_balances
                     WHERE company_id = $1::uuid AND lease_id = $2
                    """,
                    company_id,
                    lease_id,
                )
                detail.balance = dict(balance_row) if balance_row is not None else None

        detail.open_work_orders = [
            dict(r)
            for r in await conn.fetch(
                f"""
                SELECT buildium_id, title, status, priority, updated_at
                  FROM helixis_work_orders
                 WHERE company_id = $1::uuid AND unit_id = $2
                   AND {open_status_sql("status")}
                 ORDER BY updated_at DESC
                 LIMIT 25
                """,
                company_id,
                unit_buildium_id,
            )
        ]

        detail.recent_work_orders = [
            dict(r)
            for r in await conn.fetch(
                """
                SELECT buildium_id, title, status, priority, updated_at
                  FROM helixis_work_orders
                 WHERE company_id = $1::uuid AND unit_id = $2
                 ORDER BY updated_at DESC
                 LIMIT 15
                """,
                company_id,
                unit_buildium_id,
            )
        ]

        if not cov.sees("helixis_tasks"):
            detail.open_tasks = None
            return detail
        detail.open_tasks = [
            dict(r)
            for r in await conn.fetch(
                f"""
                SELECT buildium_id, title, status, priority, due_date,
                       category_name
                  FROM helixis_tasks
                 WHERE company_id = $1::uuid AND unit_id = $2
                   AND {open_status_sql("status")}
                 ORDER BY due_date NULLS LAST
                 LIMIT 10
                """,
                company_id,
                unit_buildium_id,
            )
        ]

    return detail


async def get_tenant_detail(
    pool: asyncpg.Pool, company_id: str, tenant_buildium_id: int
) -> TenantDetail | None:
    """One tenant's detail from the mirror, or None if not mirrored."""
    cov = await coverage_for(pool, company_id)
    async with pool.acquire() as conn:
        tenant_row = await conn.fetchrow(
            """
            SELECT buildium_id, first_name, last_name, email, phone
              FROM helixis_tenants
             WHERE company_id = $1::uuid AND buildium_id = $2
            """,
            company_id,
            tenant_buildium_id,
        )
        if tenant_row is None:
            return None
        detail = TenantDetail(
            tenant=dict(tenant_row),
            balances_sent=cov.sees("helixis_lease_balances"),
            pms_name=cov.name,
        )
        # Every list below is reached through the tenant's lease links.
        if not cov.sees("helixis_lease_tenants"):
            detail.leases = None
            detail.recent_transactions = None
            detail.open_work_orders = None
            return detail

        detail.leases = [
            dict(r)
            for r in await conn.fetch(
                """
                SELECT l.buildium_id, l.status, l.lease_from_date,
                       l.lease_to_date, l.rent, l.unit_id, u.unit_number,
                       p.name AS property_name, lt.move_in_date,
                       b.total_balance
                  FROM helixis_lease_tenants lt
                  JOIN helixis_leases l
                    ON l.buildium_id = lt.lease_buildium_id
                   AND l.company_id = lt.company_id
                  LEFT JOIN helixis_units u
                    ON u.buildium_id = l.unit_id AND u.company_id = l.company_id
                  LEFT JOIN helixis_properties p
                    ON p.buildium_id = l.property_id
                   AND p.company_id = l.company_id
                  LEFT JOIN helixis_lease_balances b
                    ON b.lease_id = l.buildium_id AND b.company_id = l.company_id
                 WHERE lt.tenant_buildium_id = $2 AND lt.company_id = $1::uuid
                 ORDER BY (l.status = 'Active') DESC, l.lease_from_date DESC
                """,
                company_id,
                tenant_buildium_id,
            )
        ]

        if not detail.balances_sent:
            for lease in detail.leases or []:
                lease["total_balance"] = None

        detail.recent_transactions = (
            None
            if not cov.sees("helixis_lease_transactions")
            else [
                dict(r)
                for r in await conn.fetch(
                    """
                SELECT t.transaction_date, t.transaction_type,
                       t.total_amount, t.lease_id
                  FROM helixis_lease_transactions t
                  JOIN helixis_lease_tenants lt
                    ON lt.lease_buildium_id = t.lease_id
                   AND lt.company_id = t.company_id
                 WHERE lt.tenant_buildium_id = $2 AND t.company_id = $1::uuid
                 ORDER BY t.transaction_date DESC
                 LIMIT 15
                """,
                    company_id,
                    tenant_buildium_id,
                )
            ]
        )

        # The units the tenant lives in now: a current link on an active lease.
        # The two lists above are their history and read every link.
        detail.open_work_orders = [
            dict(r)
            for r in await conn.fetch(
                f"""
                SELECT wo.buildium_id, wo.title, wo.status, wo.priority,
                       wo.updated_at, wo.unit_id
                  FROM helixis_work_orders wo
                 WHERE wo.company_id = $1::uuid
                   AND {open_status_sql("wo.status")}
                   AND wo.unit_id IN (
                        SELECT l.unit_id
                          FROM helixis_leases l
                          JOIN helixis_lease_tenants lt
                            ON lt.lease_buildium_id = l.buildium_id
                           AND lt.company_id = l.company_id
                         WHERE lt.tenant_buildium_id = $2
                           AND l.company_id = $1::uuid
                           AND l.status = 'Active'
                           AND {current_link_sql()}
                           AND l.unit_id IS NOT NULL
                   )
                 ORDER BY wo.updated_at DESC
                 LIMIT 10
                """,
                company_id,
                tenant_buildium_id,
            )
        ]

    return detail


__all__ = [
    "EXPIRING_WINDOW_DAYS",
    "PropertyDetail",
    "PropertySummary",
    "TenantDetail",
    "UnitDetail",
    "get_property_detail",
    "get_tenant_detail",
    "get_unit_detail",
    "list_property_summaries",
]
