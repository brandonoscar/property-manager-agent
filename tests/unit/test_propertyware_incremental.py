"""Slice 4: which pass runs, the change windows, the watermarks, the budget.

``sync_company`` runs for real over the fake Propertyware (which applies the
change window and the sort the way the spec documents them) and the recording
pool (which keeps the ``pms.watermarks`` rows). What is asserted is what would
have been sent to Propertyware and to Postgres, pass after pass.

The rules (design, "Sync"; brief slice 4): no markers, a week-old full pass, a
changed connection or incremental reads switched off read everything whole; a
day-old reconcile reads everything whole too (Propertyware cannot read ids
alone); otherwise each list is read from its watermark, deduped, and written as
a delta that prunes nothing. Only a clean list moves its watermark, only a
clean pass moves its markers, and a pass stops at its call budget.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from helixis.pms import registry, sync_cycle
from helixis.pms.errors import Failure, PmsApiError
from helixis.pms.mirror import KEYED_COLUMNS, SyncResult
from helixis.pms.paging import OffsetPaging
from helixis.pms.watermarks import LAG
from helixis.propertyware import client, credentials, sync
from tests.unit._propertyware_fake import (
    API_PREFIX,
    COMPANY,
    FIRST_PASS,
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

REPO = Path(__file__).resolve().parents[2]
KEY = registry.PROPERTYWARE

#: Every list with a watermark: all but the charges.
WATERMARKED = [p for p in client.READS if p not in sync.WHOLE_PASS_ONLY]

#: The tables keyed on their own id, which only a whole read may prune.
KEYED = [t for t in sync.SYNC_STATE_STAGE if t in KEYED_COLUMNS]


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    pinned = Clock()
    monkeypatch.setattr(sync, "_now", pinned)
    return pinned


@pytest.fixture
def switched_on(monkeypatch: pytest.MonkeyPatch, clock: Clock) -> None:
    current = settings(enabled=True)
    monkeypatch.setattr("helixis.settings.get_settings", lambda: current)
    monkeypatch.setattr(sync, "today_in", lambda _zone: TODAY)


async def _run(server: FakePropertyware, pool: FakePool) -> SyncResult:
    return await sync.sync_company(pool, COMPANY, http=server.http())


def _iso(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _mark(pool: FakePool, name: str) -> datetime | None:
    return pool.mark(sync.resource(name))


def _changed(rows: dict[str, list[dict[str, Any]]], path: str, row_id: int, when: str) -> dict:
    """Stamp one row as changed at ``when``, as Propertyware does; return it."""
    row = next(r for r in rows[path] if r["id"] == row_id)
    row["lastModifiedDateTime"] = when
    return row


async def _after_a_full_pass(clock: Clock) -> FakePool:
    """A pool that has had one clean FULL pass at :data:`FIRST_PASS`, its log
    cleared, and the clock moved four hours on (the cycle's interval)."""
    pool = FakePool(creds=creds_row())
    first = await _run(FakePropertyware(), pool)
    assert first.failed == {}
    pool.log.clear()
    clock.advance(hours=4)
    return pool


def _windowed(server: FakePropertyware) -> list[bool]:
    return ["lastModifiedDateTimeStart" in r.url.params for r in server.requests]


# ── which pass runs ────────────────────────────────────────────────────────

NOW = FIRST_PASS
RECENT = NOW - timedelta(hours=1)
CONNECTED = NOW - timedelta(days=30)


def _choose(**changes: Any) -> sync.PassKind:
    given: dict[str, Any] = {
        "now": NOW,
        "full": RECENT,
        "reconcile": RECENT,
        "connection_changed": CONNECTED,
        "incremental": True,
    }
    given.update(changes)
    kind, why = sync.choose_pass(**given)
    assert why, "every choice says why, for the log"
    return kind


@pytest.mark.parametrize(
    ("changes", "kind"),
    [
        pytest.param({}, sync.PassKind.INCREMENTAL, id="recent-markers"),
        pytest.param({"full": None, "reconcile": None}, sync.PassKind.FULL, id="no-markers"),
        pytest.param({"full": None}, sync.PassKind.FULL, id="no-full-marker"),
        pytest.param({"full": NOW - timedelta(days=7)}, sync.PassKind.FULL, id="full-7-days-old"),
        pytest.param(
            {"full": NOW - timedelta(days=7) + timedelta(seconds=1)},
            sync.PassKind.INCREMENTAL,
            id="full-a-second-under-7-days",
        ),
        pytest.param(
            {"reconcile": NOW - timedelta(hours=24)},
            sync.PassKind.RECONCILE,
            id="reconcile-24-hours-old",
        ),
        pytest.param(
            {"reconcile": NOW - timedelta(hours=24) + timedelta(seconds=1)},
            sync.PassKind.INCREMENTAL,
            id="reconcile-a-second-under-24-hours",
        ),
        pytest.param({"reconcile": None}, sync.PassKind.RECONCILE, id="no-reconcile-marker"),
        pytest.param({"incremental": False}, sync.PassKind.FULL, id="incremental-off"),
        pytest.param(
            {"incremental": False, "full": None},
            sync.PassKind.FULL,
            id="incremental-off-no-markers",
        ),
        pytest.param(
            {"connection_changed": RECENT + timedelta(minutes=5)},
            sync.PassKind.FULL,
            id="connection-changed-after-the-full-pass",
        ),
        pytest.param(
            {"connection_changed": RECENT}, sync.PassKind.FULL, id="connection-changed-at-it"
        ),
        pytest.param(
            {"connection_changed": None}, sync.PassKind.FULL, id="connection-change-unknown"
        ),
        pytest.param(
            {"full": NOW - timedelta(days=8), "reconcile": NOW - timedelta(days=2)},
            sync.PassKind.FULL,
            id="full-due-beats-reconcile-due",
        ),
        pytest.param(
            {"full_asked": RECENT + timedelta(minutes=5)},
            sync.PassKind.FULL,
            id="full-asked-for-after-the-full-pass",
        ),
        pytest.param({"full_asked": RECENT}, sync.PassKind.FULL, id="full-asked-for-at-it"),
        pytest.param(
            {"full_asked": RECENT - timedelta(minutes=5)},
            sync.PassKind.INCREMENTAL,
            id="full-asked-for-before-the-full-pass",
        ),
    ],
)
def test_which_pass_runs(changes: dict[str, Any], kind: sync.PassKind) -> None:
    assert _choose(**changes) is kind


def test_only_an_incremental_pass_reads_by_window() -> None:
    assert sync.PassKind.FULL.whole and sync.PassKind.RECONCILE.whole
    assert not sync.PassKind.INCREMENTAL.whole


async def test_the_plan_reads_oldest_watermark_first_from_lag_behind_it() -> None:
    pool = FakePool(creds=creds_row())
    for name in (sync.FULL_MARKER, sync.RECONCILE_MARKER):
        pool.watermarks[(COMPANY, sync.resource(name))] = RECENT
    marks = {path: RECENT - timedelta(minutes=i) for i, path in enumerate(WATERMARKED)}
    del marks["/vendors"]  # never read clean
    marks["/bills"] = RECENT - timedelta(days=2)  # held back two days
    for path, when in marks.items():
        pool.watermarks[(COMPANY, sync.resource(path))] = when
    plan = await sync.plan_pass(pool, COMPANY, start=NOW, connection_changed=CONNECTED)
    assert plan.kind is sync.PassKind.INCREMENTAL and plan.start == NOW
    order = [path for path, _start in plan.reads]
    assert "/leases/charges" not in order, "the charges follow the balances"
    assert order[:2] == ["/vendors", "/bills"], "never read clean first, then the oldest"
    assert order[2:] == sorted(order[2:], key=lambda p: marks[p])
    starts = dict(plan.reads)
    assert starts["/vendors"] is None, "a list with no watermark is read whole"
    for path, when in marks.items():
        assert starts[path] == when - LAG == when - timedelta(minutes=10)
    # Rule 11: every watermark read names the company.
    reads = pool.statements("SELECT watermark FROM helixis_pms_sync_watermarks")
    assert reads and all(args[0] == COMPANY for _sql, args in reads)


async def test_a_watermark_that_cannot_be_read_reads_more_never_less() -> None:
    pool = FakePool(creds=creds_row(), fail_on="helixis_pms_sync_watermarks")
    plan = await sync.plan_pass(pool, COMPANY, start=NOW, connection_changed=CONNECTED)
    assert plan.kind is sync.PassKind.FULL
    assert [p for p, _s in plan.reads] == list(client.READS)
    assert all(start is None for _p, start in plan.reads)


# ── the first pass: FULL, everything moves ─────────────────────────────────


async def test_a_first_pass_reads_whole_and_moves_every_watermark_and_both_markers(
    switched_on: None,
) -> None:
    server, pool = FakePropertyware(), FakePool(creds=creds_row())
    result = await _run(server, pool)
    assert result.failed == {}
    assert server.paths() == list(client.READS)
    assert not any(_windowed(server)), "a whole read pins no window (As built, slice 1)"
    assert inserted_rows(pool, "helixis_lease_balances"), "a full pass refreshes the balances"
    for path in WATERMARKED:
        assert _mark(pool, path) == FIRST_PASS, path
    assert _mark(pool, "/leases/charges") is None, "the charges carry no watermark"
    assert _mark(pool, sync.FULL_MARKER) == FIRST_PASS
    assert _mark(pool, sync.RECONCILE_MARKER) == FIRST_PASS
    advances = pool.statements("INSERT INTO helixis_pms_sync_watermarks")
    assert advances and all(args[0] == COMPANY for _sql, args in advances), "rule 11"


# ── the incremental pass ───────────────────────────────────────────────────


async def test_the_next_pass_reads_each_list_from_its_window_and_writes_a_delta(
    switched_on: None, clock: Clock
) -> None:
    pool = await _after_a_full_pass(clock)
    rows = all_rows()
    _changed(rows, "/buildings", 990100001, "2026-10-05T13:00:00Z")["name"] = "Renamed"
    _changed(rows, "/leases", 990600001, "2026-10-05T13:30:00Z")
    _changed(rows, "/workorders", 990700001, "2026-10-05T14:00:00+00:00")
    server = FakePropertyware(rows)
    result = await _run(server, pool)

    assert result.failed == {}
    assert server.paths() == WATERMARKED, "every list but the charges, in the cycle's order"
    for request in server.requests:
        path = request.url.path.removeprefix(API_PREFIX)
        params = request.url.params
        assert (
            params["lastModifiedDateTimeStart"]
            == _iso(FIRST_PASS - LAG)
            == ("2026-10-05T11:50:00Z")
        )
        assert params["lastModifiedDateTimeEnd"] == _iso(clock.now) == "2026-10-05T16:00:00Z"
        assert params["limit"] == "500"
        expected = "id asc" if path == "/accounting/glaccounts" else "lastmodifieddatetime asc"
        assert params["orderby"] == expected, path
    by_path = {r.url.path.removeprefix(API_PREFIX): r.url.params for r in server.requests}
    assert by_path["/contacts"]["type"] == "TENANT"
    assert by_path["/leases"]["includeOtherBalances"] == "true"
    assert by_path["/accounting/glaccounts"]["includeDeactivated"] == "true"

    # Only the changed rows are written, as deltas...
    assert [r["name"] for r in inserted_rows(pool, "helixis_properties")] == ["Renamed"]
    assert [r["buildium_id"] for r in inserted_rows(pool, "helixis_leases")] == [990600001]
    assert [r["buildium_id"] for r in inserted_rows(pool, "helixis_work_orders")] == [990700001]
    assert result.rows_by_table["helixis_units"] == 0
    # ...and nothing is pruned: a row that did not change is not in a delta.
    for table in KEYED:
        assert pool.deletes(table) == [], table
    assert result.pruned.get("helixis_properties", 0) == 0
    # Every stage but the balances is stamped: a complete delta is fresh.
    assert pool.stamped() == set(sync.SYNC_STATE_STAGE.values()) - {"balances"}
    assert result.confirmed_complete == set(sync.SYNC_STATE_STAGE) - {sync.BALANCES}
    # Each clean list moves to this pass's start; the markers stay.
    for path in WATERMARKED:
        assert _mark(pool, path) == clock.now, path
    assert _mark(pool, sync.FULL_MARKER) == FIRST_PASS
    assert _mark(pool, sync.RECONCILE_MARKER) == FIRST_PASS


async def test_an_incremental_pass_leaves_the_balances_as_they_were(
    switched_on: None, clock: Clock
) -> None:
    pool = await _after_a_full_pass(clock)
    rows = all_rows()
    _changed(rows, "/leases", 990600001, "2026-10-05T13:00:00Z")["arBalance"] = 9999.0
    server = FakePropertyware(rows)
    result = await _run(server, pool)
    assert "/leases/charges" not in server.paths()
    assert pool.inserts("helixis_lease_balances") == []
    assert pool.deletes("helixis_lease_balances") == []
    assert "balances" not in pool.stamped()
    assert sync.BALANCES not in result.rows_by_table and sync.BALANCES not in result.failed


def test_a_window_with_the_balances_off_holds_no_snapshot() -> None:
    reads = {
        path: sync.ResourceRead(rows=fixture_rows(path), complete=True, delta=True)
        for path in WATERMARKED
    }
    rows, _report = sync.mirror_rows(reads, today=TODAY, now=1.0, balances=False)
    assert sync.BALANCES not in rows
    assert set(rows) == set(sync.SYNC_STATE_STAGE) - {sync.BALANCES}
    assert all(read.delta for read in rows.values())
    assert all(read.keep_ids is None for read in rows.values()), "no id list: nothing pruned"


async def test_a_window_is_deduped_on_id_keeping_the_newest_change(
    switched_on: None, clock: Clock
) -> None:
    pool = await _after_a_full_pass(clock)
    rows = all_rows()
    old = _changed(rows, "/buildings", 990100001, "2026-10-05T13:00:00Z")
    old["name"] = "Older"
    rows["/buildings"].append(
        {**old, "name": "Newer", "lastModifiedDateTime": "2026-10-05T14:00:00Z"}
    )
    rows["/buildings"].append(dict(old))  # the overlap, read twice
    result = await _run(FakePropertyware(rows), pool)
    assert [r["name"] for r in inserted_rows(pool, "helixis_properties")] == ["Newer"]
    assert result.rows_by_table["helixis_properties"] == 1


def test_dedupe_compares_change_times_as_times_not_text() -> None:
    newer = {"id": 1, "lastModifiedDateTime": "2026-10-05T14:00:00Z", "v": "newer"}
    older = {"id": 1, "lastModifiedDateTime": "2026-10-05T13:00:00+00:00", "v": "older"}
    # Listed newer first: an older version after it never overwrites it.
    assert sync.newest_per_id([newer, older]) == [newer]
    # As text "+" sorts before "Z", so the same instant written two ways
    # would read as two times; as times they are one, and the first stays.
    same = {"id": 2, "lastModifiedDateTime": "2026-10-05T14:00:00+00:00", "v": "same"}
    zulu = {"id": 2, "lastModifiedDateTime": "2026-10-05T14:00:00Z", "v": "zulu"}
    assert sync.newest_per_id([same, zulu]) == [same]
    unreadable = {"id": 3, "lastModifiedDateTime": "yesterday", "v": "unreadable"}
    readable = {"id": 3, "lastModifiedDateTime": "2026-10-05T14:00:00Z", "v": "readable"}
    assert sync.newest_per_id([unreadable, readable]) == [readable]
    no_id = {"lastModifiedDateTime": "2026-10-05T14:00:00Z"}
    assert sync.newest_per_id([no_id]) == [no_id], "left for the writer to refuse"


async def test_only_a_window_is_deduped(switched_on: None) -> None:
    """On a whole read an id that came back twice is the writer's sign that a
    page boundary moved: it must see both, and then prunes nothing."""
    rows = all_rows()
    rows["/buildings"].append(dict(rows["/buildings"][0]))
    pool = FakePool(creds=creds_row())
    await _run(FakePropertyware(rows), pool)
    assert len(pool.inserts("helixis_properties")) == len(rows["/buildings"])
    assert pool.deletes("helixis_properties") == []


async def test_a_window_checks_new_leases_against_the_mirrors_units(
    switched_on: None, clock: Clock
) -> None:
    """A new lease on a unit that did not change: its unit is not in the
    window, but it is in the mirror. One naming a unit in neither keeps
    whatever is stored: never "no unit" from a partial view."""
    pool = await _after_a_full_pass(clock)
    pool.mirror_units = [
        {"buildium_id": u["id"], "property_id": u["buildingID"]} for u in fixture_rows("/units")
    ]
    rows = all_rows()
    template = next(r for r in rows["/leases"] if r["id"] == 990600001)
    rows["/leases"] += [
        {**template, "id": 990600099, "unitID": 990200002, "contacts": []},
        {**template, "id": 990600098, "unitID": 990299999, "contacts": []},
    ]
    for lease in rows["/leases"][-2:]:
        lease["lastModifiedDateTime"] = "2026-10-05T13:00:00Z"
    await _run(FakePropertyware(rows), pool)
    leases = {r["buildium_id"]: r for r in inserted_rows(pool, "helixis_leases")}
    assert leases[990600099]["unit_id"] == 990200002
    assert "unit_id" not in leases[990600098]
    ((sql, args),) = pool.statements("SELECT buildium_id, property_id FROM helixis_units")
    assert "WHERE company_id = $1::uuid AND pms = $2" in sql, (
        "rule 11: this company's, this system's"
    )
    assert args == (COMPANY, "propertyware")


async def test_a_full_pass_never_reads_the_mirrors_units(switched_on: None) -> None:
    pool = FakePool(creds=creds_row())
    await _run(FakePropertyware(), pool)
    assert pool.statements("SELECT buildium_id, property_id FROM helixis_units") == []


async def test_a_window_leaves_each_owners_properties_as_they_were(
    switched_on: None, clock: Clock
) -> None:
    """Which buildings a portfolio holds needs every building; a window of
    the buildings names only the ones that changed."""
    pool = await _after_a_full_pass(clock)
    rows = all_rows()
    _changed(rows, "/portfolios", 990300001, "2026-10-05T13:00:00Z")
    _changed(rows, "/buildings", 990100001, "2026-10-05T13:00:00Z")
    await _run(FakePropertyware(rows), pool)
    owners = inserted_rows(pool, "helixis_owners")
    assert owners and all("property_ids" not in row for row in owners)


# ── links in a window ──────────────────────────────────────────────────────


async def test_in_a_window_a_lease_that_lost_its_tenant_loses_the_link_and_another_keeps_its_own(
    switched_on: None, clock: Clock
) -> None:
    pool = FakePool(creds=creds_row())
    await _run(FakePropertyware(), pool)
    stored = {(args[1], args[2]) for _sql, args in pool.inserts("helixis_lease_tenants")}
    assert stored == {(990600001, 990400001), (990600001, 990400002), (990600002, 990400003)}
    pool.log.clear()
    clock.advance(hours=4)

    rows = all_rows()
    _changed(rows, "/leases", 990600002, "2026-10-05T13:00:00Z")["contacts"] = []
    _changed(rows, "/leases", 990600001, "2026-10-05T13:05:00Z")  # tenants unchanged
    result = await _run(FakePropertyware(rows), pool)

    deletes = pool.deletes("helixis_lease_tenants")
    by_lease = {args[2]: set(args[3]) for _sql, args in deletes}
    assert by_lease == {990600002: set(), 990600001: {990400001, 990400002}}
    for sql, args in deletes:
        assert "lease_buildium_id = $3" in sql and args[:2] == (COMPANY, "propertyware")
    removed = {pair for pair in stored if pair[0] in by_lease and pair[1] not in by_lease[pair[0]]}
    assert removed == {(990600002, 990400003)}
    kept = {(args[1], args[2]) for _sql, args in pool.inserts("helixis_lease_tenants")}
    assert kept == {(990600001, 990400001), (990600001, 990400002)}
    assert "helixis_lease_tenants" in result.confirmed_complete


def test_a_windows_links_name_every_lease_it_read_as_their_parents() -> None:
    reads = {
        path: sync.ResourceRead(rows=fixture_rows(path), complete=True, delta=True)
        for path in WATERMARKED
    }
    rows, _report = sync.mirror_rows(reads, today=TODAY, now=1.0, balances=False)
    links = rows["helixis_lease_tenants"]
    assert links.delta
    assert links.parents == {r["id"] for r in fixture_rows("/leases")}, "inactive ones too"


# ── a list that did not come through keeps its watermark ──────────────────


async def test_a_failed_or_partial_list_keeps_its_watermark_and_the_next_pass_reads_from_it(
    switched_on: None, clock: Clock
) -> None:
    pool = await _after_a_full_pass(clock)
    second = clock.now
    broken = FakePropertyware(
        fail={"/vendors": 403}, totals={"/units": len(fixture_rows("/units")) + 3}
    )
    result = await _run(broken, pool)
    assert {"helixis_vendors", "helixis_units"} <= set(result.failed)
    assert _mark(pool, "/vendors") == FIRST_PASS
    assert _mark(pool, "/units") == FIRST_PASS
    for path in set(WATERMARKED) - {"/vendors", "/units"}:
        assert _mark(pool, path) == second, path

    clock.advance(hours=4)
    server = FakePropertyware()
    await _run(server, pool)
    assert server.paths()[:2] == ["/units", "/vendors"], "held lists lead the next pass"
    starts = {p: q["lastModifiedDateTimeStart"] for p in WATERMARKED for q in server.query_of(p)}
    assert starts["/units"] == starts["/vendors"] == _iso(FIRST_PASS - LAG)
    assert starts["/leases"] == _iso(second - LAG)
    assert _mark(pool, "/vendors") == clock.now, "read clean now, so it moves"


def _landed(**changes: Any) -> SyncResult:
    """A write in which every table this pass fills landed, one row each."""
    every = dict.fromkeys(sync.SYNC_STATE_STAGE, 1)
    result = SyncResult(company_id=COMPANY, pms="propertyware", rows_by_table=every)
    for name, value in changes.items():
        setattr(result, name, value)
    return result


def test_a_list_is_clean_only_when_its_read_was_complete_and_its_tables_landed() -> None:
    """Both halves of ``pms.watermarks``' rule, each on its own. The writer
    marks an incomplete read's tables stale today; the read's own word is
    checked too, so a writer that stopped saying so could not move a mark.

    Re-pointed from ``ended_clean`` to ``markers_may_move`` when a permission
    refusal stopped holding the markers: none of the failures here is one,
    so each still holds them."""
    reads = {p: sync.ResourceRead(complete=True) for p in client.READS}
    assert sync.clean_lists(reads, _landed()) == WATERMARKED, "the charges carry none"
    assert sync.markers_may_move(reads, _landed())

    partial = {**reads, "/units": sync.ResourceRead(complete=False)}
    assert "/units" not in sync.clean_lists(partial, _landed())
    assert not sync.markers_may_move(partial, _landed())
    failed = {**reads, "/vendors": sync.ResourceRead(error="synthetic")}
    assert "/vendors" not in sync.clean_lists(failed, _landed())
    assert not sync.markers_may_move(failed, _landed())

    stale_lines = _landed(failed={"helixis_bill_lines": "synthetic"})
    assert "/bills" not in sync.clean_lists(reads, stale_lines), "every table it feeds"
    assert not sync.markers_may_move(reads, stale_lines)
    unwritten = _landed()
    del unwritten.rows_by_table["helixis_lease_tenants"]
    assert "/leases" not in sync.clean_lists(reads, unwritten)
    stale_snapshot = _landed(failed={sync.BALANCES: "synthetic"})
    assert "/leases" in sync.clean_lists(reads, stale_snapshot), "never waits on the snapshot"
    assert not sync.markers_may_move(reads, stale_snapshot), "but the markers hold"


# ── a refusal reading again cannot fix does not hold the markers ─────────


def _refused(path: str) -> sync.ResourceRead:
    exc = PmsApiError(403, Failure.PERMISSION, False, "synthetic")
    return sync.ResourceRead(error=sync.read_failure(path, exc), refused=True)


def test_only_permission_refusals_and_their_tables_let_the_markers_move() -> None:
    reads = {p: sync.ResourceRead(complete=True) for p in client.READS}
    refused = {**reads, "/vendors": _refused("/vendors"), "/bills": _refused("/bills")}
    their_tables = _landed(
        failed={t: "refused" for t in ("helixis_vendors", "helixis_bills", "helixis_bill_lines")}
    )
    assert sync.refused_lists(refused) == ["/vendors", "/bills"]
    assert sync.markers_may_move(refused, their_tables)
    # The leases refused: the snapshot they feed is stale for the same reason.
    no_leases = {**reads, "/leases": _refused("/leases")}
    leases_tables = _landed(
        failed={t: "refused" for t in ("helixis_leases", "helixis_lease_tenants", sync.BALANCES)}
    )
    assert sync.markers_may_move(no_leases, leases_tables)
    # Any other failure beside a refusal holds them.
    other_read = {**refused, "/units": sync.ResourceRead(error="synthetic 503")}
    assert not sync.markers_may_move(other_read, their_tables)
    unreached = {**refused, "/units": sync.ResourceRead(error=sync.CALL_BUDGET_REACHED)}
    assert not sync.markers_may_move(unreached, their_tables)
    partial = {**refused, "/units": sync.ResourceRead(complete=False)}
    assert not sync.markers_may_move(partial, their_tables)
    other_table = _landed(failed={**their_tables.failed, "helixis_units": "write failed"})
    assert not sync.markers_may_move(refused, other_table), "a stale table no refusal feeds"
    snapshot = _landed(failed={sync.BALANCES: "a lease without a balance"})
    assert not sync.markers_may_move(reads, snapshot)


async def test_a_pass_refused_only_for_permissions_moves_its_markers(
    switched_on: None, clock: Clock
) -> None:
    """A key without the VENDORS and BILLS Read permissions: every pass is
    refused there, and must not read everything whole forever because of it."""
    pool = FakePool(creds=creds_row())
    refusing = {"/vendors": 403, "/bills": 403}
    result = await _run(FakePropertyware(fail=refusing), pool)
    stale = {"helixis_vendors", "helixis_bills", "helixis_bill_lines"}
    assert set(result.failed) == stale
    assert "VENDORS - Read" in sync.stale_reason(result.failed["helixis_vendors"])
    assert _mark(pool, sync.FULL_MARKER) == FIRST_PASS
    assert _mark(pool, sync.RECONCILE_MARKER) == FIRST_PASS
    assert _mark(pool, "/vendors") is None and _mark(pool, "/bills") is None
    assert _mark(pool, "/leases") == FIRST_PASS

    clock.advance(hours=4)
    server = FakePropertyware(fail=refusing)
    again = await _run(server, pool)
    by_path = {p: q for p in client.READS for q in server.query_of(p)}
    assert "lastModifiedDateTimeStart" in by_path["/leases"], "the next pass reads by window"
    assert "lastModifiedDateTimeStart" not in by_path["/vendors"], "never read: read whole"
    assert set(again.failed) == stale, "still stale, with their reason"
    assert "/leases/charges" not in server.paths()


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param({"fail": {"/vendors": 500}}, id="5xx"),
        pytest.param({"fail": {"/vendors": 401}}, id="credentials"),
        pytest.param({"fail": {"/vendors": 404}}, id="not-found"),
        pytest.param({"totals": {"/vendors": 9}}, id="incomplete"),
        pytest.param({"budget": 4}, id="budget"),
    ],
)
@pytest.mark.parametrize("with_a_refusal", [False, True], ids=["alone", "beside-a-403"])
async def test_any_other_failure_holds_the_markers(
    switched_on: None,
    monkeypatch: pytest.MonkeyPatch,
    failure: dict[str, Any],
    with_a_refusal: bool,
) -> None:
    monkeypatch.setattr("helixis.pms.http._sleep", AsyncMock())  # the 5xx's retries
    if "budget" in failure:
        monkeypatch.setattr(sync, "CALL_BUDGET", failure["budget"])
    fail = dict(failure.get("fail", {}))
    if with_a_refusal:
        fail["/buildings"] = 403
    pool = FakePool(creds=creds_row())
    result = await _run(FakePropertyware(fail=fail, totals=failure.get("totals")), pool)
    assert "helixis_vendors" in result.failed
    assert _mark(pool, sync.FULL_MARKER) is None
    assert _mark(pool, sync.RECONCILE_MARKER) is None


