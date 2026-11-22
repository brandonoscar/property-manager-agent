"""Applicant and application handlers."""

from __future__ import annotations

import logging
from typing import Any

from helixis.buildium.errors import BuildiumApiError
from helixis.buildium.schemas import Applicant
from helixis.db.engine import get_pool
from helixis.db.repositories import ActivityEntry, write_activity
from helixis.db.repositories import entities as entities_repo
from helixis.webhooks.crm_applicant_sync import sync_lead_from_applicant
from helixis.webhooks.handlers._helpers import (
    envelope_account_id,
    envelope_int,
    open_api_for_company,
)

logger = logging.getLogger(__name__)


async def handle_applicant_changed(company_id: str, envelope: dict[str, Any]) -> None:
    """Applicant.Created / Updated / Deleted.

    Beyond the activity line: mirrors the applicant (0040) and runs the CRM
    auto-move — a lead who applies in Buildium advances to 'applied' (or
    'leased' on AddedToLease) on their pipeline board within one webhook.
    """
    applicant_id = envelope_int(envelope, "ApplicantId")
    if applicant_id is None:
        return
    account_id = envelope_account_id(envelope)
    event_name = str(envelope.get("EventName", ""))
    pool = get_pool()

    full_name: str | None = None
    applicant: Applicant | None = None
    if event_name.endswith(".Deleted"):
        await entities_repo.delete_applicant(pool, applicant_id, company_id=company_id)
    else:
        async with open_api_for_company(company_id) as api:
            if api is not None:
                try:
                    applicant = await api.applicants.get(applicant_id)
                    full_name = (
                        f"{applicant.first_name or ''} {applicant.last_name or ''}".strip() or None
                    )
                except BuildiumApiError as exc:
                    if exc.status != 404:
                        raise
        if applicant is not None:
            await entities_repo.upsert_applicant(
                pool, applicant, account_id=account_id, company_id=company_id
            )
            # Webhook path creates a Leasing lead when none matches — a
            # direct-in-Buildium applicant shows up on the board. Backfill
            # (buildium/backfill.py) omits allow_create so a historical
            # import stays link/move-only (never mass-creates).
            await sync_lead_from_applicant(
                pool,
                applicant,
                company_id=company_id,
                account_id=account_id,
                allow_create=True,
            )
    name_part = f" — {full_name}" if full_name else ""
    await write_activity(
        pool,
        ActivityEntry(
            account_id=account_id,
            company_id=company_id,
            source="webhook",
            source_label=f"Buildium · {event_name}",
            action_type=f"applicant_{event_name.split('.')[-1].lower()}",
            description=f"Buildium {event_name.split('.')[-1].lower()} "
            f"applicant #{applicant_id}{name_part}",
            entity_type="applicant",
            entity_id=applicant_id,
            raw={"event_name": event_name, "applicant_id": applicant_id},
        ),
    )


async def handle_application_changed(company_id: str, envelope: dict[str, Any]) -> None:
    """ApplicantApplication.Created / Updated."""
    applicant_id = envelope_int(envelope, "ApplicantId")
    application_id = envelope_int(envelope, "ApplicationId")
    if applicant_id is None or application_id is None:
        return
    account_id = envelope_account_id(envelope)
    event_name = str(envelope.get("EventName", ""))

    pool = get_pool()
    await write_activity(
        pool,
        ActivityEntry(
            account_id=account_id,
            company_id=company_id,
            source="webhook",
            source_label=f"Buildium · {event_name}",
            action_type="application_event",
            description=f"Application #{application_id} "
            f"{event_name.split('.')[-1].lower()} for applicant #{applicant_id}",
            entity_type="application",
            entity_id=application_id,
            raw={
                "event_name": event_name,
                "applicant_id": applicant_id,
                "application_id": application_id,
            },
        ),
    )


__all__ = [
    "handle_applicant_changed",
    "handle_application_changed",
]
