"""Fallback handler for event names we haven't wired up yet."""

from __future__ import annotations

import logging

from helixis.buildium.api import BuildiumApi
from helixis.webhooks.queue import ClaimedEvent

logger = logging.getLogger(__name__)


async def noop(event: ClaimedEvent, _api: BuildiumApi) -> None:
    logger.info(
        "No handler registered for event=%s (id=%s). Payload persisted.",
        event.event_name,
        event.id,
    )
