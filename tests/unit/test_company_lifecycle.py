"""Leaving, exporting and deleting a workspace.

Three gaps that shipped together because they depend on each other:

* **Leaving required asking an admin.** Somebody who changed jobs stayed on
  the members list, kept a billed seat, and kept access to a portfolio they
  had no business seeing. "Ask someone else to revoke your access" is not an
  access control.
* **Nothing exported.** Property managers live in spreadsheets, and a product
  holding a portfolio that cannot hand it back reads as a trap.
* **Nothing deleted.** Self-service erasure is a GDPR/CCPA obligation, and it
  is what makes "your data is yours" a fact rather than a slogan.

⚠ The order matters: a delete offered without an export is a data-loss button,
so the export is the precondition rather than a nice-to-have beside it.
"""

from __future__ import annotations

import ast
import csv
import io
import pathlib
import re
import zipfile
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from helixis.common.support import SUPPORT_EMAIL
from helixis.exports.company_export import EXPORT_TABLES, build_export, export_filename

_COMPANY = "22222222-2222-2222-2222-222222222222"


def _pool(rows_by_table: dict[str, list[dict[str, Any]]]) -> MagicMock:
    """A pool whose fetch answers per table name found in the SQL.

    ⚠ Reaches into the auto-generated AsyncMock tree rather than hand-building
    the connection (gotcha 69) — `acquire().__aenter__()` already yields one
    whose methods await, and replacing it breaks every other await in the path.
    """
    pool = MagicMock()
    conn = pool.acquire.return_value.__aenter__.return_value

    async def _fetch(sql: str, *_a: Any) -> list[dict[str, Any]]:
        for table in EXPORT_TABLES:
            if f'"helixis_{table}"' in sql:
                return rows_by_table.get(table, [])
        return []

    conn.fetch = AsyncMock(side_effect=_fetch)
    return pool


def _open(payload: bytes) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(payload))


# ── the export ───────────────────────────────────────────────────────


async def test_every_table_becomes_a_file_even_when_empty() -> None:
    """⚠ An empty CSV says "you have no vendors". A MISSING file is
    indistinguishable from an export that broke, and the reader cannot tell
    which without asking us."""
    payload = await build_export(_pool({}), _COMPANY, "Blue Ridge")
    names = set(_open(payload).namelist())
    for table in EXPORT_TABLES:
        assert f"{table}.csv" in names, f"{table} silently absent from the export"
    assert "README.txt" in names


async def test_an_empty_table_still_carries_its_header_row() -> None:
    payload = await build_export(_pool({}), _COMPANY, "Blue Ridge")
    body = _open(payload).read("vendors.csv").decode()
    assert body.strip(), "an empty file with no header cannot be opened in Excel"
    assert "buildium_id" in body


async def test_rows_reach_the_csv() -> None:
    payload = await build_export(
        _pool({"vendors": [{"buildium_id": 7, "name": "Redbud", "email": "a@b.c", "phone": ""}]}),
        _COMPANY,
        "Blue Ridge",
    )
    rows = list(csv.DictReader(io.StringIO(_open(payload).read("vendors.csv").decode())))
    assert rows[0]["name"] == "Redbud"


async def test_datetimes_export_as_iso() -> None:
    """⚠ `str(datetime)` gives `2026-09-05 12:00:00+00:00`, which Excel parses
    inconsistently across locales. A date a customer's spreadsheet reads wrong
    is worse than no export."""
    payload = await build_export(
        _pool({"tasks": [{"buildium_id": 1, "created_at": datetime(2026, 9, 5, 12, tzinfo=UTC)}]}),
        _COMPANY,
        "X",
    )
    body = _open(payload).read("tasks.csv").decode()
    assert "2026-09-05T12:00:00+00:00" in body


async def test_one_unreadable_table_does_not_lose_the_whole_export() -> None:
    """A deployment mid-migration is missing a table. An export that refuses
    entirely gives the customer nothing; a zip short one file gives them their
    portfolio."""
    pool = MagicMock()
    conn = pool.acquire.return_value.__aenter__.return_value

    async def _fetch(sql: str, *_a: Any) -> list[dict[str, Any]]:
        if '"helixis_owners"' in sql:
            raise RuntimeError('relation "helixis_owners" does not exist')
        return []

    conn.fetch = AsyncMock(side_effect=_fetch)
    payload = await build_export(pool, _COMPANY, "X")
    assert "properties.csv" in _open(payload).namelist()


