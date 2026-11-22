"""Renewal offers (migration 0044) — the property-page renewals flow.

Candidates come from the entity mirror: active leases on the property ending
within the window, joined to the tenant roster for names + a recipient email
and to any existing offer row. "Renewed" is COMPUTED, not stored: an offer
whose lease end date has since moved out past the window means the PM
executed the renewal in Buildium and the mirror caught it via ``Lease.*``
webhooks — no extra webhook coupling required.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import asyncpg

from helixis.db.tenant_links import current_link_sql

RENEWAL_WINDOW_DAYS = 90


@dataclass
class RenewalCandidate:
    """One expiring lease on the property + its offer state."""

    lease_buildium_id: int
    lease_to_date: date | None
    current_rent: float | None
    unit_id: int | None
    unit_number: str | None
    tenant_names: str | None
    tenant_email: str | None
    offer: dict[str, Any] | None = None


@dataclass
class RenewalOffer:
    id: str
    lease_buildium_id: int
    status: str
    proposed_rent: float | None
    term_months: int | None
    subject: str | None
    body: str | None
    recipient_email: str | None
    sent_at: datetime | None
    renewed: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


def _offer_from_record(r: asyncpg.Record, *, renewed: bool = False) -> RenewalOffer:
    return RenewalOffer(
        id=str(r["id"]),
        lease_buildium_id=r["lease_buildium_id"],
        status=r["status"],
        proposed_rent=float(r["proposed_rent"]) if r["proposed_rent"] is not None else None,
        term_months=r["term_months"],
        subject=r["subject"],
        body=r["body"],
        recipient_email=r["recipient_email"],
        sent_at=r["sent_at"],
        renewed=renewed,
    )


async def list_candidates(
    pool: asyncpg.Pool,
    *,
    company_id: str,
    property_id: int,
    window_days: int = RENEWAL_WINDOW_DAYS,
) -> list[RenewalCandidate]:
    """Expiring active leases on the property + tenant contact + offer state."""
    async with pool.acquire() as conn:
        # ⚠ Current links only (``db.tenant_links``): the offer is written to
        # ``tenant_email``, and a co-tenant who moved out keeps a MovedOut link
        # on the lease the others still hold.
        rows = await conn.fetch(
            f"""
            SELECT l.buildium_id AS lease_id, l.lease_to_date, l.rent,
                   l.unit_id, u.unit_number,
                   (SELECT string_agg(
                            NULLIF(TRIM(CONCAT(t.first_name, ' ', t.last_name)), ''),
                            ', ' ORDER BY t.last_name NULLS LAST)
                      FROM helixis_lease_tenants lt
                      JOIN helixis_tenants t
                        ON t.buildium_id = lt.tenant_buildium_id
                       AND t.company_id = lt.company_id
                     WHERE lt.company_id = l.company_id
                       AND lt.lease_buildium_id = l.buildium_id
                       AND {current_link_sql()}) AS tenant_names,
                   (SELECT t.email
                      FROM helixis_lease_tenants lt
                      JOIN helixis_tenants t
                        ON t.buildium_id = lt.tenant_buildium_id
                       AND t.company_id = lt.company_id
                     WHERE lt.company_id = l.company_id
                       AND lt.lease_buildium_id = l.buildium_id
                       AND {current_link_sql()}
                       AND t.email IS NOT NULL
                     ORDER BY t.last_name NULLS LAST
                     LIMIT 1) AS tenant_email,
                   o.id AS offer_id, o.lease_buildium_id AS offer_lease_id,
                   o.status, o.proposed_rent, o.term_months, o.subject,
                   o.body, o.recipient_email, o.sent_at
              FROM helixis_leases l
              LEFT JOIN helixis_units u
                ON u.buildium_id = l.unit_id AND u.company_id = l.company_id
              LEFT JOIN helixis_renewal_offers o
                ON o.lease_buildium_id = l.buildium_id AND o.company_id = l.company_id
             WHERE l.company_id = $1::uuid
               AND l.property_id = $2
               AND l.status = 'Active'
               AND l.lease_to_date BETWEEN now() AND now() + make_interval(days => $3)
             ORDER BY l.lease_to_date
            """,
            company_id,
            property_id,
            window_days,
        )
    out: list[RenewalCandidate] = []
    for r in rows:
        offer = None
        if r["offer_id"] is not None:
            offer = {
                "id": str(r["offer_id"]),
                "lease_buildium_id": r["offer_lease_id"],
                "status": r["status"],
                "proposed_rent": (
                    float(r["proposed_rent"]) if r["proposed_rent"] is not None else None
                ),
                "term_months": r["term_months"],
                "subject": r["subject"],
                "body": r["body"],
                "recipient_email": r["recipient_email"],
                "sent_at": r["sent_at"],
                "renewed": False,
            }
        out.append(
            RenewalCandidate(
                lease_buildium_id=r["lease_id"],
                lease_to_date=(
                    r["lease_to_date"].date()
                    if isinstance(r["lease_to_date"], datetime)
                    else r["lease_to_date"]
                ),
                current_rent=float(r["rent"]) if r["rent"] is not None else None,
                unit_id=r["unit_id"],
                unit_number=r["unit_number"],
                tenant_names=r["tenant_names"],
                tenant_email=r["tenant_email"],
                offer=offer,
            )
        )
    return out


async def list_renewed_offers(
    pool: asyncpg.Pool,
    *,
    company_id: str,
    property_id: int,
    window_days: int = RENEWAL_WINDOW_DAYS,
) -> list[RenewalOffer]:
    """Sent offers whose lease no longer expires inside the window — the PM
    executed the renewal in Buildium and the mirror moved the end date."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT o.*
              FROM helixis_renewal_offers o
              JOIN helixis_leases l
                ON l.buildium_id = o.lease_buildium_id AND l.company_id = o.company_id
             WHERE o.company_id = $1::uuid
               AND o.property_id = $2
               AND o.status = 'sent'
               AND l.status = 'Active'
               AND l.lease_to_date > now() + make_interval(days => $3)
             ORDER BY o.sent_at DESC NULLS LAST
            """,
            company_id,
            property_id,
            window_days,
        )
    return [_offer_from_record(r, renewed=True) for r in rows]


async def upsert_draft(
    pool: asyncpg.Pool,
    *,
    company_id: str,
    lease_buildium_id: int,
    property_id: int,
    unit_id: int | None,
    proposed_rent: float | None,
    term_months: int | None,
    subject: str,
    body: str,
    recipient_email: str | None,
    created_by: str | None,
) -> RenewalOffer:
    """Create or refresh the draft for a lease. Re-drafting a sent offer
    resets it to draft (the PM is revising terms)."""
    async with pool.acquire() as conn:
        r = await conn.fetchrow(
            """
            INSERT INTO helixis_renewal_offers
                (company_id, lease_buildium_id, property_id, unit_id, status,
                 proposed_rent, term_months, subject, body, recipient_email,
                 created_by)
            VALUES ($1::uuid, $2, $3, $4, 'draft', $5, $6, $7, $8, $9, $10::uuid)
            ON CONFLICT (company_id, lease_buildium_id) DO UPDATE SET
                status          = 'draft',
                proposed_rent   = EXCLUDED.proposed_rent,
                term_months     = EXCLUDED.term_months,
                subject         = EXCLUDED.subject,
                body            = EXCLUDED.body,
                recipient_email = EXCLUDED.recipient_email,
                sent_at         = NULL,
                sent_message_id = NULL,
                updated_at      = now()
            RETURNING *
            """,
            company_id,
            lease_buildium_id,
            property_id,
            unit_id,
            proposed_rent,
            term_months,
            subject,
            body,
            recipient_email,
            created_by,
        )
    return _offer_from_record(r)


async def update_draft(
    pool: asyncpg.Pool,
    *,
    company_id: str,
    lease_buildium_id: int,
    proposed_rent: float | None,
    term_months: int | None,
    subject: str,
    body: str,
    recipient_email: str | None,
) -> RenewalOffer | None:
    """Save the PM's edits. Only drafts are editable."""
    async with pool.acquire() as conn:
        r = await conn.fetchrow(
            """
            UPDATE helixis_renewal_offers
               SET proposed_rent = $3, term_months = $4, subject = $5,
                   body = $6, recipient_email = $7, updated_at = now()
             WHERE company_id = $1::uuid AND lease_buildium_id = $2
               AND status = 'draft'
            RETURNING *
            """,
            company_id,
            lease_buildium_id,
            proposed_rent,
            term_months,
            subject,
            body,
            recipient_email,
        )
    return _offer_from_record(r) if r else None


