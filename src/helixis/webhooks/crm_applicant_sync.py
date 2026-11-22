"""CRM ↔ Buildium applicant sync — auto-move the lead when they apply.

When Buildium fires ``Applicant.Created/Updated`` (or a backfill sweeps
applicants), the applicant is matched to a CRM lead and the lead's pipeline
stage is advanced to reflect reality in Buildium:

  applicant exists            → the lead has APPLIED   (stage 'applied')
  Status == AddedToLease      → the lead has LEASED    (stage 'leased')
  Status Rejected/Cancelled   → link only, no move (the PM decides whether
                                the lead is lost — we never auto-drop)

Matching order: sticky applicant link (0040) → email → phone. Moves are
FORWARD-ONLY along ``LEAD_STAGES`` — a Buildium status can never demote a
lead — and 'lost' leads are linked but never moved (resurrecting a lost
lead is the PM's call).

The stage mapping is a pure function (``stage_for_applicant_status``) so
it's unit-testable without a DB; :func:`sync_lead_from_applicant` is the
persistence wrapper the webhook handler and backfill both call.
"""

from __future__ import annotations

import logging

import asyncpg

from helixis.buildium.schemas import Applicant
from helixis.db.repositories import leads as leads_repo
from helixis.db.repositories.activity import ActivityEntry, write_activity
from helixis.db.repositories.entities import _primary_phone
from helixis.db.repositories.leads import LEAD_STAGES, LeadRow

logger = logging.getLogger(__name__)

# Buildium applicant Status values (verified enum — see schemas.Applicant)
# that mean "on a lease": the pipeline's terminal happy stage.
_LEASED_STATUSES = frozenset({"addedtolease"})
# Statuses where the applicant is out of the running: link the lead for the
# record, but never auto-move (and certainly never auto-lose) it.
_NO_MOVE_STATUSES = frozenset({"rejected", "cancelled"})


def stage_for_applicant_status(status: str | None, current_stage: str) -> str | None:
    """Target CRM stage for an applicant status, or ``None`` for no move.

    Forward-only: returns a stage strictly later in ``LEAD_STAGES`` than
    ``current_stage`` (an unknown current stage never moves).
    """
    normalized = (status or "").strip().lower()
    if normalized in _NO_MOVE_STATUSES:
        return None
    target = "leased" if normalized in _LEASED_STATUSES else "applied"
    try:
        if LEAD_STAGES.index(target) <= LEAD_STAGES.index(current_stage):
            return None
    except ValueError:
        return None
    return target


def create_stage_for_applicant_status(status: str | None) -> str:
    """Initial pipeline stage for a lead CREATED from a Buildium applicant.

    Unlike :func:`stage_for_applicant_status` (which is forward-only over an
    existing lead's stage), a brand-new lead has no current stage — an
    applicant we're seeing for the first time IS at least 'applied'
    (they applied in Buildium), or 'leased' once AddedToLease. A
    rejected/cancelled first-sight applicant still lands at 'applied' (they
    did apply; the PM marks them lost from the board) — we never fabricate
    a 'leased'/earlier stage.
    """
    normalized = (status or "").strip().lower()
    return "leased" if normalized in _LEASED_STATUSES else "applied"


async def _match_lead(
    pool: asyncpg.Pool, applicant: Applicant, *, company_id: str
) -> LeadRow | None:
    """Sticky link first, then email, then phone."""
    lead = await leads_repo.find_lead_by_applicant(
        pool, company_id=company_id, buildium_applicant_id=applicant.id
    )
    if lead is not None:
        return lead
    if applicant.email:
        lead = await leads_repo.find_lead_by_email(
            pool, company_id=company_id, email=applicant.email
        )
        if lead is not None:
            return lead
    phone = _primary_phone(applicant)
    if phone:
        return await leads_repo.find_lead_by_phone(pool, company_id=company_id, phone=phone)
    return None


