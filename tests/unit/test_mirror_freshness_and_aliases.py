"""Mirror data layer (migration 0057) — freshness, aliases, fuzzy fallback.

DB-free: fake pools pin the SQL shapes and the fail-open postures. The
design rules under test: freshness bookkeeping is NEVER load-bearing (any
failure degrades to the legacy behavior), the footer names the stalest core
table instead of hiding it behind a fresh lease row, aliases beat fuzzy
matching, and every new read path survives the pre-migration deploy window
(gotcha 8) by degrading, not raising.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from helixis.db.repositories import entity_aliases, mirror_sync_state
from helixis.db.repositories.mirror_sync_state import stalest_core
from helixis.webhooks.dispatch import _table_for_event

# ── fakes ────────────────────────────────────────────────────────────


class _AcquireCM:
    def __init__(self, conn: MagicMock) -> None:
        self._conn = conn

    async def __aenter__(self) -> MagicMock:
        return self._conn

    async def __aexit__(self, *_a: object) -> bool:
        return False


def _pool(conn: MagicMock) -> MagicMock:
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AcquireCM(conn))
    return pool


COMPANY = "5ca1ab1e-0000-4000-8000-000000000004"


def _pool_with_no_recent_write() -> MagicMock:
    """A pool whose recent-write lookup finds nothing — the ORDINARY state.

    ⚠ A bare ``MagicMock()`` cannot express this and silently expresses the
    OPPOSITE. Its ``fetchrow`` returns a MagicMock, which is TRUTHY, so
    ``_recent_write_note`` reads it as "Occupella wrote to this account seconds
    ago" and every footer in this file grew a caveat it should not have: three
    tests failed and six passed by luck. A fixture has to say which state it
    MEANS (gotchas 72/73) — every scenario below is about the freshness
    verdict, with nothing written just now.
    """
    pool = MagicMock()
    pool.acquire.return_value.__aenter__.return_value.fetchrow.return_value = None
    return pool


# ── sync-state repo ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_sync_upserts_and_never_raises() -> None:
    conn = MagicMock()
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    await mirror_sync_state.record_sync(
        _pool(conn), company_id=COMPANY, table_name="bills", row_count=12, source="backfill"
    )
    sql = conn.execute.await_args.args[0]
    assert "ON CONFLICT (company_id, table_name)" in sql
    # Pre-migration deploy window (gotcha 8): missing table must not raise.
    conn.execute = AsyncMock(side_effect=RuntimeError("relation does not exist"))
    await mirror_sync_state.record_sync(_pool(conn), company_id=COMPANY, table_name="bills")


@pytest.mark.asyncio
async def test_get_freshness_empty_on_failure() -> None:
    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=RuntimeError("relation does not exist"))
    assert await mirror_sync_state.get_freshness(_pool(conn), COMPANY) == {}


def test_stalest_core_ignores_non_core_tables() -> None:
    now = datetime.now(UTC)
    freshness = {
        "leases": now,
        "bills": now - timedelta(hours=30),
        "gl_accounts": now - timedelta(days=9),  # non-core: must not win
    }
    got = stalest_core(freshness)
    assert got is not None and got[0] == "bills"
    assert stalest_core({"gl_accounts": now}) is None


# ── freshness footer ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_footer_names_the_stale_core_table(monkeypatch: pytest.MonkeyPatch) -> None:
    from helixis.agents import mirror_tools

    now = datetime.now(UTC)

    async def fake_freshness(_pool: Any, _company: str) -> dict[str, datetime]:
        return {"leases": now, "bills": now - timedelta(hours=30)}

    monkeypatch.setattr(mirror_tools.sync_state, "get_freshness", fake_freshness)
    line = await mirror_tools._freshness_line(_pool_with_no_recent_write(), COMPANY)
    # Overall recency comes from the freshest core table…
    assert "moments ago" in line
    # …but the day-stale table is NAMED, not hidden behind it (§1.11 fix).
    assert "bills last synced 30h ago" in line


@pytest.mark.asyncio
async def test_stale_data_with_no_credential_says_it_will_not_catch_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Age alone reads as a LAG; a disconnected account is not a lag.

    The founder hit this live: "leases last synced 93h ago" on an account whose
    Buildium key had been removed. Every word was true and the reader is sent
    to wait for a sync that can never run. Gotcha 46's shape — the layer that
    knows the cause is not the layer wording the message.

    ⚠ **RE-POINTED, and the fixture is what had to change.** The branch now
    asks whether ANY property-management connection is on file, because since
    ADR 0010 Decision (a) the mirror has two possible writers and the Buildium
    question answered ``False`` for every Rentvine company by construction
    (migration 0100's one-PMS trigger). This fixture stated only half of "no
    credential" and so no longer described the state its own name claims — the
    shape gotchas 67/72/73 record, where a fixture that does not say what it
    means is how a gap stays green. The sentence stops naming a vendor for the
    reason ``_mirror_will_catch_up`` gives: in this state the credentials row
    was the only record of which system they used.
    """
    from helixis.agents import mirror_tools

    now = datetime.now(UTC)

    async def fake_freshness(_pool: Any, _company: str) -> dict[str, datetime]:
        return {"leases": now - timedelta(hours=93)}

    async def no_row(_company: str) -> None:
        return None

    monkeypatch.setattr(mirror_tools.sync_state, "get_freshness", fake_freshness)
    monkeypatch.setattr(mirror_tools.credentials, "fetch_buildium_row", no_row)
    monkeypatch.setattr(mirror_tools, "fetch_rentvine_row", no_row)
    # Re-pointed (rule 19): "no connection" now has to be stated for every
    # system, Rent Manager's and Propertyware's tables included.
    monkeypatch.setattr(
        mirror_tools, "_holds_pms_connector_credentials", AsyncMock(return_value=False)
    )
    line = await mirror_tools._freshness_line(_pool_with_no_recent_write(), COMPANY)

    assert "leases last synced 93h ago" in line
    assert "no property-management connection on file" in line
    assert "will NOT" in line
    assert "Settings → Connectors" in line


