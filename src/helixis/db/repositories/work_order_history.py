"""Work-order status-transition history (migration 0058).

The mirror (``helixis_work_orders``) keeps only the CURRENT status — every
webhook upsert overwrites the previous one, so "how long did this sit in
New?" / "who reopens the most work orders?" were unanswerable. This repo
appends one row per observed transition so the history starts accruing now;
Feature 1's analyst wave reads it later.

Append-only and best-effort: a history failure must never break the webhook
handler that observed the transition. Call
:func:`record_status_transition_if_changed` BEFORE the mirror upsert — it
reads the mirror's current status as "previous", so calling it after the
upsert would always see prev == new and record nothing.
"""

from __future__ import annotations

import logging

import asyncpg

logger = logging.getLogger(__name__)


async def record_status_transition_if_changed(
    pool: asyncpg.Pool,
    *,
    company_id: str,
    work_order_buildium_id: int,
    new_status: str | None,
    source: str = "webhook",
) -> bool:
    """Append a history row when the work order's status differs from the
    mirror's current value (or the work order is first seen). Returns True
    when a row was written; False on no-change, empty status, or any error.
    """
    status = (new_status or "").strip()
    if not status:
        return False
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT status FROM helixis_work_orders
                 WHERE company_id = $1::uuid AND buildium_id = $2
                """,
                company_id,
                work_order_buildium_id,
            )
            previous = (row["status"] or "").strip() if row is not None else None
            if previous == status:
                return False
            await conn.execute(
                """
                INSERT INTO public.helixis_work_order_status_history
                    (company_id, work_order_buildium_id, previous_status, status, source)
                VALUES ($1::uuid, $2, $3, $4, $5)
                """,
                company_id,
                work_order_buildium_id,
                previous,
                status,
                source,
            )
        return True
    except Exception:  # noqa: BLE001 — history is telemetry, never load-bearing
        logger.exception(
            "work_order_history.record_status_transition_if_changed failed (wo=%s)",
            work_order_buildium_id,
        )
        return False


__all__ = ["record_status_transition_if_changed"]
