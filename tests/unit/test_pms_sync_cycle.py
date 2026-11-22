"""The worker tick for Rent Manager and Propertyware, and the switches it reads.

The behavioural tests drive ``run_pms_sync_cycle`` with the population and the
connector's pass replaced. A test that drives the cycle directly cannot see
that nothing calls it (gotcha 62), so the AST guards at the bottom check the
worker's side: it awaits the cycle, and only when a switch is on.

The switches are deployment kill switches (design P4): ``enabled`` (connect,
sync, reads) and ``writes_enabled`` (write tools). Both ship off, in code and
in ``render.yaml``, and both are read at call time.
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import logging
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml

from helixis.db import company_population
from helixis.pms import registry, sync_cycle
from helixis.pms.mirror import SyncResult
from helixis.settings import PropertywareSettings, RentManagerSettings, Settings

REPO = Path(__file__).resolve().parents[2]
WORKER = REPO / "src" / "helixis" / "webhooks" / "worker.py"
CYCLE = REPO / "src" / "helixis" / "pms" / "sync_cycle.py"
#: Re-pointed 2026-10-06 (rule 19): DoorLoop joined the layer (design/doorloop-connector.md).
TOOLKIT = ("rentmanager", "propertyware", "doorloop")


def _settings(**switches: dict[str, Any]) -> Settings:
    """Settings with some systems' switches set, e.g.
    ``_settings(rentmanager={"enabled": True})``."""
    base = Settings()
    update = {key: getattr(base, key).model_copy(update=values) for key, values in switches.items()}
    return base.model_copy(update=update)


@pytest.fixture
def use(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Serve these settings to everything that asks at call time."""

    def _use(settings: Settings) -> None:
        monkeypatch.setattr("helixis.settings.get_settings", lambda: settings)

    return _use


def _on(*keys: str, **extra: Any) -> Settings:
    return _settings(**{key: {"enabled": True, **extra} for key in keys})


def _result(company_id: str, pms: str = "rentmanager", **kw: Any) -> SyncResult:
    return SyncResult(company_id=company_id, pms=pms, **kw)


