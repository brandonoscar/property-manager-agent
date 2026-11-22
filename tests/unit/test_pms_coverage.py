"""Mirror-reading Inbox producers skip what a company's PMS never fills.

The Rentvine sync mirrors seven entities and no ledger transactions. Before
this, the detector cycle ran ``rent_not_collected`` and ``rent_under_charged``
for every company with leases, Rentvine companies included. On a real
Rentvine account both report every active lease as unpaid or never charged,
and the Inbox bridge drafts a message to the tenant about each one.

What is pinned, and why each half needs the other:
* ``PMS_FED_TABLES`` against migration 0101's own list, and Rentvine's set
  against the table the sync stamps. A hand-typed copy is how the two drift.
* Every detector's and producer's declared tables against the SQL it actually
  EXECUTES. A declaration that under-reports is the fail-open direction:
  the reader would look like it needs nothing and run blind.
* The cycles act on it: a Rentvine company runs only what it can feed, a
  Buildium company runs exactly what it did before, and a failed PMS lookup
  skips the company instead of guessing Buildium.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import pathlib
import re
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

import helixis.detectors.runner as runner
from helixis.db import company_pms as company_pms_mod
from helixis.db.pms_coverage import BUILDIUM, FED_BY, PMS_FED_TABLES, RENTVINE, blind_tables
from helixis.detectors import registry
from helixis.detectors.base import Detector
from helixis.reminders import producers, sweep
from helixis.rentvine.sync import SYNC_STATE_STAGE

REPO = pathlib.Path(__file__).resolve().parents[2]
COMPANY = "00000000-0000-0000-0000-0000000000aa"
_TABLE = re.compile(r"(?i)(?:from|join)\s+(?:public\.)?((?:helixis|buildium|crm)_\w+)")


# ── the recording fakes ──────────────────────────────────────────────


class _Recorder:
    """A connection that answers every query with nothing and keeps the SQL."""

    def __init__(self) -> None:
        self.sql: list[str] = []

    async def fetch(self, q: str, *a: Any) -> list[Any]:
        self.sql.append(q)
        return []

    async def fetchrow(self, q: str, *a: Any) -> None:
        self.sql.append(q)
        return None

    async def fetchval(self, q: str, *a: Any) -> int:
        self.sql.append(q)
        return 0

    async def execute(self, q: str, *a: Any) -> str:
        self.sql.append(q)
        return "OK"

    def tables(self) -> frozenset[str]:
        return frozenset(m for q in self.sql for m in _TABLE.findall(q))


class _Pool:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    @contextlib.asynccontextmanager
    async def acquire(self) -> Any:
        yield self.conn

    async def fetch(self, q: str, *a: Any) -> Any:
        return await self.conn.fetch(q, *a)

    async def fetchrow(self, q: str, *a: Any) -> Any:
        return await self.conn.fetchrow(q, *a)


# ── the table lists ──────────────────────────────────────────────────


def _migration_0101_tables() -> set[str]:
    path = next((REPO / "supabase" / "migrations").glob("0101_*.sql"))
    sql = path.read_text(encoding="utf-8")
    block = sql[sql.index("array array[") : sql.index("]", sql.index("array array["))]
    return set(re.findall(r"'(\w+)'", block))


#: Carry 0101's ``pms`` column but are written by Occupella, not a PMS.
_OCCUPELLA_WRITTEN = {"helixis_renewal_offers", "helixis_property_intel"}


def test_pms_fed_tables_are_0101s_list_minus_what_occupella_writes() -> None:
    tables_0101 = _migration_0101_tables()
    assert len(tables_0101) == 21, f"parsed {len(tables_0101)} tables from 0101 — bad anchor?"
    assert _OCCUPELLA_WRITTEN <= tables_0101
    assert PMS_FED_TABLES == (tables_0101 - _OCCUPELLA_WRITTEN) | {"buildium_events"}


def test_rentvine_fills_exactly_what_its_sync_stamps() -> None:
    # Re-pointed 2026-10-02: plus the tables the sync DERIVES (turnovers,
    # opened from the lease export), which are written but never stamped.
    from helixis.rentvine.sync import DERIVED_TABLES

    assert FED_BY[RENTVINE] == frozenset(SYNC_STATE_STAGE) | DERIVED_TABLES
    assert not DERIVED_TABLES & set(SYNC_STATE_STAGE)
    assert FED_BY[RENTVINE] < PMS_FED_TABLES
    assert FED_BY[BUILDIUM] == PMS_FED_TABLES


def test_blind_tables_names_only_pms_fed_gaps() -> None:
    # Re-pointed 2026-10-02: lease transactions sync now; tasks still do not
    # (Rentvine documents no task endpoint).
    reads = {"helixis_leases", "helixis_tasks", "helixis_activity_log"}
    assert blind_tables(RENTVINE, reads) == {"helixis_tasks"}
    assert blind_tables(BUILDIUM, reads) == frozenset()
    # An Occupella-written table is never a reason to decline.
    assert blind_tables(RENTVINE, {"helixis_activity_log", "crm_leads"}) == frozenset()
    # A PMS nobody has mapped fills nothing: every PMS-fed read is blind.
    assert blind_tables("appfolio", reads) == {"helixis_leases", "helixis_tasks"}


# ── declarations match the SQL that actually runs ────────────────────


@pytest.mark.parametrize("det", registry.state_rule_detectors(), ids=lambda d: d.detector_id)
async def test_each_detector_declares_exactly_the_tables_it_reads(det: Detector) -> None:
    conn = _Recorder()
    await det.scan(conn, COMPANY)
    executed = conn.tables()
    assert executed, f"{det.detector_id} executed no SQL the scan could see — bad recorder?"
    assert det.reads_tables == executed, (
        f"{det.detector_id} declares {sorted(det.reads_tables or ())} and reads "
        f"{sorted(executed)}. An under-declared table is one the cycle cannot "
        f"skip for a PMS that never fills it."
    )


def test_every_producer_declares_its_reads() -> None:
    assert set(producers.PRODUCER_READS) == set(producers.PRODUCERS)


@pytest.mark.parametrize("name", sorted(producers.PRODUCERS))
async def test_each_producer_declares_exactly_the_tables_it_reads(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The dark producer self-gates on this allowlist, so it must be on to be read.
    monkeypatch.setenv("HELIXIS_REMINDERS__ENABLED_IDS", name)
    conn = _Recorder()
    await producers.PRODUCERS[name](_Pool(conn), COMPANY, today=dt.date(2026, 9, 24))
    assert producers.PRODUCER_READS[name] == conn.tables()


def test_registration_refuses_a_detector_that_declares_no_reads() -> None:
    class Undeclared(Detector):
        detector_id = "__undeclared__"
        rule_keys = ("__undeclared__",)

        async def scan(self, conn: Any, company_id: str) -> list[Any]:
            return []

    with pytest.raises(ValueError, match="reads_tables"):
        registry.register(Undeclared())


# ── the detector cycle acts on it ────────────────────────────────────


#: Every stage a Rentvine pass can stamp, as ``get_freshness`` returns them.
_ALL_LANDED = {stage: "2026-10-02T00:00:00Z" for stage in SYNC_STATE_STAGE.values()}


async def _run_detectors(
    monkeypatch: pytest.MonkeyPatch, pms: Any, landed: dict[str, Any] | None = None
) -> tuple[set[str], set[str]]:
    ran: list[str] = []
    monkeypatch.setattr(
        runner, "get_freshness", AsyncMock(return_value=_ALL_LANDED if landed is None else landed)
    )

    async def _fake_run(pool: Any, company_id: str, dets: list[Detector], **kw: Any) -> dict:
        ran.extend(d.detector_id for d in dets)
        return {d.detector_id: 0 for d in dets}

    monkeypatch.setattr(runner, "company_pms_strict", pms)
    monkeypatch.setattr(runner, "run_detectors_for_company", _fake_run)
    every = frozenset(d.detector_id for d in registry.state_rule_detectors())
    blind: set[str] = set()
    await runner.run_state_rule_detectors(object(), COMPANY, every, blind=blind)
    return set(ran), blind


async def test_a_rentvine_company_runs_only_what_rentvine_can_feed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran, blind = await _run_detectors(monkeypatch, AsyncMock(return_value=RENTVINE))
    assert not ran & blind
    # Re-pointed 2026-10-01: Rentvine bills sync now (sync._sync_bills), so the
    # duplicate-bill detector moved from blind to run. Re-pointed 2026-10-02:
    # lease transactions sync now, so the two rent detectors run once landed
    # (the test below holds them back until then).
    assert {
        "active_lease_no_rent",
        "weather_alert",
        "promise_to_pay_broken",
        "duplicate_vendor_bill",
        "rent_not_collected",
        "rent_under_charged",
    } <= ran


async def test_a_rentvine_detector_waits_until_the_tables_it_reads_have_landed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rentvine lease transactions fill a few hundred leases a pass. Until the
    stage is stamped, a lease with no Payment row may just be unread, and the
    rent detectors would call that tenant unpaid."""
    landed = {k: v for k, v in _ALL_LANDED.items() if k != "transactions"}
    ran, blind = await _run_detectors(monkeypatch, AsyncMock(return_value=RENTVINE), landed)
    assert {"rent_not_collected", "rent_under_charged"} <= blind
    assert not ran & {"rent_not_collected", "rent_under_charged"}
    # Detectors that read nothing unlanded are untouched.
    assert "duplicate_vendor_bill" in ran