async def test_a_table_that_could_not_be_saved_holds_its_lists_watermark(
    switched_on: None, clock: Clock
) -> None:
    """Clean is the read complete AND every table it feeds written: a write
    that failed must be read again."""
    pool = await _after_a_full_pass(clock)
    pool.fail_on = "INSERT INTO helixis_work_orders"
    rows = all_rows()
    _changed(rows, "/workorders", 990700001, "2026-10-05T13:00:00Z")
    result = await _run(FakePropertyware(rows), pool)
    assert "helixis_work_orders" in result.failed
    assert _mark(pool, "/workorders") == FIRST_PASS
    assert _mark(pool, "/leases") == clock.now


async def test_a_lists_watermark_never_waits_on_the_balance_snapshot(switched_on: None) -> None:
    """The snapshot is replaced whole on its own schedule: a lease without a
    readable balance keeps the old snapshot, but the leases came through."""
    rows = all_rows()
    del rows["/leases"][0]["arBalance"]
    pool = FakePool(creds=creds_row())
    result = await _run(FakePropertyware(rows), pool)
    assert sync.BALANCES in result.failed
    assert _mark(pool, "/leases") == FIRST_PASS
    assert _mark(pool, sync.FULL_MARKER) is None, "the pass did not end clean"


# ── the reconcile: a full read for Propertyware ───────────────────────────


