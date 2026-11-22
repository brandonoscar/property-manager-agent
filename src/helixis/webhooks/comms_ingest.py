"""The RECEIVER for inbound texts/calls — the seam ``comms_triage`` was
written against and never got.

``webhooks/comms_triage.py`` has been complete and unit-tested since it
landed: it gates on "known Buildium contact + has content", projects the
message onto the ``EnrichedEvent`` shape, and builds a deterministic floor
surface. Nothing ever called it. Every function in that module was reachable
only from its own test file, so an inbound resident text has never once
produced an Inbox card.

This module is the missing half: resolve WHICH COMPANY the number belongs to,
resolve the Buildium contact within it, apply the gate, and enqueue onto the
existing ``buildium_events`` pipeline — the same shape as
``detectors/inbox_bridge`` and the email-ingest poll. No parallel "comms
brain".

**Company resolution is the hard part, and it declines rather than guesses.**
An inbound SMS arrives at our Twilio number carrying no company context at
all — just a phone number. The only company linkage that exists is the consent
ledger, so that is what we read. Three outcomes:

- exactly one company holds a ledger row for that number → route there;
- no company holds one → no relationship, no card (never a cross-tenant scan);
- more than one company holds one → **decline**, and log it.

That last branch matters: picking arbitrarily would file a resident's text in
another landlord's Inbox. A guard that cannot see must decline to act rather
than act on a guess (gotcha 55), and cross-tenant is the one direction where
the cost of being wrong is unbounded.

⚠ The ledger read deliberately includes REVOKED rows. Consent governs what we
may SEND; it says nothing about reading what someone chose to send us, and a
resident who texted STOP last month and "water pouring through my ceiling"
today must still reach their PM. This mirrors the log row's existing
attribution, which also counts revoked rows.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import asyncpg

from helixis.comms.contact_resolver import resolve_all_contacts_by_phone
from helixis.comms.phone import normalize_e164
from helixis.comms.sms import redact_phone
from helixis.webhooks.comms_triage import (
    InboundComms,
    build_comms_action_surface,
    build_comms_enrichment,
    comms_hazards,
    should_surface_comms,
)
from helixis.webhooks.queue import enqueue, store_enrichment

logger = logging.getLogger(__name__)

# Same convention as email ingest / team tasks / the risk bridge: no Buildium
# account is involved, so the envelope carries the sentinel and ``company_id``
# rides explicitly.
_COMMS_ACCOUNT_SENTINEL = 0

# Every company holding a consent-ledger row for this number, live or revoked.
#
# ⚠ Deliberately NOT company-scoped (gotcha 47's escape hatch, stated in SQL as
# that rule requires): this query's entire job is to DISCOVER the company from
# a bare phone number, so a company_id filter would be circular. Its callers
# treat a multi-company result as ambiguous and refuse to route.
_COMPANIES_FOR_PHONE_SQL = """
SELECT DISTINCT company_id
  FROM public.helixis_sms_consent
 WHERE phone = $1
   AND company_id IS NOT NULL
