"""Activity-log writer.

Feeds the Activity page UI. Every agent action, webhook processed, and
scheduled run should land here so there's a single audit stream.

The ``company_id`` field is a Helixis company UUID (matches
``public.companies.id`` in prod). ``account_id`` is the Buildium
``AccountId`` that originated the row when applicable; it's optional
because some sources (chat, scheduled, manual) don't have one.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ActivityEntry:
    """Shape of a single activity-log row. Mirrors the frontend type."""

    account_id: int | None
    company_id: str
    """Helixis company UUID. Required — RLS / multi-tenancy live here."""

    source: str  # 'webhook' | 'chat' | 'scheduled' | 'manual'
    action_type: str
    description: str
    source_label: str | None = None
    property: str | None = None
    tenant: str | None = None
    entity_type: str | None = None
    entity_id: int | None = None
    user_id: str | None = None
    raw: dict[str, Any] | None = None


async def write_activity(pool: asyncpg.Pool, entry: ActivityEntry) -> None:
    """Insert an activity-log row. Never raises — activity logging is
    best-effort and must not take down the handler that triggered it.
    """
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO helixis_activity_log
                    (account_id, company_id, source, source_label, action_type,
                     description, property, tenant, entity_type, entity_id,
                     user_id, raw)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb)
                """,
                entry.account_id,
                entry.company_id,
                entry.source,
                entry.source_label,
                entry.action_type,
                entry.description,
                entry.property,
                entry.tenant,
                entry.entity_type,
                entry.entity_id,
                entry.user_id,
                json.dumps(entry.raw) if entry.raw is not None else None,
            )
    except Exception:  # noqa: BLE001
        logger.exception(
            "Failed to write activity log entry (source=%s action=%s)",
            entry.source,
            entry.action_type,
        )
