"""Lease event handlers.

Webhook payloads are notifications — they carry the entity ID but not
the full record. We re-fetch from Buildium's REST API to get the current
state, upsert the lease mirror row, and write an activity-log entry so
the change surfaces in the Activity UI.
"""

from __future__ import annotations

import logging

from helixis.buildium.api import BuildiumApi
from helixis.buildium.errors import BuildiumApiError
from helixis.db.engine import get_pool
from helixis.db.repositories import ActivityEntry, upsert_lease, write_activity
from helixis.webhooks.queue import ClaimedEvent

logger = logging.getLogger(__name__)


def _action_label(event_name: str) -> tuple[str, str]:
    """Return (action_type, verb) for the activity log. Verb is used in
    the human description — e.g. "created" / "updated"."""
    if event_name.endswith(".Created"):
        return "lease_created", "created"
    if event_name.endswith(".Updated"):
        return "lease_updated", "updated"
    if event_name.endswith(".MoveOut.Created"):
        return "lease_move_out", "move-out notice filed on"
    return "lease_event", "event on"


async def _handle(event: ClaimedEvent, api: BuildiumApi) -> None:
    lease_id = event.entity_ids.get("LeaseId")
    if lease_id is None:
        # Log payload KEYS only — a full payload dump can put tenant PII
        # (names, emails, phone numbers) into Render logs; the keys are all
        # that's needed to diagnose an envelope-shape change (gotcha 15).
        logger.warning(
            "%s event missing LeaseId (payload keys: %s)",
            event.event_name,
            sorted(event.payload or {}),
        )
        return

    try:
        lease = await api.leases.get(lease_id)
    except BuildiumApiError as exc:
        if exc.status == 404:
            logger.info("Lease %s no longer exists (race with delete)", lease_id)
            return
        raise

    pool = get_pool()
    await upsert_lease(pool, lease, account_id=event.account_id, company_id="")

    action_type, verb = _action_label(event.event_name)
    rent = f"${lease.rent:,.2f}/mo" if lease.rent else "rent tbd"
    ends = lease.lease_to_date.date().isoformat() if lease.lease_to_date else "no end date"
    description = f"Buildium {verb} lease #{lease.id} (unit {lease.unit_id}, {rent}, ends {ends})"

    await write_activity(
        pool,
        ActivityEntry(
            account_id=event.account_id,
            company_id=event.company_id or str(event.account_id),
            source="webhook",
            source_label=f"Buildium · {event.event_name}",
            action_type=action_type,
            description=description,
            entity_type="lease",
            entity_id=lease.id,
            raw={
                "event_name": event.event_name,
                "event_id": str(event.id),
                "buildium_lease": lease.model_dump(mode="json", by_alias=True),
            },
        ),
    )
    logger.info("Lease %s synced (%s) for account %s", lease.id, event.event_name, event.account_id)


async def handle_lease_created(event: ClaimedEvent, api: BuildiumApi) -> None:
    await _handle(event, api)


async def handle_lease_updated(event: ClaimedEvent, api: BuildiumApi) -> None:
    await _handle(event, api)
