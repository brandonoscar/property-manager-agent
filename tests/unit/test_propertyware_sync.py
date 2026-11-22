"""One company's Propertyware pass, end to end.

``sync_company`` runs for real over a fake Propertyware (an
``httpx.MockTransport`` paging the synthetic fixtures) and a recording pool
that refuses what asyncpg refuses. What is asserted is what would have been
sent to Propertyware and to Postgres.

The rules: a switched-off connector or a company without credentials is
refused before any call; a list that fails is STALE and the rest land; a
partial read is written but neither stamped nor pruned; a complete read stamps
its stage; every mapped row carries only the writer's own columns; each table
takes ids from one Propertyware sequence; turnovers open from recent move-outs.
"""

from __future__ import annotations

import ast
import inspect
import logging
from datetime import date
from typing import Any

import pytest

from helixis.pms import mirror
from helixis.pms.errors import Failure, PmsApiError, PmsUnavailableError
from helixis.pms.mirror import BALANCE_COLUMNS, KEYED_COLUMNS, LINK_TABLE, PER_PARENT, SyncResult
from helixis.propertyware import _paths, client, mapper, sync
from tests.unit._propertyware_fake import (
    API_PREFIX,
    CLIENT_ID,
    CLIENT_SECRET,
    COMPANY,
    TODAY,
    Clock,
    FakePool,
    FakePropertyware,
    all_rows,
    creds_row,
    fixture_rows,
    inserted_rows,
    settings,
)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    """The pass's start (``sync._now``), moved by a test that runs two."""
    pinned = Clock()
    monkeypatch.setattr(sync, "_now", pinned)
    return pinned


@pytest.fixture
def switched_on(monkeypatch: pytest.MonkeyPatch, clock: Clock) -> None:
    current = settings(enabled=True)
    monkeypatch.setattr("helixis.settings.get_settings", lambda: current)
    monkeypatch.setattr(sync, "today_in", lambda _zone: TODAY)


async def _run(
    server: FakePropertyware | None = None, pool: FakePool | None = None
) -> tuple[SyncResult, FakePool, FakePropertyware]:
    server = server or FakePropertyware()
    pool = pool or FakePool(creds=creds_row())
    result = await sync.sync_company(pool, COMPANY, http=server.http())
    return result, pool, server


# ── refusals: nothing read, nothing sent ──────────────────────────────────


async def test_a_switched_off_connector_refuses_before_any_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = settings(enabled=False)
    monkeypatch.setattr("helixis.settings.get_settings", lambda: current)
    server, pool = FakePropertyware(), FakePool(creds=creds_row())
    with pytest.raises(sync.PropertywareSyncRefused, match="switched off"):
        await sync.sync_company(pool, COMPANY, http=server.http())
    assert server.requests == [] and pool.log == []


async def test_a_company_without_credentials_is_refused_before_any_call(switched_on: None) -> None:
    server, pool = FakePropertyware(), FakePool(creds=None)
    with pytest.raises(sync.PropertywareSyncRefused, match="no Propertyware connection"):
        await sync.sync_company(pool, COMPANY, http=server.http())
    assert server.requests == []
    assert [s for s, _a in pool.log if not s.startswith("SELECT")] == [], "nothing was written"


async def test_unusable_credentials_are_refused_before_any_call(switched_on: None) -> None:
    server, pool = FakePropertyware(), FakePool(creds=creds_row(system_id="99\r\nX-Evil: 1"))
    with pytest.raises(sync.PropertywareSyncRefused, match="system id"):
        await sync.sync_company(pool, COMPANY, http=server.http())
    assert server.requests == []


# ── a clean pass ──────────────────────────────────────────────────────────


async def test_a_clean_pass_lands_every_table_and_stamps_every_stage(switched_on: None) -> None:
    result, pool, _server = await _run()
    assert result.pms == "propertyware" and result.company_id == COMPANY
    assert result.failed == {}
    assert set(result.rows_by_table) == set(sync.SYNC_STATE_STAGE) | sync.DERIVED_TABLES
    assert result.confirmed_complete == set(sync.SYNC_STATE_STAGE)
    assert pool.stamped() == set(sync.SYNC_STATE_STAGE.values())
    sources = {args[3] for _s, args in pool.inserts("helixis_mirror_sync_state")}
    assert sources == {sync.SYNC_SOURCE}
    # Every row is Propertyware's and this company's.
    for _sql, args in pool.inserts("helixis_properties"):
        assert args[2:4] == (COMPANY, "propertyware")
    # A complete read prunes what Propertyware no longer has, by id.
    ((_sql, args),) = pool.deletes("helixis_properties")
    assert args[:3] == (COMPANY, "propertyware", [r["id"] for r in fixture_rows("/buildings")])


