"""Work-order event handlers.

Re-fetch the work order from Buildium, upsert the Helixis-side mirror
row, and write an activity-log entry.
"""

from __future__ import annotations

import logging

from helixis.buildium.api import BuildiumApi
from helixis.buildium.errors import BuildiumApiError
from helixis.db.engine import get_pool
from helixis.db.repositories import ActivityEntry, upsert_work_order, write_activity
from helixis.settings import get_settings
from helixis.webhooks.queue import ClaimedEvent
from helixis.webhooks.work_order_photo_triage import triage_work_order_photos

logger = logging.getLogger(__name__)


def _action_label(event_name: str) -> tuple[str, str]:
    if event_name.endswith(".Created"):
        return "work_order_created", "opened"
    if event_name.endswith(".Updated"):
        return "work_order_updated", "updated"
    if event_name.endswith(".Deleted"):
        return "work_order_deleted", "closed"
    return "work_order_event", "event on"


async def _handle(event: ClaimedEvent, api: BuildiumApi, *, triage_photos: bool = False) -> None:
    wo_id = event.entity_ids.get("WorkOrderId")
    if wo_id is None:
        # Keys only — never dump the full payload (tenant PII) into logs.
        logger.warning(
            "%s event missing WorkOrderId (payload keys: %s)",
            event.event_name,
            sorted(event.payload or {}),
        )
        return

    try:
        wo = await api.work_orders.get(wo_id)
    except BuildiumApiError as exc:
        if exc.status == 404:
            return
        raise

    pool = get_pool()
    await upsert_work_order(pool, wo, account_id=event.account_id, company_id="")

    action_type, verb = _action_label(event.event_name)
    priority = f" [{wo.priority}]" if wo.priority else ""
    description = (
        f"Buildium {verb} work order #{wo.id}{priority} "
        f"(property {wo.property_id}, status {wo.status or 'unknown'})"
    )

    await write_activity(
        pool,
        ActivityEntry(
            account_id=event.account_id,
            company_id=event.company_id or str(event.account_id),
            source="webhook",
            source_label=f"Buildium · {event.event_name}",
            action_type=action_type,
            description=description,
            entity_type="work_order",
            entity_id=wo.id,
            raw={
                "event_name": event.event_name,
                "event_id": str(event.id),
                "buildium_work_order": wo.model_dump(mode="json", by_alias=True),
            },
        ),
    )
    logger.info(
        "Work order %s synced (%s) for account %s",
        wo.id,
        event.event_name,
        event.account_id,
    )

    # Photo triage runs on CREATION only (#533 entry point 2): a new work order
    # may carry a tenant/staffer photo of the problem on its task history. Assess
    # each into a DRAFT work order for the PM. Gating to creation bounds cost and
    # avoids re-assessing the same photos on every status flip. Best-effort — a
    # missing key / no photos / a fetch failure degrades to "nothing added" and
    # never breaks the WO sync above.
    if triage_photos:
        await triage_work_order_photos(
            wo,
            api,
            pool,
            settings=get_settings(),
            company_id=event.company_id or str(event.account_id),
            account_id=event.account_id,
        )


async def handle_work_order_created(event: ClaimedEvent, api: BuildiumApi) -> None:
    await _handle(event, api, triage_photos=True)


async def handle_work_order_updated(event: ClaimedEvent, api: BuildiumApi) -> None:
    await _handle(event, api)
