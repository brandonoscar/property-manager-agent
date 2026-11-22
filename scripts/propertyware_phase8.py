"""Phase 8 for Propertyware: does the mirror hold what Propertyware holds?

WHAT IT DOES
    For ONE company, after a real sync, it reads every list the sync reads
    straight from Propertyware, and the company's mirror rows from the
    database, and compares them per table:

      1. the count each side holds;
      2. which record ids are on one side only;
      3. for a sample of ids, every column the sync writes, field by field.

    Each difference is reported by table, record id and column name.

    Run a sync FIRST: Settings → Connectors → Propertyware → Sync now, on a
    backend where the Propertyware connector is switched on. A company with
    no finished Propertyware sync is refused (exit 2), because every table
    would read as missing and say nothing.

THE EXPECTED ROWS ARE THE SYNC'S OWN MAPPING OF A FRESH READ
    The lists are read the way the sync reads them (``sync.query_for``) and
    mapped the way it maps them (``sync.mirror_rows``). So a difference means
    the mirror does not hold what a sync of Propertyware's current data
    writes: a table that went stale, a write that failed, a row from another
    company or system, a column the writer dropped. Whether the MAPPING is
    right is the probe's question (``scripts/propertyware_probe.py``), asked
    against Propertyware's own screens. The raw row count of each list is
    printed beside, so a mapping that drops rows still shows.

    ⚠ A table that could not be read on either side is UNKNOWN, never 0: a
    failed read and an empty table must not reconcile as 0 == 0 (gotchas
    26/55/57, and Rentvine's ``rentvine_reconcile.py``).

READ-ONLY ON BOTH SIDES, BY CONSTRUCTION
    * Propertyware: the connector's own client, whose configuration allows
      no write verb (``send_once`` refuses everything before sending).
    * The database: every statement runs inside a READ ONLY transaction
      (:class:`ReadOnlyPool` has no ``execute`` at all), so Postgres refuses
      any write. Each is a SELECT scoped to the company (rule 11), and each
      mirror read to ``pms = 'propertyware'`` too, the shared writer's own
      predicates. ``tests/unit/test_propertyware_tooling.py`` walks this
      file's AST for both.

CREDENTIALS
    From the environment only, never arguments (CLAUDE.md rule 3), the same
    three names the probe uses, plus the database:

      $env:PYTHONUTF8 = "1"
      $env:PROPERTYWARE_SYSTEM_ID     = "<the organization ID the workspace synced>"
      $env:PROPERTYWARE_CLIENT_ID     = Read-Host "Propertyware client ID"
      $s = Read-Host "Propertyware client secret" -AsSecureString
      $env:PROPERTYWARE_CLIENT_SECRET = [Net.NetworkCredential]::new("", $s).Password
      $s = Read-Host "helixis-test database URL" -AsSecureString
      $env:HELIXIS_SUPABASE__DB_URL   = [Net.NetworkCredential]::new("", $s).Password
      python scripts/propertyware_phase8.py --check
      python scripts/propertyware_phase8.py --company-id <the workspace's company id>

    It refuses to compare when the company's stored organization ID, or the
    last four of its stored client ID, differs from these: that would set one
    account's mirror against another account's data.

EXIT CODES
    0  reconciled: every table agrees, by count, by id and on every sampled field
    1  NOT reconciled: the per-table lines name each difference
    2  could not run (credentials, database, no sync yet). Never a pass.

OUTPUT
    ``docs/propertyware/phase8-report.json`` (git-ignored): counts, record
    ids, column names and value TYPES, never a value.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO / "src") not in sys.path:  # operator tooling: runnable from a clone
    sys.path.insert(0, str(REPO / "src"))

from helixis.pms.errors import PmsApiError  # noqa: E402
from helixis.pms.mirror import (  # noqa: E402
    BALANCE_COLUMNS,
    KEYED_COLUMNS,
    LINK_TABLE,
    PER_PARENT,
    TableRead,
)
from helixis.pms.paging import read_all  # noqa: E402
from helixis.propertyware import client  # noqa: E402
from helixis.propertyware import sync as pw_sync  # noqa: E402
from helixis.propertyware.credentials import TABLE as CREDENTIALS_TABLE  # noqa: E402
from helixis.propertyware.credentials import PropertywareCredentials  # noqa: E402
from helixis.security import CredentialCipher  # noqa: E402

DEFAULT_OUT = "docs/propertyware/phase8-report.json"

CRED_VARS = (
    "PROPERTYWARE_SYSTEM_ID",
    "PROPERTYWARE_CLIENT_ID",
    "PROPERTYWARE_CLIENT_SECRET",
)
DB_VAR = "HELIXIS_SUPABASE__DB_URL"

PMS = client.PMS

#: Ids compared field by field per table, by default (``--sample``).
DEFAULT_SAMPLE = 5

#: Ids listed per table for a one-side-only finding.
IDS_LISTED = 10

#: The tables compared: every table the sync fills from a Propertyware list.
#: Derived tables (turnovers) are opened from move-outs and can be dismissed,
#: so they have nothing on Propertyware's side to equal.
TABLES: tuple[str, ...] = tuple(pw_sync.SYNC_STATE_STAGE)

#: The balance columns compared. The aging buckets are left out: they depend
#: on the day they were worked out, so a sync yesterday and this check today
#: would differ for no fault. Whether the aging was stated is compared.
_BALANCE_COMPARED = ("property_id", "unit_id", "total_balance")


# ── the database, read-only ───────────────────────────────────────────────


class ReadOnlyPool:
    """The three reads Phase 8 makes, each inside a READ ONLY transaction:
    Postgres refuses any write in one. There is no ``execute``."""

    def __init__(self, pool: Any):
        self._pool = pool

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        async with self._pool.acquire() as conn, conn.transaction(readonly=True):
            return list(await conn.fetch(sql, *args))

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        async with self._pool.acquire() as conn, conn.transaction(readonly=True):
            return await conn.fetchrow(sql, *args)


_STAMPS_SQL = """
    SELECT table_name, synced_at, source
      FROM helixis_mirror_sync_state
     WHERE company_id = $1::uuid
