"""Work order, task, vendor, and vendor-transaction handlers."""

from __future__ import annotations

import logging
from typing import Any

from helixis.buildium.errors import BuildiumApiError
from helixis.db.engine import get_pool
from helixis.db.repositories import ActivityEntry, upsert_work_order, write_activity
from helixis.db.repositories import entities as entities_repo
from helixis.db.repositories.work_order_history import record_status_transition_if_changed
from helixis.webhooks.handlers._helpers import (
    envelope_account_id,
    envelope_int,
    open_api_for_company,
)

logger = logging.getLogger(__name__)


async def handle_workorder_changed(company_id: str, envelope: dict[str, Any]) -> None:
    """WorkOrder.Created / WorkOrder.Updated.

    Refetch the work order, upsert the mirror row, and write an
    activity entry. A Created event is the place to notify the property
    manager; an Updated where Status flipped to "Completed" is the
    place to notify the tenant. Both notifications are stubbed for
    now — see Notification TODOs below.
    """
    wo_id = envelope_int(envelope, "WorkOrderId")
    if wo_id is None:
        logger.warning("%s missing WorkOrderId", envelope.get("EventName"))
        return
    account_id = envelope_account_id(envelope)
    event_name = str(envelope.get("EventName", ""))

    async with open_api_for_company(company_id) as api:
        if api is None:
            return
        try:
            wo = await api.work_orders.get(wo_id)
        except BuildiumApiError as exc:
            if exc.status == 404:
                return
            raise

    pool = get_pool()
    # Status-transition history (0058) — must run BEFORE the upsert: it reads
    # the mirror's current status as "previous", which the upsert overwrites.
    # Best-effort inside the repo; a history failure never blocks the mirror.
    await record_status_transition_if_changed(
        pool,
        company_id=company_id,
        work_order_buildium_id=wo.id,
        new_status=wo.status,
        source="webhook",
    )
    await upsert_work_order(pool, wo, account_id=account_id, company_id=company_id)

    # PR #80 Bug 1 — pre-PR-#80 this rendered "work order #2916" with
    # the raw Buildium id; HARD PRESENTATION RULES (PR #75) ban that.
    # Prefer the title that the just-fetched work-order carries; fall
    # back to a neutral phrase if Buildium didn't populate it.
    verb = "opened" if event_name.endswith(".Created") else "updated"
    title = (getattr(wo, "title", None) or "").strip() or "a work order"
    priority = f" [{wo.priority}]" if wo.priority else ""
    status_part = f" ({wo.status})" if wo.status else ""
    description = f"Buildium {verb} work order: {title}{priority}{status_part}"
    await write_activity(
        pool,
        ActivityEntry(
            account_id=account_id,
            company_id=company_id,
            source="webhook",
            source_label=f"Buildium · {event_name}",
            action_type=f"work_order_{verb}",
            description=description,
            entity_type="work_order",
            entity_id=wo.id,
            raw={"event_name": event_name, "work_order_id": wo.id},
        ),
    )


async def handle_workorder_deleted(company_id: str, envelope: dict[str, Any]) -> None:
    wo_id = envelope_int(envelope, "WorkOrderId")
    if wo_id is None:
        return
    account_id = envelope_account_id(envelope)
    pool = get_pool()
    # ⚠ This handler is NAMED handle_workorder_deleted and, until 2026-08-29,
    # only wrote an activity line while leaving the row in
    # `helixis_work_orders`. Every open-maintenance count reads that table, so
    # a deleted work order kept being reported as outstanding work — the same
    # confident false PRESENCE as the lease and property handlers.
    await entities_repo.delete_work_order(pool, wo_id, company_id=company_id)
    # PR #80 Bug 1 — Buildium doesn't ship a title on .Deleted events
    # and the entity is gone, so we can't backfill. Keep the message
    # generic; the entity_id+raw_payload carry the audit trail.
    await write_activity(
        pool,
        ActivityEntry(
            account_id=account_id,
            company_id=company_id,
            source="webhook",
            source_label="Buildium · WorkOrder.Deleted",
            action_type="work_order_deleted",
            description="Buildium deleted a work order",
            entity_type="work_order",
            entity_id=wo_id,
            raw={"event_name": "WorkOrder.Deleted", "work_order_id": wo_id},
        ),
    )


