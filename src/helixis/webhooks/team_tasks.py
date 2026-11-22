"""Teammate task requests — one user's agent hands a task to a teammate's
Inbox (multiplayer PR B).

"Ask Alex to review the COI on vendor 4821" → the orchestrator's
``send_task_to_teammate`` tool (confirm-gated) calls
:func:`enqueue_team_task`, which rides the EXISTING webhook pipeline —
queue → enrichment → planner → Inbox — exactly the way email ingest does
(``email_triage`` / ``email_ingest`` are the template). No parallel
delivery system: the teammate gets a real Inbox card with the planner's
"What Helixis noticed" + suggested actions, and can run THEIR agent on it
via the existing inbox freeform path. That's the agent→agent handshake,
with a human at the seam.

This module is the **deterministic core**: pure payload/enrichment/surface
builders (unit-testable, no LLM) plus the enqueue seam. The agent tool
(``agents/team_task_tools.py``) is the only caller.

Event identity: rides ``buildium_events`` with ``event_name``
``"Helixis.TaskRequest"``. The per-request UUID goes into the payload's
``MessageId`` — the documented non-Buildium string key that
``event_schemas.dedup_key`` folds in — so two tasks created in the same
second can never collide on the dedup index (the same trap the email
ingest solved).
"""

from __future__ import annotations

import logging
import uuid
import zlib
from datetime import UTC, datetime
from typing import Any

import asyncpg

from helixis.webhooks.queue import enqueue, store_enrichment

logger = logging.getLogger(__name__)

TEAM_TASK_EVENT_NAME = "Helixis.TaskRequest"

# Same convention as the email ingest: no Buildium account is involved, so
# the envelope carries the sentinel and ``company_id`` is passed explicitly.
_TEAM_TASK_ACCOUNT_SENTINEL = 0

TEAM_TASK_SURFACE_SCHEMA_VERSION = 1


def team_task_synthetic_task_id(request_id: str) -> int:
    """Stable positive int surrogate for ``EnrichedTask.id`` (typed int,
    required) — same crc32 derivation as ``email_synthetic_task_id``; the
    real identity (the request UUID) is preserved on ``raw``."""
    return zlib.crc32(request_id.encode("utf-8")) & 0x7FFFFFFF


def build_team_task_payload(
    *,
    request_id: str,
    from_user_id: str,
    from_name: str,
    to_user_id: str,
    to_name: str,
    title: str,
    created_at: str,
) -> dict[str, Any]:
    """The ``buildium_events.payload`` envelope for a task request.

    Shaped like a Buildium envelope (EventName / EventDateTime / AccountId)
    so it flows through the same queue + dedup. Deliberately does NOT carry
    the full ``details`` body — like email, the body lives on the enrichment
    column so the queue row stays small; the fallback enricher degrades to
    title-only if that column is ever lost.
    """
    return {
        "EventName": TEAM_TASK_EVENT_NAME,
        "EventDateTime": created_at,
        "AccountId": _TEAM_TASK_ACCOUNT_SENTINEL,
        "MessageId": request_id,  # the dedup identity (see module docstring)
        "FromUserId": from_user_id,
        "FromName": from_name,
        "ForUserId": to_user_id,
        "ForName": to_name,
        "Title": title,
    }


def build_team_task_enrichment(
    *,
    request_id: str,
    company_id: str,
    from_user_id: str,
    from_name: str,
    to_user_id: str,
    to_name: str,
    title: str,
    details: str,
    created_at: str,
) -> dict[str, Any]:
    """Project a task request onto the ``EnrichedEvent`` task shape so the
    existing planner/summarizer pipeline consumes it natively (the email
    mapping's twin): title → ``task.title``, the full details verbatim →
    ``task.description``, the SENDER → ``requested_by`` (the planner frames
    "who's asking"), routing identities on ``task.raw``."""
    first, _, last = (from_name or "").strip().partition(" ")
    return {
        "schema_version": 1,
        "event_name": TEAM_TASK_EVENT_NAME,
        "company_id": company_id,
        "task": {
            "id": team_task_synthetic_task_id(request_id),
            "title": (title or "").strip() or "(untitled task)",
            "description": details or "",
            "status": None,
            "category": "TeamTask",
            "priority": None,
            "requested_by_first_name": first or None,
            "requested_by_last_name": last or None,
            "raw": {
                "source": "team_task",
                "request_id": request_id,
                "from_user_id": from_user_id,
                "from_name": (from_name or "").strip(),
                "for_user_id": to_user_id,
                "for_name": (to_name or "").strip(),
                "created_at": created_at,
            },
        },
    }


def build_team_task_action_surface(
    *,
    from_name: str,
    to_name: str,
    title: str,
    details: str,
) -> dict[str, Any]:
    """The deterministic floor surface: even before the planner's signals
    land, the Inbox shows who asked whom for what, with the full details.
    The planner's enriched surface replaces this once its pass completes
    (same lifecycle as the email floor card)."""
    clean_title = (title or "").strip() or "(untitled task)"
    return {
        "schema_version": TEAM_TASK_SURFACE_SCHEMA_VERSION,
        "kind": "team_task",
        "summary": f"Task from {from_name} for {to_name}: {clean_title}",
        "team_task": {
            "from_name": from_name,
            "for_name": to_name,
            "title": clean_title,
            "details": details or "",
        },
        "signals": [],
        "actions": [],
    }


async def enqueue_team_task(
    pool: asyncpg.Pool,
    *,
    company_id: str,
    from_user_id: str,
    from_name: str,
    to_user_id: str,
    to_name: str,
    title: str,
    details: str,
) -> bool:
    """Insert one task-request row + its enrichment and floor surface, so
    the worker's existing planner/summarizer queues pick it up and the
    teammate's Inbox renders it. Returns False on a dedup collision
    (practically unreachable with a fresh UUID per call)."""
    request_id = str(uuid.uuid4())
    created_at = datetime.now(UTC).isoformat()
    payload = build_team_task_payload(
        request_id=request_id,
        from_user_id=from_user_id,
        from_name=from_name,
        to_user_id=to_user_id,
        to_name=to_name,
        title=title,
        created_at=created_at,
    )
    result = await enqueue(
        pool,
        event_name=TEAM_TASK_EVENT_NAME,
        event_time=datetime.now(UTC),
        account_id=_TEAM_TASK_ACCOUNT_SENTINEL,
        entity_ids={},
        payload=payload,
        company_id=company_id,
    )
    if result.duplicate:
        logger.warning("team task enqueue hit a duplicate (request_id=%s)", request_id)
        return False
    await store_enrichment(
        pool,
        result.id,
        enrichment=build_team_task_enrichment(
            request_id=request_id,
            company_id=company_id,
            from_user_id=from_user_id,
            from_name=from_name,
            to_user_id=to_user_id,
            to_name=to_name,
            title=title,
            details=details,
            created_at=created_at,
        ),
        action_surface=build_team_task_action_surface(
            from_name=from_name,
            to_name=to_name,
            title=title,
            details=details,
        ),
    )
    return True


__all__ = [
    "TEAM_TASK_EVENT_NAME",
    "build_team_task_action_surface",
    "build_team_task_enrichment",
    "build_team_task_payload",
    "enqueue_team_task",
    "team_task_synthetic_task_id",
]
