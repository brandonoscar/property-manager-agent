"""Lease, lease-tenant, lease-transaction, and move-out handlers.

Webhook payloads carry the ID and not the full entity, so each
handler re-fetches from Buildium's REST API. The expanded schema
(Lease.current_tenants, Lease.cosigners) means we can answer "who
joined this lease?" without a per-tenant follow-up call.
"""

from __future__ import annotations

import logging
from typing import Any

from helixis.buildium.errors import BuildiumApiError
from helixis.db.engine import get_pool
from helixis.db.repositories import ActivityEntry, upsert_lease, write_activity
from helixis.db.repositories import turnovers as turnovers_repo
from helixis.db.repositories.entities import (
    delete_lease,
    delete_lease_tenant_link,
    delete_lease_transaction,
    refresh_lease_tenant_links,
    upsert_lease_transaction,
    upsert_tenant,
)
from helixis.webhooks.handlers._helpers import (
    envelope_account_id,
    envelope_int,
    open_api_for_company,
)

logger = logging.getLogger(__name__)


async def handle_lease_changed(company_id: str, envelope: dict[str, Any]) -> None:
    """Lease.Created / Lease.Updated.

    Refetch the lease, upsert the mirror row, and write an activity-log
    entry. The expanded schema brings ``current_tenants`` along for
    the ride so downstream consumers don't need a second API call to
    show "Sarah Chen + John Chen on lease #5678".
    """
    lease_id = envelope_int(envelope, "LeaseId")
    if lease_id is None:
        logger.warning(
            "%s missing LeaseId for company %s",
            envelope.get("EventName"),
            company_id,
        )
        return
    account_id = envelope_account_id(envelope)
    event_name = str(envelope.get("EventName", ""))

    async with open_api_for_company(company_id) as api:
        if api is None:
            return
        try:
            lease = await api.leases.get(lease_id)
        except BuildiumApiError as exc:
            if exc.status == 404:
                logger.info(
                    "Lease %s no longer exists (race with delete)",
                    lease_id,
                )
                return
            raise

    pool = get_pool()
    await upsert_lease(pool, lease, account_id=account_id, company_id=company_id)
    # Cockpit mirror (0038): keep the lease↔tenant join fresh from the same
    # payload — it carries Tenants[] with status + move_in_date.
    await refresh_lease_tenant_links(pool, lease, company_id=company_id)

    verb = "created" if event_name.endswith(".Created") else "updated"
    rent_part = f"${lease.rent:,.2f}/mo" if lease.rent else "rent tbd"
    tenants = lease.current_tenants or []
    if tenants:
        names = ", ".join(
            f"{t.first_name or ''} {t.last_name or ''}".strip() or f"#{t.id}" for t in tenants
        )
        tenants_part = f" — {names}"
    else:
        tenants_part = ""
    description = (
        f"Buildium {verb} lease #{lease.id} (unit {lease.unit_id}, {rent_part}){tenants_part}"
    )

    await write_activity(
        pool,
        ActivityEntry(
            account_id=account_id,
            company_id=company_id,
            source="webhook",
            source_label=f"Buildium · {event_name}",
            action_type=f"lease_{verb}",
            description=description,
            entity_type="lease",
            entity_id=lease.id,
            raw={"event_name": event_name, "lease_id": lease.id},
        ),
    )


async def handle_lease_deleted(company_id: str, envelope: dict[str, Any]) -> None:
    lease_id = envelope_int(envelope, "LeaseId")
    if lease_id is None:
        return
    account_id = envelope_account_id(envelope)
    pool = get_pool()
    # ⚠ This handler is NAMED handle_lease_deleted and, until 2026-08-29, only
    # wrote an activity line saying the lease was deleted while leaving the
    # row in `helixis_leases`. Every mirror rollup reads that table, so a
    # deleted lease kept appearing in delinquency and expiry reports — a
    # confident false PRESENCE about a contract that no longer exists.
    # The tenant links go with it (see `delete_lease`).
    await delete_lease(pool, lease_id, company_id=company_id)
    await write_activity(
        pool,
        ActivityEntry(
            account_id=account_id,
            company_id=company_id,
            source="webhook",
            source_label="Buildium · Lease.Deleted",
            action_type="lease_deleted",
            description=f"Buildium deleted lease #{lease_id}",
            entity_type="lease",
            entity_id=lease_id,
            raw={"event_name": "Lease.Deleted", "lease_id": lease_id},
        ),
    )


