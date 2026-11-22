"""Bills + bill payments."""

from __future__ import annotations

# See applicants.py: the ``list`` method shadows the builtin in PEP 563
# annotation resolution, so list-returning annotations use ``builtins.list``.
import builtins
from typing import Any, cast

from helixis.buildium.client import BuildiumClient
from helixis.buildium.schemas import Bill, BillPayment


class Bills:
    def __init__(self, client: BuildiumClient) -> None:
        self._client = client

    async def list(self, **filters: Any) -> list[Bill]:
        return await self._client.list_all("/bills", filters or None, Bill)

    async def get(self, bill_id: int) -> Bill:
        return cast(Bill, await self._client.request("GET", f"/bills/{bill_id}", schema=Bill))

    async def create(self, body: dict[str, Any]) -> dict[str, Any]:
        """POST /v1/bills (``CreateBillRequest``, spec-grounded 2026-07-05).

        Required: ``Date``, ``DueDate``, ``VendorId``, ``Lines`` — each line
        ``{AccountingEntity: {Id, AccountingEntityType (Rental|Association|
        Company), UnitId?}, GlAccountId, Amount, Memo?}``. Optional:
        ``Memo`` (245 max), ``WorkOrderId`` (the WO→bill link),
        ``ReferenceNumber`` (40 max).
        """
        return cast(
            dict[str, Any],
            await self._client.request("POST", "/bills", body=body),
        )

    async def list_payments(self, bill_id: int, **filters: Any) -> builtins.list[BillPayment]:
        """List payments applied to a bill.

        Buildium spec: ``GET /v1/bills/{billId}/payments``. The bare
        ``/bills/payments`` path is POST-only — it *creates* a
        multi-bill payment, not a list. Calling GET against it
        silently 404s, which the previous helper did on every call.
        """
        return await self._client.list_all(
            f"/bills/{bill_id}/payments",
            filters or None,
            BillPayment,
        )

    async def get_payment(self, bill_id: int, payment_id: int) -> BillPayment:
        """Get one payment on a bill."""
        return cast(
            BillPayment,
            await self._client.request(
                "GET",
                f"/bills/{bill_id}/payments/{payment_id}",
                schema=BillPayment,
            ),
        )
