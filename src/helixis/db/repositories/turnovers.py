"""Turnover checklist (migration 0046) — the property-page make-ready flow.

A turnover opens when the PMS files a move-out (Buildium's
``Lease.MoveOut.Created`` webhook; a Rentvine lease export row gaining a
move-out date, read by the sync) and tracks three steps to a rent-ready
unit: the move-out inspection, make-ready work orders, and listing the unit.
Steps are timestamps so the page can show when each happened; the row is
'done' exactly when all three are set. The open-work-order count per unit
rides along from the mirror as a hint for the work-orders step — advisory
only, never auto-checked.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import asyncpg

from helixis.buildium.maintenance_status import open_status_sql
from helixis.db.repositories import reminders as reminders_repo

STEPS = ("inspection", "work_orders", "listed")

# Step name → column, interpolated into SQL. The allowlist above is the
# only path in, so no user input ever reaches the format call.
_STEP_COLUMNS = {
    "inspection": "inspection_done_at",
    "work_orders": "work_orders_done_at",
    "listed": "listed_done_at",
}


@dataclass
class Turnover:
    id: str
    lease_buildium_id: int
    property_id: int | None
    unit_id: int | None
    unit_number: str | None
    move_out_date: date | None
    status: str
    inspection_done_at: datetime | None
    work_orders_done_at: datetime | None
    listed_done_at: datetime | None
    open_work_orders: int = 0


def _from_record(r: asyncpg.Record | dict[str, Any]) -> Turnover:
    def _get(key: str, default: Any = None) -> Any:
        try:
            return r[key]
        except (KeyError, IndexError):
            return default

    return Turnover(
        id=str(r["id"]),
        lease_buildium_id=r["lease_buildium_id"],
        property_id=r["property_id"],
        unit_id=r["unit_id"],
        unit_number=_get("unit_number"),
        move_out_date=r["move_out_date"],
        status=r["status"],
        inspection_done_at=r["inspection_done_at"],
        work_orders_done_at=r["work_orders_done_at"],
        listed_done_at=r["listed_done_at"],
        open_work_orders=_get("open_work_orders", 0) or 0,
    )


async def open_turnover(
    pool: asyncpg.Pool,
    *,
    company_id: str,
    lease_buildium_id: int,
    property_id: int | None,
    unit_id: int | None,
    move_out_date: date | None,
    pms: str = "buildium",
) -> None:
    """Open (or refresh) the turnover for a lease's move-out.

    Re-filed move-outs update the date. A dismissed row reactivates —
    a NEW move-out notice is a new reason to track. A 'done' row stays
    done: re-opening a finished turnover is the PM's call, not ours.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO helixis_turnovers
                (company_id, lease_buildium_id, property_id, unit_id, move_out_date, pms)
            VALUES ($1::uuid, $2, $3, $4, $5, $6)
            ON CONFLICT (company_id, lease_buildium_id) DO UPDATE SET
                move_out_date = EXCLUDED.move_out_date,
                pms           = EXCLUDED.pms,
                property_id   = COALESCE(EXCLUDED.property_id, helixis_turnovers.property_id),
                unit_id       = COALESCE(EXCLUDED.unit_id, helixis_turnovers.unit_id),
                status        = CASE WHEN helixis_turnovers.status = 'dismissed'
                                     THEN 'active' ELSE helixis_turnovers.status END,
                updated_at    = now()
            """,
            company_id,
            lease_buildium_id,
            property_id,
            unit_id,
            move_out_date,
            pms,
        )


async def open_turnover_if_new(
    pool: asyncpg.Pool,
    *,
    company_id: str,
    lease_buildium_id: int,
    property_id: int | None,
    unit_id: int | None,
    move_out_date: date,
    pms: str,
) -> bool:
    """:func:`open_turnover` for a PMS that is POLLED rather than evented.
    Returns whether a turnover was opened or its date moved.

    ⚠ A poll sees the same move-out on every pass. :func:`open_turnover`
    reactivates a dismissed row each time it runs, which is right for a
    webhook (each delivery is a new filing) and wrong here: a turnover the
    manager dismissed would come back every few hours. So a row is touched
    only when it is NEW or its move-out date CHANGED (a re-filed move-out,
    which is a new reason to track, as for Buildium).
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO helixis_turnovers
                (company_id, lease_buildium_id, property_id, unit_id, move_out_date, pms)
            VALUES ($1::uuid, $2, $3, $4, $5, $6)
            ON CONFLICT (company_id, lease_buildium_id) DO UPDATE SET
                move_out_date = EXCLUDED.move_out_date,
                pms           = EXCLUDED.pms,
                property_id   = COALESCE(EXCLUDED.property_id, helixis_turnovers.property_id),
                unit_id       = COALESCE(EXCLUDED.unit_id, helixis_turnovers.unit_id),
                status        = CASE WHEN helixis_turnovers.status = 'dismissed'
                                     THEN 'active' ELSE helixis_turnovers.status END,
                updated_at    = now()
             WHERE helixis_turnovers.move_out_date IS DISTINCT FROM EXCLUDED.move_out_date
            RETURNING id
            """,
            company_id,
            lease_buildium_id,
            property_id,
            unit_id,
            move_out_date,
            pms,
        )
    return row is not None


async def start_turnover(
    pool: asyncpg.Pool,
    *,
    company_id: str,
    lease_id: int,
    move_out_date: date | None,
    pms: str = "buildium",
    only_if_new: bool = False,
) -> bool:
    """The move-out trigger, one home for both PMSs: a desk card saying
    "start turnover" and the property-page checklist row it links to.
    Returns whether it fired.

    ``only_if_new`` is the polled path (:func:`open_turnover_if_new`): no row
    touched and NO card unless the turnover is new or its date moved, so a
    card the manager handled is not re-raised by the next sync.
    """
    async with pool.acquire() as conn:
        ctx = await conn.fetchrow(
            """
            SELECT l.property_id, l.unit_id, p.name AS property_name, u.unit_number
              FROM helixis_leases l
              LEFT JOIN helixis_properties p
                ON p.buildium_id = l.property_id AND p.company_id = l.company_id
              LEFT JOIN helixis_units u
                ON u.buildium_id = l.unit_id AND u.company_id = l.company_id
             WHERE l.company_id = $1::uuid AND l.buildium_id = $2
            """,
            company_id,
            lease_id,
        )
    property_id = ctx["property_id"] if ctx else None
    unit_id = ctx["unit_id"] if ctx else None
    if only_if_new:
        if move_out_date is None or not await open_turnover_if_new(
            pool,
            company_id=company_id,
            lease_buildium_id=lease_id,
            property_id=property_id,
            unit_id=unit_id,
            move_out_date=move_out_date,
            pms=pms,
        ):
            return False
    else:
        await open_turnover(
            pool,
            company_id=company_id,
            lease_buildium_id=lease_id,
            property_id=property_id,
            unit_id=unit_id,
            move_out_date=move_out_date,
            pms=pms,
        )
    where_bits = []
    link_path = None
    if ctx:
        if ctx["property_name"]:
            where_bits.append(ctx["property_name"])
        # A single-unit property's unit is often named after the property
        # ("111 Kane · Unit 111 Kane", measured on Rentvine 2026-10-02): say it once.
        unit = (ctx["unit_number"] or "").strip()
        if unit and unit.casefold() != (ctx["property_name"] or "").strip().casefold():
            where_bits.append(f"Unit {unit}")
        if ctx["property_id"]:
            link_path = f"/properties/{ctx['property_id']}"
    where = " · ".join(where_bits) or f"lease #{lease_id}"
    when = move_out_date.isoformat() if move_out_date is not None else "tbd"
    await reminders_repo.upsert_card(
        pool,
        company_id=company_id,
        producer="turnover",
        candidate=reminders_repo.ReminderCandidate(
            entity_key=f"lease:{lease_id}",
            title=f"Move-out at {where} on {when} — start turnover",
            body=(
                "Schedule the move-out inspection, create make-ready work "
                "orders, and list the unit."
            ),
            severity="high",
            link_path=link_path,
            payload={"lease_id": lease_id, "move_out_date": when},
        ),
    )
    return True


async def list_turnovers(
    pool: asyncpg.Pool,
    *,
    company_id: str,
    property_id: int,
) -> list[Turnover]:
    """Turnovers on the property (active + done, never dismissed), with the
    unit label and the mirror's open-work-order count on that unit."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT t.*, u.unit_number,
                   (SELECT count(*)
                      FROM helixis_work_orders wo
                     WHERE wo.company_id = t.company_id
                       AND wo.unit_id = t.unit_id
                       AND {open_status_sql("wo.status")}) AS open_work_orders
              FROM helixis_turnovers t
              LEFT JOIN helixis_units u
                ON u.buildium_id = t.unit_id AND u.company_id = t.company_id
             WHERE t.company_id = $1::uuid
               AND t.property_id = $2
               AND t.status <> 'dismissed'
             ORDER BY (t.status = 'active') DESC, t.move_out_date NULLS LAST
            """,
            company_id,
            property_id,
        )
    return [_from_record(r) for r in rows]


async def set_step(
    pool: asyncpg.Pool,
    *,
    company_id: str,
    turnover_id: str,
    step: str,
    done: bool,
) -> Turnover | None:
    """Check or uncheck one step, then recompute done-ness from the three
    step timestamps. Dismissed rows aren't editable."""
    column = _STEP_COLUMNS.get(step)
    if column is None:
        raise ValueError(f"Unknown turnover step: {step!r} (expected one of {STEPS})")
    async with pool.acquire() as conn:
        r = await conn.fetchrow(
            f"""
            UPDATE helixis_turnovers
               SET {column} = CASE WHEN $3 THEN COALESCE({column}, now()) ELSE NULL END,
                   updated_at = now()
             WHERE company_id = $1::uuid AND id = $2::uuid AND status <> 'dismissed'
            RETURNING id
            """,
            company_id,
            turnover_id,
            done,
        )
        if r is None:
            return None
        r = await conn.fetchrow(
            """
            UPDATE helixis_turnovers
               SET status = CASE WHEN inspection_done_at IS NOT NULL
                                  AND work_orders_done_at IS NOT NULL
                                  AND listed_done_at IS NOT NULL
                                 THEN 'done' ELSE 'active' END,
                   completed_at = CASE WHEN inspection_done_at IS NOT NULL
                                        AND work_orders_done_at IS NOT NULL
                                        AND listed_done_at IS NOT NULL
                                       THEN COALESCE(completed_at, now()) ELSE NULL END,
                   updated_at = now()
             WHERE company_id = $1::uuid AND id = $2::uuid
            RETURNING *
            """,
            company_id,
            turnover_id,
        )
    return _from_record(r) if r else None


async def dismiss(
    pool: asyncpg.Pool,
    *,
    company_id: str,
    turnover_id: str,
) -> Turnover | None:
    async with pool.acquire() as conn:
        r = await conn.fetchrow(
            """
            UPDATE helixis_turnovers
               SET status = 'dismissed', updated_at = now()
             WHERE company_id = $1::uuid AND id = $2::uuid
            RETURNING *
            """,
            company_id,
            turnover_id,
        )
    return _from_record(r) if r else None


__all__ = [
    "STEPS",
    "Turnover",
    "dismiss",
    "list_turnovers",
    "open_turnover",
    "set_step",
]