async def _create_lead_from_applicant(
    pool: asyncpg.Pool,
    applicant: Applicant,
    *,
    company_id: str,
    account_id: int,
) -> str | None:
    """Insert a new Leasing lead for an unmatched Buildium applicant.

    The applicant applied directly in Buildium and has no matching Helixis
    lead, so nothing was on the pipeline board for them. Create one at the
    stage their applicant status implies (``applied`` / ``leased``), tagged
    ``source='buildium'`` (0061), and stick the applicant link so later
    webhooks move (never re-create) it.
    """
    name = f"{applicant.first_name or ''} {applicant.last_name or ''}".strip()
    who = name or applicant.email or f"Applicant #{applicant.id}"
    stage = create_stage_for_applicant_status(applicant.status)
    new_lead = LeadRow(
        company_id=company_id,
        name=who,
        kind="renter",  # applicants are prospective tenants
        stage=stage,
        source="buildium",
        email=applicant.email,
        phone=_primary_phone(applicant),
        raw={
            "buildium_applicant_id": applicant.id,
            "applicant_status": applicant.status,
        },
    )
    lead_id = await leads_repo.insert_lead(pool, new_lead)
    if lead_id is None:
        return None
    # Stick the applicant link so the next webhook matches + moves this lead
    # rather than inserting a duplicate.
    await leads_repo.link_lead_applicant(
        pool,
        company_id=company_id,
        lead_id=lead_id,
        buildium_applicant_id=applicant.id,
    )
    description = (
        f"Added {who} to Leasing as {stage.capitalize()} — Buildium applicant "
        f"#{applicant.id} ({applicant.status or 'no status'})"
    )
    await write_activity(
        pool,
        ActivityEntry(
            account_id=account_id,
            company_id=company_id,
            source="webhook",
            source_label="Buildium · Applicant",
            action_type="crm_lead_created",
            description=description,
            entity_type="applicant",
            entity_id=applicant.id,
            raw={
                "lead_id": lead_id,
                "to_stage": stage,
                "applicant_status": applicant.status,
                "created_from": "buildium_applicant",
            },
        ),
    )
    return description


async def sync_lead_from_applicant(
    pool: asyncpg.Pool,
    applicant: Applicant,
    *,
    company_id: str,
    account_id: int,
    allow_create: bool = False,
) -> str | None:
    """Link + auto-move (or create) the CRM lead matching a Buildium applicant.

    Returns a human description of the change (also written to the Team
    Activity feed), or ``None`` when nothing happened. Never raises — the
    sync is an enrichment on top of the mirror write.

    ``allow_create`` (webhook path only): when no lead matches, INSERT a new
    Leasing lead from the applicant. The backfill sweep leaves this ``False``
    so a full historical import stays link/move-only and can't mass-create a
    lead for every past applicant.
    """
    try:
        lead = await _match_lead(pool, applicant, company_id=company_id)
        if lead is None:
            if allow_create:
                return await _create_lead_from_applicant(
                    pool, applicant, company_id=company_id, account_id=account_id
                )
            return None
        if lead.id is None:
            return None

        if lead.buildium_applicant_id != applicant.id:
            await leads_repo.link_lead_applicant(
                pool,
                company_id=company_id,
                lead_id=lead.id,
                buildium_applicant_id=applicant.id,
            )

        if lead.status == "lost":
            return None
        target = stage_for_applicant_status(applicant.status, lead.stage)
        if target is None:
            return None

        await leads_repo.update_stage(pool, company_id=company_id, lead_id=lead.id, stage=target)
        who = lead.name or lead.email or lead.phone or "a lead"
        description = (
            f"Moved {who} to {target.capitalize()} — Buildium applicant "
            f"#{applicant.id} ({applicant.status or 'no status'})"
        )
        await write_activity(
            pool,
            ActivityEntry(
                account_id=account_id,
                company_id=company_id,
                source="webhook",
                source_label="Buildium · Applicant",
                action_type="crm_stage_moved",
                description=description,
                entity_type="applicant",
                entity_id=applicant.id,
                raw={
                    "lead_id": lead.id,
                    "from_stage": lead.stage,
                    "to_stage": target,
                    "applicant_status": applicant.status,
                },
            ),
        )
        return description
    except Exception:  # noqa: BLE001 — enrichment must not fail the mirror write
        logger.exception(
            "CRM applicant sync failed (company=%s applicant=%s)", company_id, applicant.id
        )
        return None
