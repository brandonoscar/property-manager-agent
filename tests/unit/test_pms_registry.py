"""``pms.registry`` agrees with every other place that lists the systems.

The list of property-management systems has to match in six places that
cannot import each other: the registry, the ``company_pms`` SQL, the one-PMS
trigger (SQL), the §18 purge classification, the analyst's SQL guard, and the
web app's ``lib/pms.ts``. A system missing from one of them is exactly the
defect Phase 0a exists to close (design/connector-shared-layer.md): before it,
anything that was not Rentvine read as Buildium in a dozen places. Each test
below reads both sides (CLAUDE.md rule 15).
"""

from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from helixis.analysis.sql_guard import ALLOWED_TABLES, guard_sql
from helixis.buildium.purge import PURGE_TABLES
from helixis.db import company_pms as company_pms_mod
from helixis.db.company_pms import company_pms, company_pms_strict, connected_to, one_pms_refusal
from helixis.pms import registry
from helixis.pms.registry import BUILDIUM, PMS, RENTVINE
from tests.unit.test_buildium_purge import KEEP_TABLES
from tests.unit.test_one_pms_race import _live_definition

REPO = Path(__file__).resolve().parents[2]
COMPANY = "00000000-0000-4000-8000-0000000000aa"


# ── the registry itself ───────────────────────────────────────────────


def test_buildium_is_first_and_the_only_system_with_live_tools() -> None:
    assert next(iter(PMS)) == BUILDIUM
    assert [key for key, found in PMS.items() if found.live_tools] == [BUILDIUM]


def test_each_entry_follows_the_naming_rule() -> None:
    """Design P3: one lowercase word, used for the `pms` column, the package
    and the credentials table alike."""
    for key, found in PMS.items():
        assert found.key == key
        assert re.fullmatch(r"[a-z]+", key)
        assert found.credentials_table == f"company_{key}_credentials"
        assert found.package == f"helixis.{key}"


def test_unknown_keys_read_as_no_system() -> None:
    for junk in (None, "", "appfolio", "Buildium", "rent_manager"):
        assert registry.spec(junk) is None
        assert registry.name_of(junk) is None
        assert registry.is_mirror_pms(junk) is False
    assert registry.is_mirror_pms(BUILDIUM) is False
    assert all(registry.is_mirror_pms(k) for k in PMS if k != BUILDIUM)


def test_a_missing_connector_package_reads_as_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Before a connector's package exists, every shared reader gets None and
    reads it as "fills nothing, says nothing".

    ⚠ Re-pointed 2026-10-05 (rule 19), not relaxed: this used Rent Manager and
    Propertyware while neither package existed. Their connector branches add
    those packages, so a system whose package is missing is stood up here.
    """
    ghost = registry.PmsSpec(
        "ghost", "Ghost", "company_ghost_credentials", False, "helixis.no_such_connector"
    )
    monkeypatch.setattr(registry, "PMS", {**registry.PMS, "ghost": ghost})
    assert registry.connector_attr("ghost", "sync", "SYNC_STATE_STAGE") is None
    # Rentvine's package exists, so the lookup is real, not a blanket None.
    assert registry.connector_attr(RENTVINE, "sync", "SYNC_STATE_STAGE")


def test_an_import_error_inside_a_connector_is_raised_not_hidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connector that exists and fails to import is a bug. Reading it as
    "not built" would make it fill nothing, in silence."""

    def _broken(name: str) -> Any:
        raise ModuleNotFoundError("No module named 'httpx_typo'", name="httpx_typo")

    monkeypatch.setattr(registry.importlib, "import_module", _broken)
    with pytest.raises(ModuleNotFoundError):
        registry.connector_attr(RENTVINE, "sync", "SYNC_STATE_STAGE")


# ── registry ↔ company_pms SQL ────────────────────────────────────────