async def handle_lease_moveout(company_id: str, envelope: dict[str, Any]) -> None:
    """Lease.MoveOut.Created — one or more tenants gave move-out notice.

    Buildium's move-out endpoint is a *list* per lease: a lease with
    multiple tenants can have multiple move-out rows. We pick the
    earliest pending move-out date for the activity blurb so the
    operator sees the most actionable date first.
    """
    lease_id = envelope_int(envelope, "LeaseId")
    if lease_id is None:
        return
    account_id = envelope_account_id(envelope)

    async with open_api_for_company(company_id) as api:
        if api is None:
            return
        try:
            move_outs = await api.leases.list_move_outs(lease_id)
        except BuildiumApiError as exc:
            if exc.status == 404:
                logger.info("Lease %s has no move-out rows yet", lease_id)
                return
            raise

    pool = get_pool()
    earliest = None
    for mo in move_outs:
        if mo.move_out_date is None:
            continue
        if earliest is None or mo.move_out_date < earliest:
            earliest = mo.move_out_date
    move_out_date = earliest.date().isoformat() if earliest is not None else "tbd"
    count = len(move_outs)
    tenant_phrase = "1 tenant" if count == 1 else f"{count} tenants"
    description = (
        f"Move-out filed on lease #{lease_id} ({tenant_phrase}) for {move_out_date}"
        if count
        else f"Move-out filed on lease #{lease_id} for {move_out_date}"
    )
    await write_activity(
        pool,
        ActivityEntry(
            account_id=account_id,
            company_id=company_id,
            source="webhook",
            source_label="Buildium · Lease.MoveOut.Created",
            action_type="lease_move_out",
            description=description,
            entity_type="lease",
            entity_id=lease_id,
            raw={
                "event_name": "Lease.MoveOut.Created",
                "lease_id": lease_id,
                "move_out_count": count,
            },
        ),
    )

    # Turnover trigger (surface-model decision 2026-07-06: Lease.MoveOut
    # "should trigger like turnover process"). Drop a desk card via the
    # reminders infra — event-driven, so the sweep never clears it; it
    # lives until the PM dismisses/handles it. Best-effort: a card
    # failure must never fail the webhook.
    try:
        # …and the property-page checklist row (0046) from the same trigger:
        # the card says "start turnover", the row is where it's worked. One
        # home for both PMSs (``turnovers_repo.start_turnover``).
        await turnovers_repo.start_turnover(
            pool,
            company_id=company_id,
            lease_id=lease_id,
            move_out_date=earliest.date() if earliest is not None else None,
        )
    except (Exception, SystemExit):
        logger.exception("turnover card failed for lease=%s — activity still logged", lease_id)


