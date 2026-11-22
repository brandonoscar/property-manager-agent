"""Enricher for inbound-email events (``Email.Received``).

The email-ingest poll already maps each email onto the ``EnrichedEvent``
task shape (subject→title, full body→description, sender→requested_by)
and persists it on ``buildium_events.enrichment`` at enqueue time — so in
the normal flow the worker reconstructs that column directly and this
enricher is never called.

This class exists because the worker resolves the PLANNER through the
enrichment registry (``_process_planner_row`` →
``get_enrichment_handler(event_name)``): without a registered handler an
email row is marked ``planner_status='failed'`` ("no enrichment handler")
and never gets its signals / drafted reply. Registering this pairs the
email events with the (email-aware) ``ActionPlanner``.

Its :meth:`enrich` is the legacy/self-heal fallback (used only when the
enrichment column is NULL — e.g. a row whose enqueue-time
``store_enrichment`` didn't land). It rebuilds a best-effort
``EnrichedEvent`` from the lightweight queue payload. NOTE: the payload
envelope does NOT carry the full body (that lives on the enrichment
column to keep the queue row small), so a fallback re-enrichment yields
subject/sender/thread without the body. That's an acceptable degraded
path; the common case keeps the full body via the persisted column.
"""

from __future__ import annotations

from typing import Any

from helixis.agent.enrichment.base import EventEnricher
from helixis.agent.enrichment.models import EnrichedEvent
from helixis.webhooks.email_triage import InboundEmail, build_email_enrichment


class EmailEventEnricher(EventEnricher):
    """Build an ``EnrichedEvent`` for an ``Email.Received`` row."""

    async def enrich(self, event: dict[str, Any], company_id: str) -> EnrichedEvent:
        # ``event`` is the queue payload (build_email_event_payload): it has
        # the envelope fields + message/thread/sender/subject, but NOT the
        # body. Reconstruct a best-effort InboundEmail; the real body
        # normally rides the pre-stored enrichment column.
        thread_id = event.get("ThreadId")
        email = InboundEmail(
            message_id=str(event.get("MessageId") or ""),
            from_email=str(event.get("From") or ""),
            from_name=str(event.get("FromName") or ""),
            subject=str(event.get("Subject") or ""),
            body_text="",  # not carried on the lightweight payload envelope
            received_at=str(event.get("EventDateTime") or ""),
            thread_id=str(thread_id) if thread_id else None,
        )
        return EnrichedEvent.model_validate(build_email_enrichment(email, company_id))
