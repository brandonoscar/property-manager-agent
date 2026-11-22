"""Enricher for WorkOrder.* Buildium webhooks.

A Buildium work order WRAPS a task (`GET /workorders/{id}` returns
``{Id, Task: {Id, ...}, WorkDetails, VendorId, ...}`` per the spec), so
the richest context for the planner is the task-based fan-out the
``BuildiumTaskEnricher`` already does — unit, active lease + tenants,
90-day history. This enricher resolves the linked task id from the work
order body, delegates to the task enricher, then grafts the work-order
specifics (vendor work details, the raw WO body) onto the result so the
planner can quote them.

Without this registration every WorkOrder.* row fell to the bare floor
surface ("Work order created — assigned to the admin", zero actions) —
the "thin webhook cards" the founder flagged on 2026-07-07.
"""

from __future__ import annotations

import logging
from typing import Any

from helixis.agent.enrichment.buildium_task_enricher import (
    BuildiumTaskEnricher,
    _open_api_for_company,
    _to_int,
)
from helixis.agent.enrichment.models import (
    EnrichedEvent,
    EnrichedTask,
    EnrichmentError,
)

logger = logging.getLogger(__name__)


class WorkOrderEnricher(BuildiumTaskEnricher):
    """WorkOrder.Created / WorkOrder.Updated → full task-shaped enrichment."""

    async def enrich(self, event: dict[str, Any], company_id: str) -> EnrichedEvent:
        event_name = str(event.get("EventName") or "WorkOrder.Unknown")
        wo_id = _to_int(event.get("WorkOrderId"))
        if wo_id is None:
            return EnrichedEvent(
                event_name=event_name,
                company_id=company_id,
                task=EnrichedTask(id=0),
                errors=[
                    EnrichmentError(
                        field="work_order",
                        message="Webhook envelope had no WorkOrderId.",
                    )
                ],
            )

        # Resolve the linked task id from the work order body.
        wo_raw: dict[str, Any] | None = None
        wo_error: EnrichmentError | None = None
        if self._api_factory is not None:
            api_cm = self._api_factory(company_id)
        else:
            api_cm = _open_api_for_company(company_id)
        async with api_cm as api:
            if api is None:
                return EnrichedEvent(
                    event_name=event_name,
                    company_id=company_id,
                    task=EnrichedTask(id=0),
                    errors=[
                        EnrichmentError(
                            field="credentials",
                            message=(
                                "No Buildium credentials configured for this "
                                "company — cannot enrich."
                            ),
                        )
                    ],
                )
            try:
                raw = await api.client.request("GET", f"/workorders/{wo_id}")
                if isinstance(raw, dict):
                    wo_raw = raw
                else:
                    wo_error = EnrichmentError(
                        field="work_order",
                        message=(f"GET /workorders/{wo_id} returned non-dict {type(raw).__name__}"),
                    )
            except Exception as exc:  # noqa: BLE001 — sub-fetch failures degrade, never raise
                wo_error = EnrichmentError(
                    field="work_order",
                    message=f"GET /workorders/{wo_id} failed: {exc}",
                )

        task_id = _to_int(((wo_raw or {}).get("Task") or {}).get("Id"))
        if task_id is None:
            return EnrichedEvent(
                event_name=event_name,
                company_id=company_id,
                task=EnrichedTask(id=0, raw={"WorkOrder": wo_raw} if wo_raw else {}),
                errors=[
                    wo_error
                    or EnrichmentError(
                        field="work_order",
                        message=f"Work order {wo_id} carries no linked Task.Id.",
                    )
                ],
            )

        # Full task fan-out (task + unit + lease + history) via the base
        # enricher, then graft the work-order specifics onto the result.
        enriched = await super().enrich({"TaskId": task_id, "EventName": event_name}, company_id)
        if wo_error is not None:
            enriched.errors.append(wo_error)
        if wo_raw is not None:
            enriched.task.raw = {**enriched.task.raw, "WorkOrder": wo_raw}
            work_details = wo_raw.get("WorkDetails")
            if isinstance(work_details, str) and work_details.strip():
                existing = (enriched.task.description or "").strip()
                if work_details.strip() not in existing:
                    enriched.task.description = (
                        f"{existing}\n\nWork details: {work_details.strip()}"
                        if existing
                        else f"Work details: {work_details.strip()}"
                    )
        return enriched


__all__ = ["WorkOrderEnricher"]