def test_company_pms_asks_every_other_systems_credentials_table() -> None:
    sql = company_pms_mod._SQL
    for key, found in PMS.items():
        if key == BUILDIUM:
            # The default: a company with no other system's row is Buildium.
            assert "ELSE 'buildium'" in sql
            continue
        assert re.search(
            rf"FROM {found.credentials_table} WHERE company_id = \$1::uuid\s*\)\s*THEN '{key}'",
            sql,
        ), f"company_pms never answers {key!r}"


def test_the_fallback_query_answers_only_what_existed_before_0107() -> None:
    sql = company_pms_mod._SQL_BEFORE_THE_NEW_TABLES
    assert "company_rentvine_credentials" in sql and "ELSE 'buildium'" in sql
    assert "rentmanager" not in sql and "propertyware" not in sql


def test_the_query_before_0109_answers_every_system_but_doorloop() -> None:
    """The step between: a database with 0107's tables and without 0109's.
    It must still answer Rent Manager and Propertyware, or code reaching it
    would read a company on either as Buildium."""
    sql = company_pms_mod._SQL_BEFORE_DOORLOOP
    for key, found in PMS.items():
        if key in (BUILDIUM, "doorloop"):
            continue
        assert re.search(
            rf"FROM {found.credentials_table} WHERE company_id = \$1::uuid\s*\)\s*THEN '{key}'",
            sql,
        ), key
    assert "doorloop" not in sql and "ELSE 'buildium'" in sql
    assert company_pms_mod._QUERIES == (
        company_pms_mod._SQL,
        company_pms_mod._SQL_BEFORE_DOORLOOP,
        company_pms_mod._SQL_BEFORE_THE_NEW_TABLES,
    )


class _Conn:
    """Answers ``rows`` in order; the first ``raise_count`` calls raise ``raise_first``."""

    def __init__(
        self, *rows: Any, raise_first: Exception | None = None, raise_count: int = 1
    ) -> None:
        self.rows = list(rows)
        self.raise_first = raise_first
        self.raise_count = raise_count
        self.sql: list[str] = []

    async def fetchrow(self, sql: str, *_a: Any) -> Any:
        self.sql.append(sql)
        if self.raise_first is not None and len(self.sql) <= self.raise_count:
            raise self.raise_first
        return self.rows.pop(0)


class _Pool:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn

    def acquire(self) -> Any:
        @asynccontextmanager
        async def _acq() -> Any:
            yield self.conn

        return _acq()


def test_a_database_without_the_new_tables_still_answers_exactly() -> None:
    """Code deployed before 0107 is applied: the new query names a table that
    does not exist. The old query is exact then, because no company can be on
    a system whose credentials table does not exist.

    ⚠ Re-pointed 2026-10-06 (rule 19), not relaxed: 0109 put a step between.
    Both newer queries name 0107's tables, so on such a database both raise
    before the pre-0107 query answers."""
    missing = asyncpg.exceptions.UndefinedTableError(
        'relation "company_rentmanager_credentials" does not exist'
    )
    conn = _Conn({"pms": "rentvine"}, raise_first=missing, raise_count=2)
    assert asyncio.run(company_pms(_Pool(conn), COMPANY)) == "rentvine"
    assert conn.sql == list(company_pms_mod._QUERIES)

    strict = _Conn({"pms": "rentvine"}, raise_first=missing, raise_count=2)
    assert asyncio.run(company_pms_strict(_Pool(strict), COMPANY)) == "rentvine"


def test_a_database_without_0109_still_answers_rent_manager_and_propertyware() -> None:
    """Code deployed before 0109 is applied: only DoorLoop's table is missing,
    so the four-system query answers, and a Propertyware company is still
    Propertyware (not Buildium, which a one-step fallback to before 0107 said)."""
    missing = asyncpg.exceptions.UndefinedTableError(
        'relation "company_doorloop_credentials" does not exist'
    )
    conn = _Conn({"pms": "propertyware"}, raise_first=missing)
    assert asyncio.run(company_pms(_Pool(conn), COMPANY)) == "propertyware"
    assert conn.sql == [company_pms_mod._SQL, company_pms_mod._SQL_BEFORE_DOORLOOP]

    strict = _Conn({"pms": "propertyware"}, raise_first=missing)
    assert asyncio.run(company_pms_strict(_Pool(strict), COMPANY)) == "propertyware"