@pytest.mark.asyncio
async def test_stale_data_with_a_credential_is_reported_as_a_plain_lag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other direction. A connected account that is merely behind must NOT
    be told its connection is gone — that would send someone to re-enter a key
    that is working, and is a fresh false claim of exactly the kind the branch
    above exists to remove (gotcha 38: guard both directions)."""
    from helixis.agents import mirror_tools

    now = datetime.now(UTC)

    async def fake_freshness(_pool: Any, _company: str) -> dict[str, datetime]:
        return {"leases": now - timedelta(hours=40)}

    async def a_row(_company: str) -> dict[str, Any]:
        return {"company_id": COMPANY, "account_id": 695763}

    monkeypatch.setattr(mirror_tools.sync_state, "get_freshness", fake_freshness)
    monkeypatch.setattr(mirror_tools.credentials, "fetch_buildium_row", a_row)
    line = await mirror_tools._freshness_line(_pool_with_no_recent_write(), COMPANY)

    assert "leases last synced 40h ago" in line
    assert "no Buildium connection" not in line


@pytest.mark.asyncio
async def test_stale_data_on_a_rent_manager_company_is_a_plain_lag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Rent Manager or Propertyware company holds neither a Buildium nor a
    Rentvine row, so the footer told a working connection it would never
    catch up and to reconnect. Its own credentials table answers now."""
    from helixis.agents import mirror_tools

    now = datetime.now(UTC)

    async def fake_freshness(_pool: Any, _company: str) -> dict[str, datetime]:
        return {"leases": now - timedelta(hours=40)}

    async def no_row(_company: str) -> None:
        return None

    asked: list[str] = []

    async def fetchval(sql: str, company_id: str) -> int | None:
        asked.append(sql)
        assert company_id == COMPANY, "every read carries the company (rule 11)"
        return 1 if "company_rentmanager_credentials" in sql else None

    conn = MagicMock()
    conn.fetchval = fetchval
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr(mirror_tools, "get_pool", lambda: pool)
    monkeypatch.setattr(mirror_tools.sync_state, "get_freshness", fake_freshness)
    monkeypatch.setattr(mirror_tools.credentials, "fetch_buildium_row", no_row)
    monkeypatch.setattr(mirror_tools, "fetch_rentvine_row", no_row)
    line = await mirror_tools._freshness_line(_pool_with_no_recent_write(), COMPANY)

    assert "leases last synced 40h ago" in line
    assert "no property-management connection on file" not in line
    assert "will NOT" not in line
    assert any("company_rentmanager_credentials" in sql for sql in asked)
    assert not any("buildium" in sql or "rentvine" in sql for sql in asked)


