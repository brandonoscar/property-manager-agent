"""Phase 8 for Propertyware (``scripts/propertyware_phase8.py``), run against
a fake Propertyware and a fake read-only mirror.

The mirror starts as exactly what the sync's mapping of the fixtures writes,
company- and system-scoped, beside rows of another company and another system
that must never be counted. Then one thing at a time is broken, and Phase 8
must name it by table, record id and column, with no value in the report.
The fake database answers only the SELECT shapes Phase 8 is allowed: a
statement outside a READ ONLY transaction, or one that does not name the
company (and, on a mirror table, the system), fails the test.
"""

from __future__ import annotations

import contextlib
import copy
import importlib.util
import json
import re
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from helixis.propertyware import client
from helixis.propertyware.credentials import PropertywareCredentials
from tests.unit._propertyware_fake import (
    CLIENT_ID,
    CLIENT_SECRET,
    COMPANY,
    SYSTEM_ID,
    FakePropertyware,
)


def load_script(name: str) -> ModuleType:
    """A script under ``scripts/`` as a module (they are not a package)."""
    path = Path(__file__).resolve().parents[2] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_{name}", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"_{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


phase8 = load_script("propertyware_phase8")

OTHER_COMPANY = "00000000-0000-4000-8000-0000000000c9"
CREDS = PropertywareCredentials(
    company_id=COMPANY, system_id=SYSTEM_ID, client_id=CLIENT_ID, client_secret=CLIENT_SECRET
)

_MIRROR_SQL = re.compile(
    r"^SELECT (?P<cols>.+?) FROM (?P<table>helixis_\w+) "
    r"WHERE company_id = \$1::uuid AND pms = \$2"
    r"(?: AND (?P<key>\w+) = ANY\(\$3::bigint\[\]\))?$"
)


def _norm(sql: str) -> str:
    return " ".join(sql.split())


class Conn:
    def __init__(self, db: MirrorDb):
        self.db = db
        self.readonly = False

    @contextlib.asynccontextmanager
    async def transaction(self, *, readonly: bool = False) -> AsyncIterator[None]:
        self.readonly = readonly
        try:
            yield
        finally:
            self.readonly = False

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        return self.db.answer(self, _norm(sql), args)

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        rows = self.db.answer(self, _norm(sql), args)
        return rows[0] if rows else None


class MirrorDb:
    """A company's mirror, as Postgres hands it to Phase 8's SELECTs."""

    def __init__(self, tables: dict[str, list[dict[str, Any]]]):
        self.tables = tables
        self.credentials = {
            COMPANY: {"system_id": SYSTEM_ID, "client_id_last4": CLIENT_ID[-4:]},
        }
        self.stamps = [
            {
                "company_id": COMPANY,
                "table_name": "leases",
                "synced_at": datetime(2026, 10, 5, 9, 0, tzinfo=UTC),
                "source": "propertyware_sync",
            },
            {
                "company_id": OTHER_COMPANY,
                "table_name": "leases",
                "synced_at": datetime(2026, 10, 5, 9, 0, tzinfo=UTC),
                "source": "propertyware_sync",
            },
        ]
        self.statements: list[str] = []

    @contextlib.asynccontextmanager
    async def acquire(self) -> AsyncIterator[Conn]:
        yield Conn(self)

    def answer(self, conn: Conn, sql: str, args: tuple[Any, ...]) -> list[dict[str, Any]]:
        assert conn.readonly, f"a statement outside a READ ONLY transaction: {sql}"
        assert sql.startswith("SELECT "), sql
        self.statements.append(sql)
        if "FROM public.company_propertyware_credentials" in sql:
            assert sql.endswith("WHERE company_id = $1::uuid"), sql
            row = self.credentials.get(args[0])
            return [dict(row)] if row else []
        if "FROM helixis_mirror_sync_state" in sql:
            assert sql.endswith("WHERE company_id = $1::uuid"), sql
            return [
                {k: v for k, v in s.items() if k != "company_id"}
                for s in self.stamps
                if s["company_id"] == args[0]
            ]
        found = _MIRROR_SQL.match(sql)
        assert found, f"a mirror read not scoped to the company and the system: {sql}"
        cols = [c.strip() for c in found["cols"].split(",")]
        out = []
        for row in self.tables.get(found["table"], []):
            if row["company_id"] != args[0] or row["pms"] != args[1]:
                continue
            if found["key"] and row.get(found["key"]) not in args[2]:
                continue
            out.append({c: row.get(c) for c in cols})
        return out


async def _expected_tables() -> dict[str, list[dict[str, Any]]]:
    """What the sync's mapping of the fixtures writes, as mirror rows of this
    company and system, beside another company's and another system's."""
    async with client.build_client(CREDS, http=FakePropertyware().http()) as pw:
        expected, _raw = await phase8.read_propertyware(pw)
    tables: dict[str, list[dict[str, Any]]] = {}
    for table in phase8.TABLES:
        rows = [
            {**copy.deepcopy(r), "company_id": COMPANY, "pms": "propertyware"}
            for r in expected[table].rows
        ]
        noise = [
            {**copy.deepcopy(r), "company_id": OTHER_COMPANY, "pms": "propertyware"}
            for r in expected[table].rows[:1]
        ] + [
            {**copy.deepcopy(r), "company_id": COMPANY, "pms": "rentvine"}
            for r in expected[table].rows[:1]
        ]
        tables[table] = rows + noise
    return tables


async def _phase8(
    db: MirrorDb, server: FakePropertyware | None = None, creds: PropertywareCredentials = CREDS
) -> tuple[dict[str, Any], int, FakePropertyware]:
    server = server or FakePropertyware()
    async with client.build_client(creds, http=server.http()) as pw:
        report, code = await phase8.run(phase8.ReadOnlyPool(db), pw, COMPANY, creds, sample=50)
    return report, code, server


def _row(db: MirrorDb, table: str, key: str, value: int) -> dict[str, Any]:
    return next(
        r
        for r in db.tables[table]
        if r.get(key) == value and r["company_id"] == COMPANY and r["pms"] == "propertyware"
    )


# ── reconciled ────────────────────────────────────────────────────────────


async def test_a_mirror_that_holds_what_propertyware_holds_reconciles() -> None:
    db = MirrorDb(await _expected_tables())
    report, code, _server = await _phase8(db)
    assert code == 0, phase8.render(report)
    assert report["verdict"] == "RECONCILED"
    tables = report["tables"]
    assert set(tables) == set(phase8.TABLES)
    for table, entry in tables.items():
        assert entry["api"] == entry["mirror"], table
        assert entry["issues"] == [], (table, entry["issues"])
    # The other company's and the other system's rows were never counted.
    assert tables["helixis_leases"]["mirror"] == tables["helixis_leases"]["api"]
    assert tables["helixis_leases"]["sampled"] == tables["helixis_leases"]["api"]


async def test_every_table_the_sync_fills_is_compared_and_nothing_else() -> None:
    from helixis.propertyware import sync

    assert set(phase8.TABLES) == set(sync.SYNC_STATE_STAGE)
    assert not set(phase8.TABLES) & set(sync.DERIVED_TABLES)
    assert set(phase8.SOURCE_LIST) == set(phase8.TABLES)
    assert set(phase8.SOURCE_LIST.values()) <= set(client.READS)


# ── each kind of difference, named, never valued ──────────────────────────


async def test_a_row_the_mirror_lacks_is_named() -> None:
    db = MirrorDb(await _expected_tables())
    gone = _row(db, "helixis_leases", "buildium_id", 990600003)
    db.tables["helixis_leases"].remove(gone)
    report, code, _ = await _phase8(db)
    entry = report["tables"]["helixis_leases"]
    assert code == 1 and report["verdict"] == "NOT RECONCILED"
    assert entry["only_in_api"] == {"count": 1, "ids": [990600003]}
    assert entry["api"] == entry["mirror"] + 1


async def test_a_row_propertyware_no_longer_has_is_named() -> None:
    db = MirrorDb(await _expected_tables())
    extra = copy.deepcopy(_row(db, "helixis_units", "buildium_id", 990200001))
    extra["buildium_id"] = 990209999
    db.tables["helixis_units"].append(extra)
    report, code, _ = await _phase8(db)
    assert code == 1
    assert report["tables"]["helixis_units"]["only_in_mirror"] == {"count": 1, "ids": [990209999]}


async def test_a_field_that_differs_is_named_by_column_and_never_valued() -> None:
    db = MirrorDb(await _expected_tables())
    _row(db, "helixis_leases", "buildium_id", 990600001)["rent"] = 4321.98
    report, code, _ = await _phase8(db)
    diffs = report["tables"]["helixis_leases"]["field_differences"]
    assert code == 1
    assert diffs == [
        {"id": 990600001, "column": "rent", "propertyware": "number", "mirror": "number"}
    ]
    assert "4321" not in json.dumps(report, default=str)


async def test_a_stale_balance_and_its_aging_are_named() -> None:
    db = MirrorDb(await _expected_tables())
    owing = _row(db, "helixis_lease_balances", "lease_id", 990600001)
    owing["total_balance"] = 99.0
    owing["balance_0_30"] = None
    report, _code, _ = await _phase8(db)
    columns = {d["column"] for d in report["tables"]["helixis_lease_balances"]["field_differences"]}
    assert columns == {"total_balance", "aging stated"}


async def test_a_list_propertyware_did_not_answer_is_unknown_never_zero() -> None:
    db = MirrorDb(await _expected_tables())
    report, code, _ = await _phase8(db, FakePropertyware(fail={"/vendors": 403}))
    entry = report["tables"]["helixis_vendors"]
    assert code == 1
    assert entry["api"] is None and entry["api_rows_read"] is None
    assert entry["issues"][0].startswith("UNKNOWN")
    assert "403 (permission)" in entry["issues"][0]
    # Propertyware's own text can echo what was sent: never in the report.
    assert "synthetic 403" not in json.dumps(report, default=str)


async def test_a_list_whose_total_moved_is_unknown_never_compared() -> None:
    db = MirrorDb(await _expected_tables())
    report, _code, _ = await _phase8(db, FakePropertyware(totals={"/units": 99}))
    entry = report["tables"]["helixis_units"]
    assert entry["api"] is None and "X-Total-Count" in entry["issues"][0]


# ── refusals ──────────────────────────────────────────────────────────────


async def test_another_accounts_keys_are_refused_before_any_call() -> None:
    db = MirrorDb(await _expected_tables())
    db.credentials[COMPANY] = {"system_id": "990000777", "client_id_last4": CLIENT_ID[-4:]}
    report, code, server = await _phase8(db)
    assert code == 2 and "another account" in report["verdict"]
    assert server.requests == []


async def test_a_company_with_no_propertyware_sync_is_refused() -> None:
    db = MirrorDb(await _expected_tables())
    db.stamps = [s for s in db.stamps if s["company_id"] != COMPANY]
    report, code, server = await _phase8(db)
    assert code == 2 and "Sync now" in report["verdict"]
    assert server.requests == []


async def test_a_company_with_no_connection_is_refused() -> None:
    db = MirrorDb(await _expected_tables())
    db.credentials.clear()
    report, code, _ = await _phase8(db)
    assert code == 2 and "no Propertyware connection" in report["verdict"]


async def test_every_statement_is_a_read_only_select() -> None:
    db = MirrorDb(await _expected_tables())
    await _phase8(db)
    assert db.statements and all(s.startswith("SELECT ") for s in db.statements)


# ── the command line ──────────────────────────────────────────────────────


def test_check_prints_set_or_unset_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name, value in zip(phase8.CRED_VARS, (SYSTEM_ID, CLIENT_ID, CLIENT_SECRET), strict=True):
        monkeypatch.setenv(name, value)
    monkeypatch.setenv(phase8.DB_VAR, "postgresql://synthetic.invalid/none")
    assert phase8.main(["--check"]) == 0
    out = capsys.readouterr().out
    assert CLIENT_SECRET not in out and "synthetic.invalid" not in out


def test_without_a_company_it_cannot_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name, value in zip(phase8.CRED_VARS, (SYSTEM_ID, CLIENT_ID, CLIENT_SECRET), strict=True):
        monkeypatch.setenv(name, value)
    monkeypatch.setenv(phase8.DB_VAR, "postgresql://synthetic.invalid/none")
    assert phase8.main([]) == 2
    assert "--company-id is required" in capsys.readouterr().out
