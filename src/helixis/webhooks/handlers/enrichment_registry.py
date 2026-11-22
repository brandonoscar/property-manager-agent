"""Registry mapping Buildium event names to their enricher + planner pair.

Adding a new event type the planner can act on is intentionally
boring: drop a new ``EventEnricher`` subclass under
:mod:`helixis.agent.enrichment` (e.g. ``LeaseCreatedEnricher``) and
register it here.

Synchronous-only — the registry is consulted on the request thread of
the inbound webhook receive. The receiver tolerates "no handler"
gracefully by skipping enrichment/planning entirely and falling
through to the existing :func:`helixis.webhooks.dispatch.dispatch_event`
path, so events Helixis isn't ready to plan for stay shipped (just
without an enrichment column populated).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from helixis.agent.enrichment import (
    ApplicantEnricher,
    BuildiumTaskEnricher,
    CommsEventEnricher,
    EmailEventEnricher,
    EnrichedEvent,
    EventEnricher,
    LeaseEnricher,
    RiskFlagEnricher,
    TeamTaskEnricher,
    WorkOrderEnricher,
)
from helixis.agent.planner import ActionPlanner, ActionSurface
from helixis.detectors.inbox_bridge import RISK_EVENT_NAME
from helixis.webhooks.comms_triage import CALL_EVENT_NAME, SMS_EVENT_NAME
from helixis.webhooks.email_triage import EMAIL_EVENT_NAME
from helixis.webhooks.team_tasks import TEAM_TASK_EVENT_NAME

logger = logging.getLogger(__name__)


@dataclass
class EnrichmentHandler:
    """A bound enricher + planner pair for one event type.

    Both halves are intentionally fields rather than classmethods so
    tests can substitute fakes via dependency injection without
    touching the registry's mutable state.
    """

    enricher: EventEnricher
    planner: ActionPlanner

    async def run(
        self,
        event: dict[str, Any],
        company_id: str,
    ) -> tuple[EnrichedEvent, ActionSurface]:
        """Enrich the event, then plan an action surface against the
        enrichment. Both halves are awaited synchronously inside the
        webhook receiver."""
        enriched = await self.enricher.enrich(event, company_id)
        surface = await self.planner.plan(enriched, company_id)
        return enriched, surface


# ── Registry — keep this short. One entry per supported event. ────────
ENRICHMENT_HANDLERS: dict[str, EnrichmentHandler] = {
    "Task.Created": EnrichmentHandler(
        enricher=BuildiumTaskEnricher(),
        planner=ActionPlanner(),
    ),
    # Task.Updated carries the same TaskId envelope — the task enricher
    # fetches CURRENT state, so the planner sees the task as it is now.
    "Task.Updated": EnrichmentHandler(
        enricher=BuildiumTaskEnricher(),
        planner=ActionPlanner(),
    ),
    # Work orders wrap a task (GET /workorders/{id} → Task.Id); the WO
    # enricher resolves it and runs the full task fan-out, then grafts
    # the vendor work details on. Without these entries WorkOrder rows
    # fell to the bare floor card ("thin webhook cards", 2026-07-07).
    "WorkOrder.Created": EnrichmentHandler(
        enricher=WorkOrderEnricher(),
        planner=ActionPlanner(),
    ),
    "WorkOrder.Updated": EnrichmentHandler(
        enricher=WorkOrderEnricher(),
        planner=ActionPlanner(),
    ),
    # Applicants map onto the task shape (name → requested_by, statuses
    # in the description) — no unit/lease fan-out, but a real summary
    # and real actions instead of the floor card.
    "Applicant.Created": EnrichmentHandler(
        enricher=ApplicantEnricher(),
        planner=ActionPlanner(),
    ),
    "Applicant.Updated": EnrichmentHandler(
        enricher=ApplicantEnricher(),
        planner=ActionPlanner(),
    ),
    # Leases. "Lease." has been in the Inbox allowlist since the filter was
    # written but never had an enricher, so every lease card rendered the bare
    # floor surface — 19 Lease.Created + 7 Lease.MoveOut.Created rows live,
    # ZERO enriched (2026-08-21). A move-out especially needs the real card:
    # who is leaving, from which unit, when, and whether a balance is still
    # owed once they're gone.
    "Lease.Created": EnrichmentHandler(
        enricher=LeaseEnricher(),
        planner=ActionPlanner(),
    ),
    "Lease.MoveOut.Created": EnrichmentHandler(
        enricher=LeaseEnricher(),
        planner=ActionPlanner(),
    ),
    # Email-ingest events (enqueued by the Gmail poll, not the Buildium
    # receiver). Without this entry the worker's planner path marks every
    # email row "no enrichment handler" and never produces the signals /
    # drafted reply. The planner is the SAME ActionPlanner — it self-
    # selects the email prompt for Email.* events.
    EMAIL_EVENT_NAME: EnrichmentHandler(
        enricher=EmailEventEnricher(),
        planner=ActionPlanner(),
    ),
    # Inbound resident texts / calls (enqueued by comms_ingest off the
    # verified Twilio webhook, not a Buildium delivery). Same registration
    # reason as email and teammate tasks: without an entry the worker marks
    # every comms row "no enrichment handler" and the message never gets its
    # noticed-signals or drafted reply. Both channels share one enricher —
    # the channel is a field on the event, not a different shape.
    SMS_EVENT_NAME: EnrichmentHandler(
        enricher=CommsEventEnricher(),
        planner=ActionPlanner(),
    ),
    CALL_EVENT_NAME: EnrichmentHandler(
        enricher=CommsEventEnricher(),
        planner=ActionPlanner(),
    ),
    # Teammate task requests (enqueued by the send_task_to_teammate agent
    # tool, not a webhook). Same registration reason as email: without this
    # entry the worker's planner path marks every row "no enrichment
    # handler" and the teammate's card never gets its noticed-signals.
    TEAM_TASK_EVENT_NAME: EnrichmentHandler(
        enricher=TeamTaskEnricher(),
        planner=ActionPlanner(),
    ),
    # Detector findings (enqueued by the risk→Inbox bridge, not a
    # webhook). Same registration reason as email/team tasks: without
    # this entry the worker's planner path marks every risk row "no
    # enrichment handler" and the card never gets its noticed-signals +
    # suggested actions — the full message format is the whole point of
    # the bridge.
    RISK_EVENT_NAME: EnrichmentHandler(
        enricher=RiskFlagEnricher(),
        planner=ActionPlanner(),
    ),
}


def get_enrichment_handler(event_name: str) -> EnrichmentHandler | None:
    """Return the handler for an event, or ``None`` if the event isn't
    in the enrichment registry. Callers (the receiver) treat ``None``
    as "skip enrichment, fall through to legacy dispatch"."""
    return ENRICHMENT_HANDLERS.get(event_name)


__all__ = [
    "ENRICHMENT_HANDLERS",
    "EnrichmentHandler",
    "get_enrichment_handler",
]
