"""Tests for the ``orchestrator_runs`` repository (PR #141).

Covers:
  * Redaction: emails / phone numbers / credential-shaped fields are
    masked before write, recursively through nested dicts and lists.
  * Best-effort discipline: every writer swallows asyncpg failures
    and only logs (mirrors ``write_activity`` in ``activity.py``).
  * Begin → record_tool_call → record_tool_output → complete_run
    happy path produces the expected SQL shapes.

No live DB. The pool is mocked; assertions inspect the SQL strings
and parameter tuples the writers would have sent.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import asyncpg
import pytest

from helixis.db.repositories import orchestrator_runs as repo

# ── Redaction ─────────────────────────────────────────────────────────


def test_redact_masks_email_in_string():
    out = repo._redact("Send to alice@example.com immediately")
    assert "alice@example.com" not in out
    assert "al***@example.com" in out


def test_redact_masks_short_email_local():
    out = repo._redact("a@b.io")
    # 1-char local → single-letter + *** pattern
    assert "a***@b.io" in out


def test_redact_masks_phone_in_string():
    out = repo._redact("call +1 555-867-5309 today")
    assert "5309" in out  # last-4 preserved
    assert "***-***-5309" in out
    assert "555-867" not in out


def test_redact_masks_password_field_wholesale():
    obj = {"username": "alice", "password": "hunter2"}
    out = repo._redact(obj)
    assert out["password"] == "***"
    # Non-secret-named fields pass through untouched (no email/phone).
    assert out["username"] == "alice"


def test_redact_masks_every_named_secret_field():
    obj = {
        "password": "p",
        "token": "t",
        "secret": "s",
        "api_key": "k",
        "apikey": "k2",
        "client_secret": "cs",
        "refresh_token": "rt",
        "access_token": "at",
        "authorization": "az",
    }
    out = repo._redact(obj)
    for k in obj:
        assert out[k] == "***", f"{k} should be wholesale-masked"


def test_redact_recurses_into_nested_dicts():
    obj = {"outer": {"middle": {"api_key": "leak", "email": "a@b.com"}}}
    out = repo._redact(obj)
    assert out["outer"]["middle"]["api_key"] == "***"
    assert "a***@b.com" in out["outer"]["middle"]["email"]


def test_redact_recurses_into_lists():
    obj = [{"email": "a@b.com"}, "call me at 555-867-5309"]
    out = repo._redact(obj)
    assert "a***@b.com" in out[0]["email"]
    assert "***-***-5309" in out[1]


def test_redact_preserves_non_string_scalars():
    obj = {"count": 42, "ratio": 3.14, "ok": True, "missing": None}
    out = repo._redact(obj)
    assert out == obj


def test_redact_returns_new_object_not_mutated_input():
    """Args/output are still streamed to the user — redaction must not
    mutate the caller's data."""
    obj = {"email": "alice@example.com", "nested": {"secret": "s"}}
    snapshot = json.dumps(obj)
    repo._redact(obj)
    assert json.dumps(obj) == snapshot


# ── Best-effort writes ────────────────────────────────────────────────


@pytest.fixture
def failing_pool() -> MagicMock:
    """Pool whose ``acquire`` raises — exercises the try/except in
    every writer. Returns the pool mock so tests can also assert
    call counts."""
    pool = MagicMock(spec=asyncpg.Pool)
    pool.acquire.side_effect = asyncpg.PostgresConnectionError("boom")
    return pool


@pytest.mark.asyncio
async def test_begin_run_returns_none_on_db_failure(failing_pool):
    result = await repo.begin_run(
        failing_pool,
        company_id=str(uuid4()),
        user_id=None,
        session_id="s",
        source="chat",
        user_query="hi",
    )
    assert result is None


