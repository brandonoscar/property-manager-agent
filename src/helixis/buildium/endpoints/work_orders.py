"""Work orders."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from helixis.buildium.client import BuildiumClient
from helixis.buildium.errors import BuildiumApiError
from helixis.buildium.schemas import WorkOrder

# UpdateWorkOrderRequest.WorkDetails cap per the Buildium spec.
_WORK_DETAILS_MAX_CHARS = 65_535


def _wo_put_body(
    wo_raw: dict[str, Any], *, vendor_id: int, work_details: str | None = None
) -> dict[str, Any]:
    """Build a full-replace ``PUT /workorders/{id}`` body from a GET response.

    ``PUT /workorders/{id}`` is full-replace over the ``WorkOrderPutMessage``
    fields, so every writable field must be carried from the GET or it's nulled.
    The ONLY meaningfully-mutable fields here are ``VendorId`` and
    ``WorkDetails`` — ``Priority`` / ``DueDate`` / ``Title`` / ``Status`` are
    task-derived and silently no-op on this endpoint (gotcha 20), so they are
    deliberately not part of this body. ``work_details=None`` keeps whatever the
    GET returned.
    """
    body: dict[str, Any] = {
        "EntryAllowed": wo_raw.get("EntryAllowed") or "Unknown",
        "VendorId": vendor_id,
    }
    details = work_details if work_details is not None else wo_raw.get("WorkDetails")
    if details:
        body["WorkDetails"] = details[:_WORK_DETAILS_MAX_CHARS]
    for key in ("InvoiceNumber", "ChargeableTo", "EntryNotes", "VendorNotes"):
        if wo_raw.get(key) is not None:
            body[key] = wo_raw[key]
    entry_contact = wo_raw.get("EntryContact")
    if isinstance(entry_contact, dict) and entry_contact.get("Id") is not None:
        body["EntryContactId"] = entry_contact["Id"]
    entry_contacts = wo_raw.get("EntryContacts")
    if isinstance(entry_contacts, list):
        ids = [c.get("Id") for c in entry_contacts if isinstance(c, dict) and c.get("Id")]
        if ids:
            body["EntryContactIds"] = ids
    line_items = wo_raw.get("LineItems")
    if isinstance(line_items, list) and line_items:
        # GET WorkOrderLineItemMessage and PUT WorkOrderLineItemSaveMessage
        # share the same four keys (spec-verified 2026-07-05).
        body["LineItems"] = [
            {
                k: item.get(k)
                for k in ("GlAccountId", "Quantity", "Memo", "UnitPrice")
                if item.get(k) is not None
            }
            for item in line_items
            if isinstance(item, dict)
        ]
    return body


class WorkOrders:
    def __init__(self, client: BuildiumClient) -> None:
        self._client = client

    async def list(self, **filters: Any) -> list[WorkOrder]:
        return await self._client.list_all("/workorders", filters or None, WorkOrder)

    async def get(self, work_order_id: int) -> WorkOrder:
        return cast(
            WorkOrder,
            await self._client.request("GET", f"/workorders/{work_order_id}", schema=WorkOrder),
        )

    async def create(self, body: dict[str, Any]) -> WorkOrder:
        return cast(
            WorkOrder,
            await self._client.request("POST", "/workorders", body=body, schema=WorkOrder),
        )

    async def update(self, work_order_id: int, body: dict[str, Any]) -> WorkOrder:
        """⚠ RAW full-replace PUT — the caller owns read-merge-write.

        ``PUT /workorders/{id}`` replaces the whole object: a partial body
        can silently blank fields (and task-derived fields no-op with a
        200 — gotcha 20). Product paths must NOT call this directly; use
        ``reassign_vendor`` / ``add_note`` (both read-merge here) or
        ``Tasks.update`` for task-derived fields. Under Buildium's ToS
        §18, data corrupted by a bad write is on the customer to repair —
        which is why the merged helpers exist.
        """
        return cast(
            WorkOrder,
            await self._client.request(
                "PUT", f"/workorders/{work_order_id}", body=body, schema=WorkOrder
            ),
        )

    async def add_note(self, work_order_id: int, *, note: str) -> dict[str, Any]:
        """Append a timestamped staff note to a work order's WorkDetails.

        Write-probe 2026-07-05 (rounds 1+2): neither ``/workorders/{id}/notes``
        nor ``/tasks/{id}/notes`` exists — Buildium's public API has NO note
        endpoint on work orders or tasks (SDK spec: ``WorkOrdersApi`` is
        create/get/list/update only). The note therefore rides the work
        order's own ``WorkDetails`` field via read-merge-write on
        ``PUT /workorders/{id}`` — a full-replace update, so every writable
        field from the GET is carried over (``UpdateWorkOrderRequest``
        requires ``EntryAllowed`` + ``VendorId``; line items map 1:1).

        Raises :class:`BuildiumApiError` (422) when the work order has no
        vendor — the PUT requires one; callers already sanitize API errors.
        """
        wo_raw = cast(
            dict[str, Any],
            await self._client.request("GET", f"/workorders/{work_order_id}"),
        )
        vendor_id = wo_raw.get("VendorId")
        if vendor_id is None:
            raise BuildiumApiError(
                status=422,
                message=f"Work order {work_order_id} has no vendor — Buildium requires "
                "VendorId to update a work order, so a note cannot be added",
            )

        stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
        line = f"[Occupella note {stamp}] {note}"
        existing = wo_raw.get("WorkDetails") or ""
        combined = f"{existing}\n\n{line}" if existing else line

        body = _wo_put_body(wo_raw, vendor_id=vendor_id, work_details=combined)
        return cast(
            dict[str, Any],
            await self._client.request(
                "PUT",
                f"/workorders/{work_order_id}",
                body=body,
            ),
        )

    async def reassign_vendor(self, work_order_id: int, vendor_id: int) -> dict[str, Any]:
        """Reassign a work order to a different vendor (read-merge-write PUT).

        ``VendorId`` is the one meaningfully-mutable field on
        ``PUT /workorders/{id}`` — priority / due date / title / status are
        task-derived and no-op here (gotcha 20; use ``Tasks.update`` for those).
        Full-replace, so every other writable field is carried over from the
        GET.
        """
        wo_raw = cast(
            dict[str, Any],
            await self._client.request("GET", f"/workorders/{work_order_id}"),
        )
        body = _wo_put_body(wo_raw, vendor_id=int(vendor_id))
        return cast(
            dict[str, Any],
            await self._client.request("PUT", f"/workorders/{work_order_id}", body=body),
        )
