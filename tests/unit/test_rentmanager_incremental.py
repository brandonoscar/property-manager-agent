"""Rent Manager's incremental sync (design "Sync"): which pass runs, what each
reads, what moves a watermark, the filter canary and the call budget.

The real client over a mock transport, a synthetic Rent Manager that applies
``filters`` and ``fields`` the way the design expects them to work (RM-6,
RM-7), and the real mirror writer over a recording pool that keeps the
watermark table (``_rentmanager_fakes``).

What is pinned, each a rule the brief or the design states:

* the pass is chosen from stored markers: none, or a full read 7 days old, is
  FULL; a reconcile 24 hours old is RECONCILE; otherwise INCREMENTAL; a
  company whose filters cannot be trusted is always FULL; a re-save is FULL;
  Sync now's ask makes the next pass FULL, once;
* an incremental read sends the window 10 minutes (and the zone margin)
  behind each watermark, keeps the newest version of a row, writes a delta
  that prunes nothing, stamps what came through clean and advances only
  those watermarks;
* the reconcile prunes exactly the ids an id-only read no longer lists, and
  an incomplete id read prunes nothing;
* links, balances and deposits follow the shared writer's delta rules;
* the pass's markers move only when every table it left stale was refused
  for permission; any other failure holds them;
* the call budget.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from helixis.db.repositories import turnovers as turnovers_repo
from helixis.pms import http, sync_cycle
from helixis.pms.mirror import SyncResult
from helixis.pms.watermarks import LAG
from helixis.rentmanager import client as rm_client
from helixis.rentmanager import sync
from helixis.rentmanager.credentials import version_of
from helixis.rentmanager.resources import RESOURCES
from helixis.rentmanager.sync import FULL, INCREMENTAL, RECONCILE
from tests.unit._rentmanager_fakes import (
    COMPANY,
    PATHS,
    TODAY,
    FakePool,
    FakeRentManager,
    app_settings,
    credentials_row,
    json_response,
    portfolio,
    serve,
)

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
#: Each collection's watermark in the incremental tests: the fixture's rows
#: changed before it, except the ones a test touches after it.
WATERMARK = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
KEYED = [name for name, resource in RESOURCES.items() if resource.incremental]
LOOKUPS = [name for name, resource in RESOURCES.items() if not resource.incremental]
TOUCHED = "2026-10-05T07:30:00-04:00"
CANARY = rm_client.CANARY_FILTER


@pytest.fixture(autouse=True)
def _pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rent Manager switched on, the clock and today pinned, waits not slept."""
    monkeypatch.setattr("helixis.settings.get_settings", lambda: app_settings())
    monkeypatch.setattr(sync, "today_in", lambda *_a, **_k: TODAY)
    monkeypatch.setattr(sync, "_now", lambda: NOW)

    async def _no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr(http, "_sleep", _no_wait)
    monkeypatch.setattr(turnovers_repo, "start_turnover", AsyncMock(return_value=False))


def _pool() -> FakePool:
    return FakePool(credentials=credentials_row(app_settings()))


def _rm(data: dict[str, Any] | None = None, **kw: Any) -> FakeRentManager:
    """A Rent Manager that applies ``filters`` and ``fields`` (the plain fake
    ignores both, as a misspelt parameter is ignored)."""
    return FakeRentManager(data, honours_query=True, **kw)


def _version(pool: FakePool) -> str:
    assert pool.credentials is not None
    return version_of(pool.credentials)


def _mark(
    pool: FakePool,
    *,
    full: datetime | None = NOW - timedelta(days=1),
    reconcile: datetime | None = NOW - timedelta(hours=1),
    honoured: bool = True,
    ignored: bool = False,
    each: datetime | None = WATERMARK,
) -> FakePool:
    """Store the markers a company would have after earlier passes."""
    version = _version(pool)
    stored = {
        sync.pass_marker(FULL, version): full,
        sync.pass_marker(RECONCILE, version): reconcile,
        sync.filters_marker(True, version): NOW - timedelta(days=2) if honoured else None,
        sync.filters_marker(False, version): NOW - timedelta(days=2) if ignored else None,
        **{name: each for name in KEYED},
    }
    pool.watermarks.update({k: v for k, v in stored.items() if v is not None})
    return pool