@pytest.mark.asyncio
async def test_record_tool_call_swallows_failure(failing_pool):
    # No raise, no return — just that it doesn't crash the orchestrator.
    await repo.record_tool_call(
        failing_pool,
        run_id=uuid4(),
        name="GMAIL_FETCH",
        args={"q": "x"},
        started_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_record_tool_output_swallows_failure(failing_pool):
    await repo.record_tool_output(
        failing_pool,
        run_id=uuid4(),
        name="GMAIL_FETCH",
        output={"ok": True},
        duration_ms=42,
        error=None,
    )


@pytest.mark.asyncio
async def test_record_handoff_swallows_failure(failing_pool):
    await repo.record_handoff(
        failing_pool,
        run_id=uuid4(),
        from_agent="Helixis",
        to_agent="Calendar Agent",
    )


@pytest.mark.asyncio
async def test_complete_run_swallows_failure(failing_pool):
    await repo.complete_run(
        failing_pool,
        run_id=uuid4(),
        status="failed",
        final_text=None,
        error_message="oops",
        error_type="RuntimeError",
        latency_ms=100,
    )


@pytest.mark.asyncio
async def test_fetch_recent_runs_returns_empty_on_failure(failing_pool):
    result = await repo.fetch_recent_runs(failing_pool, company_id=str(uuid4()))
    assert result == []


# ── Happy-path SQL shapes ─────────────────────────────────────────────


def _make_pool_with_conn(conn_mock: MagicMock) -> MagicMock:
    """Build a MagicMock pool whose ``acquire().__aenter__`` returns
    the given connection mock. asyncpg uses an async context manager
    for ``pool.acquire()``; we mimic that here."""
    pool = MagicMock(spec=asyncpg.Pool)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn_mock)
    cm.__aexit__ = AsyncMock(return_value=None)
    pool.acquire.return_value = cm
    return pool


@pytest.mark.asyncio
async def test_begin_run_inserts_running_row_and_returns_id():
    new_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"id": new_id})
    pool = _make_pool_with_conn(conn)

    result = await repo.begin_run(
        pool,
        company_id=str(uuid4()),
        user_id=str(uuid4()),
        session_id="sess",
        source="chat",
        user_query="what's on my calendar today",
    )

    assert result == new_id
    # The first call is the INSERT we care about.
    call = conn.fetchrow.await_args_list[0]
    sql, args = call.args[0], call.args[1:]
    assert "INSERT INTO public.orchestrator_runs" in sql
    assert "'running'" in sql  # default status pinned in SQL
    assert args[2] == "sess"
    assert args[3] == "chat"
    assert args[4] == "what's on my calendar today"


@pytest.mark.asyncio
async def test_record_tool_call_redacts_args_before_write():
    conn = MagicMock()
    conn.execute = AsyncMock()
    pool = _make_pool_with_conn(conn)
    run_id = uuid4()

    await repo.record_tool_call(
        pool,
        run_id=run_id,
        name="GMAIL_SEND",
        args={"to": "alice@example.com", "password": "x"},
        started_at=datetime(2026, 5, 23, 1, 0, 0, tzinfo=UTC),
    )

    sql = conn.execute.await_args.args[0]
    assert "tool_calls = tool_calls || $2::jsonb" in sql
    raw_json = conn.execute.await_args.args[2]
    stub = json.loads(raw_json)
    assert stub["name"] == "GMAIL_SEND"
    # Email partially masked, password wholesale-masked.
    assert "al***@example.com" in stub["args"]["to"]
    assert stub["args"]["password"] == "***"
    assert stub["started_at"].startswith("2026-05-23T01:00:00")


@pytest.mark.asyncio
async def test_record_tool_output_casts_ordinal_to_int_for_jsonb_operator():
    """Regression for PYTHON-FASTAPI-5: WITH ORDINALITY yields a bigint,
    and the jsonb ``->`` operator has no bigint overload, so the index
    must be cast to int or the UPDATE raises
    ``operator does not exist: jsonb -> bigint``. The pool is mocked
    (no live DB executes the SQL), so guard the cast at the string level."""
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=0)  # matched stub at index 0
    pool = _make_pool_with_conn(conn)

    await repo.record_tool_output(
        pool,
        run_id=uuid4(),
        name="GMAIL_FETCH",
        output={"ok": True},
        duration_ms=10,
        error=None,
    )

    sql = conn.fetchval.await_args.args[0]
    assert "(SELECT ord FROM target)::int)" in sql
    # The un-cast form is the bug; make sure it never comes back.
    assert "(tool_calls -> (SELECT ord FROM target))" not in sql


@pytest.mark.asyncio
async def test_record_tool_output_appends_orphan_when_no_stub_matches():
    """jsonb_set update returns None, fallback path appends a standalone
    record with ``orphaned=True``."""
    conn = MagicMock()
    # First call (CTE update): no matching stub — RETURNING is None.
    conn.fetchval = AsyncMock(return_value=None)
    # Second call (fallback append): plain execute.
    conn.execute = AsyncMock()
    pool = _make_pool_with_conn(conn)

    await repo.record_tool_output(
        pool,
        run_id=uuid4(),
        name="UNMATCHED_TOOL",
        output={"ok": True},
        duration_ms=15,
        error=None,
    )

    assert conn.fetchval.await_count == 1
    assert conn.execute.await_count == 1
    # execute(sql, run_id, json_string) → args is (sql, run_id, json_string)
    raw_json = conn.execute.await_args.args[2]
    payload = json.loads(raw_json)
    assert payload["orphaned"] is True
    assert payload["name"] == "UNMATCHED_TOOL"