async def test_every_request_is_a_documented_paged_read_and_nothing_else(switched_on: None) -> None:
    """The captured-URL half of "no beta read and no payment path in any
    production call"; the AST half is in test_propertyware_client."""
    _result, _pool, server = await _run()
    assert server.paths() == list(client.READS)
    for request in server.requests:
        path = request.url.path.removeprefix(API_PREFIX)
        key = f"GET {path}"
        assert request.method == "GET"
        assert key in _paths.QUERY_PARAMS and key not in _paths.BETA
        assert not client.is_payment_path(path)
        assert request.url.params["limit"] == "500"
        assert request.url.params["orderby"] == "id asc"
        assert CLIENT_SECRET not in str(request.url) and CLIENT_ID not in str(request.url)
    by_path = {r.url.path.removeprefix(API_PREFIX): r.url.params for r in server.requests}
    assert by_path["/leases"]["includeOtherBalances"] == "true", "arBalance needs it (PW-6)"
    assert by_path["/contacts"]["type"] == "TENANT"


async def test_the_lease_unit_rule_reaches_the_mirror(switched_on: None) -> None:
    _result, pool, _server = await _run()
    leases = {row["buildium_id"]: row for row in inserted_rows(pool, "helixis_leases")}
    assert leases[990600001]["unit_id"] == 990200001
    # Single-family (its building id, while a unit elsewhere has that id),
    # a unit in another building, and no unit: all unknown, never guessed.
    assert leases[990600002]["unit_id"] is None
    assert leases[990600003]["unit_id"] is None
    assert leases[990600006]["unit_id"] is None
    assert all(row["security_deposit"] is None for row in leases.values())


async def test_the_balance_snapshot_holds_only_leases_that_owe(switched_on: None) -> None:
    _result, pool, _server = await _run()
    balances = {row["lease_id"]: row for row in inserted_rows(pool, "helixis_lease_balances")}
    assert set(balances) == {990600001, 990600003, 990600004, 990600005}
    assert balances[990600005]["balance_0_30"] is None, "aging that does not add up is unknown"
    # Leases that paid up drop out of the snapshot.
    ((_sql, args),) = pool.deletes("helixis_lease_balances")
    assert sorted(args[2]) == sorted(balances)


async def test_lease_status_reaches_the_mirror_in_the_readers_word(switched_on: None) -> None:
    """Every lease reader filters ``status = 'Active'``: a customer whose word
    for an active lease is "Current" must not read as having none."""
    _result, pool, _server = await _run()
    statuses = {row["buildium_id"]: row["status"] for row in inserted_rows(pool, "helixis_leases")}
    assert statuses[990600001] == "Active", "the customer calls it Current"
    assert statuses[990600004] == "Inactive"
    assert statuses[990600007] is None, "not active, yet its word says Active: unknown"
    assert statuses[990600008] is None, "no active flag: unknown, never inactive"


# ── lease-tenant links: active leases only ────────────────────────────────


def _link_pairs(pool: FakePool) -> set[tuple[int, int]]:
    return {(args[1], args[2]) for _sql, args in pool.inserts("helixis_lease_tenants")}


async def test_only_active_leases_are_linked(switched_on: None) -> None:
    _result, pool, _server = await _run()
    assert _link_pairs(pool) == {
        (990600001, 990400001),
        (990600001, 990400002),
        (990600002, 990400003),
    }


