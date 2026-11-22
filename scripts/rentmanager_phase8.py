"""Phase 8: does one company's mirror hold what its Rent Manager account holds?

READ-ONLY ON BOTH SIDES. Run it after a real sync (Settings → Connectors →
Rent Manager → Sync now, or the worker's own pass); it syncs nothing itself.

WHAT IT DOES
    1. Checks the company is on Rent Manager, that its saved connection is the
       account and location the environment names, and that a sync has landed.
    2. Reads every collection the sync reads, from the founder's own login,
       with a client that cannot write (``rentmanager_readonly``), and maps it
       exactly as the sync does (``sync.build_mirror_rows``): the rows the
       mirror should hold.
    3. Reads the mirror inside ONE ``READ ONLY`` transaction (one snapshot, and
       the database refuses any write), through the company-scoped queries the
       product uses: ``company_pms_strict``, ``get_freshness``,
       ``company_timezone``, and ``WHERE company_id = $1::uuid AND pms = $2`` on
       every mirror table (rule 11: without the company a count reaches another
       workspace's rows and reads CLOSER, never further).
    4. Compares the count per table, then a sample of record ids per table,
       field by field. Each difference is named by table, record id, column and
       kind, never by value: no name, email or amount reaches the report.

⚠ A COUNT NOT READ IS UNKNOWN, NEVER 0. A failed or unconfirmed read is
"unread", and an unread side never reconciles: 0 == 0 from a failed read is a
green light derived from nothing (gotchas 26/55).

HOW TO RUN (PowerShell 5.1, on the founder's machine)
    The same variables as the probe, plus the database URL (a secret too, so
    it is prompted for). Never arguments: argv reaches shell history.

        $env:PYTHONUTF8 = "1"
        $env:RENTMANAGER_CORP_ID     = "<the part before .api.rentmanager.com>"
        $env:RENTMANAGER_LOCATION_ID = "<the location number, such as 1>"
        $env:RENTMANAGER_USERNAME    = Read-Host "Rent Manager API username"
        $s = Read-Host "Rent Manager API password" -AsSecureString
        $env:RENTMANAGER_PASSWORD    = [Net.NetworkCredential]::new("", $s).Password
        ## Only if Rent Manager issued a partner token:
        $s = Read-Host "Rent Manager partner token" -AsSecureString
        $env:RENTMANAGER_PARTNER_TOKEN = [Net.NetworkCredential]::new("", $s).Password
        $s = Read-Host "helixis-test database URL" -AsSecureString
        $env:HELIXIS_SUPABASE__DB_URL = [Net.NetworkCredential]::new("", $s).Password

        python scripts/rentmanager_phase8.py --check                # set/unset only
        python scripts/rentmanager_phase8.py --company-id <uuid>    # the comparison

    A sync that lands while it runs (the worker's own pass, or a Sync now
    click) can make the two sides differ for a reason that is not a defect.
    It reads the sync stamps before and after, and says so instead of
    reporting a difference.

EXIT CODES, and the middle one is the point
    0  reconciled: every table's count matches and no sampled field differs
    1  NOT reconciled: a real finding; the lines name each table and field
    2  could not run: a missing variable, the wrong company or location, no
       sync landed yet, the login refused, or a sync landed while it ran and
       the sides differ. Never a pass.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import os
import re
import sys
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import rentmanager_readonly as ro  # noqa: E402  (also puts src/ on the path)

from helixis.common.company_clock import company_timezone, today_in  # noqa: E402
from helixis.db.company_pms import company_pms_strict  # noqa: E402
from helixis.db.repositories.mirror_sync_state import get_freshness  # noqa: E402
from helixis.pms import registry  # noqa: E402
from helixis.pms.errors import PmsApiError, RefusedBeforeSendingError  # noqa: E402
from helixis.pms.mirror import BALANCE_COLUMNS, KEYED_COLUMNS, TableRead  # noqa: E402
from helixis.pms.paging import read_all  # noqa: E402
from helixis.rentmanager import client as rm_client  # noqa: E402
from helixis.rentmanager.credentials import RentManagerCredentials  # noqa: E402
from helixis.rentmanager.resources import RESOURCES  # noqa: E402
from helixis.rentmanager.sync import SYNC_STATE_STAGE, Read, build_mirror_rows  # noqa: E402

PMS = registry.RENTMANAGER
DB_VAR = "HELIXIS_SUPABASE__DB_URL"
DEFAULT_OUT = "docs/rentmanager/phase8-report.json"
#: Record ids compared field by field, per table.
SAMPLE = 5

#: Every table a pass fills, in the sync's own map: a table added there is
#: compared here on the same commit (gotcha 28's both-sides rule).
TABLES: tuple[str, ...] = tuple(SYNC_STATE_STAGE)
BALANCES = "helixis_lease_balances"
#: Tables compared field by field: ``table -> (key column, columns)``.
FIELD_TABLES: dict[str, tuple[str, tuple[str, ...]]] = {
    **{t: ("buildium_id", KEYED_COLUMNS[t]) for t in TABLES if t in KEYED_COLUMNS},
    BALANCES: ("lease_id", tuple(c for c in BALANCE_COLUMNS if c != "lease_id")),
}

_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

#: The saved connection's two plain columns, never a ciphertext.
SAVED_SQL = (
    "SELECT corp_id, location_id FROM public.company_rentmanager_credentials "
    "WHERE company_id = $1::uuid"
)


def count_sql(table: str) -> str:
    """One table's rows for one company on this system. Both predicates are
    load-bearing: without ``pms`` another system's rows count; without
    ``company_id`` another workspace's do (gotcha 47)."""
    if table not in TABLES:
        raise ValueError(f"{table} is not a table this pass fills")
    return f"SELECT count(*) FROM {table} WHERE company_id = $1::uuid AND pms = $2"  # noqa: S608


