"""Abstract base for per-event enrichers.

Every Buildium webhook event Helixis wants to act on has its own
enricher. ``BuildiumTaskEnricher`` is the first; ``LeaseCreatedEnricher``,
``WorkOrderCreatedEnricher``, etc. land in future PRs and only need to
implement :meth:`enrich`. The handler registry in
``helixis.webhooks.handlers`` is what binds an event name to its
enricher + planner pair.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from helixis.agent.enrichment.models import EnrichedEvent


class EventEnricher(ABC):
    """Turn a raw webhook envelope into an ``EnrichedEvent`` for the planner."""

    @abstractmethod
    async def enrich(self, event: dict[str, Any], company_id: str) -> EnrichedEvent:
        """Fan out Buildium read calls and return a populated ``EnrichedEvent``.

        Implementations MUST:
        - never raise on a single sub-fetch failure (append to
          ``EnrichedEvent.errors`` instead),
        - scope every Buildium call to the company's decrypted
          credentials,
        - keep history slices bounded (≤50 rows per query, 90-day
          window),
        - return within a few seconds in the common case so the
          synchronous webhook receiver stays under Buildium's 10s
          timeout deadline.
        """
        raise NotImplementedError
