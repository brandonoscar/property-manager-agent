"""Inbox reminders — desk cards + the snooze/dismiss/handled actions.

- ``GET  /reminders``               — active cards (open + expired snoozes)
- ``POST /reminders/{id}/snooze``   — hide until a date ("remind me in 2 days");
                                      the sweep RECHECKS the condition before
                                      the card can return
- ``POST /reminders/{id}/dismiss``  — stop tracking this instance
- ``POST /reminders/{id}/handled``  — "I dealt with it outside Occupella";
                                      optionally logs a note to Team Activity
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from helixis.api.auth import AuthenticatedUser, get_current_user
from helixis.db.engine import get_pool
from helixis.db.repositories import ActivityEntry, write_activity
from helixis.db.repositories import reminders as reminders_repo
from helixis.reminders.aging import MAX_SNOOZE_DAYS

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/reminders", tags=["reminders"])


def _company_id(user: AuthenticatedUser) -> str:
    if not user.company_ids:
        raise HTTPException(status_code=400, detail="User is not a member of any company")
    return user.company_ids[0]


class ReminderOut(BaseModel):
    id: str
    producer: str
    entity_key: str
    status: str
    severity: str
    title: str
    body: str | None = None
    link_path: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    snoozed_until: datetime | None = None
    first_fired_at: datetime | None = None
    last_fired_at: datetime | None = None
    #: How loud this card has become — the greater of what its producer asked
    #: for and what ``reminders/aging.py`` adds for the time it has sat there.
    #: It was on the row and dropped here, so a client could not tell a card
    #: raised this morning from one ignored for six weeks (gotchas 41/50/51).
    escalation: int = 0
    #: When somebody last said they had dealt with this outside Occupella. A
    #: value on an OPEN card means the claim did not settle it and the
    #: cooldown expired — which is the one thing the user needs told.
    handled_at: datetime | None = None


class RemindersOut(BaseModel):
    reminders: list[ReminderOut]


class SnoozeIn(BaseModel):
    days: float = Field(default=2, gt=0, le=MAX_SNOOZE_DAYS)


class HandledIn(BaseModel):
    note: str = ""


def _out(r: reminders_repo.ReminderRow) -> ReminderOut:
    return ReminderOut(
        id=r.id,
        producer=r.producer,
        entity_key=r.entity_key,
        status=r.status,
        severity=r.severity,
        title=r.title,
        body=r.body,
        link_path=r.link_path,
        payload=r.payload,
        snoozed_until=r.snoozed_until,
        first_fired_at=r.first_fired_at,
        last_fired_at=r.last_fired_at,
        escalation=r.escalation,
        handled_at=r.handled_at,
    )


@router.get("", response_model=RemindersOut)
async def list_reminders(user: AuthenticatedUser = Depends(get_current_user)) -> RemindersOut:
    rows = await reminders_repo.list_active(get_pool(), company_id=_company_id(user))
    return RemindersOut(reminders=[_out(r) for r in rows])


@router.post("/{reminder_id}/snooze", response_model=ReminderOut)
async def snooze_reminder(
    reminder_id: str,
    payload: SnoozeIn,
    user: AuthenticatedUser = Depends(get_current_user),
) -> ReminderOut:
    until = datetime.now(UTC) + timedelta(days=payload.days)
    row = await reminders_repo.set_status(
        get_pool(),
        company_id=_company_id(user),
        reminder_id=reminder_id,
        status="snoozed",
        snoozed_until=until,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="No active reminder with that id.")
    return _out(row)


@router.post("/{reminder_id}/dismiss", response_model=ReminderOut)
async def dismiss_reminder(
    reminder_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> ReminderOut:
    row = await reminders_repo.set_status(
        get_pool(),
        company_id=_company_id(user),
        reminder_id=reminder_id,
        status="dismissed",
    )
    if row is None:
        raise HTTPException(status_code=404, detail="No active reminder with that id.")
    return _out(row)


@router.post("/{reminder_id}/handled", response_model=ReminderOut)
async def handled_reminder(
    reminder_id: str,
    payload: HandledIn,
    user: AuthenticatedUser = Depends(get_current_user),
) -> ReminderOut:
    """The PM acted outside Occupella (phone call, in person), and Team
    Activity records that they said so.

    ⚠ **The row is written whether or not a note came with it, and the
    condition used to be the other way round.** "I dealt with this" is a CLAIM
    about the world — `reminders/aging.py` exists precisely because such a
    claim can turn out to be wrong and the card has to come back. So the thing
    worth recording is the ACT; the note is extra detail on it. Gating the
    whole row on the note meant a teammate asking "who dealt with the Garden
    Row renewal?" got nothing.

    ⚠ And it was UNREACHABLE, not merely rare: no caller anywhere sent a note,
    so `action_type="reminder_handled"` could never appear in the feed — the
    same shape as the two verbs this route serves having had no UI at all
    (gotcha 140).
    """
    company_id = _company_id(user)
    pool = get_pool()
    row = await reminders_repo.set_status(
        pool, company_id=company_id, reminder_id=reminder_id, status="handled"
    )
    if row is None:
        raise HTTPException(status_code=404, detail="No active reminder with that id.")
    note = payload.note.strip()
    try:
        await write_activity(
            pool,
            ActivityEntry(
                account_id=None,
                company_id=company_id,
                source="manual",
                source_label="Reminders",
                action_type="reminder_handled",
                description=(
                    f"Handled outside Occupella — {row.title}: {note[:300]}"
                    if note
                    else f"Handled outside Occupella — {row.title}"
                ),
                # ⚠ The actor, which this route was the one place not to send.
                # `crm.py` passes it at five call sites; without it the feed
                # says somebody made a claim and cannot say who — on the one
                # entry whose whole value is that it is attributable.
                user_id=user.user_id,
                raw={"reminder_id": row.id, "producer": row.producer},
            ),
        )
    except Exception:  # noqa: BLE001 — the audit row is best-effort, never the action
        logger.exception("handled activity write failed")
    return _out(row)


__all__ = ["router"]