"""


async def companies_for_phone(pool: asyncpg.Pool, phone: str) -> list[str]:
    """Distinct companies with a consent-ledger row for ``phone``.

    Returns ``[]`` for an empty/unknown number. Ordered for determinism so a
    caller logging the ambiguous set prints it the same way twice.
    """
    if not phone or not phone.strip():
        return []
    async with pool.acquire() as conn:
        rows = await conn.fetch(_COMPANIES_FOR_PHONE_SQL, phone)
    return sorted(str(r["company_id"]) for r in rows)


async def resolve_company_for_inbound(pool: asyncpg.Pool, phone: str) -> str | None:
    """The one company an inbound message belongs to, or ``None``.

    ``None`` covers BOTH "nobody knows this number" and "two companies know
    it" — from the caller's side the action is identical (do not card it), and
    collapsing them here keeps the ambiguity refusal impossible to skip. The
    two cases log differently so an operator can tell them apart.
    """
    companies = await companies_for_phone(pool, phone)
    if not companies:
        return None
    if len(companies) > 1:
        # Not an error — a phone number legitimately belongs to two landlords'
        # ledgers (a vendor serving both). It is simply not routable without
        # asking a human, and filing it in the wrong Inbox is the worse
        # outcome by a wide margin.
        logger.warning(
            "comms ingest: phone=%s is in %d companies' consent ledgers — "
            "not routing to an Inbox (cannot disambiguate)",
            redact_phone(phone),
            len(companies),
        )
        return None
    return companies[0]


def build_comms_event_payload(comms: InboundComms, *, company_id: str) -> dict[str, Any]:
    """The ``buildium_events.payload`` envelope for one inbound message.

    Buildium-envelope-shaped (EventName / EventDateTime / AccountId) so it
    rides the same queue and dedup path. Deliberately LIGHT — the full body
    lives on the enrichment column, exactly as email ingest does it, so the
    queue row stays small. ``MessageId`` is Twilio's SID, which folds into
    ``dedup_key`` and makes a provider retry idempotent.
    """
    contact = comms.contact
    return {
        "EventName": ("Comms.CallCompleted" if comms.channel == "voice" else "Comms.SmsReceived"),
        "EventDateTime": comms.occurred_at,
        "AccountId": _COMMS_ACCOUNT_SENTINEL,
        "MessageId": comms.sid,
        "CompanyId": company_id,
        "Channel": comms.channel,
        "Direction": comms.direction,
        "From": normalize_e164(comms.from_number) or comms.from_number,
        "To": normalize_e164(comms.to_number) or comms.to_number,
        # Hazard slugs ride the ENVELOPE as well as the enrichment: the
        # deterministic life-safety scan must survive a lost enrichment column
        # (gotcha 8's "committed is not applied" applies to 0008's columns too,
        # and store_enrichment swallows UndefinedColumn by design).
        "Hazards": comms_hazards(comms),
        "ContactKind": contact.kind if contact else None,
        "ContactId": contact.buildium_id if contact else None,
        "ContactName": contact.name if contact else None,
    }


async def bridge_inbound_comms(
    pool: asyncpg.Pool,
    *,
    channel: str,
    from_number: str,
    to_number: str,
    body: str,
    sid: str,
    occurred_at: datetime | None = None,
    company_id: str = "",
) -> UUID | None:
    """Route one verified inbound message into the Inbox pipeline.

    Returns the queue row id when a card was created, ``None`` in every
    declined case (unknown/ambiguous company, unknown contact, empty body,
    duplicate). NEVER raises — this runs inside the Twilio webhook, which must
    return 2xx or Twilio retries the delivery.
    """
    try:
        # ⚠ WHO RECEIVED IT beats who we recognise. The caller resolves the
        # company from the number that was TEXTED (``helixis_twilio_numbers``,
        # unique index — migration 0082), which is unambiguous by
        # construction. Falling back to "which ledgers know this sender" is
        # the pre-0082 mechanism and it answers a question we no longer have
        # to ask: a resident who appears in TWO companies' ledgers — they
        # moved landlords, or a vendor serves both — made that lookup
        # ambiguous, so it correctly refused to guess and silently carded
        # NOTHING, for exactly the people most likely to be in two ledgers.
        # We knew whose number they dialled the whole time.
        owner = company_id or await resolve_company_for_inbound(pool, from_number)
        if not owner:
            return None

        # ⚠ ALL of them, not the best one. A household handset is shared, and
        # `resolve_contact_by_phone` picks by precedence and logs the rest — so
        # a text from a couple carded as "Text from Josh Gil", a confident
        # false statement about which real person said it, with the auto-task
        # attributing the request to them. The candidates were fetched and
        # thrown away (gotchas 41/51), and `resolve_all_contacts_by_phone` —
        # whose own docstring says callers that must disambiguate should use it
        # — had no callers at all (gotcha 62).
        candidates = tuple(
            await resolve_all_contacts_by_phone(pool, company_id=owner, phone=from_number)
        )
        contact = candidates[0] if candidates else None
        when = occurred_at or datetime.now(UTC)
        comms = InboundComms(
            channel=channel,
            from_number=from_number,
            to_number=to_number,
            body=body,
            occurred_at=when.isoformat(),
            contact=contact,
            candidates=candidates,
            sid=sid,
        )
        if not should_surface_comms(comms):
            # A number on the consent ledger that is NOT a Buildium contact is
            # the ordinary case for a manager's own phone — not a fault.
            logger.info(
                "comms ingest: %s from=%s not surfaced (known_contact=%s, has_body=%s)",
                channel,
                redact_phone(from_number),
                contact is not None,
                bool(body and body.strip()),
            )
            return None

        payload = build_comms_event_payload(comms, company_id=owner)
        result = await enqueue(
            pool,
            event_name=str(payload["EventName"]),
            event_time=when,
            account_id=_COMMS_ACCOUNT_SENTINEL,
            entity_ids={},
            payload=payload,
            company_id=owner,
        )
        if result.duplicate:
            return None
        await store_enrichment(
            pool,
            result.id,
            enrichment=build_comms_enrichment(comms, owner),
            action_surface=build_comms_action_surface(comms),
        )
        logger.info(
            "comms ingest: carded %s from=%s company=%s contact=%s",
            channel,
            redact_phone(from_number),
            owner,
            contact.kind if contact else "none",
        )
        return result.id
    except (Exception, SystemExit):
        # The webhook's 2xx is worth more than this card: a non-2xx makes
        # Twilio retry the whole delivery, which would re-run the consent
        # mirroring above it. The message is already logged either way.
        logger.exception(
            "comms ingest: bridging %s from=%s failed — message logged, no card",
            channel,
            redact_phone(from_number),
        )
        return None


__all__ = [
    "bridge_inbound_comms",
    "build_comms_event_payload",
    "companies_for_phone",
    "resolve_company_for_inbound",
]