async def handle_lease_transaction(company_id: str, envelope: dict[str, Any]) -> None:
    """LeaseTransaction.* — rent payment, late fee, charge, etc."""
    lease_id = envelope_int(envelope, "LeaseId")
    transaction_id = envelope_int(envelope, "TransactionId")
    if lease_id is None or transaction_id is None:
        logger.warning(
            "LeaseTransaction.* missing LeaseId/TransactionId: %s",
            envelope,
        )
        return
    account_id = envelope_account_id(envelope)
    event_name = str(envelope.get("EventName", ""))
    pool = get_pool()

    # Cockpit mirror (0038): re-fetch the transaction and mirror it so the
    # rent-roll "collected" number moves the moment a payment posts, instead
    # of waiting for the daily re-sync.
    #
    # ⚠ This comment used to continue "Deletes are left to the re-sync (rare,
    # and the mirror is advisory — Buildium stays source of truth)", and the
    # delete is now handled here instead. Overriding a documented decision, so
    # the reason is on the record: these rows are MONEY. They feed the balance
    # arithmetic behind `delinquency_report`, which drives late-notice
    # recommendations — so a deleted charge surviving until the next daily
    # sync means up to 24 hours of telling a manager a resident owes something
    # they do not, and recommending they be chased for it. "Advisory" is true
    # of the mirror in general and not of a number someone acts on. Rare is a
    # reason to make the fix cheap, not to skip it; this is one call.
    if event_name.endswith(".Deleted"):
        await delete_lease_transaction(pool, transaction_id, company_id=company_id)
    else:
        async with open_api_for_company(company_id) as api:
            if api is not None:
                try:
                    txn = await api.leases.get_transaction(lease_id, transaction_id)
                    if account_id:
                        await upsert_lease_transaction(
                            pool, txn, account_id=account_id, company_id=company_id
                        )
                except BuildiumApiError as exc:
                    if exc.status != 404:
                        raise

    await write_activity(
        pool,
        ActivityEntry(
            account_id=account_id,
            company_id=company_id,
            source="webhook",
            source_label=f"Buildium · {event_name}",
            action_type="lease_transaction",
            description=f"Buildium {event_name.split('.')[-1].lower()} transaction "
            f"#{transaction_id} on lease #{lease_id}",
            entity_type="lease_transaction",
            entity_id=transaction_id,
            raw={
                "event_name": event_name,
                "lease_id": lease_id,
                "transaction_id": transaction_id,
            },
        ),
    )

    # Operational memory (#534 Stage B): derive the deterministic late-payer
    # signal for this lease from the mirror's aged-receivables and store it
    # (anchored to the lease entity) for injection into the "noticed" card +
    # drafted replies. Best-effort — advisory memory is a bonus on top of the
    # mirror sync, never allowed to break the webhook pipeline.
    try:
        from helixis.webhooks.operational_signals import derive_and_store_lease_signals

        await derive_and_store_lease_signals(pool, company_id=company_id, lease_id=lease_id)
    except (Exception, SystemExit):
        logger.warning("operational signal derivation failed for lease %s — continuing", lease_id)


async def handle_lease_tenant(company_id: str, envelope: dict[str, Any]) -> None:
    """LeaseTenant.* — a tenant joined / left / was updated on a lease."""
    lease_id = envelope_int(envelope, "LeaseId")
    tenant_id = envelope_int(envelope, "TenantId")
    if lease_id is None or tenant_id is None:
        logger.warning("LeaseTenant.* missing LeaseId/TenantId: %s", envelope)
        return
    account_id = envelope_account_id(envelope)
    event_name = str(envelope.get("EventName", ""))

    tenant = None
    async with open_api_for_company(company_id) as api:
        if api is None:
            return
        try:
            lease = await api.leases.get(lease_id)
        except BuildiumApiError as exc:
            if exc.status == 404:
                return
            raise
        # Cockpit mirror (0038): also re-fetch the tenant themselves so the
        # roster (name/email/phone) stays fresh, not just the join row. A 404
        # is fine — a removed tenant may already be gone.
        try:
            tenant = await api.rentals.get_tenant(tenant_id)
        except BuildiumApiError as exc:
            if exc.status != 404:
                raise

    pool = get_pool()
    await upsert_lease(pool, lease, account_id=account_id, company_id=company_id)
    await refresh_lease_tenant_links(pool, lease, company_id=company_id)
    if event_name.endswith(".Deleted"):
        # ⚠ `refresh_lease_tenant_links` only UPSERTS from the lease payload's
        # `Tenants` list — it never removes a link the payload no longer
        # mentions. So a tenant removed from a lease kept their join row, and
        # `contact_lookup` reads exactly that join to decide whether someone
        # is a current resident. Gotcha 51 is this defect one field over: a
        # person described as living somewhere on the strength of a lease
        # record that is not what the reader thinks it is.
        await delete_lease_tenant_link(
            pool,
            lease_buildium_id=lease_id,
            tenant_buildium_id=tenant_id,
            company_id=company_id,
        )
    if tenant is not None and account_id:
        await upsert_tenant(pool, tenant, account_id=account_id, company_id=company_id)
    await write_activity(
        pool,
        ActivityEntry(
            account_id=account_id,
            company_id=company_id,
            source="webhook",
            source_label=f"Buildium · {event_name}",
            action_type="lease_tenant_change",
            description=f"Tenant #{tenant_id} {event_name.split('.')[-1].lower()} "
            f"on lease #{lease_id}",
            entity_type="lease",
            entity_id=lease_id,
            raw={
                "event_name": event_name,
                "lease_id": lease_id,
                "tenant_id": tenant_id,
            },
        ),
    )


__all__ = [
    "handle_lease_changed",
    "handle_lease_deleted",
    "handle_lease_moveout",
    "handle_lease_tenant",
    "handle_lease_transaction",
]