async def test_an_unreadable_table_is_named_and_never_shipped_empty() -> None:
    """An unreadable table was written as an empty file under a README saying
    an empty file means "you have none" (rule 14). Measured 2026-10-05: three
    tables read that way for every customer, on columns no migration made."""
    pool = MagicMock()
    conn = pool.acquire.return_value.__aenter__.return_value

    async def _fetch(sql: str, *_a: Any) -> list[dict[str, Any]]:
        if '"helixis_units"' in sql:
            raise RuntimeError('column "bedrooms" does not exist')
        return []

    conn.fetch = AsyncMock(side_effect=_fetch)
    archive = _open(await build_export(pool, _COMPANY, "X"))
    assert "units.csv" not in archive.namelist(), "an unread table must not read as an empty one"
    readme = archive.read("README.txt").decode()
    assert "units.csv — 0 rows" not in readme
    assert "NOT EXPORTED" in readme and "units.csv" in readme
    assert "NOT an empty table" in readme and SUPPORT_EMAIL in readme
    # The readable ones are untouched, empty files and all.
    assert "vendors.csv" in archive.namelist() and "vendors.csv — 0 rows" in readme


async def test_a_clean_export_claims_nothing_was_left_out() -> None:
    readme = _open(await build_export(_pool({}), _COMPANY, "X")).read("README.txt").decode()
    assert "NOT EXPORTED" not in readme


# ── every column exists (rule 15: the module against the schema) ──────

#: Where the mirror's tables are declared: the base schema, then every
#: migration in order (``test_rentvine_mirror_column_types`` learned that one
#: source alone reports a clean corpus while missing real columns).
_REPO = pathlib.Path(__file__).resolve().parents[2]
_SCHEMA_SOURCES = (_REPO / "src/helixis/db/schema", _REPO / "supabase/migrations")
_CREATE = re.compile(
    r"create table (?:if not exists )?(?:public\.)?(helixis_\w+)\s*\((.*?)\n\);", re.S | re.I
)
_ALTER = re.compile(
    r"alter table (?:if exists )?(?:only )?(?:public\.)?(helixis_\w+)(.*?);", re.S | re.I
)
#: A column added to a list of tables in a loop (0101 adds ``pms`` this way:
#: ``foreach t in array array[...] loop execute format('alter table
#: public.%I add column ...', t)``). Without it, the reader missed ``pms`` on
#: every mirror table, and the live check below said so.
_LOOP = re.compile(r"foreach \w+ in array array\[(.*?)\]\s*loop(.*?)end loop;", re.S | re.I)
_LOOP_ADD = re.compile(r"alter table (?:public\.)?%I add column (?:if not exists )?(\w+)", re.I)
_ADD = re.compile(r"add column (?:if not exists )?\"?(\w+)", re.I)
_DROP = re.compile(r"drop column (?:if exists )?\"?(\w+)", re.I)
_RENAME = re.compile(r"rename column \"?(\w+)\"? to \"?(\w+)", re.I)
_NOT_A_COLUMN = (
    "primary",
    "unique",
    "constraint",
    "foreign",
    "check",
    "references",
    "on ",
    "exclude",
)