async def test_a_lease_that_turns_inactive_loses_its_links_at_the_next_complete_read(
    switched_on: None, clock: Clock
) -> None:
    """The shared writer prunes every link a complete read no longer lists.
    Applying the second pass's DELETEs to what the first pass stored leaves
    exactly the inactive lease's link to be removed.

    Re-pointed from exactly one DELETE: since the incremental writer, the
    leases prune also removes the links of a lease Propertyware no longer has
    (``pms.mirror._CHILDREN``), so every DELETE on the links is applied.

    Re-pointed to run the second pass a day later: since the incremental
    sync, the pass after a full one reads only the changes, and the next
    complete read is the nightly reconcile (a full read for Propertyware).
    The window's own rule, a lease read in a window loses the link it no
    longer has, is in test_propertyware_incremental."""
    _first, pool, _server = await _run()
    stored = _link_pairs(pool)
    assert (990600002, 990400003) in stored

    rows = all_rows()
    birch = next(r for r in rows["/leases"] if r["id"] == 990600002)
    birch["active"] = False
    pool.log.clear()
    clock.advance(hours=24)
    second, pool, _server = await _run(FakePropertyware(rows), pool=pool)
    assert "helixis_lease_tenants" in second.confirmed_complete
    deletes = pool.deletes("helixis_lease_tenants")
    links = [(sql, args) for sql, args in deletes if "NOT EXISTS" in sql]
    by_lease = [(sql, args) for sql, args in deletes if "NOT EXISTS" not in sql]
    ((_sql, args),) = links
    assert args[:2] == (COMPANY, "propertyware")
    removed = stored - set(zip(args[2], args[3], strict=True))
    for sql, lease_args in by_lease:
        assert "NOT (lease_buildium_id = ANY(" in sql, sql
        assert lease_args[:2] == (COMPANY, "propertyware")
        removed |= {pair for pair in stored if pair[0] not in set(lease_args[2])}
    assert removed == {(990600002, 990400003)}


async def test_a_partial_leases_read_prunes_no_link(switched_on: None) -> None:
    server = FakePropertyware(totals={"/leases": len(fixture_rows("/leases")) + 1})
    result, pool, _server = await _run(server)
    assert pool.deletes("helixis_lease_tenants") == []
    assert "helixis_lease_tenants" in result.failed


# ── stale, never empty ────────────────────────────────────────────────────