async def test_a_reconcile_prunes_exactly_the_ids_a_complete_read_no_longer_lists(
    switched_on: None, clock: Clock
) -> None:
    pool = FakePool(creds=creds_row())
    await _run(FakePropertyware(), pool)
    pool.log.clear()
    clock.advance(hours=24)
    rows = all_rows()
    rows["/buildings"] = [r for r in rows["/buildings"] if r["id"] != 990100004]
    server = FakePropertyware(rows)
    result = await _run(server, pool)

    assert not any(_windowed(server)), "Propertyware cannot read ids alone: a full read"
    assert "/leases/charges" in server.paths()
    prunes = [(s, a) for s, a in pool.deletes("helixis_properties") if "buildium_id = ANY" in s]
    ((_sql, args),) = prunes
    assert sorted(args[2]) == [990100001, 990100002, 990100003]
    assert inserted_rows(pool, "helixis_lease_balances"), "a reconcile refreshes the balances"
    assert result.failed == {}
    assert _mark(pool, sync.RECONCILE_MARKER) == clock.now
    assert _mark(pool, sync.FULL_MARKER) == FIRST_PASS, "a reconcile moves only its own"


async def test_a_reconcile_whose_read_is_partial_prunes_nothing_and_comes_again(
    switched_on: None, clock: Clock
) -> None:
    pool = FakePool(creds=creds_row())
    await _run(FakePropertyware(), pool)
    pool.log.clear()
    clock.advance(hours=24)
    rows = all_rows()
    rows["/buildings"] = [r for r in rows["/buildings"] if r["id"] != 990100004]
    partial = FakePropertyware(rows, totals={"/buildings": len(rows["/buildings"]) + 1})
    result = await _run(partial, pool)
    assert "helixis_properties" in result.failed
    assert [s for s, _a in pool.deletes("helixis_properties") if "buildium_id = ANY" in s] == []
    assert _mark(pool, sync.RECONCILE_MARKER) == FIRST_PASS

    clock.advance(hours=4)
    again = FakePropertyware(rows)
    await _run(again, pool)
    assert not any(_windowed(again)), "still due: the reconcile comes again"
    assert _mark(pool, sync.RECONCILE_MARKER) == clock.now


