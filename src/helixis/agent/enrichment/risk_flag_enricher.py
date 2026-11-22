"""Enricher for detector-finding events (``Risk.FlagRaised``).

The detector→Inbox bridge (``detectors/inbox_bridge.py``) already maps
each finding group onto the ``EnrichedEvent`` task shape (grouped title →
title, joined details → description, structured findings on ``task.raw``)
and persists it on ``buildium_events.enrichment`` at enqueue time — so in
the normal flow the worker reconstructs that column directly and this
enricher is never called.

This class exists because the worker resolves the PLANNER through the
enrichment registry (``_process_planner_row`` →
``get_enrichment_handler(event_name)``): without a registered handler a
risk row is marked ``planner_status='failed'`` ("no enrichment handler")
and never gets its signals / suggested actions — the exact failure mode
this bridge was built to close.

Its :meth:`enrich` is the legacy/self-heal fallback (used only when the
enrichment column is NULL). The payload envelope carries the grouped
Title / RuleKey / Count / TotalAmount but NOT the per-finding details
(those live on the enrichment column to keep the queue row small), so a
fallback re-enrichment yields a title-level card. Acceptable degraded
path; the common case keeps the full findings via the persisted column.
"""

from __future__ import annotations

from typing import Any

from helixis.agent.enrichment.base import EventEnricher
from helixis.agent.enrichment.models import EnrichedEvent
from helixis.detectors.inbox_bridge import RISK_EVENT_NAME, risk_synthetic_task_id


class RiskFlagEnricher(EventEnricher):
    """Build an ``EnrichedEvent`` for a ``Risk.FlagRaised`` row."""

    async def enrich(self, event: dict[str, Any], company_id: str) -> EnrichedEvent:
        # ``event`` is the queue payload (build_risk_event_payload): the
        # envelope + grouped Title/RuleKey/Count/TotalAmount, but NOT the
        # per-finding details. Rebuild a best-effort task; the real
        # findings normally ride the pre-stored enrichment column.
        message_id = str(event.get("MessageId") or "")
        title = str(event.get("Title") or "").strip() or "Risk finding"
        rule_key = str(event.get("RuleKey") or "")
        count = event.get("Count")
        total = event.get("TotalAmount")
        parts: list[str] = []
        if isinstance(count, int) and count > 1:
            parts.append(f"{count} findings")
        if isinstance(total, (int, float)) and total > 0:
            parts.append(f"${float(total):,.0f} involved")
        description = f"{title}" + (f" ({'; '.join(parts)})" if parts else "")
        return EnrichedEvent.model_validate(
            {
                "schema_version": 1,
                "event_name": RISK_EVENT_NAME,
                "company_id": company_id,
                "task": {
                    "id": risk_synthetic_task_id(message_id or title),
                    "title": title,
                    "description": description,
                    "status": None,
                    "category": "RiskFlag",
                    "priority": None,
                    "requested_by_first_name": None,
                    "requested_by_last_name": None,
                    "raw": {
                        "source": "risk_detector",
                        "rule_key": rule_key,
                        "flag_ids": event.get("FlagIds") or [],
                    },
                },
            }
        )