async def _pass(
    monkeypatch: pytest.MonkeyPatch, server: FakeRentManager, pool: FakePool
) -> SyncResult:
    serve(monkeypatch, server)
    return await sync.sync_company(pool, COMPANY)


def _filters(server: FakeRentManager, path: str) -> list[str | None]:
    return [r.url.params.get("filters") for r in server.to(path)]


def _since(when: datetime) -> str:
    """Where a read by change starts: the shared 10 minutes behind the
    watermark, and the zone margin more, written as the probe writes it."""
    return (when - LAG - sync.ZONE_MARGIN).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")


def _touch(data: dict[str, Any], name: str, ident: int, **change: Any) -> None:
    """A record changed in Rent Manager after the watermark."""
    key = RESOURCES[name].key
    row = next(r for r in data[name] if r[key] == ident)
    row.update(change, UpdateDate=TOUCHED)


# ── which pass runs ───────────────────────────────────────────────────


DAY_AGO, HOUR_AGO = NOW - timedelta(days=1), NOW - timedelta(hours=1)


@pytest.mark.parametrize(
    ("full", "reconciled", "off", "asked", "expected"),
    [
        (None, None, False, None, FULL),
        (NOW - timedelta(days=7), HOUR_AGO, False, None, FULL),
        (NOW - timedelta(days=7) + timedelta(seconds=1), NOW, False, None, INCREMENTAL),
        (DAY_AGO, NOW - timedelta(hours=24), False, None, RECONCILE),
        (DAY_AGO, NOW - timedelta(hours=23, minutes=59), False, None, INCREMENTAL),
        (DAY_AGO, None, False, None, RECONCILE),
        (HOUR_AGO, HOUR_AGO, True, None, FULL),
        (DAY_AGO, HOUR_AGO, False, HOUR_AGO, FULL),
        (HOUR_AGO, HOUR_AGO, False, DAY_AGO, INCREMENTAL),
    ],
    ids=[
        "no markers",
        "a full read 7 days old",
        "a full read just inside the week",
        "a reconcile 24 hours old",
        "a reconcile just inside the day",
        "no reconcile yet",
        "filters not trusted",
        "a full pass asked for since the last",
        "a full pass asked for before the last",
    ],
)
def test_the_pass_is_chosen_from_the_stored_markers(
    full: datetime | None,
    reconciled: datetime | None,
    off: bool,
    asked: datetime | None,
    expected: str,
) -> None:
    chosen = sync.choose_pass(NOW, full=full, reconciled=reconciled, filters_off=off, asked=asked)
    assert chosen == expected


async def test_sync_now_gets_one_full_pass_and_then_reads_by_change_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rule 14: the Connectors page shows the counts of the pass Sync now
    started, and only a full pass's are totals."""
    pool = _mark(_pool())
    await sync.ask_for_full_pass(pool, COMPANY)
    first = _rm()
    result = await _pass(monkeypatch, first, pool)
    assert all("filters" not in r.url.params for r in first.collection_reads)
    assert result.rows_by_table["helixis_properties"] == len(portfolio()["properties"])
    assert pool.watermarks[sync.pass_marker(FULL, _version(pool))] == NOW

    second = _rm()
    await _pass(monkeypatch, second, pool)
    assert any("filters" in r.url.params for r in second.collection_reads), "the ask is spent"