class _Connector:
    """Stands in for ``<package>.sync.sync_company`` of each system."""

    def __init__(self, behaviour: Any = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.behaviour = behaviour

    def lookup(self, key: str | None, module: str, attr: str) -> Any:
        assert (module, attr) == ("sync", "sync_company")

        async def _pass(pool: Any, company_id: str) -> Any:
            self.calls.append((str(key), company_id))
            if self.behaviour is not None:
                return self.behaviour(str(key), company_id)
            return _result(company_id, str(key))

        return _pass


@pytest.fixture
def connector(monkeypatch: pytest.MonkeyPatch) -> _Connector:
    found = _Connector()
    monkeypatch.setattr(registry, "connector_attr", found.lookup)
    return found


@pytest.fixture(autouse=True)
def enrichment(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """The geocode and intel steps after a pass, recorded as ``(step,
    company)``: hermetic for every test here, and read by the ones about it."""
    calls: list[tuple[str, str]] = []

    async def _geocode(pool: Any, *, settings: Any, company_id: str) -> int:
        calls.append(("geocode", company_id))
        return 0

    async def _intel(pool: Any, company_id: str, *, settings: Any) -> int:
        calls.append(("intel", company_id))
        return 0

    monkeypatch.setattr(sync_cycle, "geocode_company_properties", _geocode)
    monkeypatch.setattr(sync_cycle, "enrich_company_properties", _intel)
    return calls


@pytest.fixture
def population(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """``{pms: [company ids]}``; each call is recorded under ``"_asked"``."""
    companies: dict[str, list[str]] = {"_asked": []}

    async def _with(pool: Any, key: str) -> list[str]:
        companies["_asked"].append(key)
        return list(companies.get(key, []))

    monkeypatch.setattr(sync_cycle, "with_pms_credentials", _with)
    return companies


# ── the switches ─────────────────────────────────────────────────────────


def test_every_switch_ships_off() -> None:
    for cls in (RentManagerSettings, PropertywareSettings):
        assert cls().enabled is False, f"{cls.__name__}.enabled ships ON"
        assert cls().writes_enabled is False, f"{cls.__name__}.writes_enabled ships ON"
    settings = Settings()
    assert isinstance(settings.rentmanager, RentManagerSettings)
    assert isinstance(settings.propertyware, PropertywareSettings)


def test_off_by_default_through_the_registry(use: Any) -> None:
    use(Settings())
    for key in TOOLKIT:
        assert registry.enabled(key) is False
        assert registry.writes_enabled(key) is False
    assert sync_cycle.switched_on() == ()


def test_writes_need_the_connector_on(use: Any) -> None:
    """``writes_enabled`` alone does nothing: a connector that is off has no
    reads to confirm a write against."""
    use(_settings(rentmanager={"writes_enabled": True}))
    assert registry.writes_enabled("rentmanager") is False
    use(_settings(rentmanager={"enabled": True, "writes_enabled": True}))
    assert registry.writes_enabled("rentmanager") is True
    assert registry.writes_enabled("propertyware") is False


def test_on_means_reads_only_until_writes_are_switched_on(use: Any) -> None:
    use(_on("propertyware"))
    assert registry.enabled("propertyware") is True
    assert registry.writes_enabled("propertyware") is False


def test_buildium_is_always_on_and_neither_older_system_has_a_write_switch(
    use: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    use(_settings(rentvine={"enabled": True}))
    assert registry.enabled("buildium") is True
    assert registry.writes_enabled("buildium") is False
    # Even if Rentvine's settings grew a field with this name, its writes keep
    # their own gate: this switch is for the helixis.pms connectors only.
    monkeypatch.setattr(
        registry, "_switches", lambda key: SimpleNamespace(enabled=True, writes_enabled=True)
    )
    assert registry.writes_enabled("rentvine") is False
    assert registry.writes_enabled("buildium") is False
    assert registry.writes_enabled("rentmanager") is True, "control: the stub does say on"


@pytest.mark.parametrize("key", ["appfolio", "", "RENTMANAGER"])
def test_an_unknown_system_is_off(use: Any, key: str) -> None:
    use(_on("rentmanager", writes_enabled=True))
    assert registry.enabled(key) is False
    assert registry.writes_enabled(key) is False


def test_the_switches_are_read_at_call_time(use: Any) -> None:
    """A redeploy (or a test) flips the switch; nothing may cache the old value."""
    use(_on("rentmanager"))
    assert sync_cycle.switched_on() == ("rentmanager",)
    use(Settings())
    assert sync_cycle.switched_on() == ()
    use(_on("rentmanager", "propertyware"))
    assert sync_cycle.switched_on() == ("rentmanager", "propertyware")


def test_the_cycle_drives_only_the_toolkit_systems() -> None:
    """Buildium has its own mirror sync and Rentvine its own cycle (design P1)."""
    assert sync_cycle.systems() == TOOLKIT


# ── who is connected ─────────────────────────────────────────────────────


class _Conn:
    def __init__(self, rows: list[dict[str, str]]) -> None:
        self.rows = rows
        self.sql: list[str] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, str]]:
        self.sql.append(sql)
        return self.rows


class _Acquire:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn

    async def __aenter__(self) -> _Conn:
        return self.conn

    async def __aexit__(self, *exc: object) -> None:
        return None


class _Pool:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn

    def acquire(self) -> _Acquire:
        return _Acquire(self.conn)


@pytest.mark.parametrize("key", TOOLKIT)
def test_the_population_reads_the_registry_credentials_table(key: str) -> None:
    conn = _Conn([{"company_id": "c1"}, {"company_id": "c2"}])
    got = asyncio.run(company_population.with_pms_credentials(_Pool(conn), key))  # type: ignore[arg-type]
    assert got == ["c1", "c2"]
    assert len(conn.sql) == 1
    table = registry.PMS[key].credentials_table
    assert f"FROM public.{table}" in conn.sql[0]


@pytest.mark.parametrize("key", ["appfolio", "x; DROP TABLE companies", ""])
def test_the_population_refuses_a_name_that_is_not_a_system(key: str) -> None:
    """The table name comes from the registry, never from the caller's string."""
    conn = _Conn([])
    with pytest.raises(ValueError):
        asyncio.run(company_population.with_pms_credentials(_Pool(conn), key))  # type: ignore[arg-type]
    assert conn.sql == []


# ── the tick ─────────────────────────────────────────────────────────────


async def test_a_dark_deployment_does_not_even_ask_which_companies_exist(
    use: Any, population: dict[str, list[str]], connector: _Connector
) -> None:
    use(Settings())
    population["rentmanager"] = ["c1"]
    assert await sync_cycle.run_pms_sync_cycle(MagicMock()) == {}
    assert population["_asked"] == []
    assert connector.calls == []


async def test_only_a_switched_on_system_is_synced(
    use: Any, population: dict[str, list[str]], connector: _Connector
) -> None:
    use(_on("rentmanager"))
    population["rentmanager"] = ["c1", "c2"]
    population["propertyware"] = ["p1"]
    out = await sync_cycle.run_pms_sync_cycle(MagicMock())
    assert population["_asked"] == ["rentmanager"]
    assert connector.calls == [("rentmanager", "c1"), ("rentmanager", "c2")]
    assert set(out) == {("rentmanager", "c1"), ("rentmanager", "c2")}
    assert all(isinstance(r, SyncResult) for r in out.values())


async def test_a_system_switched_on_before_its_connector_exists_does_nothing(
    use: Any,
    population: dict[str, list[str]],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real lookup, not a stand-in: a system whose connector package does
    not exist, switched on, must sync nothing, loudly.

    ⚠ Re-pointed 2026-10-05 (rule 19), not relaxed: this relied on
    ``helixis.rentmanager`` not being built. The Rent Manager branch builds
    it, so the missing package is stood up here: Rent Manager's own entry,
    pointed at a package that does not exist, through the real lookup."""
    missing = dataclasses.replace(registry.PMS["rentmanager"], package="helixis.no_such_connector")
    monkeypatch.setattr(registry, "PMS", {**registry.PMS, "rentmanager": missing})
    use(_on("rentmanager"))
    population["rentmanager"] = ["c1"]
    with caplog.at_level(logging.WARNING, logger=sync_cycle.__name__):
        out = await sync_cycle.run_pms_sync_cycle(MagicMock())
    assert out == {}
    assert "connector is not built" in caplog.text
    assert sync_cycle._running == {}


async def test_a_second_tick_inside_the_interval_syncs_nobody(
    use: Any, population: dict[str, list[str]], connector: _Connector
) -> None:
    use(_on("rentmanager"))
    population["rentmanager"] = ["c1"]
    await sync_cycle.run_pms_sync_cycle(MagicMock())
    assert await sync_cycle.run_pms_sync_cycle(MagicMock()) == {}
    assert connector.calls == [("rentmanager", "c1")]


async def test_the_interval_comes_from_the_settings(
    use: Any, population: dict[str, list[str]], connector: _Connector
) -> None:
    """Started 2 hours ago: due on a 1-hour interval, not on the 4-hour default."""
    population["rentmanager"] = ["c1"]
    sync_cycle._last_synced[("rentmanager", "c1")] = time.monotonic() - 2 * 3600

    use(_on("rentmanager"))
    assert await sync_cycle.run_pms_sync_cycle(MagicMock()) == {}

    use(_on("rentmanager", sync_interval_hours=1.0))
    out = await sync_cycle.run_pms_sync_cycle(MagicMock())
    assert set(out) == {("rentmanager", "c1")}


async def test_the_tick_budget_caps_each_system_and_the_rest_go_next(
    use: Any, population: dict[str, list[str]], connector: _Connector
) -> None:
    """Rule 13: one tick's length is bounded in code, not by how many
    companies exist. The ones left over are first in line next tick."""
    use(_on("rentmanager", "propertyware"))
    many = [f"c{i}" for i in range(sync_cycle.COMPANIES_PER_TICK + 2)]
    population["rentmanager"] = many
    population["propertyware"] = ["p1"]

    first = await sync_cycle.run_pms_sync_cycle(MagicMock())
    synced = [cid for pms, cid in first if pms == "rentmanager"]
    assert synced == many[: sync_cycle.COMPANIES_PER_TICK]
    assert ("propertyware", "p1") in first, "one system's budget is not another's"

    second = await sync_cycle.run_pms_sync_cycle(MagicMock())
    assert [cid for _pms, cid in second] == many[sync_cycle.COMPANIES_PER_TICK :]


async def test_a_company_already_syncing_is_not_started_twice(
    use: Any, population: dict[str, list[str]], connector: _Connector
) -> None:
    """An on-demand sync holds the slot; the tick skips that company and
    does not spend budget on it."""
    use(_on("rentmanager"))
    population["rentmanager"] = ["c1", "c2"]
    held = sync_cycle.claim("rentmanager", "c1")
    assert held is not None

    out = await sync_cycle.run_pms_sync_cycle(MagicMock())

    assert connector.calls == [("rentmanager", "c2")]
    assert set(out) == {("rentmanager", "c2")}
    assert sync_cycle._running == {("rentmanager", "c1"): held}, (
        "the tick freed a slot it never held"
    )
    assert ("rentmanager", "c1") not in sync_cycle._last_synced, (
        "a company the tick skipped was marked as synced, so it would wait a full interval"
    )


def test_claim_is_one_per_company_per_system() -> None:
    first = sync_cycle.claim("rentmanager", "c1")
    assert first is not None
    assert sync_cycle.claim("rentmanager", "c1") is None
    assert sync_cycle.claim("propertyware", "c1") is not None, "systems do not share a slot"
    sync_cycle.release("c1", first)
    assert first.finished_at is not None
    assert sync_cycle.claim("rentmanager", "c1") is not None


def test_a_stale_run_cannot_free_a_newer_one() -> None:
    old = sync_cycle.claim("rentmanager", "c1")
    assert old is not None
    sync_cycle.release("c1", old)
    new = sync_cycle.claim("rentmanager", "c1")
    sync_cycle.release("c1", old)
    assert sync_cycle._running.get(("rentmanager", "c1")) is new


@pytest.mark.parametrize("error", [RuntimeError("boom"), SystemExit(1)])
async def test_one_company_raising_does_not_abandon_the_others(
    use: Any,
    population: dict[str, list[str]],
    connector: _Connector,
    error: BaseException,
) -> None:
    def _behaviour(pms: str, company_id: str) -> SyncResult:
        if company_id == "c1":
            raise error
        return _result(company_id, pms)

    connector.behaviour = _behaviour
    use(_on("rentmanager"))
    population["rentmanager"] = ["c1", "c2"]

    out = await sync_cycle.run_pms_sync_cycle(MagicMock())

    assert set(out) == {("rentmanager", "c2")}
    assert sync_cycle._running == {}, "a failed run kept its slot, so the company never syncs again"


async def test_a_pass_that_ran_geocodes_and_enriches_its_company_and_no_other(
    use: Any,
    population: dict[str, list[str]],
    connector: _Connector,
    enrichment: list[tuple[str, str]],
) -> None:
    """Rentvine's cycle ran these after each pass; this one never did, so no
    Rent Manager or Propertyware property had a point or a jurisdiction:
    "what's within 5 miles" answered "none" and every local-law question
    said to check back later."""
    connector.behaviour = lambda pms, company_id: (
        None if company_id == "c2" else _result(company_id, pms)
    )
    use(_on("rentmanager", "propertyware"))
    population["rentmanager"] = ["c1", "c2"]
    population["propertyware"] = ["p1"]

    await sync_cycle.run_pms_sync_cycle(MagicMock())

    # The order matters: the intel reads the point the geocode stores.
    assert enrichment == [("geocode", "c1"), ("intel", "c1"), ("geocode", "p1"), ("intel", "p1")]


@pytest.mark.parametrize("error", [RuntimeError("maps down"), SystemExit(1)])
async def test_a_failed_enrichment_never_stops_the_cycle(
    use: Any,
    population: dict[str, list[str]],
    connector: _Connector,
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
) -> None:
    async def _boom(*_a: Any, **_k: Any) -> int:
        raise error

    monkeypatch.setattr(sync_cycle, "geocode_company_properties", _boom)
    monkeypatch.setattr(sync_cycle, "enrich_company_properties", _boom)
    use(_on("rentmanager"))
    population["rentmanager"] = ["c1", "c2"]

    out = await sync_cycle.run_pms_sync_cycle(MagicMock())

    assert set(out) == {("rentmanager", "c1"), ("rentmanager", "c2")}


async def test_a_failed_company_waits_for_its_interval(
    use: Any, population: dict[str, list[str]], connector: _Connector
) -> None:
    """Marked before the run, so a company that fails every time cannot turn
    each hourly tick into a full crawl of a customer's API budget."""

    def _fail(pms: str, company_id: str) -> SyncResult:
        raise RuntimeError("down")

    connector.behaviour = _fail
    use(_on("rentmanager"))
    population["rentmanager"] = ["c1"]
    await sync_cycle.run_pms_sync_cycle(MagicMock())
    await sync_cycle.run_pms_sync_cycle(MagicMock())
    assert connector.calls == [("rentmanager", "c1")]


async def test_a_pass_that_returns_something_else_is_refused(
    use: Any,
    population: dict[str, list[str]],
    connector: _Connector,
    caplog: pytest.LogCaptureFixture,
) -> None:
    connector.behaviour = lambda pms, cid: {"rows": 3}
    use(_on("rentmanager"))
    population["rentmanager"] = ["c1"]
    with caplog.at_level(logging.ERROR, logger=sync_cycle.__name__):
        out = await sync_cycle.run_pms_sync_cycle(MagicMock())
    assert out == {}
    assert "not a SyncResult" in caplog.text


async def test_an_unreadable_population_skips_only_that_system(
    use: Any, monkeypatch: pytest.MonkeyPatch, connector: _Connector
) -> None:
    async def _with(pool: Any, key: str) -> list[str]:
        if key == "rentmanager":
            raise ConnectionError("pool closed")
        return ["p1"]

    monkeypatch.setattr(sync_cycle, "with_pms_credentials", _with)
    use(_on("rentmanager", "propertyware"))
    out = await sync_cycle.run_pms_sync_cycle(MagicMock())
    assert set(out) == {("propertyware", "p1")}


async def test_sync_one_refuses_a_switched_off_system(use: Any, connector: _Connector) -> None:
    """The tick checks the switch, and so does the per-company pass: an
    on-demand caller reaches ``sync_one`` without going through the tick."""
    use(Settings())
    assert await sync_cycle.sync_one(MagicMock(), "rentmanager", "c1") is None
    assert connector.calls == []


async def test_stale_tables_and_unwritten_rows_reach_the_log(
    use: Any, connector: _Connector, caplog: pytest.LogCaptureFixture
) -> None:
    """Fields computed and read by nobody are gotcha 62's shape: ``failed``
    (stale, not empty) and ``skipped_no_id`` must both reach a log line."""
    connector.behaviour = lambda pms, cid: _result(
        cid,
        pms,
        failed={"helixis_leases": "HTTP 503"},
        skipped_no_id={"helixis_tenants": 2},
        pruned={"helixis_units": 1},
    )
    use(_on("rentmanager"))
    with caplog.at_level(logging.INFO, logger=sync_cycle.__name__):
        result = await sync_cycle.sync_one(MagicMock(), "rentmanager", "c1")
    assert result is not None
    assert "STALE, not empty" in caplog.text and "helixis_leases: HTTP 503" in caplog.text
    assert "NOT written" in caplog.text and "helixis_tenants=2" in caplog.text
    assert "helixis_units=1" in caplog.text


@pytest.mark.parametrize(
    ("rows", "delta", "error"),
    [
        ({}, set(), True),  # nothing answered
        ({"helixis_units": 0}, {"helixis_units"}, False),  # a quiet incremental read answered
        ({"helixis_units": 4}, set(), False),  # rows came back
    ],
    ids=["nothing-answered", "quiet-delta", "rows"],
)
async def test_a_pass_where_nothing_answered_is_an_error_naming_its_tables(
    use: Any,
    connector: _Connector,
    caplog: pytest.LogCaptureFixture,
    rows: dict[str, int],
    delta: set[str],
    error: bool,
) -> None:
    """The STALE line is a WARNING, which Sentry keeps only as a breadcrumb; a
    pass failing every table every day must reach Sentry as an ERROR. A quiet
    incremental read is a table that answered, so it never trips it."""
    connector.behaviour = lambda pms, cid: _result(
        cid,
        pms,
        rows_by_table=dict(rows),
        delta=set(delta),
        failed={"helixis_leases": "HTTP 401 for tenant Jane Roe", "helixis_tenants": "HTTP 401"},
    )
    use(_on("rentmanager"))
    with caplog.at_level(logging.INFO, logger=sync_cycle.__name__):
        await sync_cycle.sync_one(MagicMock(), "rentmanager", "c1")
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    if not error:
        assert errors == []
        return
    assert len(errors) == 1
    assert "rentmanager" in errors[0] and "helixis_leases, helixis_tenants" in errors[0]
    assert "Jane" not in errors[0] and "401" not in errors[0]


@pytest.mark.parametrize("result_type", ["pms", "rentvine"])
def test_fetched_nothing_means_no_table_answered(result_type: str) -> None:
    """Both result types answer the same way (rule 15: the two cycles read it)."""
    from helixis.rentvine.sync import SyncResult as RentvineResult

    def make(**kw: Any) -> Any:
        if result_type == "pms":
            return _result("c1", "rentmanager", **kw)
        return RentvineResult(company_id="c1", **kw)

    assert make(failed={"a": "x"}).fetched_nothing
    assert not make().fetched_nothing  # an empty account that answered
    assert not make(failed={"a": "x"}, rows_by_table={"b": 0}).fetched_nothing
    # Rows written before a table's late step failed are still rows.
    assert not make(failed={"a": "x"}, rows_by_table={"a": 2}).fetched_nothing


# ── a sync started for a page (Sync now, and the first sync after a test) ──


async def test_a_page_sync_runs_in_the_background_and_reports_how_it_ended(
    use: Any, connector: _Connector
) -> None:
    use(_on("rentmanager"))
    run = sync_cycle.start_now(MagicMock(), "rentmanager", "c1")
    assert run.finished_at is None
    assert sync_cycle.sync_status("rentmanager", "c1") is run, "the running sync is the status"
    await asyncio.gather(*sync_cycle._background)
    assert run.finished_at is not None and isinstance(run.result, SyncResult)
    assert connector.calls == [("rentmanager", "c1")]
    assert sync_cycle._running == {}
    assert sync_cycle.sync_status("rentmanager", "c1") is run, "how it ended is kept"
    assert sync_cycle._background == set(), "a finished task is let go"


async def test_a_second_click_waits_on_the_running_sync(use: Any, connector: _Connector) -> None:
    """Never a second crawl of the same account side by side."""
    use(_on("rentmanager"))
    first = sync_cycle.start_now(MagicMock(), "rentmanager", "c1")
    again = sync_cycle.start_now(MagicMock(), "rentmanager", "c1")
    assert again is first
    await asyncio.gather(*sync_cycle._background)
    assert connector.calls == [("rentmanager", "c1")]


def test_claim_or_join_hands_back_the_run_already_going() -> None:
    first, started = sync_cycle.claim_or_join("rentmanager", "c1")
    assert started
    again, started_again = sync_cycle.claim_or_join("rentmanager", "c1")
    assert again is first and not started_again
    other, other_started = sync_cycle.claim_or_join("propertyware", "c1")
    assert other_started and other is not first, "systems do not share a slot"


def test_the_status_is_the_running_sync_before_the_last_one_that_ended() -> None:
    assert sync_cycle.sync_status("rentmanager", "c1") is None, "nothing known since a restart"
    done = sync_cycle.claim("rentmanager", "c1")
    assert done is not None
    sync_cycle.release("c1", done)
    assert sync_cycle.sync_status("rentmanager", "c1") is done
    now = sync_cycle.claim("rentmanager", "c1")
    assert sync_cycle.sync_status("rentmanager", "c1") is now


def test_a_sync_that_cannot_start_frees_its_slot_and_reads_as_ended(
    use: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Left held, every later click would wait on a run that never ends."""
    use(_on("rentmanager"))

    def _refuse(coro: Any) -> Any:
        raise RuntimeError("no running event loop")

    monkeypatch.setattr(sync_cycle.asyncio, "create_task", _refuse)
    run = sync_cycle.start_now(MagicMock(), "rentmanager", "c1")
    assert run.finished_at is not None and run.result is None
    assert sync_cycle._running == {}
    assert sync_cycle.sync_status("rentmanager", "c1") is run


async def test_a_cancelled_sync_still_frees_its_slot(
    use: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deploy stopping the process cancels the task; the slot must not stay held."""
    use(_on("rentmanager"))
    started = asyncio.Event()

    async def _forever(pool: Any, pms: str, company_id: str) -> SyncResult | None:
        started.set()
        await asyncio.Event().wait()
        return None

    monkeypatch.setattr(sync_cycle, "sync_one", _forever)
    run = sync_cycle.start_now(MagicMock(), "rentmanager", "c1")
    await started.wait()
    for task in list(sync_cycle._background):
        task.cancel()
    await asyncio.gather(*sync_cycle._background, return_exceptions=True)
    assert run.finished_at is not None and run.result is None
    assert sync_cycle._running == {}


# ── the wiring (a behavioural test cannot see an absent caller) ─────────


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _assigns_to(node: ast.AST, name: str) -> list[ast.Assign]:
    return [
        sub
        for sub in ast.walk(node)
        if isinstance(sub, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == name for t in sub.targets)
    ]


def test_the_worker_actually_runs_the_cycle() -> None:
    """A ``Call`` that is run, not a mention (gotchas 94/128/141/143).

    Re-pointed 2026-10-06: the cycle no longer runs inline in the claim loop
    (it held every claim for a whole pass). The loop hands a call of it to
    ``_SingleFlight.start``, and the runner awaits what it was handed.
    ``test_worker_background_cycles`` pins the runner's behaviour.
    """
    tree = _tree(WORKER)
    started = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "start"
        and node.args
        and isinstance(node.args[0], ast.Lambda)
        and isinstance(node.args[0].body, ast.Call)
        and isinstance(node.args[0].body.func, ast.Name)
        and node.args[0].body.func.id == "run_pms_sync_cycle"
    ]
    assert started, "webhooks/worker.py never hands run_pms_sync_cycle to the runner"

    runner = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "_SingleFlight"
    )
    awaited = [
        node
        for node in ast.walk(runner)
        if isinstance(node, ast.Await)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "cycle"
    ]
    assert awaited, "the runner never awaits the cycle it was handed"


def test_the_worker_arms_the_cycle_only_when_a_switch_is_on() -> None:
    """``run_pms_sync_cycle`` starts as None and gets the real cycle only under
    ``if switched_on:``, where ``switched_on`` is the cycle module's own
    ``switched_on()``: one definition of which systems exist."""
    tree = _tree(WORKER)
    armed = [
        a
        for a in _assigns_to(tree, "run_pms_sync_cycle")
        if not (isinstance(a.value, ast.Constant) and a.value.value is None)
    ]
    assert len(armed) == 1, "the cycle must be armed in exactly one place"

    gates = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Name)
        and node.test.id == "switched_on"
        and any(sub is armed[0] for sub in ast.walk(node))
    ]
    assert gates, "the cycle is armed outside `if switched_on:`"

    sources = _assigns_to(tree, "switched_on")
    assert sources and all(
        isinstance(a.value, ast.Call)
        and isinstance(a.value.func, ast.Attribute)
        and a.value.func.attr == "switched_on"
        for a in sources
    ), "`switched_on` in the worker must come from pms.sync_cycle.switched_on()"