def sample_sql(table: str) -> str:
    """The sampled rows of one table, by their key, for one company."""
    key, columns = FIELD_TABLES[table]
    return (
        f"SELECT {key}, {', '.join(columns)} FROM {table} "  # noqa: S608 — module constants
        f"WHERE company_id = $1::uuid AND pms = $2 AND {key} = ANY($3::bigint[])"
    )


class _ReadOnlyPool:
    """The product's readers take a pool; this hands them the one connection
    that is inside the run's READ ONLY transaction, so none of them can write."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[Any]:
        yield self._conn

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        return await self._conn.fetchrow(sql, *args)

    async def fetch(self, sql: str, *args: Any) -> Any:
        return await self._conn.fetch(sql, *args)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        return await self._conn.fetchval(sql, *args)


# ── the comparison, pure ──────────────────────────────────────────────────


RECONCILED = "reconciled"
MISSING = "missing"
EXTRA = "extra"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class TableResult:
    table: str
    expected: int | None
    mirror: int | None
    verdict: str
    detail: str


def compare_count(table: str, expected: int | None, mirror: int | None) -> TableResult:
    """One table's count. ``None`` is unread and never reconciles."""
    if expected is None or mirror is None:
        unread = " and ".join(
            side for side, v in (("Rent Manager", expected), ("the mirror", mirror)) if v is None
        )
        return TableResult(
            table,
            expected,
            mirror,
            UNKNOWN,
            f"could not read {unread}: NOT a match (a failed read and an empty table both "
            "look like 0)",
        )
    if expected == mirror:
        return TableResult(table, expected, mirror, RECONCILED, f"{expected} rows both sides")
    if mirror < expected:
        return TableResult(
            table,
            expected,
            mirror,
            MISSING,
            f"the mirror is short by {expected - mirror}. Read the sync's result first: the table "
            "may be in `failed` (stale), or rows were skipped for having no usable id; a read "
            "that stopped early is the third cause, and this comparison cannot tell them apart.",
        )
    return TableResult(
        table,
        expected,
        mirror,
        EXTRA,
        f"the mirror holds {mirror - expected} MORE: a record Rent Manager no longer has and "
        "the last pass did not prune (it prunes only after a complete read), or a duplicate.",
    )