async def test_a_first_pass_reads_everything_and_marks_every_watermark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, server = _pool(), FakeRentManager()
    result = await _pass(monkeypatch, server, pool)
    assert result.failed == {}
    assert all("filters" not in r.url.params for r in server.collection_reads)
    assert not server.to_filtered(CANARY), "no canary before a full read"
    version = _version(pool)
    assert pool.watermarks[sync.pass_marker(FULL, version)] == NOW
    assert pool.watermarks[sync.pass_marker(RECONCILE, version)] == NOW, "a full read reconciles"
    assert {name: pool.watermarks.get(name) for name in KEYED} == dict.fromkeys(KEYED, NOW)
    assert not set(LOOKUPS) & set(pool.watermarks), "a lookup is read whole every pass"


async def test_a_resave_reads_everything_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """New details may name another location, whose ids collide with the old
    ones in silence: a full read is what replaces them (design R2)."""
    pool = _pool()
    _mark(pool)
    pool.credentials = credentials_row(app_settings())  # saved again: a new version
    server = FakeRentManager()
    result = await _pass(monkeypatch, server, pool)
    assert result.failed == {}
    assert all("filters" not in r.url.params for r in server.collection_reads)
    assert pool.deletes_from("helixis_properties"), "a full read prunes"


# ── an incremental pass ───────────────────────────────────────────────


async def test_an_incremental_pass_reads_only_what_changed_since_each_watermark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = portfolio()
    _touch(data, "tenants", 880000301, LastName="Changed")
    pool, server = _mark(_pool()), _rm(data)
    result = await _pass(monkeypatch, server, pool)

    for name in KEYED:
        path = RESOURCES[name].path
        assert _filters(server, path) == [f"UpdateDate,ge,{_since(WATERMARK)}"], name
    for name in LOOKUPS:
        assert _filters(server, RESOURCES[name].path) == [None], "a lookup is read whole"
    assert result.rows_by_table["helixis_tenants"] == 1, "only the changed tenant"
    assert result.rows_by_table["helixis_properties"] == 0
    assert result.failed == {}


async def test_a_delta_prunes_nothing_and_stamps_what_came_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _mark(_pool())
    result = await _pass(monkeypatch, _rm(), pool)
    for table in sync.SYNC_STATE_STAGE:
        assert pool.deletes_from(table) == [], f"{table}: a delta pruned"
    assert pool.stamped == set(sync.SYNC_STATE_STAGE.values()) - {"balances"}
    assert result.pruned == {"helixis_lease_tenants": 0}


async def test_a_clean_incremental_pass_advances_only_the_watermarks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _mark(_pool())
    await _pass(monkeypatch, _rm(), pool)
    version = _version(pool)
    assert {name: pool.watermarks[name] for name in KEYED} == dict.fromkeys(KEYED, NOW)
    assert pool.watermarks[sync.pass_marker(FULL, version)] == NOW - timedelta(days=1)
    assert pool.watermarks[sync.pass_marker(RECONCILE, version)] == NOW - timedelta(hours=1)


@pytest.mark.parametrize(
    ("newer_at", "older_at"),
    [
        ("2026-10-05T08:00:00-04:00", "2026-10-05T07:00:00-04:00"),
        # Either side of a change of UTC offset: as text, the older sorts last.
        ("2026-11-01T01:15:00-05:00", "2026-11-01T01:30:00-04:00"),
    ],
    ids=["one offset", "across a change of offset"],
)
async def test_two_versions_of_one_row_keep_the_newest(
    monkeypatch: pytest.MonkeyPatch, newer_at: str, older_at: str
) -> None:
    """A row changed during the read comes back twice; the older copy, listed
    second, must not overwrite the newer."""
    tenant = next(t for t in portfolio()["tenants"] if t["TenantID"] == 880000301)
    newer = {**tenant, "LastName": "Newer", "UpdateDate": newer_at}
    older = {**tenant, "LastName": "Older", "UpdateDate": older_at}
    server = _rm(overrides={"/Tenants": lambda r: FakeRentManager.page(r, [newer, older])})
    pool = _mark(_pool())
    result = await _pass(monkeypatch, server, pool)
    assert result.rows_by_table["helixis_tenants"] == 1
    ((_sql, args),) = pool.inserts_into("helixis_tenants")
    assert "Newer" in args and "Older" not in args


