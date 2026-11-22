"""Leases, lease transactions, move-outs."""

from __future__ import annotations

# See applicants.py: the ``list`` method shadows the builtin in PEP 563
# annotation resolution, so list-returning annotations use ``builtins.list``.
import builtins
from typing import Any, cast

from helixis.buildium.client import BuildiumClient
from helixis.buildium.schemas import (
    Lease,
    LeaseMoveOut,
    LeaseTransaction,
    OutstandingBalance,
)


class Leases:
    def __init__(self, client: BuildiumClient) -> None:
        self._client = client

    async def list(self, **filters: Any) -> list[Lease]:
        """List leases.

        Buildium accepts a ``leasestatuses`` array filter with values
        ``Active``, ``Past``, ``Future``. Pass it as a list — the client
        flattens lists into repeated query params.
        """
        return await self._client.list_all("/leases", filters or None, Lease)

    async def get(self, lease_id: int) -> Lease:
        return cast(Lease, await self._client.request("GET", f"/leases/{lease_id}", schema=Lease))

    async def list_transactions(
        self, lease_id: int, **filters: Any
    ) -> builtins.list[LeaseTransaction]:
        return await self._client.list_all(
            f"/leases/{lease_id}/transactions", filters or None, LeaseTransaction
        )

    async def get_transaction(self, lease_id: int, transaction_id: int) -> LeaseTransaction:
        return cast(
            LeaseTransaction,
            await self._client.request(
                "GET",
                f"/leases/{lease_id}/transactions/{transaction_id}",
                schema=LeaseTransaction,
            ),
        )

    async def list_move_outs(self, lease_id: int) -> builtins.list[LeaseMoveOut]:
        """List tenant move-outs for a lease.

        Buildium spec: ``GET /v1/leases/{leaseId}/moveouts``. The
        legacy singular ``/moveout`` path 404s.
        """
        return await self._client.list_all(
            f"/leases/{lease_id}/moveouts",
            None,
            LeaseMoveOut,
        )

    async def get_move_out_for_tenant(self, lease_id: int, tenant_id: int) -> LeaseMoveOut:
        """Get one tenant's move-out record on a lease."""
        return cast(
            LeaseMoveOut,
            await self._client.request(
                "GET",
                f"/leases/{lease_id}/moveouts/{tenant_id}",
                schema=LeaseMoveOut,
            ),
        )

    async def list_outstanding_balances(self, **filters: Any) -> builtins.list[OutstandingBalance]:
        """List outstanding lease balances — i.e. who owes money."""
        return await self._client.list_all(
            "/leases/outstandingbalances", filters or None, OutstandingBalance
        )

    async def post_charge(
        self,
        lease_id: int,
        *,
        date: str,
        lines: builtins.list[dict[str, Any]],
        memo: str | None = None,
    ) -> dict[str, Any]:
        """Post a charge to a lease ledger (``POST /leases/{id}/charges``).

        Prod-probed 2026-07-25 (``CreateLeaseLedgerChargeRequest``): the body is
        ``{Date, Memo?, Lines:[{GlAccountId, Amount, Memo?}]}`` — a flat body
        (no ``Lines``) 422s. Note the casing: charge lines use **``GlAccountId``**
        (lower-l), UNLIKE payment lines (``GLAccountId``) — gotcha 21. Charges are
        editable via PUT but NOT deletable; posting is not idempotent, so the
        agent tool claims an idempotency key before calling this.
        """
        body: dict[str, Any] = {"Date": date, "Lines": lines}
        if memo is not None:
            body["Memo"] = memo
        return cast(
            dict[str, Any],
            await self._client.request("POST", f"/leases/{lease_id}/charges", body=body),
        )

    async def record_payment(
        self,
        lease_id: int,
        *,
        date: str,
        payment_method: str,
        lines: builtins.list[dict[str, Any]],
        send_email_receipt: bool = False,
        memo: str | None = None,
    ) -> dict[str, Any]:
        """Record a resident payment (``POST /leases/{id}/payments``).

        Prod-verified 2026-07-25 (``CreatePaymentRequest``): the body is
        ``{Date, PaymentMethod, SendEmailReceipt, Memo?, Lines:[{GLAccountId,
        Amount}]}``. Payment lines use **``GLAccountId``** (capital-L), UNLIKE
        charge lines (``GlAccountId``) — gotcha 21. ⚠ Payments are IMMUTABLE:
        ``DELETE …/payments/{id}`` 404s, there is no edit. Posting is not
        idempotent — the agent tool claims an idempotency key first.
        """
        body: dict[str, Any] = {
            "Date": date,
            "PaymentMethod": payment_method,
            "SendEmailReceipt": send_email_receipt,
            "Lines": lines,
        }
        if memo is not None:
            body["Memo"] = memo
        return cast(
            dict[str, Any],
            await self._client.request("POST", f"/leases/{lease_id}/payments", body=body),
        )

    async def renew_lease(
        self,
        lease_id: int,
        *,
        lease_type: str,
        rent: dict[str, Any],
        send_welcome_email: bool = False,
    ) -> dict[str, Any]:
        """Renew a lease (``POST /leases/{id}/renewals``).

        SDK-grounded (``CreateLeaseRenewalRequest``, 2026-07-25) — NOT yet
        live-probed, so the ``buildium_renew_lease`` tool is registered dark
        until a prod dogfood confirms the field floor. Body: ``{LeaseType,
        Rent:{Cycle, Charges:[{Amount, GlAccountId, NextDueDate}]},
        SendWelcomeEmail}``. ``rent`` is passed pre-shaped by the caller.
        """
        body: dict[str, Any] = {
            "LeaseType": lease_type,
            "Rent": rent,
            "SendWelcomeEmail": send_welcome_email,
        }
        return cast(
            dict[str, Any],
            await self._client.request("POST", f"/leases/{lease_id}/renewals", body=body),
        )

    async def record_move_out(
        self, lease_id: int, *, tenant_id: int, move_out_date: str
    ) -> dict[str, Any]:
        """Record a tenant move-out (``POST /leases/{id}/moveouts``).

        SDK-grounded (``CreateMoveOutDataRequest``: ``{TenantId, MoveOutDate}``)
        and **live-probed 2026-07-26 on lease 99797** — the minimal body 200s and
        ``NoticeGivenDate`` is optional (gotcha 22). ``buildium_record_move_out``
        is **registered live** and confirm-gated; it is not dark. Unlike a WO
        close or a payment, a move-out is REVERSIBLE via :meth:`undo_move_out`.

        ⚠ **A 2xx here is not proof the move-out exists.** On 2026-08-30 this
        returned 2xx, the guard ledger logged ``succeeded``, and re-reading the
        account showed NO move-out row — the only observable effect was
        ``LeaseToDate`` moving (gotcha 70). Any caller that reports success must
        re-GET :meth:`list_move_outs` and assert the row is there;
        ``scripts/write_verify.py`` is that check for the write harness.
        """
        body = {"TenantId": tenant_id, "MoveOutDate": move_out_date}
        return cast(
            dict[str, Any],
            await self._client.request("POST", f"/leases/{lease_id}/moveouts", body=body),
        )

    async def undo_move_out(self, lease_id: int, tenant_id: int) -> None:
        """Undo a tenant move-out (``DELETE /leases/{id}/moveouts/{tenantId}``).

        Reachable from inside the product since **PR #943**, which registered
        the ``buildium_undo_move_out`` agent tool — so the reversibility
        ``buildium_record_move_out`` cites as its reason for carrying only a
        confirm gate (rather than the Manager+ floor the hard-to-undo writes
        take) is now real at BOTH the API and the product.

        ⚠ This docstring previously said "this method has no callers", and it
        stayed that way for weeks after #943 landed. A session read it as
        current fact and spent a night blocked on a problem that was already
        fixed (gotcha 70). It is the reason gotcha 48 says a change that
        invalidates a comment has to go and find it — keep this in sync with
        ``buildium_tools._record_move_out_impl``.
        """
        await self._client.request("DELETE", f"/leases/{lease_id}/moveouts/{tenant_id}")
