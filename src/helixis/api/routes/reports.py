"""Reports — deterministic, mirror-backed company reports.

First slice (roadmap §7 rank 1): ``GET /reports/first-scan`` — the day-one
operations scan the onboarding wizard's launch screen renders right after
Buildium connects ("Helixis found N things in your Buildium data").

Every number is a mirror row count (see ``db/repositories/first_scan.py``);
no LLM in this path, so the report can never claim something the data
doesn't show. Owner reports (roadmap §7 rank 5) will grow here later —
narrative on top of these same grounded numbers.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from helixis.api.auth import AuthenticatedUser, get_current_user
from helixis.db.engine import get_pool
from helixis.db.repositories.first_scan import run_first_scan
from helixis.db.repositories.morning_brief import build_morning_brief
from helixis.db.repositories.noticed import load_calibration, load_noticed
from helixis.detectors.attention import DEFAULT_DAILY_CAP, NoticedItem

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/reports", tags=["reports"])


def _require_company(user: AuthenticatedUser) -> str:
    if not user.company_ids:
        raise HTTPException(status_code=403, detail="No company for this user")
    return user.company_ids[0]


@router.get("/first-scan")
async def first_scan(user: AuthenticatedUser = Depends(get_current_user)) -> dict:
    """The company's day-one operations numbers, straight from the mirror.

    Safe to call repeatedly — the wizard polls while the initial backfill
    fills the mirror (``synced`` flips true once entity rows exist).
    """
    company_id = _require_company(user)
    try:
        pool = get_pool()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="Database unavailable") from exc

    scan = await run_first_scan(pool, company_id)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        **scan.to_dict(),
    }


@router.get("/morning-brief")
async def morning_brief(user: AuthenticatedUser = Depends(get_current_user)) -> dict:
    """Today's "things that need you" list — severity-ordered, max five.

    Zero-count items are omitted; an empty list is a legitimate quiet
    morning, and the client renders nothing rather than inventing urgency.
    """
    company_id = _require_company(user)
    try:
        pool = get_pool()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="Database unavailable") from exc

    items = await build_morning_brief(pool, company_id)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "items": [i.to_dict() for i in items],
    }


# ── Risk flags (reconciliation, roadmap §7 rank 4) ───────────────────

_FLAG_COLUMNS = (
    "id, source, rule_key, severity, title, detail, amount, property_id, "
    "entity_type, entity_id, evidence, status, first_seen_at, last_seen_at"
)

# The statuses a PM may set. 'new' and 'resolved' are engine-owned:
# sweeps create 'new' and auto-resolve healed conditions.
_SETTABLE_STATUSES = frozenset({"confirmed", "dismissed"})


@router.get("/risk-flags")
async def list_risk_flags(
    status: str = "open",
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict:
    """The company's risk-flag ledger.

    ``status=open`` (default) returns new + confirmed — the actionable
    set. Any single status name filters exactly; ``all`` returns
    everything including dismissed/resolved history.
    """
    company_id = _require_company(user)
    try:
        pool = get_pool()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="Database unavailable") from exc

    args: list[str] = []
    if status == "open":
        where = "status IN ('new', 'confirmed')"
    elif status == "all":
        where = "TRUE"
    elif status in {"new", "confirmed", "dismissed", "resolved"}:
        where = "status = $2"
        args = [status]
    else:
        raise HTTPException(status_code=400, detail="Unknown status filter")

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT {_FLAG_COLUMNS} FROM helixis_risk_flags "  # noqa: S608 — columns are a module constant
            f"WHERE company_id = $1::uuid AND {where} "
            "ORDER BY severity = 'critical' DESC, last_seen_at DESC LIMIT 200",
            company_id,
            *args,
        )
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "flags": [dict(r) | {"id": str(r["id"])} for r in rows],
    }


class FlagStatusUpdate(BaseModel):
    status: str


@router.patch("/risk-flags/{flag_id}")
async def set_risk_flag_status(
    flag_id: str,
    body: FlagStatusUpdate,
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict:
    """Confirm ('yes, that's real') or dismiss ('this is fine') a flag.

    A dismissal suppresses the flag from re-surfacing even while the
    underlying condition persists — the engine respects it on upsert.
    """
    company_id = _require_company(user)
    if body.status not in _SETTABLE_STATUSES:
        raise HTTPException(status_code=400, detail="status must be confirmed or dismissed")
    try:
        pool = get_pool()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="Database unavailable") from exc

    async with pool.acquire() as conn:
        updated = await conn.fetchval(
            "UPDATE helixis_risk_flags SET status = $3 "
            "WHERE id = $1::uuid AND company_id = $2::uuid RETURNING id",
            flag_id,
            company_id,
            body.status,
        )
    if updated is None:
        raise HTTPException(status_code=404, detail="flag not found")
    return {"id": str(updated), "status": body.status}


# ── "Occupella noticed" — attention-budgeted findings digest ─────────


def _daily_cap() -> int:
    """Delivered-per-day cap for the noticed digest; env-overridable."""
    raw = os.getenv("HELIXIS_DETECTORS__DAILY_NOTICED_CAP", "")
    try:
        return max(0, int(raw)) if raw.strip() else DEFAULT_DAILY_CAP
    except ValueError:
        return DEFAULT_DAILY_CAP


def _finding_dict(f: object) -> dict:
    return {
        "id": f.id,  # type: ignore[attr-defined]
        "rule_key": f.rule_key,  # type: ignore[attr-defined]
        "severity": f.severity,  # type: ignore[attr-defined]
        "title": f.title,  # type: ignore[attr-defined]
        "detail": f.detail,  # type: ignore[attr-defined]
        "amount": f.amount,  # type: ignore[attr-defined]
        "property_id": f.property_id,  # type: ignore[attr-defined]
        "entity_type": f.entity_type,  # type: ignore[attr-defined]
        "entity_id": f.entity_id,  # type: ignore[attr-defined]
    }


def _item_dict(item: NoticedItem) -> dict:
    return {**_finding_dict(item.finding), "related": [_finding_dict(r) for r in item.related]}


@router.get("/noticed")
async def noticed(user: AuthenticatedUser = Depends(get_current_user)) -> dict:
    """What Occupella noticed today, volume-governed by the attention budget.

    ``urgent`` (critical findings) is delivered uncapped; ``noticed`` is the
    day's capped digest; ``overflow_count`` is what didn't make the cut (still
    in ``/reports/risk-flags``). ``miscalibrated`` lists detectors dismissed
    more than half the time — noise to review, not silently left running.
    """
    company_id = _require_company(user)
    try:
        pool = get_pool()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="Database unavailable") from exc

    digest = await load_noticed(pool, company_id, daily_cap=_daily_cap())
    calibration = await load_calibration(pool, company_id)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "urgent": [_item_dict(i) for i in digest.urgent],
        "noticed": [_item_dict(i) for i in digest.noticed],
        "overflow_count": digest.overflow_count,
        "delivered_count": digest.delivered_count,
        "miscalibrated": [
            {
                "rule_key": r.rule_key,
                "total": r.total,
                "dismissed": r.dismissed,
                "dismissal_rate": r.dismissal_rate,
            }
            for r in calibration
            if r.miscalibrated
        ],
    }
