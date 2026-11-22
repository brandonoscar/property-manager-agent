"""Replay-dedup and mirror-staleness invariants — deterministic, zero LLM.

The eval-suite audit (docs/research_investigation.md §1.9 Q4) found the
adversarial suite covered injection, cross-tenant, and browser walls but had
ZERO coverage of two load-bearing mechanics: a redelivered webhook must not
double-process, and the chat freshness footer must stay honest when the
mirror is stale or empty. Both are pure-code paths — these pin them forever
at no eval cost.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from helixis.agents.mirror_tools import _as_of_line
from helixis.webhooks.event_schemas import dedup_key
from helixis.webhooks.queue import enqueue

# ── replay dedup: the key ────────────────────────────────────────────


def _envelope(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "AccountId": 1234,
        "EventName": "WorkOrder.Created",
        "EventDateTime": "2026-07-23T10:00:00Z",
        "WorkOrderId": 987,
    }
    base.update(overrides)
    return base


def test_redelivered_envelope_produces_the_same_key() -> None:
    """Buildium legitimately redelivers — byte-identical envelopes MUST
    collide on the dedup key or replay protection doesn't exist."""
    assert dedup_key(_envelope()) == dedup_key(dict(_envelope()))


def test_distinct_entities_and_accounts_never_collide() -> None:
    base = dedup_key(_envelope())
    assert dedup_key(_envelope(WorkOrderId=988)) != base
    assert dedup_key(_envelope(AccountId=9999)) != base
    assert dedup_key(_envelope(EventDateTime="2026-07-23T10:00:01Z")) != base


def test_two_emails_in_the_same_second_do_not_collide() -> None:
    """Email ingest rides the same queue keyed by MessageId — two distinct
    emails arriving the same second must not dedup each other."""
    a = _envelope(EventName="Email.Received", MessageId="m-1")
    b = _envelope(EventName="Email.Received", MessageId="m-2")
    del a["WorkOrderId"], b["WorkOrderId"]
    assert dedup_key(a) != dedup_key(b)
    assert dedup_key(a) == dedup_key(dict(a))  # same email redelivered → same key


# ── replay dedup: the insert ─────────────────────────────────────────


class _AcquireCM:
    def __init__(self, conn: MagicMock) -> None:
        self._conn = conn

    async def __aenter__(self) -> MagicMock:
        return self._conn

    async def __aexit__(self, *_a: object) -> bool:
        return False


def _pool(fetchrow_result: Any) -> tuple[MagicMock, MagicMock]:
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=fetchrow_result)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AcquireCM(conn))
    return pool, conn


@pytest.mark.asyncio
async def test_enqueue_uses_on_conflict_do_nothing_and_flags_duplicates() -> None:
    """The replay guarantee is the unique index + ON CONFLICT DO NOTHING —
    a duplicate returns duplicate=True (no raise, no second row, and per the
    docstring a redelivery never re-claims an existing row)."""
    # RETURNING id yields no row (conflict path), then the follow-up SELECT
    # returns the existing row's id so the caller still has a handle.
    pool, conn = _pool(None)
    conn.fetchrow = AsyncMock(side_effect=[None, {"id": 7}])
    result = await enqueue(
        pool,
        event_name="WorkOrder.Created",
        event_time=datetime(2026, 7, 23, 10, 0, tzinfo=UTC),
        account_id=1234,
        entity_ids={"WorkOrderId": 987},
        payload=_envelope(),
    )
    assert result.duplicate is True
    assert result.id == 7
    sql = conn.fetchrow.await_args_list[0].args[0]
    assert "ON CONFLICT (dedup_key) DO NOTHING" in sql


@pytest.mark.asyncio
async def test_enqueue_fresh_row_is_not_marked_duplicate() -> None:
    pool, _ = _pool({"id": 42})
    result = await enqueue(
        pool,
        event_name="WorkOrder.Created",
        event_time=datetime(2026, 7, 23, 10, 0, tzinfo=UTC),
        account_id=1234,
        entity_ids={"WorkOrderId": 987},
        payload=_envelope(),
    )
    assert result.duplicate is False


# ── mirror staleness: the footer must not overstate freshness ────────


def test_as_of_line_admits_when_no_sync_time_is_known() -> None:
    """No timestamp → the footer must NOT invent a recency claim."""
    line = _as_of_line(None)
    assert "updated" not in line  # no fabricated "updated moments ago"
    assert "synced" in line


def test_as_of_line_reports_stale_age_honestly() -> None:
    """A mirror a day stale must SAY so — '26h ago', never 'moments ago'.
    (Freshness is portfolio-global today — research_investigation.md §1.11 —
    so this footer is the only staleness signal the user gets.)"""
    day_old = datetime.now(UTC) - timedelta(hours=26)
    line = _as_of_line(day_old)
    assert "26h ago" in line
    assert "moments" not in line


def test_as_of_line_recent_and_minutes_bands() -> None:
    now = datetime.now(UTC)
    assert "moments ago" in _as_of_line(now)
    assert "30 min ago" in _as_of_line(now - timedelta(minutes=30))


def test_as_of_line_handles_naive_timestamps() -> None:
    """Postgres MAX() can come back tz-naive through some drivers — the
    footer must not crash or mis-band on it."""
    naive = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=3)
    assert "3h ago" in _as_of_line(naive)