def _declared_columns() -> dict[str, set[str]]:
    """Table -> its columns, replaying every CREATE and ALTER in file order."""
    tables: dict[str, set[str]] = {}
    for root in _SCHEMA_SOURCES:
        for path in sorted(root.glob("*.sql")):
            sql = re.sub(r"--[^\n]*", "", path.read_text(encoding="utf-8"))
            events = sorted(
                [(m.start(), True, m) for m in _CREATE.finditer(sql)]
                + [(m.start(), False, m) for m in _ALTER.finditer(sql)],
                key=lambda e: e[0],
            )
            for _at, created, m in events:
                columns = tables.setdefault(m.group(1).lower(), set())
                if created:
                    for line in m.group(2).splitlines():
                        text = line.strip()
                        if text and not text.lower().startswith(_NOT_A_COLUMN):
                            columns.add(text.split()[0].strip('",').lower())
                    continue
                columns |= {c.lower() for c in _ADD.findall(m.group(2))}
                columns -= {c.lower() for c in _DROP.findall(m.group(2))}
                for before, after in _RENAME.findall(m.group(2)):
                    columns.discard(before.lower())
                    columns.add(after.lower())
            for loop in _LOOP.finditer(sql):
                added = {c.lower() for c in _LOOP_ADD.findall(loop.group(2))}
                for table in re.findall(r"'(helixis_\w+)'", loop.group(1)):
                    tables.setdefault(table.lower(), set()).update(added)
    return tables


#: Measured on helixis-test (information_schema.columns, 2026-10-05), for the
#: three tables the export read wrongly. The reader must see at least these,
#: or a green guard means a blind one (rule 10).
_LIVE = {
    "helixis_units": {
        "id",
        "buildium_id",
        "account_id",
        "company_id",
        "property_id",
        "unit_number",
        "market_rent",
        "raw",
        "created_at",
        "updated_at",
        "is_occupied",
        "pms",
    },
    "helixis_leases": {
        "id",
        "buildium_id",
        "account_id",
        "company_id",
        "property_id",
        "unit_id",
        "status",
        "lease_from_date",
        "lease_to_date",
        "rent",
        "raw",
        "created_at",
        "updated_at",
        "security_deposit",
        "pms",
    },
    "helixis_lease_balances": {
        "company_id",
        "lease_id",
        "account_id",
        "property_id",
        "unit_id",
        "total_balance",
        "snapshot_at",
        "raw",
        "balance_0_30",
        "balance_31_60",
        "balance_61_90",
        "balance_over_90",
        "pms",
    },
}


def test_the_schema_reader_sees_what_the_live_database_has() -> None:
    declared = _declared_columns()
    for table, live in _LIVE.items():
        missing = sorted(live - declared.get(table, set()))
        assert not missing, (table, missing)


def test_every_export_column_exists() -> None:
    """Each table's export reads named columns. One the schema does not have
    fails the read, and the customer's file came out empty."""
    declared = _declared_columns()
    for table, columns in EXPORT_TABLES.items():
        have = declared.get(f"helixis_{table}")
        assert have, f"helixis_{table} is not declared anywhere the reader looks"
        assert set(columns) <= have, (table, sorted(set(columns) - have))


async def test_every_export_query_is_company_scoped() -> None:
    """⚠ The single most important property in this module (gotcha 47). Every
    byte of the output is somebody's business records, so a missing WHERE does
    not produce a confusing answer — it hands one landlord another's books in
    a file they keep."""
    seen: list[str] = []
    pool = MagicMock()
    conn = pool.acquire.return_value.__aenter__.return_value

    async def _fetch(sql: str, *args: Any) -> list[dict[str, Any]]:
        seen.append(sql)
        assert _COMPANY in args, f"company_id was not bound: {sql}"
        return []

    conn.fetch = AsyncMock(side_effect=_fetch)
    await build_export(pool, _COMPANY, "X")

    assert len(seen) == len(EXPORT_TABLES), "not every table was queried"
    for sql in seen:
        assert "company_id = $1::uuid" in sql, f"unscoped export query: {sql}"


def test_the_export_omits_other_peoples_messages() -> None:
    """⚠ A deliberate exclusion, not an oversight. Chat transcripts and SMS
    bodies carry residents' words — people who never agreed to appear in a
    landlord's downloadable file. "Export everything" is a request for the
    company's own records, not a licence to widen who can read a text."""
    forbidden = {"sms_log", "orchestrator_runs", "chat_messages", "messages", "feedback"}
    assert not (forbidden & set(EXPORT_TABLES)), (
        "a table carrying third-party message content entered the export"
    )


def test_the_filename_survives_a_company_name_with_punctuation() -> None:
    name = export_filename('Blue Ridge / "Properties", LLC')
    assert "/" not in name and '"' not in name
    assert name.endswith(".zip")


