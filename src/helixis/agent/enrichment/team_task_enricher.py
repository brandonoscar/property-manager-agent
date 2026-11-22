"""Enricher for teammate task requests (``Helixis.TaskRequest``).

Twin of :class:`EmailEventEnricher`, for the same reason: the enqueue path
(``webhooks/team_tasks.enqueue_team_task``) persists the full enrichment at
enqueue time, so in the normal flow the worker reconstructs that column
directly and never calls this. This class exists so the worker's planner
path resolves a handler for the event name — without a registry entry the
row is marked ``planner_status='failed'`` ("no enrichment handler") and
never gets its "Helixis noticed" signals.

:meth:`enrich` is the self-heal fallback for a row whose enrichment column
was lost: it rebuilds a best-effort ``EnrichedEvent`` from the lightweight
queue payload. The payload deliberately doesn't carry the ``details`` body
(it lives on the enrichment column, like the email body), so the fallback
degrades to title-only — acceptable; the common case keeps everything.
"""

from __future__ import annotations

from typing import Any

from helixis.agent.enrichment.base import EventEnricher
from helixis.agent.enrichment.models import EnrichedEvent
from helixis.webhooks.team_tasks import build_team_task_enrichment


class TeamTaskEnricher(EventEnricher):
    """Build an ``EnrichedEvent`` for a ``Helixis.TaskRequest`` row."""

    async def enrich(self, event: dict[str, Any], company_id: str) -> EnrichedEvent:
        return EnrichedEvent.model_validate(
            build_team_task_enrichment(
                request_id=str(event.get("MessageId") or ""),
                company_id=company_id,
                from_user_id=str(event.get("FromUserId") or ""),
                from_name=str(event.get("FromName") or ""),
                to_user_id=str(event.get("ForUserId") or ""),
                to_name=str(event.get("ForName") or ""),
                title=str(event.get("Title") or ""),
                details="",  # not carried on the lightweight payload envelope
                created_at=str(event.get("EventDateTime") or ""),
            )
        )