@pytest.mark.parametrize(
    "answer",
    [
        lambda _r: json_response(403, {}),
        lambda _r: json_response(200, portfolio()["vendors"]),
    ],
    ids=["failed", "not confirmed whole"],
)
async def test_a_resource_that_did_not_come_through_keeps_its_watermark(
    monkeypatch: pytest.MonkeyPatch, answer: Any
) -> None:
    pool = _mark(_pool())
    result = await _pass(monkeypatch, _rm(overrides={"/Vendors": answer}), pool)
    assert "helixis_vendors" in result.failed
    assert pool.watermarks["vendors"] == WATERMARK
    assert pool.watermarks["owners"] == NOW, "the others advanced"

    again = _rm()
    await _pass(monkeypatch, again, pool)
    assert _filters(again, "/Vendors") == [f"UpdateDate,ge,{_since(WATERMARK)}"]
    assert _filters(again, "/Owners") == [f"UpdateDate,ge,{_since(NOW)}"]


async def test_a_lease_that_lost_its_tenant_loses_the_link_and_the_rest_keep_theirs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = portfolio()
    # Lease 880000601 moved out yesterday: it is past, so no one lives there now.
    _touch(data, "leases", 880000601, MoveOutDate="2026-10-04T00:00:00")
    pool = _mark(_pool())
    result = await _pass(monkeypatch, _rm(data), pool)
    links = pool.deletes_from("helixis_lease_tenants")
    assert [args[2:] for _s, args in links] == [(880000601, [])]
    assert pool.inserts_into("helixis_lease_tenants") == []
    assert "helixis_lease_tenants" in result.confirmed_complete


async def test_balances_and_deposits_stand_untouched_on_an_incremental_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = portfolio()
    _touch(data, "leases", 880000601)
    _touch(data, "tenants", 880000301)
    pool = _mark(_pool())
    result = await _pass(monkeypatch, _rm(data), pool)
    assert pool.inserts_into("helixis_lease_balances") == []
    assert pool.deletes_from("helixis_lease_balances") == []
    assert "helixis_lease_balances" not in result.failed
    assert "balances" not in pool.stamped
    ((sql, _args),) = pool.inserts_into("helixis_leases")
    assert "security_deposit" not in sql, "a deposit is placed only from whole reads"


# ── the reconcile ─────────────────────────────────────────────────────


def _id_reads(server: FakeRentManager) -> dict[str, str]:
    """``{path: fields}`` of every id-only read the pass sent."""
    return {
        r.url.path: r.url.params["fields"]
        for r in server.collection_reads
        if "filters" not in r.url.params
        and r.url.params.get("fields") == RESOURCES[PATHS[r.url.path]].ids_query()["fields"]
    }


async def test_a_reconcile_reads_every_id_and_prunes_exactly_the_ones_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = portfolio()
    # Rent Manager deleted owner 880000403; the mirror still has it.
    data["owners"] = [o for o in data["owners"] if o["OwnerID"] != 880000403]
    pool = _mark(_pool(), reconcile=NOW - timedelta(hours=25))
    server = _rm(data)
    result = await _pass(monkeypatch, server, pool)
    assert result.failed == {}

    delta = [n for n in KEYED if n not in sync.WHOLE_ON_RECONCILE]
    assert _id_reads(server) == {
        RESOURCES[n].path: ",".join((RESOURCES[n].key, *RESOURCES[n].id_extra)) for n in delta
    }
    ((_sql, args),) = pool.deletes_from("helixis_owners")
    assert args[2] == sorted(o["OwnerID"] for o in data["owners"])
    # The reversed bill is still in Rent Manager, and still not in the mirror.
    ((_sql, args),) = pool.deletes_from("helixis_bills")
    assert args[2] == sorted(b["ID"] for b in data["bills"] if not b.get("ReversalDate"))
    assert pool.watermarks[sync.pass_marker(RECONCILE, _version(pool))] == NOW