@pytest.mark.parametrize(
    ("holding", "expected"),
    [
        ("company_propertyware_credentials", True),
        ("company_rentmanager_credentials", True),
        (None, False),
    ],
)
@pytest.mark.asyncio
async def test_each_pms_connector_s_table_is_read_for_the_company(
    monkeypatch: pytest.MonkeyPatch, holding: str | None, expected: bool
) -> None:
    from helixis.agents import mirror_tools

    async def fetchval(sql: str, company_id: str) -> int | None:
        assert company_id == COMPANY
        return 1 if holding and holding in sql else None

    conn = MagicMock()
    conn.fetchval = fetchval
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr(mirror_tools, "get_pool", lambda: pool)
    assert await mirror_tools._holds_pms_connector_credentials(COMPANY) is expected


@pytest.mark.asyncio
async def test_an_unreadable_credential_state_asserts_no_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UNKNOWN must not collapse into "disconnected". A footer that cannot read
    the credential table still reports the age honestly and claims nothing
    about why (gotcha 55's decline-to-assert)."""
    from helixis.agents import mirror_tools

    now = datetime.now(UTC)

    async def fake_freshness(_pool: Any, _company: str) -> dict[str, datetime]:
        return {"leases": now - timedelta(hours=40)}

    async def boom(_company: str) -> None:
        raise RuntimeError("pool is gone")

    monkeypatch.setattr(mirror_tools.sync_state, "get_freshness", fake_freshness)
    monkeypatch.setattr(mirror_tools.credentials, "fetch_buildium_row", boom)
    line = await mirror_tools._freshness_line(_pool_with_no_recent_write(), COMPANY)

    assert "leases last synced 40h ago" in line
    assert "no Buildium connection" not in line


@pytest.mark.asyncio
async def test_fresh_data_never_reads_the_credential_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The healthy path pays nothing. If this ever fires on every footer it is
    a per-answer query added to the hottest read in the product."""
    from helixis.agents import mirror_tools

    now = datetime.now(UTC)
    calls: list[str] = []

    async def fresh(_pool: Any, _company: str) -> dict[str, datetime]:
        return {"leases": now, "bills": now - timedelta(hours=2)}

    async def counted(company: str) -> None:
        calls.append(company)
        return None

    monkeypatch.setattr(mirror_tools.sync_state, "get_freshness", fresh)
    monkeypatch.setattr(mirror_tools.credentials, "fetch_buildium_row", counted)
    await mirror_tools._freshness_line(_pool_with_no_recent_write(), COMPANY)

    assert calls == []


@pytest.mark.asyncio
async def test_footer_falls_back_to_legacy_when_no_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    from helixis.agents import mirror_tools

    async def empty(_pool: Any, _company: str) -> dict[str, datetime]:
        return {}

    async def legacy(_pool: Any, _company: str) -> datetime:
        return datetime.now(UTC) - timedelta(minutes=30)

    monkeypatch.setattr(mirror_tools.sync_state, "get_freshness", empty)
    monkeypatch.setattr(mirror_tools, "_latest_updated", legacy)
    line = await mirror_tools._freshness_line(_pool_with_no_recent_write(), COMPANY)
    assert "30 min ago" in line and "⚠" not in line


@pytest.mark.asyncio
async def test_footer_no_warning_when_everything_fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    from helixis.agents import mirror_tools

    now = datetime.now(UTC)

    async def fresh(_pool: Any, _company: str) -> dict[str, datetime]:
        return {"leases": now, "bills": now - timedelta(hours=2)}

    monkeypatch.setattr(mirror_tools.sync_state, "get_freshness", fresh)
    assert "⚠" not in await mirror_tools._freshness_line(_pool_with_no_recent_write(), COMPANY)


@pytest.mark.asyncio
async def test_the_as_of_override_keeps_the_callers_timestamp_and_still_warns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``delinquency_report`` holds a BETTER number than the verdict does.

    ``verdict.as_of`` is the newest core table — for a collections answer that
    is a flattering figure about some other table. The balances snapshot is
    the age of the rows actually printed, so the override keeps it. What the
    tool was missing is the VERDICT, and the override must not cost that:
    both halves are asserted here, because taking the timestamp and dropping
    the warning would be the same defect wearing a fix.
    """
    from helixis.agents import mirror_tools

    now = datetime.now(UTC)
    snapshot = now - timedelta(hours=40)

    async def fake_freshness(_pool: Any, _company: str) -> dict[str, datetime]:
        return {"leases": now, "balances": now - timedelta(hours=40)}

    async def has_row(_company: str) -> object:
        return {"company_id": COMPANY}

    monkeypatch.setattr(mirror_tools.sync_state, "get_freshness", fake_freshness)
    monkeypatch.setattr(mirror_tools.credentials, "fetch_buildium_row", has_row)
    line = await mirror_tools._freshness_line(_pool_with_no_recent_write(), COMPANY, as_of=snapshot)

    assert "40h ago" in line, "the caller's own snapshot must survive the override"
    assert "moments ago" not in line, (
        "the verdict's newest-core-table figure is exactly the flattering "
        "number the override exists to avoid"
    )
    assert "balances last synced 40h ago" in line, "the verdict has to survive too"


@pytest.mark.asyncio
async def test_the_override_reaches_the_pre_migration_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An account with no ``helixis_mirror_sync_state`` rows still gets the
    caller's timestamp. Honouring the override on one branch and not the other
    would make the footer's age depend on whether 0057 had been applied."""
    from helixis.agents import mirror_tools

    snapshot = datetime.now(UTC) - timedelta(hours=3)
    called = False

    async def empty(_pool: Any, _company: str) -> dict[str, datetime]:
        return {}

    async def legacy(_pool: Any, _company: str) -> datetime:
        nonlocal called
        called = True
        return datetime.now(UTC)

    monkeypatch.setattr(mirror_tools.sync_state, "get_freshness", empty)
    monkeypatch.setattr(mirror_tools, "_latest_updated", legacy)
    line = await mirror_tools._freshness_line(_pool_with_no_recent_write(), COMPANY, as_of=snapshot)

    assert "3h ago" in line
    assert not called, (
        "the legacy GREATEST was consulted even though the caller supplied a "
        "more specific timestamp — that is the flattering number returning"
    )


# ── dispatch event→table map ─────────────────────────────────────────


def test_event_table_map_longest_prefix_wins() -> None:
    assert _table_for_event("BankAccount.Transaction.Created") == "bank_accounts"
    assert _table_for_event("Lease.MoveOut.Created") == "leases"
    assert _table_for_event("LeaseTransaction.Created") == "transactions"
    assert _table_for_event("WorkOrder.Updated") == "work_orders"
    assert _table_for_event("PhoneLog.Created") is None  # untracked family


# ── aliases ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_alias_lookup_case_insensitive_sql_and_fail_open() -> None:
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        return_value={
            "alias": "Mike the plumber",
            "entity_type": "vendor",
            "entity_id": 4821,
            "label": "Mike Rowe Plumbing",
        }
    )
    row = await entity_aliases.lookup_alias(
        _pool(conn), company_id=COMPANY, alias="mike the plumber"
    )
    assert row is not None and row.entity_id == 4821
    assert "lower(alias) = lower($2)" in conn.fetchrow.await_args.args[0]
    # Pre-migration window: degrade to no-alias, never raise.
    conn.fetchrow = AsyncMock(side_effect=RuntimeError("relation does not exist"))
    assert await entity_aliases.lookup_alias(_pool(conn), company_id=COMPANY, alias="x") is None


