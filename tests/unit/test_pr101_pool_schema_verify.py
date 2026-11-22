"""PR #101 — boot-time schema verification.

Tonight (May 19) prod hit a confusing bug: ``summary_text`` was
populated in the DB and a fresh side connection inside the Render
container could see it, but the long-lived pool that the API was
actually serving traffic from returned ``summary.text=null`` and
``summary.attempts=0`` with the rest of the row populated. PR #96
fixed this class of bug at the asyncpg layer with
``statement_cache_size=0``, but the symptom returned because pool
connections bound to PgBouncer backends from before migration 0018
still held a stale view of the schema.

PR #101 adds a one-shot self-check at boot: ``verify_schema`` runs
through ``pool.acquire()`` (NOT a side connection) so we are
testing the pool's view of the schema. Missing columns raise
``SchemaVerificationError`` which propagates out of the lifespan
and fails the Render deploy health-check — exactly what we want
when the alternative is serving lies for 30 minutes.

Three contracts pinned here:

1. ``verify_schema`` raises ``SchemaVerificationError`` listing the
   missing ``(table, column)`` pairs in the exception message.
2. ``verify_schema`` returns silently when every expected column is
   present.
3. ``RECENT_EVENTS_COLUMNS`` (the always-on column subset the
   recent-events route SELECTs) is the same constant the lifespan
   uses to verify schema. Drift between the two is what would
   re-introduce the bug.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from helixis.db.engine import SchemaVerificationError, verify_schema


def _make_pool(rows: list[dict[str, str]]) -> MagicMock:
    """Return a mock asyncpg.Pool whose ``acquire()`` yields a conn
    whose ``fetch`` returns ``rows``.

    Mirrors the production usage pattern: ``async with pool.acquire()
    as conn: rows = await conn.fetch(query, ...)``.
    """
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=rows)

    @asynccontextmanager
    async def _acquire() -> Any:
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    pool._captured_conn = conn  # let tests assert on the fetch call
    return pool


@pytest.mark.asyncio
async def test_verify_schema_passes_when_all_columns_present() -> None:
    """Every expected column is returned by ``information_schema.columns``
    → verify_schema returns silently with no exception."""
    pool = _make_pool(
        [
            {"table_name": "buildium_events", "column_name": "id"},
            {"table_name": "buildium_events", "column_name": "summary_text"},
            {"table_name": "buildium_events", "column_name": "summary_attempts"},
        ]
    )

    # No raise.
    await verify_schema(
        pool,
        {"buildium_events": ("id", "summary_text", "summary_attempts")},
    )

    # Sanity: we actually went through pool.acquire() → conn.fetch().
    pool._captured_conn.fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_verify_schema_raises_with_missing_column_in_message() -> None:
    """Pool's view of the schema is missing ``summary_text`` and
    ``summary_attempts`` — exception message must name both columns
    verbatim so the next operator can act on it without grepping
    the traceback."""
    pool = _make_pool(
        [
            {"table_name": "buildium_events", "column_name": "id"},
            # summary_text + summary_attempts intentionally absent
        ]
    )

    with pytest.raises(SchemaVerificationError) as exc_info:
        await verify_schema(
            pool,
            {"buildium_events": ("id", "summary_text", "summary_attempts")},
        )

    msg = str(exc_info.value)
    assert "buildium_events" in msg
    assert "summary_text" in msg
    assert "summary_attempts" in msg
    # ``id`` was present, must not appear in the missing list.
    assert "buildium_events: id" not in msg


@pytest.mark.asyncio
async def test_verify_schema_raises_when_entire_table_absent() -> None:
    """information_schema returned zero rows for the table → every
    column counts as missing."""
    pool = _make_pool([])

    with pytest.raises(SchemaVerificationError) as exc_info:
        await verify_schema(pool, {"buildium_events": ("id", "summary_text")})

    msg = str(exc_info.value)
    assert "id" in msg
    assert "summary_text" in msg


@pytest.mark.asyncio
async def test_verify_schema_noop_on_empty_expected() -> None:
    """Empty mapping → no SQL issued, no exception."""
    pool = _make_pool([])

    await verify_schema(pool, {})

    pool._captured_conn.fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_verify_schema_multi_table_partial_miss() -> None:
    """Two tables expected; one table fully covered, one missing a
    column. Exception lists only the partially-missing table."""
    pool = _make_pool(
        [
            {"table_name": "buildium_events", "column_name": "id"},
            {"table_name": "buildium_events", "column_name": "summary_text"},
            {"table_name": "companies", "column_name": "id"},
            # companies.name missing
        ]
    )

    with pytest.raises(SchemaVerificationError) as exc_info:
        await verify_schema(
            pool,
            {
                "buildium_events": ("id", "summary_text"),
                "companies": ("id", "name"),
            },
        )

    msg = str(exc_info.value)
    assert "companies: name" in msg
    # buildium_events was fully satisfied → must not appear.
    assert "buildium_events:" not in msg


# ── Drift guard: route + lifespan share one column list ──────────


def test_recent_events_columns_constant_is_route_select_baseline() -> None:
    """The route's hot path SELECTs ``RECENT_EVENTS_COLUMNS`` directly
    (extended with ``?include=``-gated columns). The lifespan passes
    the same constant to ``verify_schema``. If a future PR adds a
    column to the route's SELECT but forgets to add it to the
    constant, the boot gate becomes useless. Lock the contract:

      - The constant must include the columns the inbox detail pane
        relies on (summary_text, summary_attempts,
        summary_last_attempted_at). Those three are the exact
        columns whose absence triggered tonight's bug.

      - The constant must NOT include the ``?include=``-gated
        columns (action_surface, enrichment, executed_action_index)
        because the route's ``UndefinedColumnError`` fallback
        already gracefully degrades when those are missing — we
        don't want to fail boot if only those are out.
    """
    from helixis.api.routes.buildium import RECENT_EVENTS_COLUMNS

    columns = set(RECENT_EVENTS_COLUMNS)
    # The bug-of-record columns must be present.
    assert "summary_text" in columns
    assert "summary_attempts" in columns
    assert "summary_last_attempted_at" in columns
    # The include-gated columns must NOT be present.
    assert "action_surface" not in columns
    assert "enrichment" not in columns
    assert "executed_action_index" not in columns


def test_recent_events_columns_constant_is_tuple_so_route_does_not_mutate_it() -> None:
    """The route builds its select list via ``list(RECENT_EVENTS_COLUMNS)``
    and then mutates that list. The constant itself must remain
    immutable so the boot gate sees the same column set on every
    boot regardless of route call ordering."""
    from helixis.api.routes.buildium import RECENT_EVENTS_COLUMNS

    assert isinstance(RECENT_EVENTS_COLUMNS, tuple)
