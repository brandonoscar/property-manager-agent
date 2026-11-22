"""Turnover checklist — the property-page make-ready tracker (0046).

- ``GET  /properties/{pid}/turnovers``                    — open + done turnovers
- ``POST /properties/{pid}/turnovers/{id}/steps/{step}``  — check/uncheck one step
- ``POST /properties/{pid}/turnovers/{id}/dismiss``       — stop tracking

Rows open automatically from ``Lease.MoveOut.Created`` (the same trigger
that drops the Inbox turnover card). Everything here is Helixis-local
state — no Buildium writes; the make-ready work orders themselves are
created via chat or in Buildium and show up as the open-WO hint.
"""

from __future__ import annotations

import logging
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from helixis.api.auth import AuthenticatedUser, get_current_user
from helixis.db.engine import get_pool
from helixis.db.repositories import ActivityEntry, write_activity
from helixis.db.repositories import turnovers as turnovers_repo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/properties/{property_id}/turnovers", tags=["turnovers"])


def _company_id(user: AuthenticatedUser) -> str:
    if not user.company_ids:
        raise HTTPException(status_code=400, detail="User is not a member of any company")
    return user.company_ids[0]


class TurnoverOut(BaseModel):
    id: str
    lease_buildium_id: int
    unit_id: int | None = None
    unit_number: str | None = None
    move_out_date: date | None = None
    status: str
    inspection_done: bool
    work_orders_done: bool
    listed_done: bool
    open_work_orders: int


class TurnoversOut(BaseModel):
    turnovers: list[TurnoverOut]


class StepIn(BaseModel):
    done: bool


def _out(t: turnovers_repo.Turnover) -> TurnoverOut:
    return TurnoverOut(
        id=t.id,
        lease_buildium_id=t.lease_buildium_id,
        unit_id=t.unit_id,
        unit_number=t.unit_number,
        move_out_date=t.move_out_date,
        status=t.status,
        inspection_done=t.inspection_done_at is not None,
        work_orders_done=t.work_orders_done_at is not None,
        listed_done=t.listed_done_at is not None,
        open_work_orders=t.open_work_orders,
    )


@router.get("", response_model=TurnoversOut)
async def list_turnovers(
    property_id: int,
    user: AuthenticatedUser = Depends(get_current_user),
) -> TurnoversOut:
    rows = await turnovers_repo.list_turnovers(
        get_pool(), company_id=_company_id(user), property_id=property_id
    )
    return TurnoversOut(turnovers=[_out(t) for t in rows])


@router.post("/{turnover_id}/steps/{step}", response_model=TurnoverOut)
async def set_step(
    property_id: int,
    turnover_id: str,
    step: str,
    payload: StepIn,
    user: AuthenticatedUser = Depends(get_current_user),
) -> TurnoverOut:
    if step not in turnovers_repo.STEPS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown step {step!r} — expected one of {', '.join(turnovers_repo.STEPS)}.",
        )
    company_id = _company_id(user)
    pool = get_pool()
    turnover = await turnovers_repo.set_step(
        pool,
        company_id=company_id,
        turnover_id=turnover_id,
        step=step,
        done=payload.done,
    )
    if turnover is None:
        raise HTTPException(status_code=404, detail="No such turnover on this property.")
    if turnover.status == "done":
        # One activity entry when the unit goes rent-ready — step ticks
        # themselves would just be noise.
        unit = f"unit {turnover.unit_number}" if turnover.unit_number else "the unit"
        await write_activity(
            pool,
            ActivityEntry(
                account_id=None,
                company_id=company_id,
                source="manual",
                source_label="Turnover",
                action_type="turnover_completed",
                description=f"Turnover complete — {unit} is rent-ready "
                f"(lease #{turnover.lease_buildium_id})",
                entity_type="lease",
                entity_id=turnover.lease_buildium_id,
                raw={"turnover_id": turnover.id, "property_id": property_id},
            ),
        )
    return _out(turnover)


@router.post("/{turnover_id}/dismiss", response_model=TurnoverOut)
async def dismiss_turnover(
    property_id: int,
    turnover_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> TurnoverOut:
    turnover = await turnovers_repo.dismiss(
        get_pool(), company_id=_company_id(user), turnover_id=turnover_id
    )
    if turnover is None:
        raise HTTPException(status_code=404, detail="No such turnover on this property.")
    return _out(turnover)


__all__ = ["router"]