"""

_CREDENTIALS_SQL = f"""
    SELECT system_id, client_id_last4
      FROM public.{CREDENTIALS_TABLE}
     WHERE company_id = $1::uuid
"""  # noqa: S608 — the table name is the credentials module's constant


def _key_columns(table: str) -> tuple[str, ...]:
    """The columns that identify one mirror row of ``table``."""
    if table == LINK_TABLE:
        return ("lease_buildium_id", "tenant_buildium_id")
    if table == "helixis_lease_balances":
        return ("lease_id",)
    if table in PER_PARENT:
        return (PER_PARENT[table][0], "line_index")
    return ("buildium_id",)


def compared_columns(table: str) -> tuple[str, ...]:
    """The columns the sync writes to ``table``, less its key, compared field
    by field. Read from the shared writer's own lists (``pms.mirror``)."""
    keys = set(_key_columns(table))
    if table == LINK_TABLE:
        return ("status",)
    if table == "helixis_lease_balances":
        return tuple(c for c in BALANCE_COLUMNS if c in _BALANCE_COMPARED)
    if table in PER_PARENT:
        return tuple(c for c in PER_PARENT[table][1] if c not in keys)
    return tuple(c for c in KEYED_COLUMNS[table] if c not in keys)


def keys_sql(table: str) -> str:
    """Every key of the company's Propertyware rows in ``table``."""
    cols = ", ".join(_key_columns(table))
    return (
        f"SELECT {cols} FROM {table} "  # noqa: S608 — module constants only
        "WHERE company_id = $1::uuid AND pms = $2"
    )


def sample_sql(table: str) -> str:
    """The compared columns of a sample of rows, picked by their first key."""
    first = _key_columns(table)[0]
    extra = ("balance_0_30",) if table == "helixis_lease_balances" else ()
    cols = ", ".join((*_key_columns(table), *compared_columns(table), *extra))
    return (
        f"SELECT {cols} FROM {table} "  # noqa: S608 — module constants only
        f"WHERE company_id = $1::uuid AND pms = $2 AND {first} = ANY($3::bigint[])"
    )


# ── comparing values without recording them ──────────────────────────────


def _norm(value: Any) -> Any:
    """A value as both sides can compare it: numbers as numbers, instants to
    the second in UTC, dates as dates, text trimmed, lists sorted."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int | float | Decimal):
        try:
            return Decimal(str(value)).normalize()
        except InvalidOperation:
            return str(value)
    if isinstance(value, datetime):
        aware = value if value.tzinfo else value.replace(tzinfo=UTC)
        return aware.astimezone(UTC).isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list | tuple):
        return sorted(_norm(v) for v in value)
    return value


def _kind(value: Any) -> str:
    """What a value is, for a report that never holds the value itself."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "yes/no"
    if isinstance(value, int | float | Decimal):
        return "number"
    if isinstance(value, datetime):
        return "date-time"
    if isinstance(value, date):
        return "date"
    if isinstance(value, list | tuple):
        return f"list of {len(value)}"
    return "text"