async def test_a_reconcile_reads_what_the_balances_need_whole(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _mark(_pool(), reconcile=NOW - timedelta(hours=25))
    server = _rm()
    result = await _pass(monkeypatch, server, pool)
    for name in sync.WHOLE_ON_RECONCILE:
        assert _filters(server, RESOURCES[name].path) == [None], name
    assert "balances" in pool.stamped and "helixis_lease_balances" in result.confirmed_complete
    assert pool.deletes_from("helixis_tenants"), "a whole read prunes by itself"


async def test_an_incomplete_id_read_prunes_nothing_and_the_reconcile_is_not_marked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = portfolio()["owners"]

    def _owners(request: httpx.Request) -> httpx.Response:
        if "filters" in request.url.params:
            return FakeRentManager.page(request, [])
        # The id-only read, without Rent Manager's total: not confirmed whole.
        return json_response(200, [{"OwnerID": o["OwnerID"]} for o in rows[:1]])

    pool = _mark(_pool(), reconcile=NOW - timedelta(hours=25))
    result = await _pass(monkeypatch, _rm(overrides={"/Owners": _owners}), pool)
    assert pool.deletes_from("helixis_owners") == []
    assert "helixis_owners" not in result.failed, "the delta itself came through"
    assert pool.watermarks[sync.pass_marker(RECONCILE, _version(pool))] < NOW


# ── what holds the pass's markers ─────────────────────────────────────


@pytest.mark.parametrize("path", ["/Bills", "/ServiceManagerStatuses"])
async def test_a_table_refused_for_permission_stays_stale_and_holds_nothing(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """A refusal no re-read can fix: were it to hold the markers, every pass
    for this company would be a full one, forever."""
    pool = _pool()
    server = _rm(overrides={path: lambda _r: json_response(403, {})})
    result = await _pass(monkeypatch, server, pool)
    stale = "helixis_bills" if path == "/Bills" else "helixis_work_orders"
    assert set(result.failed) == {stale}
    assert stale.removeprefix("helixis_") not in pool.watermarks, "it keeps its own watermark"
    version = _version(pool)
    assert pool.watermarks[sync.pass_marker(FULL, version)] == NOW
    assert pool.watermarks[sync.pass_marker(RECONCILE, version)] == NOW

    again = _rm(overrides={path: lambda _r: json_response(403, {})})
    later = await _pass(monkeypatch, again, pool)
    assert set(later.failed) == {stale}, "still stale, with its reason"
    assert any("filters" in r.url.params for r in again.collection_reads), "not a full pass"


@pytest.mark.parametrize(
    "answer",
    [
        lambda _r: httpx.Response(503),
        lambda _r: json_response(200, portfolio()["bills"]),
    ],
    ids=["an outage", "a read not confirmed whole"],
)
async def test_any_other_failure_holds_the_markers_and_the_next_pass_is_full_again(
    monkeypatch: pytest.MonkeyPatch, answer: Any
) -> None:
    pool = _pool()
    result = await _pass(monkeypatch, _rm(overrides={"/Bills": answer}), pool)
    assert set(result.failed) == {"helixis_bills"}
    version = _version(pool)
    assert sync.pass_marker(FULL, version) not in pool.watermarks
    assert sync.pass_marker(RECONCILE, version) not in pool.watermarks

    again = _rm()
    await _pass(monkeypatch, again, pool)
    assert all("filters" not in r.url.params for r in again.collection_reads), "full again"


async def test_a_reconcile_whose_id_read_is_refused_is_still_marked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The collection read by change came through; only its id list was
    refused, which no re-read fixes. Nothing is pruned from it."""
    rows = portfolio()["owners"]

    def _owners(request: httpx.Request) -> httpx.Response:
        if "filters" in request.url.params:
            return FakeRentManager.page(request, [])
        return json_response(403, {})

    pool = _mark(_pool(), reconcile=NOW - timedelta(hours=25))
    result = await _pass(monkeypatch, _rm(overrides={"/Owners": _owners}), pool)
    assert result.failed == {} and rows
    assert pool.deletes_from("helixis_owners") == []
    assert pool.watermarks[sync.pass_marker(RECONCILE, _version(pool))] == NOW


# ── the filter canary ─────────────────────────────────────────────────


async def test_the_canary_runs_once_before_the_first_incremental_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _mark(_pool(), honoured=False)
    server = _rm()
    result = await _pass(monkeypatch, server, pool)
    (canary,) = server.to_filtered(CANARY)
    assert server.collection_reads.index(canary) == 0, "before any other read"
    by_change = {
        r.url.path
        for r in server.collection_reads
        if r is not canary and ("filters" in r.url.params)
    }
    assert by_change == {RESOURCES[n].path for n in KEYED}
    assert pool.watermarks[sync.filters_marker(True, _version(pool))] == NOW
    assert sync.INCREMENTAL_OFF not in result.failed

    again = _rm()
    await _pass(monkeypatch, again, pool)
    assert again.to_filtered(CANARY) == [], "the canary is sent once"


async def test_a_filter_that_is_ignored_turns_incremental_reads_off_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _mark(_pool(), honoured=False)
    server = FakeRentManager()  # ignores filters, as a misspelt parameter is ignored
    result = await _pass(monkeypatch, server, pool)
    assert len(server.to_filtered(CANARY)) == 1
    others = [r for r in server.collection_reads if r not in server.to_filtered(CANARY)]
    assert all("filters" not in r.url.params for r in others), "this pass reads in full"
    note = result.failed[sync.INCREMENTAL_OFF]
    assert "returned records for a filter that can match none" in note
    assert "every pass reads everything" in note and "Saving the connection again" in note
    assert pool.watermarks[sync.filters_marker(False, _version(pool))] == NOW
    assert pool.deletes_from("helixis_properties"), "the full read prunes as usual"
    assert set(result.failed) == {sync.INCREMENTAL_OFF}, "no table is stale for it"
    version = _version(pool)
    assert pool.watermarks[sync.pass_marker(FULL, version)] == DAY_AGO, "it holds the markers"
    assert pool.watermarks[sync.pass_marker(RECONCILE, version)] == HOUR_AGO

    again = FakeRentManager()
    later = await _pass(monkeypatch, again, pool)
    assert again.to_filtered(CANARY) == [], "stored, not asked again"
    assert all("filters" not in r.url.params for r in again.collection_reads)
    assert sync.INCREMENTAL_OFF in later.failed


async def test_a_canary_that_cannot_be_asked_reads_in_full_and_asks_next_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _properties(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("filters") == CANARY:
            return json_response(400, {"Message": "bad filter"})
        return FakeRentManager.page(request, portfolio()["properties"])

    pool = _mark(_pool(), honoured=False)
    server = _rm(overrides={"/Properties": _properties})
    result = await _pass(monkeypatch, server, pool)
    assert all(
        "filters" not in r.url.params
        for r in server.collection_reads
        if r.url.params.get("filters") != CANARY
    )
    assert "could not be made" in result.failed[sync.INCREMENTAL_OFF]
    version = _version(pool)
    assert sync.filters_marker(True, version) not in pool.watermarks
    assert sync.filters_marker(False, version) not in pool.watermarks
    assert pool.watermarks[sync.pass_marker(FULL, version)] == DAY_AGO, "it holds the markers"

    again = _rm()
    await _pass(monkeypatch, again, pool)
    assert len(again.to_filtered(CANARY)) == 1


# ── the call budget ───────────────────────────────────────────────────


async def test_a_pass_that_reaches_its_budget_leaves_the_rest_stale_and_unmarked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The location read and the first four collections: five GETs."""
    monkeypatch.setattr(sync, "CALL_BUDGET", 5)
    pool, server = _pool(), FakeRentManager()
    result = await _pass(monkeypatch, server, pool)
    read = ["properties", "units", "tenants", "leases"]
    assert [r.url.path for r in server.collection_reads] == [RESOURCES[n].path for n in read]
    not_reached = {t for t, source in sync.TABLE_SOURCES.items() if source not in read}
    for table in not_reached:
        assert "call budget reached" in result.failed[table], table
        assert pool.inserts_into(table) == [] and pool.deletes_from(table) == [], table
    assert {n for n in KEYED if n in pool.watermarks} == set(read)
    assert sync.pass_marker(FULL, _version(pool)) not in pool.watermarks


async def test_the_next_pass_starts_with_what_the_budget_left(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sync, "CALL_BUDGET", 5)
    pool = _pool()
    await _pass(monkeypatch, FakeRentManager(), pool)
    monkeypatch.setattr(sync, "CALL_BUDGET", 300)
    server = FakeRentManager()
    result = await _pass(monkeypatch, server, pool)
    order = [r.url.path for r in server.collection_reads]
    first = ["/Owners", "/Vendors", "/ServiceManagerStatuses", "/ServiceManagerPriorities"]
    assert order[: len(first)] == first
    assert result.failed == {}
    assert sync.pass_marker(FULL, _version(pool)) in pool.watermarks


async def test_the_budget_counts_every_request_a_pass_sends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A full pass of the fixture is twelve GETs: the location and one page of
    each of eleven collections. Twelve is enough; eleven is not."""
    gets = 1 + len(RESOURCES)
    monkeypatch.setattr(sync, "CALL_BUDGET", gets)
    result = await _pass(monkeypatch, FakeRentManager(), _pool())
    assert result.failed == {}
    monkeypatch.setattr(sync, "CALL_BUDGET", gets - 1)
    short = await _pass(monkeypatch, FakeRentManager(), _pool())
    assert set(short.failed) == {"helixis_gl_accounts"}


async def test_a_read_the_budget_cuts_short_is_written_but_not_stamped_or_marked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One row a page: the budget runs out inside the tenants read."""
    monkeypatch.setattr(rm_client, "PAGING", dataclasses.replace(rm_client.PAGING, page_size=1))
    monkeypatch.setattr(sync, "CALL_BUDGET", 1 + 2 + 3 + 2)
    pool = _pool()
    result = await _pass(monkeypatch, FakeRentManager(), pool)
    assert "incomplete" in result.failed["helixis_tenants"]
    assert len(pool.inserts_into("helixis_tenants")) == 2, "the rows that arrived are real"
    assert pool.deletes_from("helixis_tenants") == []
    assert "tenants" not in pool.watermarks and "units" in pool.watermarks
    assert "call budget reached" in result.failed["helixis_leases"]


def test_the_budget_fits_well_inside_the_cycle() -> None:
    """At the design's one request a second, a pass that spends its whole
    budget reads for minutes, not hours (R6: a pass every 4 hours)."""
    seconds = sync.CALL_BUDGET / rm_client.REQUESTS_PER_SEC
    assert seconds <= 4 * 3600 / 20


# ── over the real shared cycle ────────────────────────────────────────


async def test_two_passes_in_a_row_through_the_shared_cycle_the_second_incremental(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _pool()
    first = serve(monkeypatch, _rm())
    one = await sync_cycle.sync_one(pool, "rentmanager", COMPANY)
    assert isinstance(one, SyncResult) and one.failed == {}
    assert all("filters" not in r.url.params for r in first.collection_reads)

    second = serve(monkeypatch, _rm())
    two = await sync_cycle.sync_one(pool, "rentmanager", COMPANY)
    assert isinstance(two, SyncResult) and two.failed == {}
    assert len(second.to_filtered(CANARY)) == 1, "the canary, once"
    filtered = {r.url.path for r in second.collection_reads if "filters" in r.url.params}
    assert filtered == {RESOURCES[n].path for n in KEYED}, "every collection but the lookups"
