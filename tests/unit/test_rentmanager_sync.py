"""One company's Rent Manager pass, end to end: the real client over a mock
transport, the real mirror writer over a recording pool.

What is pinned, each a rule the shared layer or the design states:

* refusals come BEFORE any call: switched off, no credentials, unusable ones;
* a location that is not the stored one writes NOTHING (design R2);
* a failed read is stale, never empty, and the other tables land;
* an incomplete read is written, but not stamped and not pruned;
* a complete read stamps its stage; turnovers open once the leases landed;
* the shared layer sees this connector (registry, coverage, the run note).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography.fernet import Fernet

from helixis.agents import run_pms
from helixis.db import pms_coverage
from helixis.db.repositories import turnovers as turnovers_repo
from helixis.db.repositories.mirror_sync_state import CORE_TABLES
from helixis.pms import http, registry, sync_cycle
from helixis.pms.mirror import PRUNE_TOUCH_MARGIN_S, SyncResult
from helixis.pms.watermarks import TABLE as WATERMARKS
from helixis.rentmanager import client as rm_client
from helixis.rentmanager import sync
from helixis.rentmanager.resources import RESOURCES
from helixis.rentmanager.sync import RentManagerSyncRefused
from tests.unit._rentmanager_fakes import (
    COMPANY,
    PARTNER,
    PASSWORD,
    SECRETS,
    TODAY,
    TOKEN,
    USERNAME,
    FakePool,
    FakeRentManager,
    app_settings,
    credentials_row,
    json_response,
    portfolio,
    serve,
)

FILLED = set(sync.SYNC_STATE_STAGE) | set(sync.DERIVED_TABLES)


@pytest.fixture(autouse=True)
def _pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rent Manager switched on, today pinned, waits recorded not slept."""
    monkeypatch.setattr("helixis.settings.get_settings", lambda: app_settings())
    monkeypatch.setattr(sync, "today_in", lambda *_a, **_k: TODAY)

    async def _no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr(http, "_sleep", _no_wait)


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """The shared turnover trigger, stood in for; it is tested in its own module."""
    start = AsyncMock(return_value=True)
    monkeypatch.setattr(turnovers_repo, "start_turnover", start)
    return start


def _pool(**row: Any) -> FakePool:
    return FakePool(credentials=credentials_row(app_settings(), **row))


Run = Callable[..., Awaitable[tuple[SyncResult, FakePool]]]


@pytest.fixture
def run(monkeypatch: pytest.MonkeyPatch) -> Run:
    """``await run(server, pool=None)``: one pass, the connector's own client
    routed to ``server`` (``_rentmanager_fakes.serve``)."""

    async def _run(
        server: FakeRentManager, pool: FakePool | None = None
    ) -> tuple[SyncResult, FakePool]:
        serve(monkeypatch, server)
        pool = pool or _pool()
        return await sync.sync_company(pool, COMPANY), pool

    return _run


# ── refusals, before any call ─────────────────────────────────────────


async def test_switched_off_refuses_before_any_call(
    monkeypatch: pytest.MonkeyPatch, run: Run
) -> None:
    monkeypatch.setattr("helixis.settings.get_settings", lambda: app_settings(enabled=False))
    server, pool = FakeRentManager(), _pool()
    with pytest.raises(RentManagerSyncRefused):
        await run(server, pool)
    assert server.requests == [] and pool.log == [], "a dark connector read something"


async def test_no_credentials_refuse_before_any_call(run: Run) -> None:
    server, pool = FakeRentManager(), FakePool(credentials=None)
    with pytest.raises(RentManagerSyncRefused):
        await run(server, pool)
    assert server.requests == []
    assert pool.mirror_writes == []


@pytest.mark.parametrize(
    "row",
    [{"corp_id": "evil.example/x"}, {"location_id": 0}, {"location_id": "1"}],
    ids=["a corp id that is not a host label", "no location", "a location that is text"],
)
async def test_unusable_credentials_refuse_before_any_call(row: dict[str, Any], run: Run) -> None:
    server = FakeRentManager()
    with pytest.raises(RentManagerSyncRefused):
        await run(server, _pool(**row))
    assert server.requests == []


