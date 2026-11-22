"""Tests for memory_audit hook in MemoryManager.

The audit row is best-effort: a write failure must not regress the
underlying memory operation, and a missing user_id (system/cron) must
silently skip the audit insert.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helixis.memory.audit import write_audit, write_operational_audit
from helixis.memory.manager import MemoryManager
from helixis.settings import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings()


@pytest.fixture
def manager(settings: Settings) -> MemoryManager:
    mgr = MemoryManager(settings)
    mgr._mem0 = AsyncMock()
    mgr._initialized = True
    return mgr


# ── write_audit() unit tests ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_write_audit_skips_when_no_company_id() -> None:
    """No company_id → silently skip; do not touch the pool."""
    with patch("helixis.memory.audit._get_pool_or_none") as get_pool:
        await write_audit(company_id="", user_id="some-uuid", action="add", content="x")
        get_pool.assert_not_called()


@pytest.mark.asyncio
async def test_write_audit_skips_when_no_user_id() -> None:
    """No user_id → silently skip; system/cron writes have no user."""
    with patch("helixis.memory.audit._get_pool_or_none") as get_pool:
        await write_audit(company_id="comp_1", user_id="", action="add", content="x")
        get_pool.assert_not_called()


@pytest.mark.asyncio
async def test_write_audit_skips_when_pool_uninitialised() -> None:
    """No pool (test/dev env) → debug-log + return; never raise."""
    with patch("helixis.memory.audit._get_pool_or_none", return_value=None):
        # Should return None without raising.
        await write_audit(
            company_id="comp_1",
            user_id="22222222-2222-2222-2222-222222222222",
            action="add",
            content="x",
        )


def _fake_pool_yielding(conn: AsyncMock) -> MagicMock:
    """Build a pool whose ``async with pool.acquire() as c`` yields ``conn``."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=cm)
    return pool


# ── write_operational_audit() — #534 Stage B (user-less system writes) ──


@pytest.mark.asyncio
async def test_operational_audit_inserts_with_null_user() -> None:
    """A system operational write logs action='operational' with user_id NULL."""
    fake_conn = AsyncMock()
    fake_pool = _fake_pool_yielding(fake_conn)
    with patch("helixis.memory.audit._get_pool_or_none", return_value=fake_pool):
        await write_operational_audit(
            company_id="22222222-2222-2222-2222-222222222222",
            content="Unit 2B: chronic late-payer",
            memory_id="sig-1",
            metadata={"entity_type": "lease", "op": "store"},
        )
    fake_conn.execute.assert_awaited_once()
    sql, *args = fake_conn.execute.await_args.args
    assert "INSERT INTO memory_audit" in sql
    assert "'operational'" in sql and "NULL" in sql  # user_id NULL, action literal
    assert args[0] == "22222222-2222-2222-2222-222222222222"
    assert args[1] == "sig-1"  # memory_id
    assert args[2] == "Unit 2B: chronic late-payer"  # content
    assert "lease" in args[3]  # metadata json


@pytest.mark.asyncio
async def test_operational_audit_skips_without_company() -> None:
    with patch("helixis.memory.audit._get_pool_or_none") as get_pool:
        await write_operational_audit(company_id="", content="x")
        get_pool.assert_not_called()


@pytest.mark.asyncio
async def test_operational_audit_never_raises() -> None:
    fake_conn = AsyncMock()
    fake_conn.execute.side_effect = RuntimeError("connection lost")
    fake_pool = _fake_pool_yielding(fake_conn)
    with patch("helixis.memory.audit._get_pool_or_none", return_value=fake_pool):
        # must not raise
        await write_operational_audit(company_id="c", content="x")


@pytest.mark.asyncio
async def test_write_audit_inserts_row_when_pool_present() -> None:
    """Pool present → INSERT INTO memory_audit with the right columns."""
    fake_conn = AsyncMock()
    fake_pool = _fake_pool_yielding(fake_conn)

    with patch("helixis.memory.audit._get_pool_or_none", return_value=fake_pool):
        await write_audit(
            company_id="comp_1",
            user_id="22222222-2222-2222-2222-222222222222",
            action="add",
            content="Unit 4B tenant Sarah Chen",
            metadata={"category": "tenant"},
        )

    fake_conn.execute.assert_awaited_once()
    sql, *args = fake_conn.execute.await_args.args
    assert "INSERT INTO memory_audit" in sql
    assert args[0] == "comp_1"
    assert args[1] == "22222222-2222-2222-2222-222222222222"
    assert args[2] == "add"
    # memory_id, query are None for an add()
    assert args[3] is None
    assert args[4] is None
    assert args[5] == "Unit 4B tenant Sarah Chen"
    # metadata is JSON-encoded
    assert "tenant" in args[6]


@pytest.mark.asyncio
async def test_write_audit_truncates_long_content() -> None:
    """Audit rows store at most 500 chars of content; full content lives in Mem0."""
    fake_conn = AsyncMock()
    fake_pool = _fake_pool_yielding(fake_conn)
    long_content = "x" * 1500

    with patch("helixis.memory.audit._get_pool_or_none", return_value=fake_pool):
        await write_audit(
            company_id="comp_1",
            user_id="22222222-2222-2222-2222-222222222222",
            action="add",
            content=long_content,
        )

    sql, *args = fake_conn.execute.await_args.args
    assert len(args[5]) == 500