# ── leaving and deleting, at the route ───────────────────────────────

_SOURCE = pathlib.Path(
    pathlib.Path(__file__).resolve().parents[2]
    / "src"
    / "helixis"
    / "api"
    / "routes"
    / "company.py"
).read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)


def _route(name: str) -> ast.AsyncFunctionDef:
    for node in ast.walk(_TREE):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} is not defined — the scan is pointed at nothing")


def _calls(fn: ast.AST) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                out.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                out.add(node.func.attr)
    return out


def test_leaving_reuses_the_last_admin_guard() -> None:
    """⚠ The reason this is not a plain delete. An admin who leaves an
    orphaned workspace strands it — nobody can invite, promote, or cancel the
    subscription, and only we can fix it."""
    calls = _calls(_route("leave_company"))
    assert "count_admins" in calls, (
        "leave_company does not check for the last admin — a workspace can be "
        "orphaned with no way back in"
    )
    assert "remove_member" in calls


def test_leaving_credits_the_seat_back() -> None:
    """Nobody complains about being over-counted in their own favour, so an
    unsynced departure sits for months and quietly overbills."""
    assert "sync_company_seats" in _calls(_route("leave_company"))


def test_deletion_requires_admin_and_a_typed_name() -> None:
    calls = _calls(_route("delete_company"))
    assert "require_role" in calls, "anybody in the workspace could delete it"
    assert "confirm_name" in _SOURCE, "no typed confirmation on an irreversible action"


def test_deletion_purges_the_buildium_mirror_explicitly() -> None:
    """⚠ NOT left to the cascade. ToS §18 requires API-derived data removed
    when the licence ends, and `purge_buildium_mirror` is the audited
    definition of that set — a cascade is whatever the schema happens to say
    today, which is exactly what `test_buildium_purge` exists to stop drifting.
    """
    assert "purge_buildium_mirror" in _calls(_route("delete_company"))


def test_deletion_verifies_the_name_before_deleting_anything() -> None:
    """Order is the property: a check that runs after the purge is decoration.

    ⚠ Compares STATEMENT line numbers, not text positions. The first cut used
    `ast.unparse(...).index(...)` and failed against the CORRECT code, because
    unparse keeps the docstring and the docstring explains this very ordering —
    so the comparison was reading the paragraph rather than the code. Gotcha
    40's trap, third variant in this session, and the tell was the same each
    time: a guard that fires on a file whose prose discusses the guard.
    """
    fn = _route("delete_company")
    body = [
        s for s in fn.body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))
    ]

    def _first_line(needle: str) -> int:
        for stmt in body:
            for node in ast.walk(stmt):
                if isinstance(node, ast.Attribute) and node.attr == needle:
                    return stmt.lineno
                if isinstance(node, ast.Name) and node.id == needle:
                    return stmt.lineno
        raise AssertionError(f"{needle} does not appear in delete_company's body")

    assert _first_line("confirm_name") < _first_line("purge_buildium_mirror"), (
        "the workspace is purged before the typed name is verified"
    )


@pytest.mark.parametrize("route", ["leave_company", "export_company_data", "delete_company"])
def test_every_new_route_is_rate_limited(route: str) -> None:
    """One of these downloads a whole portfolio and one destroys it."""
    assert "check_by_user" in _calls(_route(route))


def test_the_export_is_admin_only() -> None:
    """A single file containing the entire portfolio is a strictly stronger
    disclosure than any screen in the app, so it takes the strongest role."""
    src = ast.unparse(_route("export_company_data"))
    assert "require_role" in src and "ADMIN" in src


def test_deletion_purges_supabase_storage() -> None:
    """The cascade reaches 46 tables and not one uploaded file.

    Every object is keyed ``{company_id}/…`` in three buckets and outlives the
    rows that recorded it, so before this the customer got a confirmation and
    their leases stayed. A behavioural test of `purge_company_storage` cannot
    see a route that stops calling it (gotchas 62/66/69/103), which is the
    whole reason this is an AST check on the call site.
    """
    assert "purge_company_storage" in _calls(_route("delete_company")), (
        "the workspace delete no longer sweeps Storage — the database cascades and the files do not"
    )