def _number(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def same(api: Any, mirror: Any) -> bool:
    """Equal as stored: Decimal 1850 is numeric 1850.00, a date is the date of
    a midnight timestamp, a list is its elements in order."""
    if api is None or mirror is None:
        return api is None and mirror is None
    if isinstance(api, bool) or isinstance(mirror, bool):
        return isinstance(api, bool) and isinstance(mirror, bool) and api == mirror
    a, b = _number(api), _number(mirror)
    if a is not None and b is not None:
        return a == b
    if isinstance(api, date) and isinstance(mirror, date):
        if isinstance(api, datetime) and isinstance(mirror, datetime):
            return api == mirror
        day_a = api.date() if isinstance(api, datetime) else api
        day_b = mirror.date() if isinstance(mirror, datetime) else mirror
        return day_a == day_b
    if isinstance(api, (list, tuple)) and isinstance(mirror, (list, tuple)):
        return len(api) == len(mirror) and all(same(x, y) for x, y in zip(api, mirror, strict=True))
    return bool(api == mirror)


def pick(ids: list[int], k: int = SAMPLE) -> list[int]:
    """Up to ``k`` ids spread across the sorted list: deterministic, so a
    re-run compares the same records."""
    ordered = sorted(set(ids))
    if len(ordered) <= k:
        return ordered
    step = (len(ordered) - 1) / (k - 1)
    return sorted({ordered[round(i * step)] for i in range(k)})


def compare_fields(
    table: str,
    expected_rows: list[dict[str, Any]],
    mirror_rows: Mapping[int, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Each difference between the expected rows and the mirror's: the table,
    the record id, the column and the KIND of difference. Never a value.

    A column the pass left out of a row (it keeps the mirror's value on
    purpose: an unresolved status, a deposit that could not be placed) is not
    compared."""
    key, columns = FIELD_TABLES[table]
    out: list[dict[str, Any]] = []
    for row in expected_rows:
        rid = row.get(key)
        stored = mirror_rows.get(rid) if isinstance(rid, int) else None
        if stored is None:
            out.append({"table": table, "id": rid, "column": None, "kind": "missing in the mirror"})
            continue
        for column in columns:
            if column not in row or same(row[column], stored.get(column)):
                continue
            if stored.get(column) is None:
                kind = "empty in the mirror"
            elif row[column] is None:
                kind = "empty from Rent Manager"
            else:
                kind = "differs"
            out.append({"table": table, "id": rid, "column": column, "kind": kind})
    return out


# ── the run ───────────────────────────────────────────────────────────────


RECONCILED_VERDICT = "RECONCILED"
NOT_RECONCILED_VERDICT = "NOT RECONCILED"
SYNCED_DURING_RUN = "SYNCED DURING THE RUN"


class CouldNotRun(Exception):  # noqa: N818 — a verdict, not a fault
    """Phase 8 cannot compare anything. The message says what to do."""


async def _preflight(conn: Any, company_id: str, creds: RentManagerCredentials) -> dict[str, Any]:
    """The company is on Rent Manager, saved for this account and location,
    and a sync has landed. Read-only; raises :class:`CouldNotRun`."""
    async with conn.transaction(readonly=True):
        pool = _ReadOnlyPool(conn)
        pms = await company_pms_strict(pool, company_id)
        saved = await conn.fetchrow(SAVED_SQL, company_id)
        stamps = await get_freshness(pool, company_id)
        zone = await company_timezone(pool, company_id)
    if pms != PMS:
        name = registry.name_of(pms) or pms
        raise CouldNotRun(f"that company is on {name}, not Rent Manager. Check --company-id.")
    if saved is None:
        raise CouldNotRun("that company has no saved Rent Manager connection.")
    if str(saved["corp_id"]).lower() != creds.corp_id:
        raise CouldNotRun(
            f"that company's saved corp ID is not {ro.CORP_VAR}: this would compare two "
            "different accounts. Check both."
        )
    if saved["location_id"] != creds.location_id:
        raise CouldNotRun(
            f"that company is saved for location {saved['location_id']}, and "
            f"{ro.LOCATION_VAR} names {creds.location_id}. Use the saved location."
        )
    stages = sorted(s for s in set(SYNC_STATE_STAGE.values()) if s in stamps)
    if not stages:
        raise CouldNotRun(
            "no Rent Manager sync has landed for that company yet (or its sync stamps could "
            "not be read). In Occupella, open Settings → Connectors → Rent Manager, click "
            "Sync now, wait for it to finish, then run this again."
        )
    return {
        "stages_landed": stages,
        "stamps": {stage: stamps[stage] for stage in stages},
        "today": today_in(zone),
    }


async def _read_api(
    creds: RentManagerCredentials, observer: ro.Observer
) -> tuple[dict[str, Read], dict[str, Any]]:
    """Every collection, read as the sync reads it. A failure is that
    resource's error, never 0; the vendor's own text stays out (it can quote
    a value)."""
    reads: dict[str, Read] = {}
    provenance: dict[str, Any] = {}
    async with ro.read_only_client(creds, ro.read_operations(), observer) as client:
        try:
            current = await rm_client.current_location_id(client)
        except PmsApiError as exc:
            raise CouldNotRun(
                f"Rent Manager refused the login or the location read ({exc.status}, "
                f"{exc.failure.value}). Run scripts/rentmanager_probe.py first: it says why."
            ) from None
        if current != creds.location_id:
            raise CouldNotRun(
                "Rent Manager's CurrentLocation does not name the location the login asked "
                "for, so the mirror and this read would be different books (design R2)."
            )
        for name, resource in RESOURCES.items():
            try:
                got = await read_all(
                    client, resource.path, strategy=rm_client.PAGING, query=resource.query()
                )
            except (PmsApiError, RefusedBeforeSendingError) as exc:
                status = getattr(exc, "status", None)
                reads[name] = Read(error=f"read failed ({status})")
                provenance[name] = {"read": False, "status": status}
                continue
            reads[name] = Read(rows=got.rows, complete=got.complete)
            provenance[name] = {
                "read": True,
                "rows": len(got.rows),
                "vendor_total": got.total,
                "confirmed_whole": got.complete,
            }
    return reads, provenance


def _expected_count(read: TableRead) -> int | None:
    return len(read.rows) if read.error is None and read.complete else None


@dataclass
class MirrorRead:
    """What one read-only snapshot of the mirror held."""

    counts: dict[str, int | None]
    stored: dict[str, dict[int, Mapping[str, Any]]]
    sampled: dict[str, list[int]]
    #: The sync stamps in the same snapshot: a pass that landed since the
    #: preflight shows here.
    stamps: Mapping[str, Any]


async def _read_mirror(conn: Any, company_id: str, expected: Mapping[str, TableRead]) -> MirrorRead:
    """Counts, the sampled rows and the sync stamps, in ONE read-only snapshot."""
    counts: dict[str, int | None] = {}
    stored: dict[str, dict[int, Mapping[str, Any]]] = {}
    sampled: dict[str, list[int]] = {}
    async with conn.transaction(readonly=True, isolation="repeatable_read"):
        stamps = await get_freshness(_ReadOnlyPool(conn), company_id)
        for table in TABLES:
            counts[table] = await conn.fetchval(count_sql(table), company_id, PMS)
        for table, (key, _columns) in FIELD_TABLES.items():
            read = expected.get(table)
            if read is None or read.error is not None:
                continue
            ids = pick([r[key] for r in read.rows if isinstance(r.get(key), int)])
            sampled[table] = ids
            if not ids:
                continue
            rows = await conn.fetch(sample_sql(table), company_id, PMS, ids)
            stored[table] = {r[key]: dict(r) for r in rows}
    return MirrorRead(counts, stored, sampled, stamps)


async def run(company_id: str, creds: RentManagerCredentials, dsn: str) -> dict[str, Any]:
    """The whole comparison: the report, with ``verdict``."""
    import asyncpg

    report: dict[str, Any] = {
        "harness": "scripts/rentmanager_phase8.py",
        "read_only": True,
        "company_id": company_id,
        "location_id": creds.location_id,
        "ran_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    observer = ro.Observer()
    conn = await asyncpg.connect(dsn, statement_cache_size=0)
    try:
        pre = await _preflight(conn, company_id, creds)
        report["stages_landed"] = pre["stages_landed"]
        reads, provenance = await _read_api(creds, observer)
        report["api"] = provenance
        expected = build_mirror_rows(reads, today=pre["today"])
        mirror = await _read_mirror(conn, company_id, expected)
    finally:
        await conn.close()
    counts, stored, sampled = mirror.counts, mirror.stored, mirror.sampled
    moved = sorted(
        stage
        for stage in set(SYNC_STATE_STAGE.values())
        if mirror.stamps.get(stage) != pre["stamps"].get(stage)
    )
    report["stages_synced_during_run"] = moved
    tables = [
        compare_count(t, _expected_count(expected[t]) if t in expected else None, counts.get(t))
        for t in TABLES
    ]
    differences: list[dict[str, Any]] = []
    for table, ids in sampled.items():
        key = FIELD_TABLES[table][0]
        rows = [r for r in expected[table].rows if r.get(key) in ids]
        differences += compare_fields(table, rows, stored.get(table, {}))
    report["tables"] = [dataclasses.asdict(t) for t in tables]
    report["sampled"] = {t: len(ids) for t, ids in sampled.items()}
    report["field_differences"] = differences
    ok = all(t.verdict == RECONCILED for t in tables) and not differences
    if ok:
        report["verdict"] = RECONCILED_VERDICT
    elif moved:
        # The two reads straddle a pass: a difference may be the pass's, not a defect.
        report["verdict"] = SYNCED_DURING_RUN
    else:
        report["verdict"] = NOT_RECONCILED_VERDICT
    return report


def _print(report: Mapping[str, Any]) -> None:
    for t in report["tables"]:
        mark = "OK  " if t["verdict"] == RECONCILED else "FAIL"
        api = "unread" if t["expected"] is None else t["expected"]
        mirror = "unread" if t["mirror"] is None else t["mirror"]
        print(
            f"  {mark} {t['table']:<24} rent_manager={api!s:<8} mirror={mirror!s:<8} {t['verdict']}"
        )
        if t["verdict"] != RECONCILED:
            print(f"       {t['detail']}")
    diffs = report["field_differences"]
    print(
        f"\n  {sum(report['sampled'].values())} records compared field by field; "
        f"{len(diffs)} differ"
    )
    for d in diffs[:40]:
        where = f"{d['table']} {d['id']}" + (f" {d['column']}" if d["column"] else "")
        print(f"    {where}: {d['kind']}")
    if len(diffs) > 40:
        print(f"    … {len(diffs) - 40} more in the report")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--company-id", help="the Occupella company whose mirror to compare")
    # In the clone, wherever the window is: that folder is the git-ignored one.
    ap.add_argument("--out", default=str(ro.REPO / DEFAULT_OUT))
    ap.add_argument(
        "--check", action="store_true", help="say which variables are set; sends nothing"
    )
    args = ap.parse_args(argv)
    ro.utf8_console()

    if args.check:
        print("Variables (set or unset only; no value is printed):")
        for line in ro.check_lines(required=(DB_VAR,)):
            print(line)
        ready = all(ro.env_is_set(n) for n in (*ro.REQUIRED_VARS, DB_VAR))
        return 0 if ready else 2

    try:
        creds = ro.credentials_from_env()
    except ValueError as exc:
        print(f"VERDICT: COULD NOT RUN: {exc}")
        print("\nSet them in this PowerShell window, never as arguments (see the runbook).")
        return 2
    if not ro.env_is_set(DB_VAR):
        print(f"VERDICT: COULD NOT RUN: missing {DB_VAR}")
        return 2
    if not args.company_id or not _UUID.match(args.company_id):
        print("VERDICT: COULD NOT RUN: --company-id must be the Occupella company's id (a UUID).")
        print("It scopes every mirror read: without it a count reaches another workspace.")
        return 2

    try:
        report = asyncio.run(run(args.company_id, creds, os.environ[DB_VAR]))
    except CouldNotRun as exc:
        print(f"VERDICT: COULD NOT RUN: {exc}")
        return 2
    except Exception as exc:  # noqa: BLE001 — the type only: a driver error can name the host
        print(f"VERDICT: COULD NOT RUN: {type(exc).__name__} while reading.")
        print(f"Check {DB_VAR} (the helixis-test pooler URL) and the network, then re-run.")
        return 2
    ro.write_report(args.out, report)
    _print(report)
    print(f"\nReport: {args.out} (ids and kinds of difference, never a value)")
    print(f"VERDICT: {report['verdict']}")
    if report["verdict"] == SYNCED_DURING_RUN:
        print(
            f"A sync landed while this ran ({', '.join(report['stages_synced_during_run'])}), so "
            "these differences may be that sync's. Wait for it to finish and run this again."
        )
        return 2
    return 0 if report["verdict"] == RECONCILED_VERDICT else 1


if __name__ == "__main__":
    raise SystemExit(main())