async def test_an_unreadable_sync_state_runs_no_rentvine_detector_that_reads_a_fed_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``get_freshness`` answers {} on any failure: nothing has landed."""
    ran, blind = await _run_detectors(monkeypatch, AsyncMock(return_value=RENTVINE), {})
    assert {"rent_not_collected", "duplicate_vendor_bill"} <= blind


def test_unlanded_tables_never_holds_back_buildium() -> None:
    from helixis.db.pms_coverage import unlanded_tables

    assert unlanded_tables(BUILDIUM, {"helixis_lease_transactions"}, ()) == frozenset()
    assert unlanded_tables(RENTVINE, {"helixis_lease_transactions"}, ()) == {
        "helixis_lease_transactions"
    }
    assert unlanded_tables(RENTVINE, {"helixis_lease_transactions"}, {"transactions"}) == set()
    # A table outside the sync's map (internal, not PMS-fed) is never held back.
    assert unlanded_tables(RENTVINE, {"helixis_activity_log"}, ()) == set()


async def test_a_buildium_company_runs_everything_it_did_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran, blind = await _run_detectors(monkeypatch, AsyncMock(return_value=BUILDIUM))
    assert blind == set()
    assert ran == {d.detector_id for d in registry.state_rule_detectors()}


async def test_a_failed_pms_lookup_runs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Failing open to Buildium would run every money rule blind."""
    with pytest.raises(RuntimeError):
        await _run_detectors(monkeypatch, AsyncMock(side_effect=RuntimeError("db down")))