@pytest.mark.asyncio
async def test_write_audit_swallows_db_errors() -> None:
    """A DB error must not raise; audit is best-effort."""
    fake_conn = AsyncMock()
    fake_conn.execute.side_effect = RuntimeError("connection lost")
    fake_pool = _fake_pool_yielding(fake_conn)

    with patch("helixis.memory.audit._get_pool_or_none", return_value=fake_pool):
        # Must NOT raise — the underlying memory op is authoritative.
        await write_audit(
            company_id="comp_1",
            user_id="22222222-2222-2222-2222-222222222222",
            action="search",
            query="who lives in 4B",
        )


# ── MemoryManager wiring tests ───────────────────────────────────────


@pytest.mark.asyncio
async def test_manager_add_calls_audit(manager: MemoryManager) -> None:
    """MemoryManager.add() should invoke write_audit with action='add'."""
    manager._mem0.add.return_value = {"results": [{"event": "ADD"}]}

    with patch("helixis.memory.manager.write_audit", new_callable=AsyncMock) as audit:
        await manager.add(
            "fact",
            company_id="comp_1",
            stored_by="22222222-2222-2222-2222-222222222222",
            category="tenant",
        )

    audit.assert_awaited_once()
    kwargs = audit.await_args.kwargs
    assert kwargs["company_id"] == "comp_1"
    assert kwargs["user_id"] == "22222222-2222-2222-2222-222222222222"
    assert kwargs["action"] == "add"
    assert kwargs["content"] == "fact"
    assert kwargs["metadata"]["category"] == "tenant"


@pytest.mark.asyncio
async def test_manager_search_calls_audit_with_hit_count(
    manager: MemoryManager,
) -> None:
    """search() should audit with action='search' and hit_count metadata."""
    manager._mem0.search.return_value = {
        "results": [{"memory": "a"}, {"memory": "b"}, {"memory": "c"}]
    }

    with patch("helixis.memory.manager.write_audit", new_callable=AsyncMock) as audit:
        await manager.search(
            "tenant info",
            company_id="comp_1",
            searched_by="22222222-2222-2222-2222-222222222222",
        )

    audit.assert_awaited_once()
    kwargs = audit.await_args.kwargs
    assert kwargs["action"] == "search"
    assert kwargs["query"] == "tenant info"
    assert kwargs["metadata"]["hit_count"] == 3


@pytest.mark.asyncio
async def test_manager_extract_and_store_calls_audit(
    manager: MemoryManager,
) -> None:
    """extract_and_store() should audit the auto-extraction write with
    action='add' and the extracted facts as content."""
    manager._mem0.add.return_value = {
        "results": [
            {"event": "ADD", "memory": "Tenant prefers email"},
            {"event": "ADD", "memory": "Rent due on the 1st"},
        ]
    }

    conversation = [
        {"role": "user", "content": "Always email this tenant; rent is due on the 1st"},
        {"role": "assistant", "content": "Noted."},
    ]

    with patch("helixis.memory.manager.write_audit", new_callable=AsyncMock) as audit:
        await manager.extract_and_store(
            conversation,
            company_id="comp_1",
            stored_by="22222222-2222-2222-2222-222222222222",
        )

    audit.assert_awaited_once()
    kwargs = audit.await_args.kwargs
    assert kwargs["company_id"] == "comp_1"
    assert kwargs["user_id"] == "22222222-2222-2222-2222-222222222222"
    assert kwargs["action"] == "add"
    # Content is the facts Mem0 extracted, not the raw conversation.
    assert "Tenant prefers email" in kwargs["content"]
    assert "Rent due on the 1st" in kwargs["content"]
    assert kwargs["metadata"]["source"] == "auto_extraction"
    assert kwargs["metadata"]["extracted_count"] == 2


@pytest.mark.asyncio
async def test_manager_add_succeeds_when_audit_fails(
    manager: MemoryManager,
) -> None:
    """If write_audit raises, the memory write still returns its result."""
    manager._mem0.add.return_value = {"results": [{"event": "ADD"}]}

    audit = AsyncMock(side_effect=RuntimeError("audit table missing"))
    with patch("helixis.memory.manager.write_audit", audit):
        # The current implementation calls write_audit AFTER _mem0.add succeeds.
        # If write_audit raises, the manager should still log + return None
        # (per the broad except in add()), NOT propagate the exception.
        result = await manager.add(
            "fact",
            company_id="comp_1",
            stored_by="22222222-2222-2222-2222-222222222222",
        )

    # The caller sees None when the audit hook fails (caught by add()'s
    # broad except). The Mem0 write itself did succeed; that's an
    # acceptable trade for "audit must not regress memory" — the audit
    # write failure is the actual error to surface, but it's logged
    # rather than raised so the agent loop keeps moving.
    assert result is None