# ── FULL comes back ────────────────────────────────────────────────────────


async def test_a_week_after_the_last_full_pass_everything_is_read_whole_again(
    switched_on: None, clock: Clock
) -> None:
    pool = FakePool(creds=creds_row())
    await _run(FakePropertyware(), pool)
    clock.advance(days=7)
    server = FakePropertyware()
    await _run(server, pool)
    assert not any(_windowed(server))
    assert _mark(pool, sync.FULL_MARKER) == clock.now


async def test_a_connection_that_changed_after_the_last_full_pass_reads_everything_again(
    switched_on: None, clock: Clock
) -> None:
    """Keys saved for another account, or a disconnect that raced a pass's
    markers: the old windows say nothing about what this connection holds."""
    pool = await _after_a_full_pass(clock)
    assert pool.creds is not None
    pool.creds["updated_at"] = FIRST_PASS + timedelta(hours=1)
    server = FakePropertyware()
    await _run(server, pool)
    assert not any(_windowed(server)) and "/leases/charges" in server.paths()
    assert _mark(pool, sync.FULL_MARKER) == clock.now


async def test_a_full_read_asked_for_makes_the_next_pass_full_and_only_that_one(
    switched_on: None, clock: Clock
) -> None:
    """Sync now's ask (``ask_for_full_pass``): one full pass, then windows."""
    pool = FakePool(creds=creds_row())
    await _run(FakePropertyware(), pool)
    clock.advance(hours=1)
    assert await sync.ask_for_full_pass(pool, COMPANY) is True
    assert _mark(pool, sync.FULL_ASKED_MARKER) == clock.now
    clock.advance(minutes=1)
    asked = FakePropertyware()
    await _run(asked, pool)
    assert not any(_windowed(asked)) and "/leases/charges" in asked.paths()
    clock.advance(hours=4)
    after = FakePropertyware()
    await _run(after, pool)
    assert after.paths() == WATERMARKED and all(_windowed(after))


