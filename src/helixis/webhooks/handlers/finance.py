"""Bill, bill-payment, and bank-transaction handlers."""

from __future__ import annotations

import logging
from typing import Any

from helixis.buildium.errors import BuildiumApiError
from helixis.buildium.money import bill_amount
from helixis.db.engine import get_pool
from helixis.db.repositories import ActivityEntry, write_activity
from helixis.db.repositories import entities as entities_repo
from helixis.webhooks.handlers._helpers import (
    envelope_account_id,
    envelope_int,
    open_api_for_company,
)

logger = logging.getLogger(__name__)


async def handle_bill_changed(company_id: str, envelope: dict[str, Any]) -> None:
    """Bill.Created / Updated / Deleted."""
    bill_id = envelope_int(envelope, "BillId")
    if bill_id is None:
        return
    account_id = envelope_account_id(envelope)
    event_name = str(envelope.get("EventName", ""))

    bill_total: float | None = None
    bill_obj = None
    if not event_name.endswith(".Deleted"):
        async with open_api_for_company(company_id) as api:
            if api is not None:
                try:
                    bill_obj = await api.bills.get(bill_id)
                    # Line-item fallback: production bills have no top-level
                    # total, so this notification read "$0" — or dropped the
                    # amount entirely — on every real bill.
                    bill_total = bill_amount(bill_obj)
                except BuildiumApiError as exc:
                    if exc.status != 404:
                        raise

    pool = get_pool()
    # Money mirror (0042): the refetch above already paid for the payload —
    # keep the bills mirror (+ per-line detail) fresh on the same fetch.
    if event_name.endswith(".Deleted"):
        await entities_repo.delete_bill(pool, bill_id, company_id=company_id)
    elif bill_obj is not None:
        await entities_repo.upsert_bill(
            pool, bill_obj, account_id=account_id, company_id=company_id
        )
    amount_part = f" — ${bill_total:,.2f}" if bill_total else ""
    await write_activity(
        pool,
        ActivityEntry(
            account_id=account_id,
            company_id=company_id,
            source="webhook",
            source_label=f"Buildium · {event_name}",
            action_type=f"bill_{event_name.split('.')[-1].lower()}",
            description=f"Buildium {event_name.split('.')[-1].lower()} "
            f"bill #{bill_id}{amount_part}",
            entity_type="bill",
            entity_id=bill_id,
            raw={"event_name": event_name, "bill_id": bill_id},
        ),
    )


async def handle_bill_payment(company_id: str, envelope: dict[str, Any]) -> None:
    """Bill.Payment.* — note: BillIds is a LIST (one payment can cover many).

    The Buildium docs show ``BillIds`` for the payment events. Each covered
    bill is refetched + upserted so the mirror's paid status stays fresh.
    """
    payment_id = envelope_int(envelope, "PaymentId")
    if payment_id is None:
        return
    account_id = envelope_account_id(envelope)
    event_name = str(envelope.get("EventName", ""))
    raw_bill_ids = envelope.get("BillIds")
    if isinstance(raw_bill_ids, list):
        bill_ids: list[int] = []
        for raw in raw_bill_ids:
            try:
                bill_ids.append(int(raw))
            except (TypeError, ValueError):
                continue
    else:
        bill_ids = []

    pool = get_pool()
    # Money mirror (0042): a payment flips the covered bills' paid status —
    # refetch each and upsert so ``helixis_bills.is_paid`` stays live-fresh.
    # 404-guarded per bill; a payment can reference a since-deleted bill.
    if bill_ids and not event_name.endswith(".Deleted"):
        async with open_api_for_company(company_id) as api:
            if api is not None:
                for covered_id in bill_ids:
                    try:
                        covered = await api.bills.get(covered_id)
                    except BuildiumApiError as exc:
                        if exc.status != 404:
                            raise
                        continue
                    await entities_repo.upsert_bill(
                        pool, covered, account_id=account_id, company_id=company_id
                    )
    affects = f" covering bill(s) {', '.join(f'#{b}' for b in bill_ids)}" if bill_ids else ""
    await write_activity(
        pool,
        ActivityEntry(
            account_id=account_id,
            company_id=company_id,
            source="webhook",
            source_label=f"Buildium · {event_name}",
            action_type="bill_payment",
            description=f"Bill payment #{payment_id} {event_name.split('.')[-1].lower()}{affects}",
            entity_type="bill_payment",
            entity_id=payment_id,
            raw={
                "event_name": event_name,
                "payment_id": payment_id,
                "bill_ids": bill_ids,
            },
        ),
    )


async def handle_gl_account_changed(company_id: str, envelope: dict[str, Any]) -> None:
    """GLAccount.Created / Updated / Deleted — refresh the whole chart.

    The envelope's entity-id field isn't publicly documented (CLAUDE.md:
    never map fields from memory), so instead of pulling one id we refetch
    the full chart of accounts (small reference data, one API call, rare
    events) and prune rows that vanished. Handles all three operations
    exactly with zero envelope-shape assumptions.
    """
    account_id = envelope_account_id(envelope)
    event_name = str(envelope.get("EventName", ""))

    async with open_api_for_company(company_id) as api:
        if api is None:
            return
        accounts = await api.general_ledger.list_accounts()

    pool = get_pool()
    for gl in accounts:
        await entities_repo.upsert_gl_account(
            pool, gl, account_id=account_id, company_id=company_id
        )
    await entities_repo.prune_gl_accounts(pool, [gl.id for gl in accounts], company_id=company_id)
    logger.info(
        "gl chart refreshed for company=%s after %s (%d accounts)",
        company_id,
        event_name,
        len(accounts),
    )


async def handle_bank_transaction(company_id: str, envelope: dict[str, Any]) -> None:
    """BankAccount.Transaction.* — refresh the account's balance + log.

    Every transaction moves the balance, so the handler refetches the one
    covered account (envelope ``BankAccountId``, already parsed) and upserts
    the 0043 mirror row. 404-guarded — the account may have been deleted.
    """
    bank_account_id = envelope_int(envelope, "BankAccountId")
    transaction_id = envelope_int(envelope, "TransactionId")
    if bank_account_id is None or transaction_id is None:
        return
    account_id = envelope_account_id(envelope)
    event_name = str(envelope.get("EventName", ""))

    pool = get_pool()
    async with open_api_for_company(company_id) as api:
        if api is not None:
            try:
                bank = await api.bank_accounts.get(bank_account_id)
            except BuildiumApiError as exc:
                if exc.status != 404:
                    raise
            else:
                await entities_repo.upsert_bank_account(
                    pool, bank, account_id=account_id, company_id=company_id
                )
    await write_activity(
        pool,
        ActivityEntry(
            account_id=account_id,
            company_id=company_id,
            source="webhook",
            source_label=f"Buildium · {event_name}",
            action_type="bank_transaction",
            description=f"Bank account #{bank_account_id} transaction "
            f"#{transaction_id} {event_name.split('.')[-1].lower()}",
            entity_type="bank_transaction",
            entity_id=transaction_id,
            raw={
                "event_name": event_name,
                "bank_account_id": bank_account_id,
                "transaction_id": transaction_id,
            },
        ),
    )


__all__ = [
    "handle_bank_transaction",
    "handle_bill_changed",
    "handle_bill_payment",
    "handle_gl_account_changed",
]