async def get_offer(
    pool: asyncpg.Pool, *, company_id: str, lease_buildium_id: int
) -> RenewalOffer | None:
    async with pool.acquire() as conn:
        r = await conn.fetchrow(
            """
            SELECT * FROM helixis_renewal_offers
             WHERE company_id = $1::uuid AND lease_buildium_id = $2
            """,
            company_id,
            lease_buildium_id,
        )
    return _offer_from_record(r) if r else None


async def mark_sent(
    pool: asyncpg.Pool,
    *,
    company_id: str,
    lease_buildium_id: int,
    message_id: str | None,
) -> RenewalOffer | None:
    async with pool.acquire() as conn:
        r = await conn.fetchrow(
            """
            UPDATE helixis_renewal_offers
               SET status = 'sent', sent_at = now(), sent_message_id = $3,
                   updated_at = now()
             WHERE company_id = $1::uuid AND lease_buildium_id = $2
            RETURNING *
            """,
            company_id,
            lease_buildium_id,
            message_id,
        )
    return _offer_from_record(r) if r else None


async def set_status(
    pool: asyncpg.Pool,
    *,
    company_id: str,
    lease_buildium_id: int,
    status: str,
) -> RenewalOffer | None:
    """Dismiss (or un-dismiss back to draft)."""
    if status not in ("draft", "dismissed"):
        raise ValueError(f"Unsupported renewal status transition: {status!r}")
    async with pool.acquire() as conn:
        r = await conn.fetchrow(
            """
            UPDATE helixis_renewal_offers
               SET status = $3, updated_at = now()
             WHERE company_id = $1::uuid AND lease_buildium_id = $2
            RETURNING *
            """,
            company_id,
            lease_buildium_id,
            status,
        )
    return _offer_from_record(r) if r else None


__all__ = [
    "RENEWAL_WINDOW_DAYS",
    "RenewalCandidate",
    "RenewalOffer",
    "get_offer",
    "list_candidates",
    "list_renewed_offers",
    "mark_sent",
    "set_status",
    "update_draft",
    "upsert_draft",
]