@pytest.mark.asyncio
async def test_complete_run_truncates_long_final_text():
    conn = MagicMock()
    conn.execute = AsyncMock()
    pool = _make_pool_with_conn(conn)
    run_id = uuid4()
    long_text = "x" * 20000

    await repo.complete_run(
        pool,
        run_id=run_id,
        status="succeeded",
        final_text=long_text,
        error_message=None,
        error_type=None,
        latency_ms=1234,
    )

    args = conn.execute.await_args.args
    # Positional: run_id, status, final_text, error_message, error_type, latency_ms
    assert args[1] == run_id
    assert args[2] == "succeeded"
    assert len(args[3]) == 16000  # truncated
    assert args[6] == 1234


@pytest.mark.asyncio
async def test_complete_run_truncates_long_error_message():
    conn = MagicMock()
    conn.execute = AsyncMock()
    pool = _make_pool_with_conn(conn)
    long_err = "e" * 5000

    await repo.complete_run(
        pool,
        run_id=uuid4(),
        status="failed",
        final_text=None,
        error_message=long_err,
        error_type="RuntimeError",
        latency_ms=10,
    )

    args = conn.execute.await_args.args
    assert len(args[4]) == 1000


@pytest.mark.asyncio
async def test_fetch_run_returns_none_when_cross_tenant():
    """A row for a different company comes back from the DB as None
    because the SELECT filters by company_id. The repo returns None,
    which the route layer maps to 404 — never a discriminating message."""
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=None)
    pool = _make_pool_with_conn(conn)

    result = await repo.fetch_run(pool, company_id=str(uuid4()), run_id=uuid4())
    assert result is None
    # Confirm the SQL filtered by company_id (not just id).
    sql = conn.fetchrow.await_args.args[0]
    assert "company_id = $2" in sql


def test_record_dataclass_roundtrip_through_to_dict():
    """The wire-format dict needs to be JSON-serialisable for the
    /dev/runs route. Sanity-check the conversion."""
    row = repo.OrchestratorRunRow(
        id=str(uuid4()),
        company_id=str(uuid4()),
        user_id=None,
        session_id="s",
        source="chat",
        user_query="hi",
        started_at=datetime(2026, 5, 23, tzinfo=UTC),
        completed_at=None,
        latency_ms=None,
        status="running",
        error_message=None,
        error_type=None,
        tool_calls=[],
        final_text=None,
        handoffs=[],
        raw=None,
    )
    out = row.to_dict()
    json.dumps(out)  # would raise if not serialisable
    assert out["status"] == "running"
    assert out["started_at"] == "2026-05-23T00:00:00+00:00"


def test_row_to_dataclass_parses_jsonb_strings():
    """asyncpg returns jsonb as raw strings when statement_cache_size=0
    is set on the pool (see db/engine.py). _row_to_dataclass has to
    handle both the string and already-decoded cases."""
    row_dict = {
        "id": uuid4(),
        "company_id": uuid4(),
        "user_id": None,
        "session_id": "s",
        "source": "chat",
        "user_query": "hi",
        "started_at": datetime.now(UTC),
        "completed_at": None,
        "latency_ms": None,
        "status": "running",
        "error_message": None,
        "error_type": None,
        # Strings — simulates the asyncpg-no-codec path.
        "tool_calls": '[{"name": "X"}]',
        "final_text": None,
        "handoffs": "[]",
        "raw": None,
    }
    out = repo._row_to_dataclass(row_dict)
    assert out.tool_calls == [{"name": "X"}]
    assert out.handoffs == []


def test_row_to_dataclass_handles_already_decoded_jsonb():
    row_dict = {
        "id": uuid4(),
        "company_id": uuid4(),
        "user_id": None,
        "session_id": "s",
        "source": "chat",
        "user_query": "hi",
        "started_at": datetime.now(UTC),
        "completed_at": None,
        "latency_ms": None,
        "status": "running",
        "error_message": None,
        "error_type": None,
        "tool_calls": [{"name": "X"}],  # already a list
        "final_text": None,
        "handoffs": [],
        "raw": {"already": "decoded"},
    }
    out = repo._row_to_dataclass(row_dict)
    assert out.tool_calls == [{"name": "X"}]
    assert out.raw == {"already": "decoded"}