async def test_company_pms_strict_raises_on_a_value_it_cannot_produce() -> None:
    class _Row(_Recorder):
        def __init__(self, value: Any) -> None:
            super().__init__()
            self.value = value

        async def fetchrow(self, q: str, *a: Any) -> Any:
            return None if self.value is None else {"pms": self.value}

    assert await company_pms_mod.company_pms_strict(_Pool(_Row("rentvine")), COMPANY) == RENTVINE
    assert await company_pms_mod.company_pms_strict(_Pool(_Row(None)), COMPANY) == BUILDIUM
    with pytest.raises(ValueError):
        await company_pms_mod.company_pms_strict(_Pool(_Row("<MagicMock>")), COMPANY)


async def test_company_pms_strict_lets_a_database_error_through() -> None:
    """The lenient ``company_pms`` swallows this and answers Buildium; the strict
    one must not, or the detector cycle runs every money rule blind."""

    class _Down(_Recorder):
        async def fetchrow(self, q: str, *a: Any) -> Any:
            raise ConnectionError("pool exhausted")

    with pytest.raises(ConnectionError):
        await company_pms_mod.company_pms_strict(_Pool(_Down()), COMPANY)


# ── the reminder sweep acts on it ────────────────────────────────────


async def test_a_rentvine_company_gets_only_the_producers_rentvine_can_feed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran: list[str] = []

    def _fake(name: str) -> Any:
        async def produce(pool: Any, company_id: str, **kw: Any) -> list[Any]:
            ran.append(name)
            return []

        return produce

    monkeypatch.setattr(sweep, "PRODUCERS", {n: _fake(n) for n in producers.PRODUCERS})
    monkeypatch.setattr(sweep, "company_pms_strict", AsyncMock(return_value=RENTVINE))
    monkeypatch.setattr(sweep, "company_timezone", AsyncMock(return_value=None))
    monkeypatch.setattr(sweep, "get_freshness", AsyncMock(return_value=_ALL_LANDED))
    monkeypatch.setattr(sweep.reminders_repo, "sync_producer", AsyncMock())
    monkeypatch.setattr(sweep.reminders_repo, "clear_unknown_producers", AsyncMock(return_value=0))
    blind: set[str] = set()
    await sweep.run_reminder_sweep(object(), COMPANY, blind=blind)
    # Re-pointed 2026-10-01: Rentvine bills sync now, so bill_due runs.
    # Re-pointed 2026-10-02: turnovers open from the lease export, so the
    # deposit-return clock can run (it stays dark by default regardless).
    assert set(ran) == {"renewal_window", "lead_cold", "bill_due", "deposit_return_clock"}
    assert blind == {"task_overdue", "webhooks_quiet"}

    # And a producer whose table has not landed waits, same rule as detectors.
    ran.clear()
    blind.clear()
    unlanded = {k: v for k, v in _ALL_LANDED.items() if k != "bills"}
    monkeypatch.setattr(sweep, "get_freshness", AsyncMock(return_value=unlanded))
    await sweep.run_reminder_sweep(object(), COMPANY, blind=blind)
    assert "bill_due" in blind and "bill_due" not in ran