async def test_credentials_that_do_not_decrypt_refuse_without_a_secret(
    caplog: pytest.LogCaptureFixture,
    run: Run,
) -> None:
    rotated = app_settings(data_key=Fernet.generate_key().decode())
    pool = FakePool(credentials=credentials_row(rotated))
    with caplog.at_level(logging.DEBUG), pytest.raises(RentManagerSyncRefused) as caught:
        await run(FakeRentManager(), pool)
    for secret in SECRETS:
        assert secret not in str(caught.value) and secret not in caplog.text


# ── the location ──────────────────────────────────────────────────────


async def test_another_location_writes_nothing_and_every_table_is_stale(
    opened: AsyncMock,
    run: Run,
) -> None:
    data = portfolio()
    data["current_location"] = {"LocationID": 2, "Name": "Synthetic Annex"}
    server = FakeRentManager(data)
    result, pool = await run(server)
    assert set(result.failed) == FILLED
    assert all("location mismatch" in why for why in result.failed.values())
    assert result.rows_by_table == {} and result.pruned == {}
    assert pool.mirror_writes == [] and pool.stamped == set()
    assert server.collection_reads == [], "nothing is read from a location this is not"
    opened.assert_not_awaited()


@pytest.mark.parametrize(
    "answer",
    [httpx.Response(404), json_response(200, {"Name": "no id"}), json_response(403, {})],
    ids=["not found", "no id in it", "not permitted"],
)
async def test_a_location_that_cannot_be_confirmed_writes_nothing(
    answer: httpx.Response, run: Run
) -> None:
    server = FakeRentManager(overrides={"/Locations/CurrentLocation": lambda _r: answer})
    result, pool = await run(server)
    assert set(result.failed) == FILLED
    assert all("could not confirm" in why for why in result.failed.values())
    assert pool.mirror_writes == [] and server.collection_reads == []


async def test_a_refused_login_writes_nothing_and_says_so(run: Run) -> None:
    server = FakeRentManager(
        overrides={"/Authentication/AuthorizeUser": lambda _r: json_response(401, {})}
    )
    result, pool = await run(server)
    assert set(result.failed) == FILLED and pool.mirror_writes == []
    assert all("credentials" in why for why in result.failed.values())


# ── the saved connection, checked around the write ────────────────────


def _meanwhile(
    pool: FakePool, row: dict[str, Any] | None
) -> Callable[[httpx.Request], httpx.Response]:
    """The last collection's answer, after which the saved row is ``row``:
    a disconnect (None) or a re-save landing while the pass reads."""

    def _answer(request: httpx.Request) -> httpx.Response:
        pool.credentials = row
        return FakeRentManager.page(request, portfolio()["gl_accounts"])

    return _answer


@pytest.mark.parametrize("resaved", [False, True], ids=["disconnected", "saved again"])
async def test_a_connection_removed_or_replaced_while_the_pass_reads_writes_nothing(
    opened: AsyncMock, run: Run, resaved: bool
) -> None:
    """A pass reads for minutes. A disconnect in that time deletes the row and
    purges; writing afterwards would put back what the customer removed. A
    re-save may name another account, so its data is not this pass's."""
    pool = _pool()
    after = credentials_row(app_settings()) if resaved else None
    server = FakeRentManager(overrides={RESOURCES["gl_accounts"].path: _meanwhile(pool, after)})
    result, _db = await run(server, pool)
    assert set(result.failed) == FILLED
    assert all("removed or replaced" in why for why in result.failed.values())
    assert pool.mirror_writes == [] and pool.stamped == set()
    opened.assert_not_awaited()