async def test_a_failed_list_is_stale_and_the_rest_land(
    switched_on: None, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        result, pool, _server = await _run(FakePropertyware(fail={"/vendors": 403}))
    assert set(result.failed) == {"helixis_vendors"}
    # Re-pointed from `"403" in` the reason: the reason is now the sentence
    # the sync status shows, naming the permission the key lacks, and the
    # status stays in the log line beside it.
    assert result.failed["helixis_vendors"] == sync.read_failure(
        "/vendors", PmsApiError(403, Failure.PERMISSION, False, "synthetic")
    )
    assert "VENDORS - Read" in result.failed["helixis_vendors"]
    assert "/vendors failed" in caplog.text and "403" in caplog.text
    assert pool.inserts("helixis_vendors") == [] and pool.deletes("helixis_vendors") == []
    assert "vendors" not in pool.stamped()
    assert pool.stamped() == set(sync.SYNC_STATE_STAGE.values()) - {"vendors"}
    assert len(pool.inserts("helixis_properties")) == len(fixture_rows("/buildings"))


async def test_a_404_on_a_list_is_stale_never_no_rows(
    switched_on: None, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        result, pool, _server = await _run(FakePropertyware(fail={"/buildings": 404}))
    # Re-pointed from `"404" in` the reason, as above: a 404 is a failure
    # Occupella did not expect (the spec answers 404 for a mis-cased path or
    # parameter), never "no rows", and the status is in the log.
    assert result.failed["helixis_properties"] == sync.read_failure(
        "/buildings", PmsApiError(404, Failure.NOT_FOUND, False, "synthetic")
    )
    assert "did not expect" in result.failed["helixis_properties"]
    assert "/buildings failed" in caplog.text and "404" in caplog.text
    assert pool.deletes("helixis_properties") == [], "a 404 must never empty the table"
    assert "properties" not in pool.stamped()
    # The owners still land, and their properties are left as they were.
    owners = inserted_rows(pool, "helixis_owners")
    assert owners and all("property_ids" not in row for row in owners)


async def test_a_partial_read_is_written_but_never_stamped_or_pruned(switched_on: None) -> None:
    server = FakePropertyware(totals={"/units": len(fixture_rows("/units")) + 7})
    result, pool, _server = await _run(server)
    assert len(pool.inserts("helixis_units")) == len(fixture_rows("/units")), "real rows land"
    # The units' own prune is by unit id. (The writer still drops units whose
    # BUILDING is gone, from the complete buildings read: that one is keyed
    # on property_id.)
    own_prune = [s for s, _a in pool.deletes("helixis_units") if "buildium_id = ANY" in s]
    assert own_prune == []
    assert all("property_id = ANY" in s for s, _a in pool.deletes("helixis_units"))
    assert "units" not in pool.stamped()
    assert "helixis_units" in result.failed
    assert "helixis_units" not in result.confirmed_complete
    # A unit missing from a partial read may exist: a lease whose unit was
    # not found keeps its stored unit, while a confirmed match is written.
    leases = {row["buildium_id"]: row for row in inserted_rows(pool, "helixis_leases")}
    assert "unit_id" not in leases[990600003]
    assert leases[990600001]["unit_id"] == 990200001


async def test_a_complete_read_stamps_its_own_stage(switched_on: None) -> None:
    """Units stamp even when bills fail: a stage waits only on its own tables."""
    _result, pool, _server = await _run(FakePropertyware(fail={"/bills": 403}))
    assert "units" in pool.stamped()
    assert "bills" not in pool.stamped()


async def test_a_lease_without_a_readable_balance_keeps_the_old_snapshot(
    switched_on: None,
) -> None:
    rows = all_rows()
    del rows["/leases"][0]["arBalance"]
    result, pool, _server = await _run(FakePropertyware(rows))
    assert "helixis_lease_balances" in result.failed
    assert pool.inserts("helixis_lease_balances") == []
    assert pool.deletes("helixis_lease_balances") == [], "one unknown balance reads as 'current'"
    assert "balances" not in pool.stamped()
    assert "leases" in pool.stamped(), "the leases themselves came through"


async def test_a_failed_charges_read_keeps_the_totals_and_leaves_aging_unknown(
    switched_on: None,
) -> None:
    result, pool, _server = await _run(FakePropertyware(fail={"/leases/charges": 403}))
    assert "helixis_lease_balances" not in result.failed
    balances = inserted_rows(pool, "helixis_lease_balances")
    assert balances and all(row["total_balance"] is not None for row in balances)
    assert all(row[name] is None for row in balances for name, _ in mapper.BUCKETS)


async def test_no_secret_reaches_a_log_or_the_result(
    switched_on: None, caplog: pytest.LogCaptureFixture
) -> None:
    rows = all_rows()
    server = FakePropertyware(rows, fail={"/vendors": 401})
    with caplog.at_level(logging.DEBUG):
        result, _pool, _server = await _run(server)
    for secret in (CLIENT_ID, CLIENT_SECRET):
        assert secret not in caplog.text
        assert all(secret not in reason for reason in result.failed.values())


# ── the writer's columns and one id sequence per table ────────────────────


def _link_keys() -> set[str]:
    """The keys the shared link writer reads off a row, from its own source."""
    tree = ast.parse(inspect.getsource(mirror._write_links))
    return {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    }


def _allowed(table: str) -> set[str]:
    if table in KEYED_COLUMNS:
        columns = set(KEYED_COLUMNS[table])
    elif table == "helixis_lease_balances":
        columns = set(BALANCE_COLUMNS)
    elif table in PER_PARENT:
        columns = set(PER_PARENT[table][1])
    elif table == LINK_TABLE:
        columns = _link_keys()
    else:
        raise AssertionError(f"{table} is not a table the shared writer knows")
    return columns | {"buildium_id", "raw"}


def _fixture_reads() -> dict[str, sync.ResourceRead]:
    return {
        path: sync.ResourceRead(rows=fixture_rows(path), complete=True, started=0.0)
        for path in client.READS
    }


def test_the_link_writer_reads_the_keys_this_test_holds_links_to() -> None:
    assert _link_keys() == {"lease_buildium_id", "tenant_buildium_id", "status", "move_in_date"}


def test_every_mapped_row_carries_only_columns_the_writer_writes() -> None:
    """The writer drops any other key without a sound, so a misspelt column
    would lose its data silently."""
    rows, _report = sync.mirror_rows(_fixture_reads(), today=TODAY, now=1.0)
    assert set(rows) == set(sync.SYNC_STATE_STAGE)
    checked = 0
    for table, read in rows.items():
        assert read.error is None, table
        allowed = _allowed(table)
        for row in read.rows:
            assert set(row) <= allowed, (table, sorted(set(row) - allowed))
            checked += 1
    assert checked > 30, "the guard checked almost nothing"


def test_each_table_takes_ids_from_one_propertyware_sequence() -> None:
    """Two sequences in one table would overwrite each other under
    UNIQUE (company_id, buildium_id). The fixtures make a unit id equal a
    building id, so a building id leaking into the units table is visible."""
    rows, _report = sync.mirror_rows(_fixture_reads(), today=TODAY, now=1.0)

    def ids(path: str) -> set[int]:
        return {row["id"] for row in fixture_rows(path)}

    owner_ids = {o["id"] for p in fixture_rows("/portfolios") for o in p["owners"]}
    source = {
        "helixis_properties": ids("/buildings"),
        "helixis_units": ids("/units"),
        "helixis_owners": owner_ids,
        "helixis_tenants": ids("/contacts"),
        "helixis_vendors": ids("/vendors"),
        "helixis_leases": ids("/leases"),
        "helixis_work_orders": ids("/workorders"),
        "helixis_bills": ids("/bills"),
        "helixis_gl_accounts": ids("/accounting/glaccounts"),
    }
    for table, expected in source.items():
        got = {row["buildium_id"] for row in rows[table].rows}
        assert got and got <= expected, table
    assert {r["buildium_id"] for r in rows["helixis_units"].rows} == ids("/units")
    assert {r["lease_id"] for r in rows["helixis_lease_balances"].rows} <= ids("/leases")
    assert {r["bill_id"] for r in rows["helixis_bill_lines"].rows} <= ids("/bills")
    links = rows["helixis_lease_tenants"].rows
    assert {x["lease_buildium_id"] for x in links} <= ids("/leases")
    assert {x["tenant_buildium_id"] for x in links} <= ids("/contacts")
    # Relationship columns name the right sequence too.
    unit_refs = {
        row.get("unit_id")
        for table in ("helixis_leases", "helixis_work_orders", "helixis_lease_balances")
        for row in rows[table].rows
    } - {None}
    assert unit_refs <= ids("/units")
    property_refs = {
        row["property_id"]
        for table in ("helixis_units", "helixis_leases")
        for row in rows[table].rows
    }
    assert property_refs <= ids("/buildings")


def test_a_mapping_bug_stales_its_table_and_spares_the_rest(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(_row: Any) -> Any:
        raise KeyError("synthetic bug")

    monkeypatch.setattr(mapper, "map_vendor", broken)
    with caplog.at_level(logging.ERROR):
        rows, _report = sync.mirror_rows(_fixture_reads(), today=TODAY, now=1.0)
    # Re-pointed from "mapping failed: KeyError": the reason is the sentence
    # the sync status shows, and the exception's name went to the log.
    assert rows["helixis_vendors"].error == sync._MAPPING_FAILED
    assert rows["helixis_properties"].error is None
    assert "mapping helixis_vendors failed (KeyError)" in caplog.text


def test_the_prune_window_runs_from_each_tables_own_read() -> None:
    reads = _fixture_reads()
    reads["/buildings"] = sync.ResourceRead(
        rows=fixture_rows("/buildings"), complete=True, started=10.0
    )
    reads["/bills"] = sync.ResourceRead(rows=fixture_rows("/bills"), complete=True, started=95.0)
    rows, _report = sync.mirror_rows(reads, today=TODAY, now=100.0)
    assert rows["helixis_properties"].read_seconds == 90.0
    assert rows["helixis_bill_lines"].read_seconds == 5.0
    assert rows["helixis_bill_lines"].parents == frozenset({990800001, 990800002, 990800004})


def test_unresolved_units_are_reported_by_table_and_outcome() -> None:
    _rows, report = sync.mirror_rows(_fixture_reads(), today=TODAY, now=1.0)
    assert report.unresolved_units["helixis_leases"] == {
        mapper.SINGLE_FAMILY: [990600002],
        mapper.NOT_FOUND: [990600003],
        mapper.NO_UNIT: [990600006],
    }
    assert report.unresolved_units["helixis_work_orders"] == {
        mapper.SINGLE_FAMILY: [990700002],
        mapper.NO_UNIT: [990700003],
    }
    assert report.not_tenants == 1 and report.credits == 1
    assert report.aging_unknown == [990600005] and report.balances_unknown == []


# ── turnovers ─────────────────────────────────────────────────────────────


async def test_a_recent_or_upcoming_move_out_opens_a_turnover_and_an_old_one_does_not(
    switched_on: None,
) -> None:
    result, pool, _server = await _run()
    # 990600002 moved out 15 days ago; 990600001 is scheduled to; 990600004
    # moved out in 2024, long past the 30-day lookback.
    assert pool.turnovers == {990600001: date(2026, 10, 31), 990600002: date(2026, 9, 20)}
    assert result.rows_by_table["helixis_turnovers"] == 2
    for _sql, args in pool.statements("INSERT INTO helixis_turnovers"):
        assert args[0] == COMPANY and args[-1] == "propertyware"
    cards = pool.inserts("helixis_reminders")
    assert len(cards) == 2, "one desk card per turnover opened"


def _touched(rows: dict[str, list[dict[str, Any]]], ids: set[int], when: str) -> None:
    """Stamp the leases ``ids`` as changed at ``when``, as Propertyware does."""
    for row in rows["/leases"]:
        if row["id"] in ids:
            row["lastModifiedDateTime"] = when


async def test_an_unchanged_move_out_is_not_opened_again(switched_on: None, clock: Clock) -> None:
    """``only_if_new``: a turnover the manager dismissed must not come back
    with the next pass.

    Re-pointed for the incremental sync: the next pass reads only what
    changed, so every lease is stamped changed (its move-out date not) and
    the pass is shown to have read them, or "no turnover" would pass on a
    pass that read no lease at all."""
    pool = FakePool(creds=creds_row())
    first, pool, _server = await _run(pool=pool)
    assert first.rows_by_table["helixis_turnovers"] == 2
    cards_after_first = len(pool.inserts("helixis_reminders"))
    rows = all_rows()
    _touched(rows, {r["id"] for r in rows["/leases"]}, "2026-10-05T13:00:00Z")
    clock.advance(hours=4)
    second, pool, _server = await _run(FakePropertyware(rows), pool=pool)
    assert second.rows_by_table["helixis_leases"] == len(rows["/leases"]), "read in the window"
    assert second.rows_by_table["helixis_turnovers"] == 0
    assert len(pool.inserts("helixis_reminders")) == cards_after_first


async def test_a_moved_move_out_date_opens_it_again(switched_on: None, clock: Clock) -> None:
    """Re-pointed for the incremental sync: the lease whose date moved carries
    the change time Propertyware gives a changed record, so the next pass,
    which reads only changes, reads it."""
    pool = FakePool(creds=creds_row())
    await _run(pool=pool)
    rows = all_rows()
    lease = next(r for r in rows["/leases"] if r["id"] == 990600001)
    lease["scheduleMoveOutDate"] = "2026-11-15"
    _touched(rows, {990600001}, "2026-10-05T13:00:00Z")
    clock.advance(hours=4)
    again, pool, _server = await _run(FakePropertyware(rows), pool=pool)
    assert again.rows_by_table["helixis_turnovers"] == 1
    assert pool.turnovers[990600001] == date(2026, 11, 15)


@pytest.mark.parametrize(
    "server",
    [
        pytest.param(lambda: FakePropertyware(fail={"/leases": 403}), id="failed"),
        pytest.param(
            lambda: FakePropertyware(totals={"/leases": len(fixture_rows("/leases")) + 1}),
            id="partial",
        ),
    ],
)
async def test_a_leases_table_that_did_not_land_opens_no_turnover(
    switched_on: None, server: Any
) -> None:
    result, pool, _server = await _run(server())
    assert "helixis_leases" in result.failed
    assert "helixis_turnovers" not in result.rows_by_table
    assert pool.statements("INSERT INTO helixis_turnovers") == []


async def test_a_turnover_that_fails_never_fails_the_pass(
    switched_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from helixis.db.repositories import turnovers as turnovers_repo

    calls: list[int] = []

    async def flaky(_pool: Any, **kw: Any) -> bool:
        calls.append(kw["lease_id"])
        if len(calls) == 1:
            raise RuntimeError("synthetic")
        return True

    monkeypatch.setattr(turnovers_repo, "start_turnover", flaky)
    result, _pool, _server = await _run()
    assert len(calls) == 2 and result.rows_by_table["helixis_turnovers"] == 1
    assert result.failed == {}


# ── a disconnect while the reads run ──────────────────────────────────────


class _CredentialsChangeMidPass(FakePool):
    """Serves ``creds`` for the pass's first credentials read and ``then``
    for every later one: a disconnect, or new keys, while the reads run."""

    def __init__(self, *, then: dict[str, Any] | None, **kwargs: Any):
        super().__init__(**kwargs)
        self._then = then
        self._served = 0

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        if "company_propertyware_credentials" in sql:
            self._served += 1
            if self._served == 2:
                self.creds = self._then
        return await super().fetchrow(sql, *args)


@pytest.mark.parametrize(
    "then",
    [
        pytest.param(None, id="disconnected"),
        pytest.param(
            creds_row(client_secret="pw-client-secret-synthetic-another-key"), id="new-keys"
        ),
        pytest.param({**creds_row(), "encrypted_client_secret": "not-a-token"}, id="unusable"),
    ],
)
async def test_a_pass_whose_credentials_changed_while_it_read_writes_nothing(
    switched_on: None, then: dict[str, Any] | None
) -> None:
    """The disconnect deletes the row and purges the mirror. A pass already
    reading must not write afterwards: that would put the purged rows back,
    or another account's rows in place of the new connection's."""
    server = FakePropertyware()
    pool = _CredentialsChangeMidPass(creds=creds_row(), then=then)
    with pytest.raises(sync.PropertywareSyncRefused, match="nothing was written"):
        await sync.sync_company(pool, COMPANY, http=server.http())
    assert server.requests, "the reads ran before the change was seen"
    assert [s for s, _a in pool.log if s.startswith(("INSERT", "DELETE", "UPDATE"))] == []


async def test_the_credentials_are_read_again_just_before_the_write(switched_on: None) -> None:
    """The check sits after the reads and before the first mirror statement,
    not at the start, where it would prove nothing about a disconnect during
    the reads."""
    _result, pool, _server = await _run()
    reads = [i for i, (s, _a) in enumerate(pool.log) if "company_propertyware_credentials" in s]
    writes = [i for i, (s, _a) in enumerate(pool.log) if s.startswith(("INSERT", "DELETE"))]
    assert len(reads) == 2 and writes
    assert reads[1] < min(writes)


# ── what the sync status says (stale reasons) ─────────────────────────────


def _error(status: int, failure: Failure) -> PmsApiError:
    return PmsApiError(status, failure, False, "synthetic vendor text")


#: Every kind of read failure, and what each reason must ask the person to do.
_ACTIONS = [
    (_error(401, Failure.CREDENTIALS), "save them again"),
    (_error(403, Failure.PERMISSION), "Create a key in Propertyware"),
    (_error(404, Failure.NOT_FOUND), "team@occupella.com"),
    (_error(400, Failure.BAD_REQUEST), "team@occupella.com"),
    (_error(429, Failure.UNDOCUMENTED), "Sync again"),
    (_error(502, Failure.UNDOCUMENTED), "Sync again"),
    (_error(0, Failure.UNDOCUMENTED), "Sync again"),
    (PmsUnavailableError("Propertyware", 120.0), "Sync again"),
]


@pytest.mark.parametrize(("exc", "action"), _ACTIONS)
def test_every_read_failure_names_an_action_and_no_vendor_text(
    exc: PmsApiError, action: str
) -> None:
    """Rule 16. The reason is shown to the customer, so the vendor's own text
    and the status code stay in the log."""
    reason = sync.read_failure("/vendors", exc)
    assert action in reason
    assert "synthetic vendor text" not in reason and str(exc.status) not in reason


def test_a_refused_read_names_the_permission_and_never_says_reconnect() -> None:
    """Gotcha 102: a 403 means Propertyware accepted the key and refused this
    read, so the fix is the key's permission. The organization ID is named too,
    because the status for a wrong one is not documented (PW-2)."""
    for path in client.READS:
        reason = sync.read_failure(path, _error(403, Failure.PERMISSION))
        assert _paths.PERMISSIONS[f"GET {path}"] in reason
        assert "organization ID" in reason and "reconnect" not in reason.lower()
    assert "save them again" in sync.read_failure("/units", _error(401, Failure.CREDENTIALS))


def test_a_reason_never_names_its_list_so_one_cause_is_said_once() -> None:
    """The status names the tables, then the reason; tables that failed for
    one cause share one sentence only if the reason leaves the list out."""
    reasons = {sync.read_failure(path, _error(401, Failure.CREDENTIALS)) for path in client.READS}
    assert len(reasons) == 1


#: Every reason the pass sets itself, which must reach the status unchanged.
_OWN_REASONS = [
    *(sync.read_failure("/vendors", exc) for exc, _action in _ACTIONS),
    sync._READ_CRASHED,
    sync._MAPPING_FAILED,
]


@pytest.mark.parametrize("reason", _OWN_REASONS)
def test_the_passs_own_reasons_reach_the_status_unchanged(reason: str) -> None:
    assert sync.stale_reason(reason) == reason


async def test_the_balance_reason_reaches_the_status_unchanged(switched_on: None) -> None:
    leases = fixture_rows("/leases")
    leases[0].pop("arBalance", None)
    result, _pool, _server = await _run(FakePropertyware({**all_rows(), "/leases": leases}))
    reason = result.failed["helixis_lease_balances"]
    assert reason.startswith("Propertyware sent 1 of") and "Sync again" in reason
    assert sync.stale_reason(reason) == reason


def _writer_reasons() -> list[str]:
    """Every stale reason ``pms.mirror`` can set, as it would read, from the
    source: each ``result.failed[...] = <text>``, both arms of a conditional,
    and an f-string with a number in each placeholder. A reason that is a
    variable (the connector's own, passed through) is not the writer's."""

    def texts(node: ast.expr) -> list[str]:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return [node.value]
        if isinstance(node, ast.JoinedStr):
            return ["".join(v.value if isinstance(v, ast.Constant) else "3" for v in node.values)]
        if isinstance(node, ast.IfExp):
            return texts(node.body) + texts(node.orelse)
        return []

    tree = ast.parse(inspect.getsource(mirror))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Attribute)
                    and target.value.attr == "failed"
                ):
                    found += texts(node.value)
    return found


def test_a_reason_is_ours_by_its_type_never_by_its_words() -> None:
    """The writer passes this pass's reason through as the same object, so
    the type decides. A plain string that reads like one of ours is the
    writer's, and is worded, never shown as it is."""
    ours = sync.read_failure("/vendors", _error(403, Failure.PERMISSION))
    assert isinstance(ours, sync.CustomerReason)
    assert sync.stale_reason(str(ours)) != str(ours)


def test_a_reason_the_writer_adds_later_is_worded_without_a_change_here() -> None:
    """Rule 16 for a reason nobody here has seen: it gets the general
    sentence, which names an action, never the operator's words."""
    later = "7 widget(s) arrived sideways: not stamped"
    worded = sync.stale_reason(later)
    assert worded != later and "widget" not in worded
    assert "Sync again" in worded and "team@occupella.com" in worded


async def test_a_failed_lists_reason_survives_the_writer_as_ours(switched_on: None) -> None:
    """Both sides of the seam (rule 15): through the real pass and the shared
    writer, a failed list's reason reaches the result as this pass's own, so
    the status shows it unchanged."""
    result, _pool, _server = await _run(FakePropertyware(fail={"/vendors": 403}))
    reason = result.failed["helixis_vendors"]
    assert isinstance(reason, sync.CustomerReason)
    assert sync.stale_reason(reason) == reason and "VENDORS - Read" in reason


def test_every_reason_the_shared_writer_sets_is_worded_for_the_customer() -> None:
    """Both sides of the seam (rule 15): the writer's reasons are written for
    an operator ("incomplete read: written, not stamped, not pruned"), and the
    sync status shows reasons to the customer. A reason added to the writer
    and not worded here would reach the page as it is, and this fails first."""
    reasons = _writer_reasons()
    # A scan that finds nothing proves nothing (rule 10): these are known.
    assert "incomplete read: written, not stamped, not pruned" in reasons
    assert any(r.startswith("write failed") for r in reasons)
    assert len(reasons) >= 6
    for reason in reasons:
        worded = sync.stale_reason(reason)
        assert worded != reason, f"the writer's reason {reason!r} is not worded"
        assert "Sync again" in worded or "team@occupella.com" in worded


def test_every_table_the_pass_fills_has_words_for_the_status() -> None:
    assert set(sync.TABLE_WORDS) == set(sync.SYNC_STATE_STAGE)