async def handle_task_changed(company_id: str, envelope: dict[str, Any]) -> None:
    """Task.* — generic to-do; we log to the activity feed and try
    to resolve the human-readable title so the row doesn't read as
    a raw Buildium id (PR #80 Bug 1)."""
    task_id = envelope_int(envelope, "TaskId")
    if task_id is None:
        return
    account_id = envelope_account_id(envelope)
    event_name = str(envelope.get("EventName", ""))

    # PR #80 Bug 1 — pre-PR-#80 we wrote "Buildium created task
    # #17434" verbatim; the raw id leaks into the user-visible
    # Activity row and violates the HARD PRESENTATION RULES from
    # PR #75. On Created / Updated we refetch the task to get its
    # title; on Deleted Buildium doesn't expose the gone entity so
    # we fall back to a generic phrase.
    title: str | None = None
    task_obj = None
    if not event_name.endswith(".Deleted"):
        async with open_api_for_company(company_id) as api:
            if api is not None:
                try:
                    task_obj = await api.tasks.get(task_id)
                    title = (getattr(task_obj, "title", None) or "").strip() or None
                except BuildiumApiError as exc:
                    if exc.status != 404:
                        # Don't crash the dispatch path over a backfill
                        # failure; log and keep the generic message.
                        logger.warning(
                            "Could not fetch task %s title for activity row: %s",
                            task_id,
                            exc,
                        )

    verb = event_name.split(".")[-1].lower() if event_name else "changed"
    if title:
        description = f"Buildium {verb} a task: {title}"
    else:
        description = f"Buildium {verb} a task"

    pool = get_pool()
    # Entity-graph mirror (0041): the refetch above already paid for the
    # payload — keep the tasks mirror fresh on the same fetch.
    if event_name.endswith(".Deleted"):
        await entities_repo.delete_task(pool, task_id, company_id=company_id)
    elif task_obj is not None:
        await entities_repo.upsert_task(
            pool, task_obj, account_id=account_id, company_id=company_id
        )
    await write_activity(
        pool,
        ActivityEntry(
            account_id=account_id,
            company_id=company_id,
            source="webhook",
            source_label=f"Buildium · {event_name}",
            action_type="task_event",
            description=description,
            entity_type="task",
            entity_id=task_id,
            raw={
                "event_name": event_name,
                "task_id": task_id,
                # Stash the resolved title on the raw payload so the
                # Inbox renderer can pick it up without a second fetch.
                "task_title": title,
            },
        ),
    )


async def handle_task_history(company_id: str, envelope: dict[str, Any]) -> None:
    """Task.History.* — comments / status changes on a task. PR #80 Bug
    1 — resolve the parent task title so the activity row reads
    "Task 'Send lease to Ryan' got a new comment" instead of leaking
    raw Buildium ids."""
    task_id = envelope_int(envelope, "TaskId")
    history_id = envelope_int(envelope, "TaskHistoryId")
    if task_id is None or history_id is None:
        return
    account_id = envelope_account_id(envelope)
    event_name = str(envelope.get("EventName", ""))

    title: str | None = None
    async with open_api_for_company(company_id) as api:
        if api is not None:
            try:
                task = await api.tasks.get(task_id)
                title = (getattr(task, "title", None) or "").strip() or None
            except BuildiumApiError as exc:
                if exc.status != 404:
                    logger.warning(
                        "Could not fetch parent task %s for task-history row: %s",
                        task_id,
                        exc,
                    )

    verb = event_name.split(".")[-1].lower() if event_name else "changed"
    if title:
        description = f"Buildium {verb} a comment on task: {title}"
    else:
        description = f"Buildium {verb} a comment on a task"

    pool = get_pool()
    await write_activity(
        pool,
        ActivityEntry(
            account_id=account_id,
            company_id=company_id,
            source="webhook",
            source_label=f"Buildium · {event_name}",
            action_type="task_history",
            description=description,
            entity_type="task",
            entity_id=task_id,
            raw={
                "event_name": event_name,
                "task_id": task_id,
                "task_history_id": history_id,
                "task_title": title,
            },
        ),
    )