def test_only_a_missing_table_falls_back() -> None:
    """Any other database error is still a failed read: the strict reader
    raises (the detector cycle then skips the company)."""
    conn = _Conn({"pms": "rentvine"}, raise_first=RuntimeError("pool exhausted"))
    with pytest.raises(RuntimeError):
        asyncio.run(company_pms_strict(_Pool(conn), COMPANY))
    assert conn.sql == [company_pms_mod._SQL]


@pytest.mark.parametrize("key", list(PMS))
def test_company_pms_returns_each_registry_key(key: str) -> None:
    assert asyncio.run(company_pms(_Pool(_Conn({"pms": key})), COMPANY)) == key
    assert asyncio.run(company_pms_strict(_Pool(_Conn({"pms": key})), COMPANY)) == key


def test_company_pms_never_returns_a_value_outside_the_registry() -> None:
    """The fail-open reader falls back rather than passing junk through: a
    caller comparing against the registry must never see a third answer."""
    assert asyncio.run(company_pms(_Pool(_Conn({"pms": "appfolio"})), COMPANY)) == BUILDIUM


# ── registry ↔ the trigger's message ↔ the connect routes ─────────────


def _trigger_message(company: str, name: str) -> str:
    """The refusal exactly as the live trigger builds it, from its SQL."""
    body, _ = _live_definition()
    fmt = re.search(
        r"errcode = 'unique_violation',\s*message = format\(\s*((?:'[^']*'\s*)+),", body
    )
    assert fmt, "the trigger's refusal is no longer a format() call; re-point this test"
    template = "".join(re.findall(r"'([^']*)'", fmt.group(1)))
    return template.replace("%s", company, 1).replace("%s", name, 1)


@pytest.mark.parametrize("key", list(PMS))
def test_the_routes_read_back_every_name_the_trigger_can_report(key: str) -> None:
    """Both sides: the message the SQL builds, parsed by the Python that turns
    it into a 409. A trigger that reworded its message would make every 409 say
    "another property-management system" with nothing failing."""
    found = PMS[key]
    message = _trigger_message(COMPANY, found.name)
    assert connected_to(message) == found
    assert connected_to(asyncpg.exceptions.UniqueViolationError(message)) == found


def test_a_message_naming_no_known_system_names_none() -> None:
    assert connected_to("duplicate key value violates unique constraint") is None
    assert connected_to("company x is already connected to Yardi; one PMS per company") is None


def test_the_rentvine_409_text_is_unchanged_when_the_other_system_is_buildium() -> None:
    message = _trigger_message(COMPANY, "Buildium")
    assert one_pms_refusal(RENTVINE, message) == (
        "This workspace is already connected to Buildium. Occupella supports one "
        "property-management system per workspace — disconnect Buildium first, then "
        "connect Rentvine."
    )


def test_the_409_names_the_real_system_or_says_another() -> None:
    message = _trigger_message(COMPANY, "Rent Manager")
    assert "already connected to Rent Manager" in one_pms_refusal(BUILDIUM, message)
    assert "then connect Buildium" in one_pms_refusal(BUILDIUM, message)
    vague = one_pms_refusal(BUILDIUM, "unique violation")
    assert "another property-management system" in vague
    assert "Buildium" in vague  # what to connect after disconnecting


# ── registry ↔ purge ↔ sql_guard ──────────────────────────────────────


def test_every_credentials_table_is_classified_and_never_purged() -> None:
    """A Buildium disconnect purges the mirror. It must never delete another
    system's credentials, and under the one-PMS rule it never needs to."""
    for found in PMS.values():
        assert found.credentials_table in KEEP_TABLES
        assert found.credentials_table not in PURGE_TABLES