@pytest.mark.asyncio
async def test_alias_upsert_validates_and_reports_failure() -> None:
    pool = _pool(MagicMock())
    # Bad type / id / empty alias → False without touching the pool.
    assert not await entity_aliases.upsert_alias(
        pool, company_id=COMPANY, alias="x", entity_type="spaceship", entity_id=1
    )
    assert not await entity_aliases.upsert_alias(
        pool, company_id=COMPANY, alias="", entity_type="vendor", entity_id=1
    )
    assert not await entity_aliases.upsert_alias(
        pool, company_id=COMPANY, alias="x", entity_type="vendor", entity_id=0
    )
    conn = MagicMock()
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    assert await entity_aliases.upsert_alias(
        _pool(conn), company_id=COMPANY, alias="Mike", entity_type="vendor", entity_id=4821
    )
    assert "ON CONFLICT (company_id, lower(alias))" in conn.execute.await_args.args[0]


# ── fuzzy fallback ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fuzzy_fallback_degrades_without_pg_trgm() -> None:
    """The similarity() query needs pg_trgm; pre-migration it must return ""
    so the caller falls through to the honest no-match reply — never a 500."""
    from helixis.agents.mirror_tools import _fuzzy_contact_fallback

    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=RuntimeError("function similarity does not exist"))
    assert await _fuzzy_contact_fallback(_pool(conn), COMPANY, "Jhon Smtih") == ""


