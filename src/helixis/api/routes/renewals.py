"""Renewals autopilot — the property-page flow (migration 0044).

- ``GET  /properties/{pid}/renewals``                 — expiring leases + offer state + renewed wins
- ``POST /properties/{pid}/renewals/{lease}/draft``   — LLM-draft (or re-draft) the offer email
- ``PUT  /properties/{pid}/renewals/{lease}``         — save the PM's edits (drafts only)
- ``POST /properties/{pid}/renewals/{lease}/send``    — send via the PM's Gmail (Composio)
- ``POST /properties/{pid}/renewals/{lease}/dismiss`` — not renewing / stop tracking

Send is the ONLY outbound step and fires only from an explicit user click —
Helixis never auto-sends. The lease renewal itself happens in Buildium; the
mirror's ``Lease.*`` webhooks move the end date and the offer then reads as
renewed (computed, see ``repositories/renewals.list_renewed_offers``).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from helixis.agents.idempotency import derive_key, guarded_action
from helixis.api.auth import AuthenticatedUser, get_current_user
from helixis.crm.email_send import send_gmail_email
from helixis.db.engine import get_pool
from helixis.db.repositories import ActivityEntry, write_activity
from helixis.db.repositories import cockpit as cockpit_repo
from helixis.db.repositories import renewals as renewals_repo
from helixis.db.repositories.action_idempotency import PgIdempotencyStore
from helixis.renewals.draft import generate_renewal_draft
from helixis.settings import Settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/properties/{property_id}/renewals", tags=["renewals"])


def _settings_from(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def _company_id(user: AuthenticatedUser) -> str:
    if not user.company_ids:
        raise HTTPException(status_code=400, detail="User is not a member of any company")
    return user.company_ids[0]


class OfferOut(BaseModel):
    id: str
    lease_buildium_id: int
    status: str
    proposed_rent: float | None = None
    term_months: int | None = None
    subject: str | None = None
    body: str | None = None
    recipient_email: str | None = None
    sent_at: datetime | None = None
    renewed: bool = False


class CandidateOut(BaseModel):
    lease_buildium_id: int
    lease_to_date: str | None = None
    current_rent: float | None = None
    unit_id: int | None = None
    unit_number: str | None = None
    tenant_names: str | None = None
    tenant_email: str | None = None
    offer: OfferOut | None = None


class RenewalsOut(BaseModel):
    candidates: list[CandidateOut]
    renewed: list[OfferOut]


class DraftIn(BaseModel):
    proposed_rent: float | None = Field(default=None, ge=0)
    term_months: int | None = Field(default=None, ge=1, le=60)
    notes: str = ""


class EditIn(BaseModel):
    subject: str
    body: str
    proposed_rent: float | None = Field(default=None, ge=0)
    term_months: int | None = Field(default=None, ge=1, le=60)
    recipient_email: str | None = None


def _offer_out(o: renewals_repo.RenewalOffer) -> OfferOut:
    return OfferOut(
        id=o.id,
        lease_buildium_id=o.lease_buildium_id,
        status=o.status,
        proposed_rent=o.proposed_rent,
        term_months=o.term_months,
        subject=o.subject,
        body=o.body,
        recipient_email=o.recipient_email,
        sent_at=o.sent_at,
        renewed=o.renewed,
    )


async def _candidate_or_404(
    pool: Any, *, company_id: str, property_id: int, lease_id: int
) -> renewals_repo.RenewalCandidate:
    candidates = await renewals_repo.list_candidates(
        pool, company_id=company_id, property_id=property_id
    )
    for c in candidates:
        if c.lease_buildium_id == lease_id:
            return c
    raise HTTPException(
        status_code=404,
        detail="That lease isn't an expiring lease on this property (window: 90 days).",
    )


@router.get("", response_model=RenewalsOut)
async def list_renewals(
    property_id: int,
    user: AuthenticatedUser = Depends(get_current_user),
) -> RenewalsOut:
    company_id = _company_id(user)
    pool = get_pool()
    candidates = await renewals_repo.list_candidates(
        pool, company_id=company_id, property_id=property_id
    )
    renewed = await renewals_repo.list_renewed_offers(
        pool, company_id=company_id, property_id=property_id
    )
    return RenewalsOut(
        candidates=[
            CandidateOut(
                lease_buildium_id=c.lease_buildium_id,
                lease_to_date=c.lease_to_date.isoformat() if c.lease_to_date else None,
                current_rent=c.current_rent,
                unit_id=c.unit_id,
                unit_number=c.unit_number,
                tenant_names=c.tenant_names,
                tenant_email=c.tenant_email,
                offer=OfferOut(**c.offer) if c.offer else None,
            )
            for c in candidates
        ],
        renewed=[_offer_out(o) for o in renewed],
    )


@router.post("/{lease_id}/draft", response_model=OfferOut)
async def draft_offer(
    property_id: int,
    lease_id: int,
    payload: DraftIn,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> OfferOut:
    """Generate (or regenerate) the offer email. Always succeeds with an
    editable draft — the LLM failing just means the deterministic template."""
    settings = _settings_from(request)
    company_id = _company_id(user)
    pool = get_pool()
    candidate = await _candidate_or_404(
        pool, company_id=company_id, property_id=property_id, lease_id=lease_id
    )
    detail = await cockpit_repo.get_property_detail(pool, company_id, property_id)
    property_name = detail.summary.name if detail else None

    draft = await generate_renewal_draft(
        settings,
        tenant_names=candidate.tenant_names,
        unit_number=candidate.unit_number,
        property_name=property_name,
        current_rent=candidate.current_rent,
        lease_to_date=candidate.lease_to_date,
        proposed_rent=payload.proposed_rent,
        term_months=payload.term_months,
        pm_notes=payload.notes,
    )
    offer = await renewals_repo.upsert_draft(
        pool,
        company_id=company_id,
        lease_buildium_id=lease_id,
        property_id=property_id,
        unit_id=candidate.unit_id,
        proposed_rent=payload.proposed_rent,
        term_months=payload.term_months,
        subject=draft.subject,
        body=draft.body,
        recipient_email=candidate.tenant_email,
        created_by=user.user_id,
    )
    return _offer_out(offer)


@router.put("/{lease_id}", response_model=OfferOut)
async def edit_offer(
    property_id: int,
    lease_id: int,
    payload: EditIn,
    user: AuthenticatedUser = Depends(get_current_user),
) -> OfferOut:
    company_id = _company_id(user)
    if not payload.subject.strip() or not payload.body.strip():
        raise HTTPException(status_code=400, detail="Subject and body can't be empty.")
    offer = await renewals_repo.update_draft(
        get_pool(),
        company_id=company_id,
        lease_buildium_id=lease_id,
        proposed_rent=payload.proposed_rent,
        term_months=payload.term_months,
        subject=payload.subject.strip(),
        body=payload.body,
        recipient_email=(payload.recipient_email or "").strip() or None,
    )
    if offer is None:
        raise HTTPException(status_code=404, detail="No editable draft for that lease.")
    return _offer_out(offer)


@router.post("/{lease_id}/send", response_model=OfferOut)
async def send_offer(
    property_id: int,
    lease_id: int,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> OfferOut:
    """Send the drafted offer via the PM's Gmail. Explicit user action only."""
    company_id = _company_id(user)
    pool = get_pool()
    # Role floor (approval-pipeline PR): sending a renewal offer to a tenant
    # takes manager+ (same gate as /agent/confirm approvals, ADR 0005).
    from helixis.api.roles import MANAGER
    from helixis.api.routes.company import require_role

    await require_role(
        pool,
        user.user_id,
        company_id,
        MANAGER,
        detail="Only managers and admins can send renewal offers.",
    )
    offer = await renewals_repo.get_offer(pool, company_id=company_id, lease_buildium_id=lease_id)
    if offer is None or offer.status != "draft":
        raise HTTPException(status_code=404, detail="No sendable draft for that lease.")
    if not offer.recipient_email:
        raise HTTPException(
            status_code=400,
            detail="No tenant email on file — add one in Buildium, re-sync, then retry.",
        )
    recipient_email: str = offer.recipient_email  # narrowed local for the closure
    if not (offer.subject or "").strip() or not (offer.body or "").strip():
        raise HTTPException(status_code=400, detail="Draft is missing a subject or body.")

    composio = getattr(request.app.state, "composio_client", None)
    if composio is None or not getattr(composio, "available", False):
        raise HTTPException(
            status_code=503,
            detail="Gmail isn't connected — connect it in Settings → Connectors.",
        )

    # Durable at-most-once guard: the pre-existing ``status != "draft"`` check
    # is a check-then-act — two concurrent submits (double-click, client retry)
    # could both read ``draft``, both send, and double-email the tenant a
    # renewal offer. Same stance as the inbox execute endpoint: the human
    # already approved by clicking send (``confirm=None``); the guard provides
    # dedup, not consent. The body/subject are part of the key, so an edited
    # draft legitimately sends again; ``window=None`` = the same draft never
    # re-sends.
    async def _send_offer_email() -> dict[str, Any]:
        ok, message_id = send_gmail_email(
            composio,
            user_id=user.user_id,
            recipient_email=recipient_email,
            subject=offer.subject or "",
            body=offer.body or "",
        )
        if not ok:
            # Pre- and post-send failures are indistinguishable here (the
            # Composio call may time out after Gmail accepted) → ambiguous.
            raise RuntimeError("gmail send failed")
        return {"message_id": message_id or ""}

    try:
        guard = await guarded_action(
            store=PgIdempotencyStore(pool),
            company_id=company_id,
            idem_key=derive_key(
                company_id,
                "renewals.send_offer",
                {
                    "lease_id": lease_id,
                    "recipient": offer.recipient_email,
                    "subject": offer.subject or "",
                    "body": offer.body or "",
                },
            ),
            action_type="renewals.send_offer",
            confirm=None,  # explicit user click is the approval
            execute=_send_offer_email,
            window=None,
        )
    except RuntimeError as exc:
        raise HTTPException(
            status_code=502,
            detail="Couldn't send the email — check the tenant's thread before retrying.",
        ) from exc
    if guard.outcome == "already_pending":
        raise HTTPException(status_code=409, detail="This offer is already being sent.")
    if guard.outcome not in ("executed", "replayed"):
        raise HTTPException(
            status_code=502,
            detail="A previous send attempt ended in an unknown state — verify before retrying.",
        )
    prior = guard.result if isinstance(guard.result, dict) else {}
    message_id = str(prior.get("message_id") or "") or None
    if guard.outcome == "replayed":
        # The send already happened (e.g. a double-click's second arrival):
        # return the current offer state without re-sending or re-logging.
        current = await renewals_repo.get_offer(
            pool, company_id=company_id, lease_buildium_id=lease_id
        )
        return _offer_out(current) if current else _offer_out(offer)

    sent = await renewals_repo.mark_sent(
        pool, company_id=company_id, lease_buildium_id=lease_id, message_id=message_id
    )
    await write_activity(
        pool,
        ActivityEntry(
            account_id=None,
            company_id=company_id,
            source="manual",
            source_label="Renewals",
            action_type="renewal_offer_sent",
            description=(
                f"Renewal offer sent to {offer.recipient_email} for lease #{lease_id}"
                + (f" (${offer.proposed_rent:,.2f}/mo)" if offer.proposed_rent else "")
            ),
            entity_type="lease",
            entity_id=lease_id,
            raw={
                "lease_id": lease_id,
                "property_id": property_id,
                "message_id": message_id,
            },
        ),
    )
    return _offer_out(sent) if sent else _offer_out(offer)


@router.post("/{lease_id}/dismiss", response_model=OfferOut)
async def dismiss_offer(
    property_id: int,
    lease_id: int,
    user: AuthenticatedUser = Depends(get_current_user),
) -> OfferOut:
    company_id = _company_id(user)
    offer = await renewals_repo.set_status(
        get_pool(), company_id=company_id, lease_buildium_id=lease_id, status="dismissed"
    )
    if offer is None:
        raise HTTPException(status_code=404, detail="No offer to dismiss for that lease.")
    return _offer_out(offer)


__all__ = ["router"]