@dataclass
class TableResult:
    """One table's comparison. ``api`` and ``mirror`` are ``None`` when that
    side could not be read: never 0."""

    table: str
    api: int | None = None
    mirror: int | None = None
    api_rows_read: int | None = None
    rows_without_an_id: int = 0
    only_in_api: list[Any] = field(default_factory=list)
    only_in_mirror: list[Any] = field(default_factory=list)
    sampled: int = 0
    differences: list[dict[str, Any]] = field(default_factory=list)
    why_unknown: str | None = None

    @property
    def issues(self) -> list[str]:
        if self.why_unknown is not None:
            return [f"UNKNOWN: {self.why_unknown}"]
        found = []
        if self.api != self.mirror:
            found.append(f"counts differ (Propertyware {self.api}, mirror {self.mirror})")
        if self.only_in_api:
            found.append(f"{len(self.only_in_api)} only in Propertyware")
        if self.only_in_mirror:
            found.append(f"{len(self.only_in_mirror)} only in the mirror")
        if self.differences:
            found.append(f"{len(self.differences)} field(s) differ")
        return found

    def report(self) -> dict[str, Any]:
        return {
            "api": self.api,
            "mirror": self.mirror,
            "api_rows_read": self.api_rows_read,
            "rows_without_an_id": self.rows_without_an_id,
            "only_in_api": {"count": len(self.only_in_api), "ids": self.only_in_api[:IDS_LISTED]},
            "only_in_mirror": {
                "count": len(self.only_in_mirror),
                "ids": self.only_in_mirror[:IDS_LISTED],
            },
            "sampled": self.sampled,
            "field_differences": self.differences,
            "issues": self.issues,
        }


def _key(table: str, row: Mapping[str, Any]) -> Any:
    """A row's identity: one id, or a tuple for the two-column keys. ``None``
    when part of it is missing (the writer skips such a row)."""
    parts: list[int] = []
    for column in _key_columns(table):
        value = row.get(column)
        if value is None:
            return None
        parts.append(int(value))
    return parts[0] if len(parts) == 1 else tuple(parts)


def _spread(keys: Sequence[Any], n: int) -> list[Any]:
    """``n`` keys spread evenly over the sorted keys, ends included."""
    if n <= 0 or not keys:
        return []
    if len(keys) <= n:
        return list(keys)
    step = (len(keys) - 1) / (n - 1) if n > 1 else 0
    return sorted({keys[round(i * step)] for i in range(n)})


def _id_for_report(key: Any) -> Any:
    return list(key) if isinstance(key, tuple) else key


def compare_table(
    table: str,
    expected: TableRead,
    raw_rows: int | None,
    mirror_keys: list[Any] | None,
    mirror_rows: Mapping[Any, Mapping[str, Any]],
    sample: list[Any],
) -> TableResult:
    """Pure: one table's comparison from both sides' rows."""
    result = TableResult(table, api_rows_read=raw_rows)
    if mirror_keys is None:
        result.why_unknown = "the mirror could not be read"
        return result
    result.mirror = len(mirror_keys)
    if expected.error is not None:
        result.why_unknown = f"Propertyware's read failed: {expected.error}"
        return result
    if not expected.complete:
        result.why_unknown = "Propertyware did not confirm the whole list (X-Total-Count)"
        return result
    by_key: dict[Any, Mapping[str, Any]] = {}
    for row in expected.rows:
        key = _key(table, row)
        if key is None:
            result.rows_without_an_id += 1
        else:
            by_key[key] = row
    result.api = len(by_key)
    api_keys, mirror_set = set(by_key), set(mirror_keys)
    result.only_in_api = [_id_for_report(k) for k in sorted(api_keys - mirror_set)]
    result.only_in_mirror = [_id_for_report(k) for k in sorted(mirror_set - api_keys)]
    for key in sample:
        row, stored = by_key.get(key), mirror_rows.get(key)
        if row is None or stored is None:
            continue
        result.sampled += 1
        for column in compared_columns(table):
            if column not in row:
                continue  # left out by the mapping: the mirror keeps what it had
            if _norm(row[column]) != _norm(stored.get(column)):
                result.differences.append(
                    {
                        "id": _id_for_report(key),
                        "column": column,
                        "propertyware": _kind(row[column]),
                        "mirror": _kind(stored.get(column)),
                    }
                )
        if table == "helixis_lease_balances":
            stated = row.get("balance_0_30") is not None
            if stated != (stored.get("balance_0_30") is not None):
                result.differences.append(
                    {
                        "id": _id_for_report(key),
                        "column": "aging stated",
                        "propertyware": "yes" if stated else "no",
                        "mirror": "no" if stated else "yes",
                    }
                )
    return result


