"""The first operations scan — company-wide rollups from the entity mirror.

Powers ``GET /reports/first-scan`` (roadmap §7 rank 1): the activation
moment right after Buildium connects. One read-only query computes the
"Helixis found N things in your Buildium data" numbers the onboarding
wizard's launch screen and the shareable day-one report render.

All numbers are deterministic mirror SQL — no LLM anywhere in this path,
so a claim on the report is exactly a row count. Reuses the cockpit's
semantics: "open" work orders are New/InProgress, occupancy = a unit with
an Active lease, delinquency = positive ``helixis_lease_balances`` rows.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import asyncpg

from helixis.buildium.maintenance_status import open_status_sql
from helixis.db.company_pms import company_pms_strict
from helixis.db.pms_coverage import blind_tables
from helixis.db.repositories.mirror_sync_state import get_stage_sync

#: The sync-state stage both PMS paths stamp after a clean balances pass.
BALANCES_STAGE = "balances"

EXPIRING_WINDOW_DAYS = 45
STALLED_AFTER_DAYS = 7

_SCAN_SQL = f"""
SELECT
    (SELECT COUNT(*) FROM helixis_properties WHERE company_id = $1::uuid) AS properties,
    (SELECT COUNT(*) FROM helixis_units      WHERE company_id = $1::uuid) AS units,
    (SELECT COUNT(*) FROM helixis_tenants    WHERE company_id = $1::uuid) AS tenants,
    (SELECT COUNT(*) FROM helixis_leases
      WHERE company_id = $1::uuid AND status = 'Active')                  AS active_leases,
    (SELECT COUNT(*) FROM helixis_work_orders
      WHERE company_id = $1::uuid
        AND {open_status_sql("status")})                  AS open_work_orders,
    (SELECT COUNT(*) FROM helixis_work_orders
      WHERE company_id = $1::uuid AND {open_status_sql("status")}
        AND COALESCE(pms_updated_at, updated_at)
            < now() - make_interval(days => $3))                          AS stalled_work_orders,
    (SELECT COUNT(*) FROM helixis_leases
      WHERE company_id = $1::uuid AND status = 'Active'
        AND lease_to_date BETWEEN now()
            AND now() + make_interval(days => $2))                        AS expiring_leases,
    (SELECT COUNT(*) FROM helixis_lease_balances
      WHERE company_id = $1::uuid AND COALESCE(total_balance, 0) > 0)     AS delinquent_leases,
    (SELECT COALESCE(SUM(total_balance), 0) FROM helixis_lease_balances
      WHERE company_id = $1::uuid AND COALESCE(total_balance, 0) > 0)     AS delinquent_total,
    (SELECT COUNT(*) FROM helixis_payment_promises
      WHERE company_id = $1::uuid AND promised_date >= current_date)      AS pending_promises
"""


@dataclass
class FirstScan:
    """Company-wide day-one numbers. ``synced`` is False while the mirror is
    still empty (backfill in flight right after the wizard's connect step) so
    clients can say "still syncing" instead of showing a wall of zeros."""

    properties: int = 0
    units: int = 0
    tenants: int = 0
    active_leases: int = 0
    open_work_orders: int = 0
    stalled_work_orders: int = 0
    expiring_leases: int = 0
    expiring_window_days: int = EXPIRING_WINDOW_DAYS
    #: ``None`` when this company's PMS never fills ``helixis_lease_balances``
    #: (Rentvine today). An empty table there means "not sent", and 0 would
    #: tell a new customer nobody owes them rent (``db/pms_coverage.py``).
    delinquent_leases: int | None = 0
    delinquent_total: float | None = 0.0
    pending_promises: int = 0
    synced: bool = False
    #: Which system the numbers came from, so a client can name it. ``None``
    #: when the lookup failed.
    pms: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


async def run_first_scan(pool: asyncpg.Pool, company_id: str) -> FirstScan:
    """Compute the scan for one company. Read-only, single round trip."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_SCAN_SQL, company_id, EXPIRING_WINDOW_DAYS, STALLED_AFTER_DAYS)

    scan = FirstScan(
        properties=row["properties"],
        units=row["units"],
        tenants=row["tenants"],
        active_leases=row["active_leases"],
        open_work_orders=row["open_work_orders"],
        stalled_work_orders=row["stalled_work_orders"],
        expiring_leases=row["expiring_leases"],
        delinquent_leases=row["delinquent_leases"],
        delinquent_total=float(row["delinquent_total"] or 0),
        pending_promises=row["pending_promises"],
    )
    scan.synced = scan.properties > 0 or scan.units > 0 or scan.active_leases > 0
    try:
        scan.pms = await company_pms_strict(pool, company_id)
    except Exception:  # noqa: BLE001 — degrade the claim, never the scan
        scan.pms = None
    # Unknown PMS declines too: 0 owed is exactly the claim we cannot back.
    # ⚠ And so does a balances stage that never landed. An empty balances
    # table reads as "nobody owes anything" whether the snapshot was written
    # or not; the stage's stamp (written only by a clean balances pass, on
    # both PMS paths) is what separates the two (rule 14).
    if (
        scan.pms is None
        or blind_tables(scan.pms, {"helixis_lease_balances"})
        or await get_stage_sync(pool, company_id, BALANCES_STAGE) is None
    ):
        scan.delinquent_leases = None
        scan.delinquent_total = None
    return scan


__all__ = ["EXPIRING_WINDOW_DAYS", "STALLED_AFTER_DAYS", "FirstScan", "run_first_scan"]