def test_the_worker_imports_no_connector_package() -> None:
    """A connector's package loads lazily inside ``sync_one`` (through
    ``registry.connector_attr``), so a dark deployment never imports one."""
    packages = {registry.PMS[key].package for key in TOOLKIT}
    for node in ast.walk(_tree(WORKER)):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert not any(node.module.startswith(p) for p in packages), node.module
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not any(alias.name.startswith(p) for p in packages), alias.name


def _docstrings(tree: ast.Module) -> set[int]:
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                out.add(id(body[0].value))
    return out


def test_the_cycle_reads_the_population_from_the_one_home() -> None:
    """``db/company_population.py`` exists because cycles that each derived
    their own company list drifted apart (gotcha 62)."""
    tree = _tree(CYCLE)
    assert any(
        isinstance(n, ast.ImportFrom)
        and n.module == "helixis.db.company_population"
        and any(a.name == "with_pms_credentials" for a in n.names)
        for n in ast.walk(tree)
    )
    skip = _docstrings(tree)
    literals = [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in skip
    ]
    assert not [s for s in literals if "_credentials" in s or "select" in s.lower()], (
        "the cycle carries its own population SQL"
    )


# ── declared in render.yaml (gotchas 29/80) ──────────────────────────────

_SERVICES = ("occupella-api", "helixis-webhooks-worker")
_FLAGS = [
    (key, field, f"HELIXIS_{key.upper()}__{field.upper()}")
    for key in TOOLKIT
    for field in ("enabled", "writes_enabled")
]