# ── the two sides ─────────────────────────────────────────────────────────


async def read_propertyware(pw: Any) -> tuple[dict[str, TableRead], dict[str, int | None]]:
    """Every list the sync reads, read and mapped the way the sync does.
    Returns the expected mirror rows per table and each list's raw row count."""
    reads: dict[str, pw_sync.ResourceRead] = {}
    raw: dict[str, int | None] = {}
    for path in client.READS:
        try:
            got = await read_all(pw, path, strategy=client.PAGING, query=pw_sync.query_for(path))
        except PmsApiError as exc:  # that list is UNKNOWN, never empty
            # The status and its meaning only: Propertyware's own text can
            # echo what was sent, and the report holds no value.
            sent = "" if exc.sent else ", not sent"
            reads[path] = pw_sync.ResourceRead(error=f"{exc.status} ({exc.failure.value}{sent})")
            raw[path] = None
            continue
        except Exception as exc:  # noqa: BLE001 — ours, named by its type only
            reads[path] = pw_sync.ResourceRead(error=f"{type(exc).__name__} (in Occupella)")
            raw[path] = None
            continue
        reads[path] = pw_sync.ResourceRead(rows=got.rows, complete=got.complete)
        raw[path] = len(got.rows)
    rows, _report = pw_sync.mirror_rows(reads, today=date.today(), now=0.0)
    return rows, raw


#: Which list each table is mapped from, for its raw row count.
SOURCE_LIST: dict[str, str] = {
    "helixis_properties": "/buildings",
    "helixis_units": "/units",
    "helixis_owners": "/portfolios",
    "helixis_tenants": "/contacts",
    "helixis_vendors": "/vendors",
    "helixis_leases": "/leases",
    "helixis_lease_tenants": "/leases",
    "helixis_lease_balances": "/leases",
    "helixis_work_orders": "/workorders",
    "helixis_bills": "/bills",
    "helixis_bill_lines": "/bills",
    "helixis_gl_accounts": "/accounting/glaccounts",
}


async def read_mirror(db: ReadOnlyPool, company_id: str, table: str) -> list[Any] | None:
    """Every key of the table's Propertyware rows for the company. ``None``
    when the table could not be read: never an empty list."""
    try:
        rows = await db.fetch(keys_sql(table), company_id, PMS)
    except Exception:  # noqa: BLE001 — UNKNOWN, never empty
        return None
    return [k for k in (_key(table, dict(r)) for r in rows) if k is not None]


async def read_sample(
    db: ReadOnlyPool, company_id: str, table: str, sample: list[Any]
) -> dict[Any, dict[str, Any]]:
    if not sample:
        return {}
    firsts = sorted({k[0] if isinstance(k, tuple) else k for k in sample})
    try:
        rows = await db.fetch(sample_sql(table), company_id, PMS, firsts)
    except Exception:  # noqa: BLE001 — the sample is then not compared
        return {}
    out: dict[Any, dict[str, Any]] = {}
    for record in rows:
        row = dict(record)
        key = _key(table, row)
        if key is not None:
            out[key] = row
    return out


async def run(
    db: ReadOnlyPool,
    pw: Any,
    company_id: str,
    creds: PropertywareCredentials,
    *,
    sample: int = DEFAULT_SAMPLE,
) -> tuple[dict[str, Any], int]:
    """Phase 8 for one company. Returns the report and the exit code."""
    report: dict[str, Any] = {
        "harness": "scripts/propertyware_phase8.py",
        "company_id": company_id,
        "read_only": True,
    }
    stored = await db.fetchrow(_CREDENTIALS_SQL, company_id)
    if stored is None:
        report["verdict"] = "COULD NOT RUN: this company holds no Propertyware connection"
        return report, 2
    if str(stored["system_id"]).strip() != creds.system_id or (
        stored["client_id_last4"] or ""
    ) != CredentialCipher.last4(creds.client_id):
        report["verdict"] = (
            "COULD NOT RUN: the company's stored organization ID or client ID is not the "
            "one in the environment, so its mirror is another account's"
        )
        return report, 2
    stamps = [
        dict(r)
        for r in await db.fetch(_STAMPS_SQL, company_id)
        if r["source"] == pw_sync.SYNC_SOURCE
    ]
    if not stamps:
        report["verdict"] = (
            "COULD NOT RUN: no Propertyware sync has finished for this company. Run one "
            "first: Settings → Connectors → Propertyware → Sync now"
        )
        return report, 2
    report["stages_last_synced"] = {
        s["table_name"]: s["synced_at"].isoformat() if s["synced_at"] else None for s in stamps
    }

    expected, raw = await read_propertyware(pw)
    results: list[TableResult] = []
    for table in TABLES:
        keys = await read_mirror(db, company_id, table)
        exp = expected.get(table, TableRead(error="not mapped"))
        on_both = {k for k in (_key(table, r) for r in exp.rows) if k is not None} & set(keys or [])
        picked = _spread(sorted(on_both), sample)
        stored_rows = await read_sample(db, company_id, table, picked)
        results.append(
            compare_table(table, exp, raw.get(SOURCE_LIST[table]), keys, stored_rows, picked)
        )
    report["tables"] = {r.table: r.report() for r in results}
    reconciled = bool(results) and not any(r.issues for r in results)
    report["verdict"] = "RECONCILED" if reconciled else "NOT RECONCILED"
    return report, 0 if reconciled else 1