@pytest.mark.asyncio
async def test_fuzzy_fallback_labels_results_as_fuzzy() -> None:
    from helixis.agents.mirror_tools import _fuzzy_contact_fallback

    conn = MagicMock()
    conn.fetch = AsyncMock(
        return_value=[
            {
                "full_name": "John Smith",
                "kind": "tenant",
                "email": "j@x.com",
                "phone": None,
                "sim": 0.62,
            }
        ]
    )
    out = await _fuzzy_contact_fallback(_pool(conn), COMPANY, "Jhon Smtih")
    assert "John Smith" in out
    # Fuzzy results must SAY they're fuzzy — never presented as exact.
    assert "fuzzy" in out.lower()
    assert "confirm" in out.lower()


# ── empty mirror: never synced vs never connected ────────────────────


@pytest.mark.asyncio
async def test_an_empty_mirror_with_no_credential_does_not_point_at_the_live_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠ The two states show the same zero rows and need OPPOSITE instructions.

    "Nothing synced yet — use the live Buildium tools" is right for a
    connected account. On a disconnected one the live tools want the same
    credential the sync wanted, so the sentence names the one route that also
    cannot work — gotcha 46's shape, and the same defect the staleness footer
    above had.
    """
    from helixis.agents import mirror_tools

    async def no_row(_company: str) -> None:
        return None

    monkeypatch.setattr(mirror_tools.credentials, "fetch_buildium_row", no_row)
    msg = await mirror_tools._not_synced(COMPANY)

    assert "no Buildium connection on file" in msg
    assert "cannot be used either" in msg
    assert "Settings → Connectors" in msg
    # The instruction that is false here must be GONE, not merely joined.
    assert "Use the live Buildium tools" not in msg


@pytest.mark.asyncio
async def test_an_empty_mirror_with_a_credential_still_points_at_the_live_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other direction (gotcha 38). A connected account that simply has
    not synced yet must keep the working instruction — telling that customer
    their connection is missing sends them to re-enter a key that is fine."""
    from helixis.agents import mirror_tools

    async def a_row(_company: str) -> dict[str, Any]:
        return {"company_id": COMPANY, "account_id": 695763}

    monkeypatch.setattr(mirror_tools.credentials, "fetch_buildium_row", a_row)
    msg = await mirror_tools._not_synced(COMPANY)

    assert "Use the live Buildium tools" in msg
    assert "no Buildium connection" not in msg