def test_an_unfinished_storage_sweep_does_not_abort_the_deletion() -> None:
    """⚠ The direction is deliberate and worth pinning, because the opposite
    reads as the safer choice and is not.

    Refusing to delete a workspace because Supabase Storage hiccuped turns a
    transient error into a customer who cannot exercise a GDPR right —
    unrecoverable from their side. Leftover objects stay under the same
    ``{company_id}/`` prefix, so proceeding is the recoverable direction and
    the response carries what was left. So: no raise, no early return, no
    branch on the report between the sweep and the row delete.
    """
    fn = _route("delete_company")
    lines = {"storage": 0, "delete_row": 0}
    for stmt in fn.body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Name) and node.id == "purge_company_storage":
                lines["storage"] = lines["storage"] or stmt.lineno
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if "DELETE FROM public.companies" in node.value:
                    lines["delete_row"] = lines["delete_row"] or stmt.lineno
    assert lines["storage"] and lines["delete_row"], "could not locate both statements"

    # Nothing between the two may raise or return on the storage verdict.
    between = [
        s
        for s in fn.body
        if lines["storage"] < s.lineno < lines["delete_row"]
        and isinstance(s, (ast.Raise, ast.Return, ast.If))
    ]
    assert not between, (
        "a guard was added between the storage sweep and the row delete — an "
        "incomplete sweep must never block the workspace deletion"
    )


def test_the_deletion_response_says_when_storage_is_unfinished() -> None:
    """A count alone tells the reader nothing they can act on. The response
    carries the verdict AND the note naming the buckets; silence here would be
    the confident false claim this codebase keeps relearning, pointed at a
    legal obligation."""
    for field in ("storage_objects_deleted", "storage_complete", "storage_note"):
        assert field in _SOURCE, f"the delete response dropped {field}"


# ── the memory leg (2026-10-23) ───────────────────────────────────────


def test_deletion_erases_agent_memory_before_the_row_goes() -> None:
    """Memory rows are not foreign-keyed to ``companies``, so the cascade never
    reached them: every agent memory outlived the workspace it described. An
    AST check on the call site, for the same reason as the Storage one above."""
    fn = _route("delete_company")
    assert "_purge_company_memory" in _calls(fn), "the workspace delete no longer erases memory"
    lines = {"memory": 0, "delete_row": 0}
    for stmt in fn.body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Name) and node.id == "_purge_company_memory":
                lines["memory"] = lines["memory"] or stmt.lineno
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if "DELETE FROM public.companies" in node.value:
                    lines["delete_row"] = lines["delete_row"] or stmt.lineno
    assert 0 < lines["memory"] < lines["delete_row"]
    for field in ("memory_erased", "memory_complete"):
        assert field in _SOURCE, f"the delete response dropped {field}"


class _Memory:
    def __init__(self, *, available: bool = True, erased: int = 0, boom: bool = False) -> None:
        self.available = available
        self.erased = erased
        self.boom = boom
        self.calls: list[tuple[str, dict]] = []

    async def delete_company_memory(self, company_id: str, **kw: object) -> int:
        self.calls.append((company_id, kw))
        if self.boom:
            raise RuntimeError("vector store down")
        return self.erased


@pytest.mark.parametrize(
    ("memory", "expected"),
    [
        (None, (0, False)),  # no manager in this process: unknown, not zero
        (_Memory(available=False), (0, False)),
        (_Memory(boom=True), (0, False)),  # a failed erase is not "nothing to erase"
        (_Memory(erased=7), (7, True)),
        (_Memory(erased=0), (0, True)),  # genuinely nothing stored
    ],
)
async def test_the_memory_leg_reports_what_it_could_not_erase(memory, expected) -> None:
    from unittest.mock import patch

    from helixis.api.routes import company

    with patch.object(company, "get_memory_manager", return_value=memory):
        assert await company._purge_company_memory("co-1", "admin-1") == expected
    if isinstance(memory, _Memory) and memory.available:
        ((company_id, kw),) = memory.calls
        assert company_id == "co-1"
        assert kw == {"requested_by": "admin-1", "raise_on_error": True}