def render(report: Mapping[str, Any]) -> str:
    lines = []
    for table, entry in report.get("tables", {}).items():
        mark = "OK  " if not entry["issues"] else "FAIL"
        api = "unread" if entry["api"] is None else entry["api"]
        mirror = "unread" if entry["mirror"] is None else entry["mirror"]
        lines.append(f"  {mark} {table:<24} propertyware={api!s:<7} mirror={mirror!s:<7}")
        for issue in entry["issues"]:
            lines.append(f"       {issue}")
        for diff in entry["field_differences"][:IDS_LISTED]:
            lines.append(
                f"       id {diff['id']}: {diff['column']} "
                f"(Propertyware {diff['propertyware']}, mirror {diff['mirror']})"
            )
    lines.append(f"\nVERDICT: {report.get('verdict')}")
    return "\n".join(lines)


def _write(path: str, report: Mapping[str, Any]) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, sort_keys=True, default=str), encoding="utf-8")


def credentials_from_env() -> tuple[PropertywareCredentials | None, list[str]]:
    values = {name: os.environ.get(name, "").strip() for name in CRED_VARS}
    missing = [name for name, value in values.items() if not value]
    if missing:
        return None, missing
    return (
        PropertywareCredentials(
            company_id="",
            system_id=values["PROPERTYWARE_SYSTEM_ID"],
            client_id=values["PROPERTYWARE_CLIENT_ID"],
            client_secret=values["PROPERTYWARE_CLIENT_SECRET"],
        ),
        [],
    )


async def _main(args: argparse.Namespace, creds: PropertywareCredentials) -> int:
    from helixis.db.engine import create_pool

    pool = await create_pool(os.environ[DB_VAR], min_size=1, max_size=2)
    creds = PropertywareCredentials(
        company_id=args.company_id,
        system_id=creds.system_id,
        client_id=creds.client_id,
        client_secret=creds.client_secret,
    )
    try:
        async with client.build_client(creds) as pw:
            report, code = await run(
                ReadOnlyPool(pool), pw, args.company_id, creds, sample=args.sample
            )
    finally:
        await pool.close()
    _write(args.out, report)
    print(render(report))
    print(f"Report: {args.out} (send that file)")
    return code


def main(argv: Iterable[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--company-id", help="the Occupella company whose mirror to compare")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--sample", type=int, default=DEFAULT_SAMPLE, help="ids compared per table")
    ap.add_argument(
        "--check", action="store_true", help="report which variables are set; send nothing"
    )
    args = ap.parse_args(list(argv) if argv is not None else None)
    try:  # a Windows console prints the report's arrows (gotcha 49)
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

    creds, missing = credentials_from_env()
    db_set = bool(os.environ.get(DB_VAR, "").strip())
    if args.check:
        print("Environment (set/unset only; no value is printed):")
        for name in CRED_VARS:
            print(f"  {name:28} {'UNSET' if name in missing else 'set'}")
        print(f"  {DB_VAR:28} {'set' if db_set else 'UNSET'}")
        return 0 if creds is not None and db_set else 2
    if creds is None or not db_set:
        absent = missing + ([] if db_set else [DB_VAR])
        print("VERDICT: COULD NOT RUN: missing " + ", ".join(absent))
        print("Set them in the environment, never as arguments. Exit 2 is not a pass.")
        return 2
    if not args.company_id:
        print("VERDICT: COULD NOT RUN: --company-id is required")
        print("It scopes every mirror read; without it a count reaches another company's rows.")
        return 2
    return asyncio.run(_main(args, creds))


if __name__ == "__main__":
    raise SystemExit(main())