async def test_a_connection_that_cannot_be_confirmed_before_the_write_writes_nothing(
    opened: AsyncMock, run: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _down(_pool: Any, _company: str) -> Any:
        raise RuntimeError("pool exhausted")

    monkeypatch.setattr(sync, "fetch_row", _down)
    result, pool = await run(FakeRentManager())
    assert set(result.failed) == FILLED and pool.mirror_writes == []
    assert all("could not confirm the saved connection" in why for why in result.failed.values())


async def test_a_disconnect_while_the_pass_writes_is_purged_after_it(
    opened: AsyncMock, run: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The disconnect deletes the row, then purges. A pass that checked before
    the delete and wrote after the purge would leave Rent Manager data behind
    a disconnect (terms rule 9 makes the disconnect the answer to a suspected
    compromise). So the pass checks again at the end and purges what it wrote."""
    from helixis.buildium.purge import PURGE_TABLES

    real = sync.write_mirror

    async def _disconnected_meanwhile(pool: Any, *args: Any, **kwargs: Any) -> SyncResult:
        written = await real(pool, *args, **kwargs)
        pool.credentials = None
        return written

    monkeypatch.setattr(sync, "write_mirror", _disconnected_meanwhile)
    result, pool = await run(FakeRentManager())
    assert set(result.failed) == FILLED
    assert all("deleted again" in why for why in result.failed.values())
    purged = [s for s, _a in pool.log if s.startswith("DELETE FROM public.")]
    assert [s.split()[2] for s in purged] == [f"public.{t}" for t in PURGE_TABLES]
    last_write = max(
        i for i, (s, _a) in enumerate(pool.log) if s.startswith("INSERT INTO helixis_")
    )
    first_purge = next(
        i for i, (s, _a) in enumerate(pool.log) if s.startswith("DELETE FROM public.")
    )
    assert last_write < first_purge, "the purge runs after everything the pass wrote"


async def test_a_pass_whose_connection_stands_is_not_purged(opened: AsyncMock, run: Run) -> None:
    result, pool = await run(FakeRentManager())
    assert result.failed == {}
    assert not [s for s, _a in pool.log if s.startswith("DELETE FROM public.")]


async def test_a_check_after_the_write_that_cannot_be_made_keeps_the_result(
    opened: AsyncMock, run: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rows were written for a connection that stood when the write began;
    a failed second look is logged, never the reason a pass reads as failed."""
    real = sync.fetch_row
    calls = {"n": 0}

    async def _second_read_fails(pool: Any, company_id: str) -> Any:
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("pool exhausted")
        return await real(pool, company_id)

    monkeypatch.setattr(sync, "fetch_row", _second_read_fails)
    result, pool = await run(FakeRentManager())
    assert result.failed == {} and calls["n"] == 2
    assert pool.mirror_writes


# ── a clean pass ──────────────────────────────────────────────────────


async def test_a_complete_pass_writes_every_table_and_stamps_every_stage(
    opened: AsyncMock,
    run: Run,
) -> None:
    server = FakeRentManager()
    result, pool = await run(server)
    assert result.failed == {}, result.failed
    assert result.confirmed_complete == set(sync.SYNC_STATE_STAGE)
    assert pool.stamped == set(sync.SYNC_STATE_STAGE.values())
    for table in sync.SYNC_STATE_STAGE:
        if table != "helixis_lease_balances":
            assert pool.inserts_into(table), f"{table}: nothing written"
        assert pool.deletes_from(table), f"{table}: a complete read prunes"
    assert result.rows_by_table["helixis_bills"] == 2, "the reversed bill is not written"
    assert result.rows_by_table["helixis_turnovers"] == 2


async def test_every_statement_names_this_company_and_rent_manager(
    opened: AsyncMock, run: Run
) -> None:
    """Rule 11. The two sync-state tables (freshness stamps, watermarks) are
    keyed by company alone, with no system column (migrations 0057, 0108):
    a company holds one system. Every other statement names both."""
    _result, pool = await run(FakeRentManager())
    writes = [(s, a) for s, a in pool.log if s.upper().startswith(("INSERT", "DELETE"))]
    assert len(writes) > 30
    assert any(WATERMARKS in sql for sql, _a in writes), "the watermarks went unwritten"
    for sql, args in writes:
        assert COMPANY in args, sql
        if "helixis_mirror_sync_state" not in sql and WATERMARKS not in sql:
            assert "rentmanager" in args, sql


async def test_every_read_is_one_whole_collection_with_its_fields(
    opened: AsyncMock, run: Run
) -> None:
    server = FakeRentManager()
    await run(server)
    read_paths = [r.url.path for r in server.collection_reads]
    assert read_paths == [r.path for r in RESOURCES.values()], "one page each, in order"
    for request in server.collection_reads:
        assert request.method == "GET"
        assert request.headers[rm_client.TOKEN_HEADER] == TOKEN
        assert set(request.url.params) <= rm_client.READ_PARAMS


async def test_no_request_ever_carries_save_options(opened: AsyncMock, run: Run) -> None:
    """The captured-URL half of the design's check (the AST half is in
    ``test_rentmanager_client``)."""
    server = FakeRentManager()
    await run(server)
    assert server.requests
    for request in server.requests:
        assert "saveoptions" not in str(request.url).lower()
        assert b"saveoptions" not in request.content.lower()


async def test_the_partner_token_rides_every_request_of_a_pass(opened: AsyncMock, run: Run) -> None:
    server = FakeRentManager()
    await run(server, _pool(partner=PARTNER))
    assert all(r.headers[rm_client.PARTNER_HEADER] == PARTNER for r in server.requests)


# ── stale, never empty ────────────────────────────────────────────────


async def test_a_failed_resource_is_stale_and_the_rest_land(opened: AsyncMock, run: Run) -> None:
    server = FakeRentManager(overrides={"/Bills": lambda _r: json_response(403, {})})
    result, pool = await run(server)
    assert set(result.failed) == {"helixis_bills"}
    assert "permission" in result.failed["helixis_bills"]
    assert pool.inserts_into("helixis_bills") == [] and pool.deletes_from("helixis_bills") == []
    assert "bills" not in pool.stamped
    assert pool.stamped == set(sync.SYNC_STATE_STAGE.values()) - {"bills"}
    assert result.rows_by_table["helixis_vendors"] == 2, "the reads after it still landed"


async def test_the_prune_window_covers_the_whole_pass(
    opened: AsyncMock, run: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every table is written after every read, so a prune spares rows touched
    since the FIRST read began, not only during its own read."""
    clock = iter([100.0, 130.0])
    monkeypatch.setattr(sync, "_monotonic", lambda: next(clock))
    _result, pool = await run(FakeRentManager())
    ((_sql, args),) = pool.deletes_from("helixis_properties")
    assert args[-1] == 30.0 + PRUNE_TOUCH_MARGIN_S


async def test_an_incomplete_read_is_written_but_not_stamped_or_pruned(
    opened: AsyncMock,
    run: Run,
) -> None:
    """No ``X-Total-Results``: Rent Manager never named the end."""
    rows = portfolio()["vendors"]
    server = FakeRentManager(overrides={"/Vendors": lambda _r: json_response(200, rows)})
    result, pool = await run(server)
    assert "incomplete" in result.failed["helixis_vendors"]
    assert len(pool.inserts_into("helixis_vendors")) == 2, "rows that arrived are real"
    assert pool.deletes_from("helixis_vendors") == []
    assert "vendors" not in pool.stamped and "properties" in pool.stamped


async def test_an_empty_collection_is_written_empty_and_stamped(
    opened: AsyncMock, run: Run
) -> None:
    """A 404 on a collection read is Rent Manager's documented "no rows": the
    company has none, which is a fact, so the table is pruned and stamped."""
    data = portfolio()
    data["bills"] = []
    result, pool = await run(FakeRentManager(data))
    assert "helixis_bills" not in result.failed
    assert result.rows_by_table["helixis_bills"] == 0
    assert pool.deletes_from("helixis_bills") and "bills" in pool.stamped


async def test_a_maintenance_window_ends_the_pass_stale_never_empty(
    opened: AsyncMock, run: Run
) -> None:
    text = "Service unavailable while performing database updates. Try again after a few minutes"
    server = FakeRentManager(overrides={"/Units": lambda _r: json_response(503, text)})
    result, pool = await run(server)
    assert "helixis_properties" not in result.failed, "the read before it landed"
    after = [t for t in sync.TABLE_SOURCES if t not in {"helixis_properties"}]
    for table in after:
        assert table in result.failed, f"{table} read as fresh during maintenance"
    assert len(server.to("/Units")) == 1, "the maintenance window was retried into"
    assert server.to("/Tenants") == [], "the hold kept every later read off Rent Manager"
    assert pool.inserts_into("helixis_units") == [] and pool.deletes_from("helixis_tenants") == []


async def test_a_lookup_that_fails_leaves_the_work_orders_as_they_were(
    opened: AsyncMock,
    run: Run,
) -> None:
    server = FakeRentManager(
        overrides={"/ServiceManagerStatuses": lambda _r: json_response(403, {})}
    )
    result, pool = await run(server)
    assert "lookup failed" in result.failed["helixis_work_orders"]
    assert pool.inserts_into("helixis_work_orders") == []
    assert "work_orders" not in pool.stamped


async def test_a_second_401_stops_the_pass_and_every_later_table_is_stale(
    opened: AsyncMock,
    run: Run,
) -> None:
    server = FakeRentManager(overrides={"/Tenants": lambda _r: json_response(401, {})})
    result, _db = await run(server)
    assert "helixis_properties" not in result.failed
    assert "credentials" in result.failed["helixis_tenants"]
    assert "Reconnect Rent Manager" in result.failed["helixis_vendors"]
    logins = server.to("/Authentication/AuthorizeUser")
    assert len(logins) == 2, "one login, then one re-login after the 401: no loop"


async def test_no_credential_reaches_a_log_or_a_result(
    opened: AsyncMock, caplog: pytest.LogCaptureFixture, run: Run
) -> None:
    echo = f"denied for {USERNAME}:{PASSWORD} with {TOKEN} and {PARTNER}"
    server = FakeRentManager(
        overrides={
            "/Bills": lambda _r: json_response(400, echo),
            "/GLAccounts": lambda _r: json_response(500, echo),
        }
    )
    with caplog.at_level(logging.DEBUG):
        result, _db = await run(server, _pool(partner=PARTNER))
    assert {"helixis_bills", "helixis_gl_accounts"} <= set(result.failed)
    seen = caplog.text + repr(result)
    for secret in SECRETS:
        assert secret not in seen


# ── turnovers ─────────────────────────────────────────────────────────


async def test_a_move_out_opens_a_turnover_and_an_old_one_does_not(
    opened: AsyncMock, run: Run
) -> None:
    result, _db = await run(FakeRentManager())
    calls = {c.kwargs["lease_id"]: c.kwargs for c in opened.await_args_list}
    # 601: a notice for 2026-10-31; 603: moved out 15 days ago. 605 moved out in
    # 2023, outside the window; 602 and 604 have no move-out.
    assert set(calls) == {880000601, 880000603}
    assert calls[880000601]["move_out_date"] == date(2026, 10, 31)
    assert calls[880000603]["move_out_date"] == date(2026, 9, 20)
    for kwargs in calls.values():
        assert kwargs["only_if_new"] is True and kwargs["pms"] == "rentmanager"
        assert kwargs["company_id"] == COMPANY
    assert result.rows_by_table["helixis_turnovers"] == 2


async def test_an_unchanged_move_out_is_not_opened_again(opened: AsyncMock, run: Run) -> None:
    opened.return_value = False
    result, _db = await run(FakeRentManager())
    assert opened.await_count == 2
    assert result.rows_by_table["helixis_turnovers"] == 0


async def test_the_real_trigger_reopens_nothing_for_an_unchanged_move_out(
    monkeypatch: pytest.MonkeyPatch,
    run: Run,
) -> None:
    """``only_if_new=True`` through the shared repository: its INSERT updates
    only when the date moved, so an unchanged one returns no row and raises
    no card."""
    card = AsyncMock()
    monkeypatch.setattr(turnovers_repo.reminders_repo, "upsert_card", card)
    result, pool = await run(FakeRentManager())
    opens = [s for s, _a in pool.log if s.startswith("INSERT INTO helixis_turnovers")]
    assert len(opens) == 2
    assert all("IS DISTINCT FROM EXCLUDED.move_out_date" in s for s in opens)
    assert result.rows_by_table["helixis_turnovers"] == 0
    card.assert_not_awaited()


@pytest.mark.parametrize(
    "answer",
    [json_response(403, {}), json_response(200, portfolio()["leases"])],
    ids=["leases failed", "leases incomplete"],
)
async def test_leases_that_did_not_land_open_no_turnover(
    opened: AsyncMock, answer: httpx.Response, run: Run
) -> None:
    server = FakeRentManager(overrides={"/Leases": lambda _r: answer})
    result, _db = await run(server)
    assert "helixis_leases" in result.failed
    opened.assert_not_awaited()
    assert "helixis_turnovers" not in result.rows_by_table


async def test_a_turnover_that_fails_never_fails_the_pass(
    opened: AsyncMock, caplog: pytest.LogCaptureFixture, run: Run
) -> None:
    opened.side_effect = [RuntimeError("boom"), True]
    with caplog.at_level(logging.ERROR):
        result, _db = await run(FakeRentManager())
    assert result.rows_by_table["helixis_turnovers"] == 1
    assert "turnover for lease" in caplog.text
    assert result.failed == {}


# ── the shared layer sees this connector ──────────────────────────────


@pytest.mark.parametrize("attr", ["SYNC_STATE_STAGE", "DERIVED_TABLES", "RUN_NOTE", "sync_company"])
def test_the_registry_finds_each_thing_the_shared_layer_reads(attr: str) -> None:
    assert registry.connector_attr("rentmanager", "sync", attr) is getattr(sync, attr)


def test_the_stage_map_is_a_plain_dict() -> None:
    """``pms_coverage._stage_map`` reads anything but a dict as fills nothing."""
    assert type(sync.SYNC_STATE_STAGE) is dict
    assert sync.DERIVED_TABLES == frozenset({"helixis_turnovers"})


def test_coverage_says_rent_manager_fills_exactly_what_a_pass_writes() -> None:
    written = set(
        sync.build_mirror_rows(
            {name: sync.Read(rows=portfolio()[name], complete=True) for name in RESOURCES},
            today=TODAY,
        )
    )
    assert written == set(sync.SYNC_STATE_STAGE)
    assert pms_coverage.fed_tables("rentmanager") == written | {"helixis_turnovers"}
    assert pms_coverage.blind_tables("rentmanager", {"helixis_lease_transactions"}) == {
        "helixis_lease_transactions"
    }


def test_the_stage_names_are_rentvines_and_ones_the_freshness_verdict_reads() -> None:
    from helixis.rentvine.sync import REFERENCE_STAGES
    from helixis.rentvine.sync import SYNC_STATE_STAGE as RENTVINE_STAGES

    for table, stage in sync.SYNC_STATE_STAGE.items():
        assert RENTVINE_STAGES[table] == stage, table
        assert stage in CORE_TABLES or stage in REFERENCE_STAGES, stage


async def test_the_shared_cycle_runs_this_connectors_pass(
    monkeypatch: pytest.MonkeyPatch, opened: AsyncMock
) -> None:
    """The real registry lookup, the real ``sync_one``: the worker's path."""
    server = serve(monkeypatch, FakeRentManager())
    result = await sync_cycle.sync_one(_pool(), "rentmanager", COMPANY)
    assert isinstance(result, SyncResult) and result.failed == {}
    assert server.collection_reads


# ── the run note ──────────────────────────────────────────────────────


def test_the_run_is_given_this_connectors_note() -> None:
    assert run_pms.run_note_for("rentmanager") == sync.RUN_NOTE


def test_the_note_names_rent_manager_and_no_other_system() -> None:
    note = sync.RUN_NOTE
    assert note.startswith("\n\n## This workspace's property-management system\n")
    assert "Rent Manager" in note and "Rent Manager Agent" in note
    for key, found in registry.PMS.items():
        if key != "rentmanager":
            assert found.name not in note, f"the note names {found.name}"
    assert "Helixis" not in note


def test_the_note_never_lets_a_missing_item_read_as_zero() -> None:
    note = sync.RUN_NOTE
    assert "not available in Occupella yet" in note
    assert "never say it is zero, empty or none" in note
    assert "never say the property has no owner" in note
    assert "never give one as a Rent Manager number" in note, "an id is not the customer's number"


#: Every PMS-fed table, and the words the note uses for it.
_NOTE_WORDS = {
    "helixis_properties": "properties",
    "helixis_units": "units",
    "helixis_leases": "leases",
    "helixis_tenants": "tenants",
    "helixis_lease_tenants": "which current tenant is on which lease",
    "helixis_owners": "owners",
    "helixis_vendors": "vendors",
    "helixis_work_orders": "work orders",
    "helixis_bills": "vendor bills",
    "helixis_gl_accounts": "chart of accounts",
    "helixis_lease_balances": "rent balances",
    "helixis_turnovers": "turnovers",
    "helixis_lease_transactions": "rent charges and payments",
    "helixis_bill_lines": "spending by category",
    "helixis_tasks": "tasks",
    "helixis_bank_accounts": "bank account",
    "helixis_applicants": "applicants",
}


def test_the_note_says_what_is_synced_and_what_is_not() -> None:
    """Both sides (rule 15): the note against what coverage says is filled.
    A table filled must be named before "Not available"; one not filled after."""
    note = sync.RUN_NOTE
    synced, missing = note.split("Not available in Occupella yet:", 1)
    fed = pms_coverage.fed_tables("rentmanager")
    for table, words in _NOTE_WORDS.items():
        if table in fed:
            assert words in synced, f"{table} is synced but the note does not say so"
        else:
            assert words in missing, f"{table} is not synced but the note does not say so"
    assert fed <= set(_NOTE_WORDS), "a newly filled table needs its words here"