@pytest.mark.asyncio
async def test_an_unreadable_credential_table_keeps_the_older_wording(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UNKNOWN is not "disconnected". Claiming a missing connection we could
    not verify is a fresh false statement about the account (gotcha 55)."""
    from helixis.agents import mirror_tools

    async def boom(_company: str) -> None:
        raise RuntimeError("pool is gone")

    monkeypatch.setattr(mirror_tools.credentials, "fetch_buildium_row", boom)
    msg = await mirror_tools._not_synced(COMPANY)

    assert "Use the live Buildium tools" in msg
    assert "no Buildium connection" not in msg


# ── the read side: row_count comes back, and unknown stays unknown ──


@pytest.mark.asyncio
async def test_get_stage_sync_returns_the_row_count_nothing_else_reads() -> None:
    """BOTH SIDES (gotcha 28). The decision tests above patch this function;
    something has to check the function actually returns what they assume.

    `record_sync` has written `row_count` on every stage since 0057 and
    `get_freshness` selects `table_name, synced_at` — so the column that
    separates "we never asked" from "your account has none" was stored and
    dropped at the read for a year.
    """
    when = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"synced_at": when, "row_count": 0})

    stage = await mirror_sync_state.get_stage_sync(_pool(conn), COMPANY, "properties")

    assert stage is not None
    assert stage.row_count == 0
    assert stage.synced_at == when
    sql = conn.fetchrow.await_args.args[0]
    # ⚠ The PROJECTED COLUMNS, not a substring (gotcha 40). `"row_count" in sql`
    # is satisfied by `SELECT synced_at, NULL AS row_count` — a mutation that
    # nulls the column for every row in production and left this test green.
    projected = [c.strip() for c in sql.split("SELECT", 1)[1].split("FROM", 1)[0].split(",")]
    assert projected == ["synced_at", "row_count"], (
        f"the stored row_count is not being read back ({projected}) — the column "
        f"that separates 'we never asked' from 'your account has none'"
    )
    assert "company_id = $1::uuid" in sql, "gotcha 47 — a mirror read is tenant-scoped"


@pytest.mark.asyncio
@pytest.mark.parametrize("row", [None, "raise"])
async def test_get_stage_sync_declines_rather_than_guessing(row: object) -> None:
    """No row and an unreadable table both answer `None`, and every caller
    must read that as "not enough evidence" — never as zero rows. Collapsing
    them is only safe because the fallback is identical (gotchas 55/57)."""
    conn = MagicMock()
    conn.fetchrow = (
        AsyncMock(side_effect=RuntimeError("pool is gone"))
        if row == "raise"
        else AsyncMock(return_value=None)
    )

    assert await mirror_sync_state.get_stage_sync(_pool(conn), COMPANY, "properties") is None


# ── empty mirror: the THIRD state — synced, and there is nothing there ──


def _stage(row_count: int | None) -> object:
    from helixis.db.repositories.mirror_sync_state import StageSync

    return StageSync(synced_at=datetime(2026, 9, 10, 12, 0, tzinfo=UTC), row_count=row_count)


def _connected(monkeypatch: pytest.MonkeyPatch) -> None:
    from helixis.agents import mirror_tools

    async def a_row(_company: str) -> dict[str, Any]:
        return {"company_id": COMPANY, "account_id": 695763}

    monkeypatch.setattr(mirror_tools.credentials, "fetch_buildium_row", a_row)


def _stage_answer(monkeypatch: pytest.MonkeyPatch, answer: object) -> None:
    """What the properties stage's sync row says. `answer` may be a StageSync,
    None, or an Exception to raise."""
    from helixis.agents import mirror_tools

    monkeypatch.setattr(mirror_tools, "get_pool", lambda: MagicMock())

    async def stage(_pool: object, _company: str, _table: str) -> object:
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(mirror_tools.sync_state, "get_stage_sync", stage)


@pytest.mark.asyncio
async def test_a_synced_account_that_owns_nothing_is_not_told_to_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE DEFECT. Connected, synced, and Buildium genuinely returned zero
    properties — the one state where the product is working perfectly, and it
    was told "no data has been synced yet … run Sync data". Following that
    instruction re-runs a sync that already succeeded and changes nothing,
    which is exactly how a working product reads as broken.

    `row_count` has been stored on every stage since 0057 and read by nothing;
    it is the only fact that separates this state from "we never asked"
    (gotcha 41's family — fetched and thrown away).
    """
    from helixis.agents import mirror_tools

    _connected(monkeypatch)
    _stage_answer(monkeypatch, _stage(0))
    msg = await mirror_tools._not_synced(COMPANY)

    assert "returned zero rental properties" in msg
    assert "no properties yet" in msg
    # The two false claims, GONE rather than merely joined.
    assert "no data has been synced yet" not in msg
    assert "no Buildium connection" not in msg
    # And the instruction that cannot help must not survive anywhere in it.
    assert "Sync data" not in msg.replace("Do NOT suggest running Sync data", "")


@pytest.mark.asyncio
async def test_a_stage_that_found_rows_still_asks_for_a_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other direction (gotcha 38), and a genuinely different third thing:
    a stored count ABOVE zero beside an EMPTY mirror means rows were synced and
    are now missing. "Your account has no properties" would be a fresh false
    claim there, and re-syncing is the right advice."""
    from helixis.agents import mirror_tools

    _connected(monkeypatch)
    _stage_answer(monkeypatch, _stage(12))
    msg = await mirror_tools._not_synced(COMPANY)

    assert "Use the live Buildium tools" in msg
    assert "returned zero rental properties" not in msg


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "why"),
    [
        (None, "no row at all — never synced, or the stage failed and was left unstamped"),
        ("null_count", "a stored NULL count is not a measurement of zero"),
        (RuntimeError("pool is gone"), "an unreadable table says nothing about their books"),
    ],
)
async def test_every_uncertain_sync_state_keeps_the_older_wording(
    monkeypatch: pytest.MonkeyPatch, answer: object, why: str
) -> None:
    """⚠ UNKNOWN is not "empty" — the whole point, pointed the other way.
    Telling a customer their Buildium account holds no properties, on the
    strength of a row we could not read, is the same defect being fixed
    (gotchas 55/57). Each of these must fall back, not assert."""
    from helixis.agents import mirror_tools

    _connected(monkeypatch)
    _stage_answer(monkeypatch, _stage(None) if answer == "null_count" else answer)
    msg = await mirror_tools._not_synced(COMPANY)

    assert "Use the live Buildium tools" in msg, why
    assert "returned zero rental properties" not in msg, why


@pytest.mark.asyncio
async def test_a_missing_credential_still_wins_over_an_empty_sync_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Order matters. A disconnected account cannot be told "your Buildium
    account has no properties" on the strength of a stale sync row — the
    credential question is answered first and its answer is the one that
    changes what the customer has to do."""
    from helixis.agents import mirror_tools

    async def no_row(_company: str) -> None:
        return None

    monkeypatch.setattr(mirror_tools.credentials, "fetch_buildium_row", no_row)
    _stage_answer(monkeypatch, _stage(0))
    msg = await mirror_tools._not_synced(COMPANY)

    assert "no Buildium connection on file" in msg
    assert "returned zero rental properties" not in msg


def test_every_empty_mirror_return_asks_which_state_it_is_in() -> None:
    """⚠ The constant is reachable from twelve tools. A thirteenth added as a
    bare `return _NOT_SYNCED` re-opens the defect for one tool silently —
    nothing fails, the message is simply wrong again on that one path. Walks
    the AST, so a comment naming the constant cannot satisfy it (gotcha 40).
    """
    import ast

    path = Path(__file__).resolve().parents[2] / "src/helixis/agents/mirror_tools.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    # ⚠ RE-POINTED, NOT RELAXED (gotchas 89/93/95). `_not_synced` used to be a
    # one-line ternary, so it had no bare `return _NOT_SYNCED` of its own and a
    # whole-file scan happened to work. It is a three-way branch now — the
    # third state being an account whose sync RAN and found nothing — so the
    # scan has to name the one function allowed to return the constant rather
    # than assume no function does. The property is unchanged: every TOOL goes
    # through the chooser.
    chooser = {
        id(node)
        for fn in ast.walk(tree)
        if isinstance(fn, ast.AsyncFunctionDef | ast.FunctionDef) and fn.name == "_not_synced"
        for node in ast.walk(fn)
    }
    bare = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Return)
        and isinstance(node.value, ast.Name)
        and node.value.id == "_NOT_SYNCED"
        and id(node) not in chooser
    ]
    assert not bare, (
        f"{path.name} lines {bare} return _NOT_SYNCED directly. Use "
        f"`return await _not_synced(company_id)` so a DISCONNECTED account is "
        f"not told to use the live Buildium tools, which need the same key."
    )