def test_the_analyst_can_read_no_credentials_table() -> None:
    """The allowlist is the real control; the deny list is the second guard,
    and it must cover a system added later without an edit."""
    assert not any("_credentials" in table for table in ALLOWED_TABLES)
    for found in PMS.values():
        for sql in (
            f"SELECT * FROM {found.credentials_table}",
            f"SELECT * FROM helixis_leases WHERE id IN (SELECT 1 FROM {found.credentials_table})",
        ):
            verdict = guard_sql(sql)
            # Refused by the deny list, before the allowlist is even asked.
            assert not verdict.allowed and "restricted identifier" in verdict.reason, sql
    assert "restricted identifier" in guard_sql("SELECT * FROM company_appfolio_credentials").reason


def test_every_system_has_its_own_analyst_column_row() -> None:
    """``sql_guard``'s fallback is for a company whose PMS could not be READ,
    not for a system nobody decided about: which ledger columns a system fills
    is a fact about its connector, written down per system."""
    from helixis.analysis.sql_guard import _HIDDEN_COLUMNS

    assert set(_HIDDEN_COLUMNS) == set(PMS)
    assert all("pms" in hidden for hidden in _HIDDEN_COLUMNS.values())


# ── registry ↔ the web app ────────────────────────────────────────────


def test_the_web_app_lists_the_same_systems() -> None:
    """``frontend/src/lib/pms.ts`` is the web app's copy. A system it does not
    know is read as Buildium there: named Buildium, sent Buildium's sync."""
    source = (REPO / "frontend/src/lib/pms.ts").read_text(encoding="utf-8")
    union = re.search(r"export type PmsId = ([^;]+);", source)
    assert union, "lib/pms.ts no longer declares PmsId"
    assert re.findall(r"'(\w+)'", union.group(1)) == list(PMS)
    entries = re.findall(
        r"\n  (\w+): \{ id: '(\w+)', name: '([^']+)', liveTools: (true|false) \},", source
    )
    assert [(key, name, live == "true") for key, entry_id, name, live in entries] == [
        (found.key, found.name, found.live_tools) for found in PMS.values()
    ]
    assert all(key == entry_id for key, entry_id, _, _ in entries)


# ── the names other modules show ──────────────────────────────────────


def test_every_customer_facing_name_comes_from_the_registry() -> None:
    from helixis.db.pms_coverage import PMS_NAME

    assert {key: found.name for key, found in PMS.items()} == PMS_NAME


def test_the_unavailable_reason_names_systems_by_their_registry_name() -> None:
    """``pms.title()`` said "Rentmanager"."""
    from helixis.agent.planner import tool_catalog

    buildium_write = next(spec for spec in tool_catalog._BY_ID.values() if spec.pms == BUILDIUM).id
    reason = tool_catalog.unavailable_reason(buildium_write, "rentmanager")
    assert reason == (
        "This writes to Buildium, and this workspace is connected to Rent Manager. "
        "Make this change in Rent Manager."
    )
    assert tool_catalog.unavailable_reason(buildium_write, BUILDIUM) is None


def _ledger_description(action_type: str) -> str:
    from helixis.agents.write_ledger import _description

    return _description(action_type, {"details": "Created a work order."}, "blocked_needs_review")


def test_an_unconfirmed_write_names_the_system_it_went_to() -> None:
    assert _ledger_description("buildium_create_work_order").endswith("check Buildium.")
    assert _ledger_description("rentvine_create_work_order").endswith("check Rentvine.")
    assert _ledger_description("rentmanager_create_work_order").endswith("check Rent Manager.")
    assert _ledger_description("propertyware_create_work_order").endswith("check Propertyware.")
    assert _ledger_description("doorloop_create_work_order").endswith("check DoorLoop.")
    # The unattended note writer writes to Buildium.
    assert _ledger_description("notes.autonomous").endswith("check Buildium.")


def test_a_confirmed_write_is_worded_as_before() -> None:
    from helixis.agents.write_ledger import _description

    confirm = {"details": "Created a work order."}
    worded = _description("rentvine_create_work_order", confirm, "executed")
    assert worded == "Created a work order."
