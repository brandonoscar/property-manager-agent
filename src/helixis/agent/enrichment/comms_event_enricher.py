"""Enricher for inbound call/text events (``Comms.SmsReceived`` /
``Comms.CallCompleted``).

``comms_ingest`` already maps each message onto the ``EnrichedEvent`` shape and
persists it on ``buildium_events.enrichment`` at enqueue time, so in the normal
flow the worker reconstructs that column directly and this enricher never runs.

It exists because the worker resolves the PLANNER through the enrichment
registry (``_process_planner_row`` → ``get_enrichment_handler(event_name)``):
without a registered handler a comms row is marked ``planner_status='failed'``
("no enrichment handler") and never gets its signals or drafted reply. Exactly
the reason ``EmailEventEnricher`` exists — same shape, same rationale.

:meth:`enrich` is the self-heal fallback used only when the enrichment column
is NULL. ⚠ The queue payload deliberately does NOT carry the message body (it
lives on the enrichment column so the queue row stays small), so a rebuilt
event has the sender, channel and contact but an EMPTY body. That is a
degraded card, not a wrong one — and it must never be read as "the resident
sent an empty message" (gotcha 26's rule about absence).

⚠ **The priority bump used to be lost with the body, and that was the worst
part of a degraded card.** Hazard slugs were re-derived from whatever body
survived — which here is none — so a "gas leak in my apartment" text rebuilt
through this path came back ``priority: None`` and sorted beside a lease
question, with the deterministic "⚠ Possible life-safety hazard" line gone
too. The queue envelope has carried ``Hazards`` since it was written,
*specifically* to survive a lost enrichment column, and nothing read it:
gotchas 41/50/51's sentence — the field was fetched and thrown away — landing
on the one signal where under-flagging is a life-safety outcome. It is passed
as a FLOOR now (union with the re-scan, never a replacement), so the rebuilt
card keeps the urgency the ingest scan measured off the real text.
"""

from __future__ import annotations

from typing import Any

from helixis.agent.enrichment.base import EventEnricher
from helixis.agent.enrichment.models import EnrichedEvent
from helixis.comms.contact_resolver import ResolvedContact
from helixis.webhooks.comms_triage import InboundComms, build_comms_enrichment


class CommsEventEnricher(EventEnricher):
    """Build an ``EnrichedEvent`` for a ``Comms.*`` row."""

    async def enrich(self, event: dict[str, Any], company_id: str) -> EnrichedEvent:
        contact_id = event.get("ContactId")
        contact: ResolvedContact | None = None
        if contact_id is not None and event.get("ContactKind"):
            contact = ResolvedContact(
                kind=str(event["ContactKind"]),
                buildium_id=int(contact_id),
                name=str(event.get("ContactName") or ""),
                phone=str(event.get("From") or "") or None,
                company_id=company_id,
            )
        comms = InboundComms(
            channel=str(event.get("Channel") or "sms"),
            from_number=str(event.get("From") or ""),
            to_number=str(event.get("To") or ""),
            body="",  # not carried on the lightweight payload envelope
            occurred_at=str(event.get("EventDateTime") or ""),
            contact=contact,
            direction=str(event.get("Direction") or "inbound"),
            sid=str(event.get("MessageId") or ""),
        )
        # The envelope's slugs were scanned off the REAL body at ingest; the
        # rebuild above has none, so without this the flag is silently dropped.
        raw_hazards = event.get("Hazards") or []
        known = [str(h) for h in raw_hazards if h] if isinstance(raw_hazards, list) else []
        return EnrichedEvent.model_validate(
            build_comms_enrichment(comms, company_id, known_hazards=known)
        )