async def handle_vendor_changed(company_id: str, envelope: dict[str, Any]) -> None:
    """Vendor.* — refetch the vendor and log."""
    vendor_id = envelope_int(envelope, "VendorId")
    if vendor_id is None:
        return
    account_id = envelope_account_id(envelope)
    event_name = str(envelope.get("EventName", ""))

    vendor_label: str | None = None
    vendor_obj = None
    if not event_name.endswith(".Deleted"):
        async with open_api_for_company(company_id) as api:
            if api is not None:
                try:
                    vendor_obj = await api.vendors.get(vendor_id)
                    vendor_label = vendor_obj.company_name or vendor_obj.name
                except BuildiumApiError as exc:
                    if exc.status != 404:
                        raise

    # PR #80 Bug 1 — pre-PR-#80 rendered "vendor #42 — Acme Plumbing";
    # drop the id, keep the name. Falls back to a neutral phrase if
    # the refetch failed.
    pool = get_pool()
    # Entity-graph mirror (0041) — same pattern as tasks above.
    if event_name.endswith(".Deleted"):
        await entities_repo.delete_vendor(pool, vendor_id, company_id=company_id)
    elif vendor_obj is not None:
        await entities_repo.upsert_vendor(
            pool, vendor_obj, account_id=account_id, company_id=company_id
        )
    verb = event_name.split(".")[-1].lower() if event_name else "changed"
    if vendor_label:
        description = f"Buildium {verb} vendor: {vendor_label}"
    else:
        description = f"Buildium {verb} a vendor"
    await write_activity(
        pool,
        ActivityEntry(
            account_id=account_id,
            company_id=company_id,
            source="webhook",
            source_label=f"Buildium · {event_name}",
            action_type="vendor_event",
            description=description,
            entity_type="vendor",
            entity_id=vendor_id,
            raw={"event_name": event_name, "vendor_id": vendor_id},
        ),
    )


async def handle_vendor_transaction(company_id: str, envelope: dict[str, Any]) -> None:
    """Vendor.Transaction.* — payment / credit / refund on a vendor."""
    vendor_id = envelope_int(envelope, "VendorId")
    transaction_id = envelope_int(envelope, "TransactionId")
    if vendor_id is None or transaction_id is None:
        return
    account_id = envelope_account_id(envelope)
    event_name = str(envelope.get("EventName", ""))
    pool = get_pool()
    # PR #80 Bug 1 — keep the action verb + the entity type out of
    # the user-facing string but drop the raw ids.
    verb = event_name.split(".")[-1].lower() if event_name else "changed"
    description = f"Buildium {verb} a vendor transaction"
    await write_activity(
        pool,
        ActivityEntry(
            account_id=account_id,
            company_id=company_id,
            source="webhook",
            source_label=f"Buildium · {event_name}",
            action_type="vendor_transaction",
            description=description,
            entity_type="vendor_transaction",
            entity_id=transaction_id,
            raw={
                "event_name": event_name,
                "vendor_id": vendor_id,
                "transaction_id": transaction_id,
            },
        ),
    )


__all__ = [
    "handle_task_changed",
    "handle_task_history",
    "handle_vendor_changed",
    "handle_vendor_transaction",
    "handle_workorder_changed",
    "handle_workorder_deleted",
]