async def test_a_full_read_that_could_not_be_asked_for_says_so(
    caplog: pytest.LogCaptureFixture,
) -> None:
    pool = FakePool(creds=creds_row(), fail_on="INSERT INTO helixis_pms_sync_watermarks")
    assert await sync.ask_for_full_pass(pool, COMPANY) is False
    assert "could not record the full read asked for" in caplog.text


async def test_incremental_reads_switched_off_make_every_pass_full(
    switched_on: None, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sync, "INCREMENTAL_READS", False)
    pool = await _after_a_full_pass(clock)
    server = FakePropertyware()
    await _run(server, pool)
    # The charges carry no watermark, so they lead every full pass after the
    # first: never starved by a budget that runs out before them.
    assert server.paths() == ["/leases/charges", *WATERMARKED]
    assert not any(_windowed(server))
    assert _mark(pool, sync.FULL_MARKER) == clock.now


# ── the call budget ────────────────────────────────────────────────────────


async def test_a_pass_that_reaches_the_call_budget_stops_and_leaves_the_rest_stale(
    switched_on: None, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sync, "CALL_BUDGET", 3)
    server, pool = FakePropertyware(), FakePool(creds=creds_row())
    result = await _run(server, pool)

    reached = ["/buildings", "/units", "/portfolios"]
    assert server.paths() == reached, "exactly the budget, then nothing is sent"
    stale = {t for t, source in sync.SOURCE.items() if source not in reached}
    assert set(result.failed) == stale
    assert all(result.failed[t] == sync.CALL_BUDGET_REACHED for t in stale)
    assert "call budget" in sync.stale_reason(result.failed["helixis_leases"])
    for table in stale:
        assert pool.inserts(table) == [] and pool.deletes(table) == [], table
    for path in reached:
        assert _mark(pool, path) == FIRST_PASS
    for path in set(WATERMARKED) - set(reached):
        assert _mark(pool, path) is None, path
    assert _mark(pool, sync.FULL_MARKER) is None, "a pass that stopped did not end clean"

    monkeypatch.setattr(sync, "CALL_BUDGET", 1440)
    clock.advance(hours=4)
    after = FakePropertyware()
    second = await _run(after, pool)
    assert second.failed == {}
    unreached = [p for p in client.READS if p not in reached]
    assert after.paths() == unreached + reached, "the next pass starts with what it missed"
    assert not any(_windowed(after)), "still a FULL pass: the last one did not end clean"


