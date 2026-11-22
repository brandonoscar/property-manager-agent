"""Compliance sweeps — the trigger the sweep never had.

- ``POST /compliance/sweeps/preview`` — who matches, changing nothing
- ``POST /compliance/sweeps``         — run it and track the findings

``run_compliance_sweep`` shipped complete, tested, and callable by nothing.
Its own docstring says "an operator triggers it with a rule change"; no
operator could, because there was no surface to trigger it from. Zero findings
have ever carried a ``compliance:*`` rule key.

⚠ **The two verbs are genuinely different and that is why preview exists.**
The sweep is read-only against Buildium — it proposes remediation text and
never acts — but it is NOT side-effect free: it persists findings, and those
bridge into the Inbox as a card. So the operator writing the rule gets to see
who it catches BEFORE anything lands on anyone's desk. A rule written slightly
wrong ("leases under $2,000" when you meant $1,200) is the normal case, not the
exceptional one.

⚠ **Re-running is the update path, not a duplicate.** Findings persist under
``compliance:<label>``, so the same label re-run updates in place and
auto-resolves leases that no longer match. A new label starts a new sweep. That
makes ``label`` the identity of a rule over time, which is why it is validated
rather than free text.
"""

from __future__ import annotations

import logging
import re
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from helixis.api.auth import AuthenticatedUser, get_current_user
from helixis.api.roles import MANAGER
from helixis.db.engine import get_pool
from helixis.detectors.compliance_sweep import run_compliance_sweep, sweep_leases

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/compliance", tags=["compliance"])

#: A label becomes a durable ``rule_key`` and appears in user-facing cards, so
#: it is constrained rather than free text — a label with a space or a colon
#: would either collide with the ``compliance:<label>`` key format or make two
#: spellings of the same rule look like two rules.
_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{2,48}$")


def _company_id(user: AuthenticatedUser) -> str:
    if not user.company_ids:
        raise HTTPException(status_code=400, detail="User is not a member of any company")
    return user.company_ids[0]


class SweepIn(BaseModel):
    label: str = Field(description="stable id for this rule, e.g. 'late-fee-cap-2026'")
    remediation: str = Field(min_length=1, max_length=2000)
    property_ids: list[int] | None = None
    rent_min: float | None = None
    rent_max: float | None = None
    # ⚠ TYPED as ``date`` — this is bound to ``$5::date`` in
    # ``_SWEEP_LEASES_SQL``, and asyncpg refuses a ``str`` there outright
    # (the live defect in ``activity.py``, Sentry PYTHON-FASTAPI-3G). The
    # field always documented YYYY-MM-DD; now the framework enforces it and
    # returns a 422 naming the field instead of a 500 naming nothing.
    started_before: date | None = Field(default=None, description="YYYY-MM-DD")


class SweepMatch(BaseModel):
    lease_id: int
    property_id: int | None = None
    unit_number: str | None = None
    rent: float | None = None


class SweepOut(BaseModel):
    label: str
    matched: int
    #: Capped: a preview exists to be read by a person, and a portfolio-wide
    #: rule can match every active lease. ``matched`` carries the true total so
    #: the count is never understated by the truncation (gotcha 26 — a
    #: truncated list must say so rather than read as the whole set).
    sample: list[SweepMatch]
    truncated: bool
    persisted: bool


_SAMPLE_LIMIT = 25


def _validated(body: SweepIn) -> None:
    if not _LABEL_RE.match(body.label):
        raise HTTPException(
            status_code=422,
            detail=(
                "label must be 3-49 chars of lowercase letters, digits, '-' or "
                "'_' — it becomes a durable rule id shown on cards"
            ),
        )
    if body.rent_min is not None and body.rent_max is not None and body.rent_min > body.rent_max:
        raise HTTPException(
            status_code=422, detail="rent_min is above rent_max — nothing can match"
        )


def _out(body: SweepIn, findings: list, *, persisted: bool) -> SweepOut:
    sample = [
        SweepMatch(
            lease_id=int(f.evidence.get("lease_id") or f.entity_id or 0),
            property_id=f.property_id,
            unit_number=f.evidence.get("unit_number"),
            rent=f.evidence.get("rent"),
        )
        for f in findings[:_SAMPLE_LIMIT]
    ]
    return SweepOut(
        label=body.label,
        matched=len(findings),
        sample=sample,
        truncated=len(findings) > _SAMPLE_LIMIT,
        persisted=persisted,
    )


@router.post("/sweeps/preview", response_model=SweepOut)
async def preview_sweep(
    body: SweepIn, user: AuthenticatedUser = Depends(get_current_user)
) -> SweepOut:
    """Who would this rule catch? Persists nothing, cards nobody."""
    _validated(body)
    company_id = _company_id(user)
    async with get_pool().acquire() as conn:
        findings = await sweep_leases(
            conn,
            company_id,
            label=body.label,
            remediation=body.remediation,
            property_ids=body.property_ids,
            rent_min=body.rent_min,
            rent_max=body.rent_max,
            started_before=body.started_before,
        )
    return _out(body, findings, persisted=False)


@router.post("/sweeps", response_model=SweepOut)
async def run_sweep(body: SweepIn, user: AuthenticatedUser = Depends(get_current_user)) -> SweepOut:
    """Run the rule and start tracking it.

    ⚠ Manager+ — this one puts a card on everybody's desk and creates findings
    that persist until the rule is retired. Same floor as the writes that are
    awkward to undo, for the same reason: not because it touches Buildium (it
    does not), but because backing it out is manual.
    """
    _validated(body)
    company_id = _company_id(user)
    # ⚠ ``require_role`` reads company_users, NOT AuthenticatedUser.role.
    # The latter is the SUPABASE JWT role and is the literal string
    # "authenticated" for every caller, so a role_at_least() against it fails
    # closed on everyone and the endpoint could never be used by anybody. The
    # first cut of this route did exactly that.
    from helixis.api.routes.company import require_role

    pool = get_pool()
    await require_role(
        pool,
        user.user_id,
        company_id,
        MANAGER,
        detail="Running a compliance sweep needs a Manager or Admin on this workspace.",
    )
    findings = await run_compliance_sweep(
        pool,
        company_id,
        label=body.label,
        remediation=body.remediation,
        property_ids=body.property_ids,
        rent_min=body.rent_min,
        rent_max=body.rent_max,
        started_before=body.started_before,
    )
    logger.info(
        "compliance sweep company=%s label=%s matched=%d", company_id, body.label, len(findings)
    )
    return _out(body, findings, persisted=True)