def _env(service_name: str) -> dict[str, str]:
    doc = yaml.safe_load((REPO / "render.yaml").read_text(encoding="utf-8"))
    services = {s["name"]: s for s in doc["services"]}
    assert service_name in services, f"{service_name} left render.yaml; re-point this test"
    return {
        e["key"]: str(e.get("value", ""))
        for e in services[service_name].get("envVars", [])
        if isinstance(e, dict) and "key" in e
    }


@pytest.mark.parametrize(("key", "field", "var"), _FLAGS)
def test_each_switch_is_declared_off_on_both_services(key: str, field: str, var: str) -> None:
    """Declared, so "is Rent Manager live?" is answerable from the repo; the
    same on web and worker, because half-on is silent in both directions;
    and off, because turning a connector on is a reviewable diff."""
    values = {name: _env(name).get(var) for name in _SERVICES}
    assert values == dict.fromkeys(_SERVICES, "false"), values


@pytest.mark.parametrize(("key", "field", "var"), _FLAGS)
def test_the_declared_name_reaches_the_setting(
    monkeypatch: pytest.MonkeyPatch, key: str, field: str, var: str
) -> None:
    """Both sides of the claim (gotcha 28): the name render.yaml declares is
    the name Settings reads, and the declared value is the code default."""
    assert getattr(Settings(), key).model_dump()[field] is False
    monkeypatch.setenv(var, "true")
    assert getattr(Settings(), key).model_dump()[field] is True