async def test_a_list_the_budget_stops_part_way_is_stale_and_writes_nothing(
    switched_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client, "PAGING", OffsetPaging(limit=2, total_header="X-Total-Count"))
    monkeypatch.setattr(sync, "CALL_BUDGET", 1)
    server, pool = FakePropertyware(), FakePool(creds=creds_row())
    result = await _run(server, pool)
    assert len(server.requests) == 1, "the second page of the buildings was never asked for"
    assert result.failed["helixis_properties"] == sync.CALL_BUDGET_REACHED
    assert pool.inserts("helixis_properties") == [] and pool.deletes("helixis_properties") == []
    assert _mark(pool, "/buildings") is None


async def test_a_pass_the_budget_stops_on_its_very_last_page_holds_its_markers(
    switched_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The budget cuts the last list part-way and nothing is left unreached:
    a stop is never a refusal, so the markers still hold."""
    monkeypatch.setattr(client, "PAGING", OffsetPaging(limit=2, total_header="X-Total-Count"))
    whole = FakePropertyware()
    await _run(whole, FakePool(creds=creds_row()))
    assert whole.paths()[-2:] == ["/accounting/glaccounts"] * 2, "the last list takes two pages"
    monkeypatch.setattr(sync, "CALL_BUDGET", len(whole.requests) - 1)
    pool = FakePool(creds=creds_row())
    result = await _run(FakePropertyware(), pool)
    assert result.failed == {"helixis_gl_accounts": sync.CALL_BUDGET_REACHED}
    assert _mark(pool, sync.FULL_MARKER) is None and _mark(pool, sync.RECONCILE_MARKER) is None
    assert _mark(pool, "/accounting/glaccounts") is None
    assert _mark(pool, "/bills") == FIRST_PASS


async def test_a_pass_stopped_before_the_charges_keeps_the_balance_snapshot(
    switched_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    before_charges = client.READS.index("/leases/charges")
    monkeypatch.setattr(sync, "CALL_BUDGET", before_charges)
    server, pool = FakePropertyware(), FakePool(creds=creds_row())
    result = await _run(server, pool)
    assert server.paths() == list(client.READS[:before_charges])
    assert result.failed[sync.BALANCES] == sync.CALL_BUDGET_REACHED
    assert pool.inserts("helixis_lease_balances") == []
    assert pool.deletes("helixis_lease_balances") == []
    assert "helixis_leases" not in result.failed, "the leases themselves came through"


async def test_the_budgeted_client_refuses_before_sending_once_spent() -> None:
    server = FakePropertyware()
    creds = credentials.PropertywareCredentials(
        company_id=COMPANY, system_id="990000001", client_id="id-0001", client_secret="s" * 24
    )
    budget = client.CallBudget(1)
    async with client.build_client(creds, http=server.http(), budget=budget) as pw:
        assert isinstance(pw, client.BudgetedClient)
        await pw.get("/buildings", query={"limit": 500, "offset": 0})
        with pytest.raises(client.CallBudgetSpent):
            await pw.get("/units", query={"limit": 500, "offset": 0})
    assert server.paths() == ["/buildings"] and budget.used == 1 and budget.spent
    with pytest.raises(ValueError, match="at least one"):
        client.CallBudget(0)
    plain = client.build_client(creds, http=server.http())
    assert not isinstance(plain, client.BudgetedClient), "the probe and the test spend none"
    await plain.aclose()


def test_the_budget_fits_the_hourly_tick_and_is_well_inside_the_interval() -> None:
    """Both sides (rule 15): the worker's tick, the cycle's companies per
    tick, this client's rate and the cycle's interval, against the budget."""
    worker = ast.parse((REPO / "src/helixis/webhooks/worker.py").read_text(encoding="utf-8"))
    (tick,) = [
        node.value.value
        for node in ast.walk(worker)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(t, ast.Name) and t.id == "_PMS_SYNC_TICK_INTERVAL_SECONDS"
            for t in node.targets
        )
        and isinstance(node.value, ast.Constant)
    ]
    seconds = sync.CALL_BUDGET / client.REQUESTS_PER_SEC
    assert seconds * sync_cycle.COMPANIES_PER_TICK <= tick
    interval = settings().propertyware.sync_interval_hours * 3600
    assert seconds <= interval / 10, "one pass is well inside the 4-hour cycle"


# ── two passes over the real shared cycle ─────────────────────────────────


async def test_two_passes_over_the_shared_cycle_the_second_incremental(
    switched_on: None, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    servers: list[FakePropertyware] = []
    real_build = client.build_client

    def build(creds: Any, *, http: Any = None, budget: Any = None) -> Any:
        servers.append(FakePropertyware())
        return real_build(creds, http=servers[-1].http(), budget=budget)

    monkeypatch.setattr(client, "build_client", build)
    pool = FakePool(creds=creds_row())
    first = await sync_cycle.sync_one(pool, KEY, COMPANY)
    clock.advance(hours=4)
    second = await sync_cycle.sync_one(pool, KEY, COMPANY)

    assert isinstance(first, SyncResult) and isinstance(second, SyncResult)
    assert first.failed == {} and second.failed == {}
    full, incremental = servers
    assert full.paths() == list(client.READS) and not any(_windowed(full))
    assert incremental.paths() == WATERMARKED and all(_windowed(incremental))
    assert sync.BALANCES in first.rows_by_table and sync.BALANCES not in second.rows_by_table


# ── a watermark is bookkeeping, never the reason a pass fails ─────────────


async def test_a_watermark_that_cannot_be_written_never_fails_the_pass(
    switched_on: None, caplog: pytest.LogCaptureFixture
) -> None:
    pool = FakePool(creds=creds_row(), fail_on="INSERT INTO helixis_pms_sync_watermarks")
    result = await _run(FakePropertyware(), pool)
    assert result.failed == {}
    assert pool.watermarks == {}
    assert "could not move" in caplog.text


async def test_a_pass_refused_after_its_reads_moves_no_watermark(switched_on: None) -> None:
    """Disconnected while reading: nothing written, so nothing earned."""

    class Gone(FakePool):
        served = 0

        async def fetchrow(self, sql: str, *args: Any) -> Any:
            if "company_propertyware_credentials" in sql:
                self.served += 1
                if self.served == 2:
                    self.creds = None
            return await super().fetchrow(sql, *args)

    pool = Gone(creds=creds_row())
    with pytest.raises(sync.PropertywareSyncRefused):
        await _run(FakePropertyware(), pool)
    assert pool.watermarks == {}