async def test_an_undeclared_producer_is_not_run(monkeypatch: pytest.MonkeyPatch) -> None:
    produce = AsyncMock(return_value=[])
    monkeypatch.setattr(sweep, "PRODUCERS", {"new_one": produce})
    monkeypatch.setattr(sweep, "company_pms_strict", AsyncMock(return_value=BUILDIUM))
    monkeypatch.setattr(sweep, "company_timezone", AsyncMock(return_value=None))
    monkeypatch.setattr(sweep.reminders_repo, "clear_unknown_producers", AsyncMock(return_value=0))
    blind: set[str] = set()
    await sweep.run_reminder_sweep(object(), COMPANY, blind=blind)
    produce.assert_not_awaited()
    assert blind == {"new_one"}


@pytest.mark.parametrize(
    ("enabled", "expected"),
    [(True, ["b1", "r1"]), (False, ["b1"]), (None, ["b1"])],
)
async def test_rentvine_companies_join_the_sweep_only_while_the_connector_is_on(
    monkeypatch: pytest.MonkeyPatch, enabled: bool | None, expected: list[str]
) -> None:
    pop = sweep.company_population
    monkeypatch.setattr(pop, "with_buildium_credentials", AsyncMock(return_value=["b1"]))
    monkeypatch.setattr(pop, "with_rentvine_credentials", AsyncMock(return_value=["r1"]))
    settings = (
        None if enabled is None else SimpleNamespace(rentvine=SimpleNamespace(enabled=enabled))
    )
    assert await sweep._companies_with_mirror(object(), settings) == expected


@pytest.mark.parametrize(
    ("switches", "expected"),
    [
        ({"rentmanager": True}, ["b1", "rm1"]),
        ({"propertyware": True}, ["b1", "pw1"]),
        ({"rentmanager": True, "propertyware": True}, ["b1", "rm1", "pw1"]),
        ({"rentmanager": False, "propertyware": False}, ["b1"]),
        ({}, ["b1"]),
    ],
)
async def test_the_pms_connectors_companies_join_the_sweep_only_while_switched_on(
    monkeypatch: pytest.MonkeyPatch, switches: dict[str, bool], expected: list[str]
) -> None:
    """Rent Manager and Propertyware companies got no Inbox reminder at all:
    the sweep visited Buildium and Rentvine companies only. Rentvine's rule
    now holds for them too: only while that system's switch is on."""
    pop = sweep.company_population
    monkeypatch.setattr(pop, "with_buildium_credentials", AsyncMock(return_value=["b1"]))
    monkeypatch.setattr(pop, "with_rentvine_credentials", AsyncMock(return_value=["r1"]))
    by_system = {"rentmanager": ["rm1"], "propertyware": ["pw1"]}
    asked: list[str] = []

    async def with_pms(_pool: object, key: str) -> list[str]:
        asked.append(key)
        return by_system[key]

    monkeypatch.setattr(pop, "with_pms_credentials", with_pms)
    settings = SimpleNamespace(
        rentvine=SimpleNamespace(enabled=False),
        **{key: SimpleNamespace(enabled=on) for key, on in switches.items()},
    )
    assert await sweep._companies_with_mirror(object(), settings) == expected
    # A switched-off system's credentials table is never even read.
    assert asked == [k for k in ("rentmanager", "propertyware") if switches.get(k)]


def test_both_cycles_actually_ask_the_coverage_module() -> None:
    """A behavioural test drives a helper; this pins that the cycles CALL it.

    Walks the AST, because the docstrings here quote the rule they enforce.
    """
    import ast

    for mod, fn in ((runner, "run_state_rule_detectors"), (sweep, "run_reminder_sweep")):
        tree = ast.parse(pathlib.Path(mod.__file__).read_text(encoding="utf-8"))
        body = next(n for n in ast.walk(tree) if getattr(n, "name", None) == fn)
        called = {
            n.func.id
            for n in ast.walk(body)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        assert {"blind_tables", "company_pms_strict"} <= called, f"{fn} no longer checks coverage"
