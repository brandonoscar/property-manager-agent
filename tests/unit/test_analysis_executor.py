"""Unit tests for the analyst executor (analysis/executor.py) + tool gating.

The fake pool records exactly what would hit Postgres, so these pin the
containment behavior: READ ONLY transaction, statement timeout, the
company-scoped shadow CTEs, and the parameter-bound company id.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

from helixis.analysis.executor import (
    MAX_ROWS,
    STATEMENT_TIMEOUT_MS,
    compose_scoped_sql,
    run_analysis_query,
)
from helixis.analysis.sql_guard import ALLOWED_TABLES

_COMPANY = "11111111-1111-1111-1111-111111111111"


class FakeConn:
    """A connection that answers the executor's TWO reads differently.

    ⚠ It used to return the same rows for every ``fetch``, which stopped being
    honest the moment the executor gained a catalog read of its own: the
    hidden-column filter would have been handed the analysis result as if it
    were ``information_schema``. A fake that answers whatever it is asked is
    how a wrong query passes (gotchas 96/123) — so the catalog read is answered
    as a catalog read, and :attr:`analysis_fetches` is what the assertions below
    index instead of ``fetches[0]``.
    """

    def __init__(
        self,
        rows: list[dict[str, Any]] | Exception | None = None,
        *,
        columns: dict[str, list[str]] | None = None,
        pms: str = "buildium",
    ) -> None:
        self.rows = rows if rows is not None else []
        self.columns = columns if columns is not None else {}
        self.pms = pms
        self.executed: list[str] = []
        self.fetches: list[tuple[str, tuple[Any, ...]]] = []
        self.txn_kwargs: dict[str, Any] | None = None

    def transaction(self, **kwargs: Any):
        self.txn_kwargs = kwargs

        @asynccontextmanager
        async def _txn():
            yield

        return _txn()

    async def execute(self, sql: str) -> None:
        self.executed.append(sql)

    async def fetchrow(self, sql: str, *_args: Any) -> dict[str, Any]:
        # `company_pms` — the only fetchrow the executor makes.
        assert "company_rentvine_credentials" in sql
        return {"pms": self.pms}

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.fetches.append((sql, args))
        if "information_schema.columns" in sql:
            # ⚠ TWO different catalog queries reach here and they do not select
            # the same columns — the describer asks for `data_type`, the
            # executor's projection builder does not. A fake that answered both
            # with one shape would hand the describer a row missing a key it
            # reads, which is a fake bug wearing a code bug's clothes.
            wanted = set(args[0]) if args else set(self.columns)
            rows = [
                {"table_name": t, "column_name": c}
                for t, cols in self.columns.items()
                if t in wanted
                for c in cols
            ]
            if "data_type" in sql:
                for r in rows:
                    r["data_type"] = "text"
            return rows
        if isinstance(self.rows, Exception):
            raise self.rows
        return self.rows

    @property
    def analysis_fetches(self) -> list[tuple[str, tuple[Any, ...]]]:
        """The agent's query only — never the executor's own catalog read."""
        return [f for f in self.fetches if "information_schema.columns" not in f[0]]


class FakePool:
    def __init__(self, conn: FakeConn) -> None:
        self.conn = conn

    def acquire(self):
        @asynccontextmanager
        async def _acq():
            yield self.conn

        return _acq()


def _run(sql: str, conn: FakeConn):
    return asyncio.run(run_analysis_query(sql, _COMPANY, FakePool(conn)))


# ── compose_scoped_sql ──────────────────────────────────────────


def test_compose_plain_select_gets_shadow_ctes_and_cap() -> None:
    out = compose_scoped_sql("SELECT COUNT(*) FROM helixis_bills", ("helixis_bills",))
    assert (
        "helixis_bills AS (SELECT * FROM public.helixis_bills WHERE company_id = $1::uuid)" in out
    )
    assert out.rstrip().endswith(f"LIMIT {MAX_ROWS}")


def test_compose_merges_agent_cte_list() -> None:
    out = compose_scoped_sql(
        "WITH monthly AS (SELECT 1 AS x FROM helixis_bills) SELECT * FROM monthly",
        ("helixis_bills",),
    )
    # One WITH only: our shadows first, then the agent's CTE, comma-joined.
    assert out.count("WITH ") == 1
    assert "helixis_bills AS (SELECT * FROM public.helixis_bills" in out
    assert "monthly AS (SELECT 1 AS x FROM helixis_bills)" in out


def test_compose_one_shadow_per_table() -> None:
    out = compose_scoped_sql(
        "SELECT * FROM helixis_bills b JOIN helixis_vendors v ON v.id = b.vendor_id",
        ("helixis_bills", "helixis_vendors"),
    )
    assert out.count("WHERE company_id = $1::uuid") == 2


# ── run_analysis_query containment ──────────────────────────────


def test_happy_path_runs_readonly_with_timeout_and_scoping() -> None:
    conn = FakeConn(rows=[{"n": 3}])
    result = _run("SELECT COUNT(*) AS n FROM helixis_bills", conn)

    assert result.ok and result.rows == [{"n": 3}] and result.row_count == 1
    # DB-enforced read-only transaction.
    assert conn.txn_kwargs == {"readonly": True}
    # Statement timeout set inside the transaction.
    assert conn.executed == [f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}"]
    # The executed SQL is the scoped form, with the company id bound as $1.
    # ⚠ `analysis_fetches`, not `fetches[0]` — the executor now also reads
    # information_schema to build the hidden-column projection, and that read
    # comes first. Re-pointed rather than deleted: the property is unchanged.
    sql, args = conn.analysis_fetches[0]
    assert "WHERE company_id = $1::uuid" in sql
    assert args == (_COMPANY,)
    assert result.executed_sql == sql


# ── the hidden-column set (ADR 0010 Decision (a)) ───────────────


def test_the_discriminator_is_not_in_the_cte_the_model_can_query() -> None:
    """Filtering the SCHEMA TOOL alone does not close this.

    A model writing `SELECT *` gets every column of the shadow CTE back
    whatever it was told, so `pms` has to be absent from the CTE itself.
    """
    conn = FakeConn(
        rows=[{"n": 1}],
        columns={"helixis_bills": ["id", "company_id", "buildium_id", "pms", "total_amount"]},
    )
    result = _run("SELECT COUNT(*) AS n FROM helixis_bills", conn)
    assert result.ok
    sql = result.executed_sql
    assert '"pms"' not in sql, "the discriminator reached the CTE"
    for kept in ('"id"', '"company_id"', '"buildium_id"', '"total_amount"'):
        assert kept in sql, f"{kept} went missing — hiding too much is the worse bug"


def test_a_table_with_nothing_hidden_keeps_its_original_sql() -> None:
    """The citation a Buildium customer sees must not churn on tables that
    migration 0101 never touched — `executed_sql` IS the REVERSE_REPORT."""
    conn = FakeConn(rows=[{"n": 1}], columns={"helixis_reminders": ["id", "company_id", "status"]})
    result = _run("SELECT COUNT(*) AS n FROM helixis_reminders", conn)
    assert result.ok
    assert (
        "helixis_reminders AS (SELECT * FROM public.helixis_reminders "
        "WHERE company_id = $1::uuid)" in result.executed_sql
    )


def test_a_rentvine_company_still_sees_the_ledger_columns() -> None:
    """Decision (b): a Rentvine analyst that cannot see `ledger_type` computes
    WRONG money totals. Only `pms` is withheld from it."""
    conn = FakeConn(
        rows=[{"n": 1}],
        pms="rentvine",
        columns={"helixis_bills": ["id", "company_id", "pms", "ledger_id", "ledger_type"]},
    )
    result = _run("SELECT COUNT(*) AS n FROM helixis_bills", conn)
    assert result.ok
    assert '"pms"' not in result.executed_sql
    assert '"ledger_id"' in result.executed_sql
    assert '"ledger_type"' in result.executed_sql


def test_a_new_ordinary_column_still_reaches_the_model() -> None:
    """The control that matters more than the filter: this is a NAMED list of
    exclusions, never "hide what I do not recognise" — the second would blind
    the analyst to every column added from now on."""
    conn = FakeConn(
        rows=[{"n": 1}],
        columns={"helixis_bills": ["id", "company_id", "pms", "some_column_added_later"]},
    )
    result = _run("SELECT COUNT(*) AS n FROM helixis_bills", conn)
    assert result.ok
    assert '"some_column_added_later"' in result.executed_sql


def test_an_unreadable_catalog_costs_the_filter_and_never_the_query() -> None:
    """Degrading to `*` leaks a constant-valued column; refusing takes the
    analyst down for a customer it worked for yesterday."""

    class Broken(FakeConn):
        async def fetch(self, sql: str, *args: Any):
            if "information_schema.columns" in sql:
                raise RuntimeError("catalog unavailable")
            return await super().fetch(sql, *args)

    conn = Broken(rows=[{"n": 1}], columns={"helixis_bills": ["id", "pms"]})
    result = _run("SELECT COUNT(*) AS n FROM helixis_bills", conn)
    assert result.ok
    assert "SELECT * FROM public.helixis_bills" in result.executed_sql


def test_the_describer_and_the_executor_read_one_set() -> None:
    """gotcha 28 on the surface where a disagreement is invisible: the schema
    tool could say a column does not exist while `SELECT *` hands it over."""
    import ast
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    for rel in ("src/helixis/agents/analysis_tools.py", "src/helixis/analysis/executor.py"):
        tree = ast.parse((repo / rel).read_text())
        calls = {
            n.func.id
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        assert "hidden_columns" in calls, f"{rel} does not read the one set"


def test_hiding_everything_falls_back_rather_than_composing_broken_sql() -> None:
    """An empty projection composes to `SELECT  FROM`, a syntax error on every
    query touching that table."""
    from helixis.analysis.executor import compose_scoped_sql as compose

    out = compose("SELECT 1 FROM helixis_bills", ("helixis_bills",), {"helixis_bills": []})
    assert "SELECT * FROM public.helixis_bills" in out


def test_the_schema_tool_output_is_unchanged_by_the_new_column() -> None:
    """The claim ADR 0010 makes, measured WITH ITS CONTROL.

    The control is the pre-migration schema in the same run: a filter that hid
    too much would pass a one-sided "is `pms` absent?" check identically, and
    hiding too much is the worse bug.

    ⚠ Imports the tool module, so it needs the agents SDK — it runs in CI. It
    was also run locally once against a minimal stub before being trusted
    (gotcha 35: a case that has never run is not a passing case): pre- and
    post-migration output came back identical, with `pms` absent.
    """
    from helixis.agents import analysis_tools as at

    before_cols = ["id", "company_id", "buildium_id", "total_amount"]
    after_cols = [*before_cols, "pms"]

    class _S:
        class analysis:  # noqa: N801 — settings shape stub
            enabled = True

    def _describe(cols: list[str]) -> Any:
        conn = FakeConn(columns={"helixis_bills": cols})
        # every allowlisted table answers; only helixis_bills is asserted on
        conn.columns = {t: cols for t in ALLOWED_TABLES}
        at_pool = FakePool(conn)
        monkey = {
            "get_settings": lambda: _S,
            "current_memory_context": lambda: type("C", (), {"company_id": _COMPANY})(),
            "get_pool": lambda: at_pool,
        }
        saved = {k: getattr(at, k) for k in monkey}
        for k, v in monkey.items():
            setattr(at, k, v)
        try:
            return json.loads(asyncio.run(at.describe_mirror_schema_impl()))["tables"]
        finally:
            for k, v in saved.items():
                setattr(at, k, v)

    before = _describe(before_cols)
    after = _describe(after_cols)
    assert after == before, "a Buildium customer's analyst schema view changed"
    assert not any(c.startswith("pms ") for c in after["helixis_bills"])


def test_guard_rejection_never_touches_the_db() -> None:
    conn = FakeConn()
    result = _run("DROP TABLE helixis_bills", conn)
    assert not result.ok and result.error_kind == "rejected_by_guard"
    assert conn.fetches == [] and conn.executed == []


def test_db_error_returns_structured_query_error() -> None:
    conn = FakeConn(rows=RuntimeError('column "total_amnt" does not exist'))
    result = _run("SELECT total_amnt FROM helixis_bills", conn)
    assert not result.ok and result.error_kind == "query_error"
    assert "total_amnt" in result.error
    # The scoped SQL is still reported so the agent can see what ran.
    assert "helixis_bills" in result.executed_sql


def test_no_company_refuses_before_guard() -> None:
    result = asyncio.run(
        run_analysis_query("SELECT 1 FROM helixis_bills", "", FakePool(FakeConn()))
    )
    assert not result.ok and result.error_kind == "no_company"


def test_values_are_json_safe_and_truncation_flagged() -> None:
    conn = FakeConn(
        rows=[
            {
                "amount": Decimal("2450.50"),
                "due": date(2026, 7, 1),
                "id": UUID(_COMPANY),
                "memo": "x" * 2000,
            }
        ]
        * MAX_ROWS
    )
    result = _run("SELECT * FROM helixis_bills", conn)
    assert result.ok and result.truncated
    row = result.rows[0]
    assert row["amount"] == 2450.5
    assert row["due"] == "2026-07-01"
    assert row["id"] == _COMPANY
    assert len(row["memo"]) <= 500
    # The whole payload serializes.
    json.dumps(result.to_payload())


# ── tool-layer self-gating (analysis_tools) ─────────────────────


def test_tool_gates_dark_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from helixis.agents import analysis_tools

    class _S:
        class analysis:  # noqa: N801 — settings shape stub
            enabled = False

    monkeypatch.setattr(analysis_tools, "get_settings", lambda: _S)
    out = json.loads(asyncio.run(analysis_tools.run_analysis_impl("SELECT 1")))
    assert out["ok"] is False and out["error_kind"] == "analysis_unavailable"
    out2 = json.loads(asyncio.run(analysis_tools.describe_mirror_schema_impl()))
    assert out2["error_kind"] == "analysis_unavailable"


def test_tool_requires_company_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from helixis.agents import analysis_tools

    class _S:
        class analysis:  # noqa: N801
            enabled = True

    monkeypatch.setattr(analysis_tools, "get_settings", lambda: _S)
    monkeypatch.setattr(analysis_tools, "current_memory_context", lambda: None)
    out = json.loads(asyncio.run(analysis_tools.run_analysis_impl("SELECT 1")))
    assert out["ok"] is False and out["error_kind"] == "no_company"


# ── coverage: a table the company's PMS does not feed is refused ─────────────


def _on_rentvine(monkeypatch: pytest.MonkeyPatch, landed: set[str] | None = None) -> None:
    from helixis.db import pms_coverage
    from helixis.rentvine.sync import SYNC_STATE_STAGE

    stages = set(SYNC_STATE_STAGE.values()) if landed is None else landed

    async def _rentvine(_pool: Any, _company_id: str) -> str:
        return "rentvine"

    async def _fresh(_pool: Any, _company_id: str) -> dict[str, str]:
        return {s: "2026-10-02T00:00:00Z" for s in stages}

    monkeypatch.setattr(pms_coverage, "company_pms_strict", _rentvine)
    monkeypatch.setattr(pms_coverage, "get_freshness", _fresh)


def test_a_table_rentvine_does_not_feed_is_refused_not_counted_as_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rentvine syncs no tasks, so COUNT(*) over helixis_tasks is 0 for a reason
    that has nothing to do with the customer's tasks (rule 14)."""
    _on_rentvine(monkeypatch)
    conn = FakeConn(rows=[{"n": 0}], columns={"helixis_tasks": ["id"]})
    result = _run("SELECT COUNT(*) AS n FROM helixis_tasks", conn)
    assert not result.ok and result.error_kind == "not_synced"
    assert "helixis_tasks" in result.error and "Rentvine" in result.error
    assert "zero" in result.error
    assert conn.analysis_fetches == [] and conn.executed == []


def test_the_same_query_runs_for_buildium() -> None:
    conn = FakeConn(rows=[{"n": 3}], columns={"helixis_tasks": ["id"]})
    result = _run("SELECT COUNT(*) AS n FROM helixis_tasks", conn)
    assert result.ok and result.rows == [{"n": 3}]


def test_a_rentvine_table_that_has_landed_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    _on_rentvine(monkeypatch)
    conn = FakeConn(rows=[{"n": 28}], columns={"helixis_leases": ["id"]})
    assert _run("SELECT COUNT(*) AS n FROM helixis_leases", conn).ok


def test_a_rentvine_table_that_has_not_landed_yet_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Payment history fills a few hundred leases a pass: before its stage is
    stamped, "nobody paid" is just "not read yet" (gotcha 174)."""
    from helixis.rentvine.sync import SYNC_STATE_STAGE

    _on_rentvine(monkeypatch, set(SYNC_STATE_STAGE.values()) - {"transactions"})
    conn = FakeConn(rows=[{"n": 0}], columns={"helixis_lease_transactions": ["id"]})
    result = _run("SELECT COUNT(*) AS n FROM helixis_lease_transactions", conn)
    assert result.error_kind == "not_synced"
    assert conn.analysis_fetches == []


def test_one_unfed_table_in_a_join_refuses_the_whole_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _on_rentvine(monkeypatch)
    conn = FakeConn(rows=[], columns={"helixis_leases": ["id"], "helixis_tasks": ["id"]})
    result = _run("SELECT COUNT(*) FROM helixis_leases l JOIN helixis_tasks t ON t.id = l.id", conn)
    assert result.error_kind == "not_synced"
    assert "helixis_leases" not in result.error.split("Not synced for this company:")[1]


def test_the_describer_leaves_out_and_names_what_the_executor_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both sides of the boundary (rule 15): the schema the model plans against
    and the query it can run agree on which tables exist for this company."""
    from helixis.agents import analysis_tools as at

    _on_rentvine(monkeypatch)

    class _S:
        class analysis:  # noqa: N801 — settings shape stub
            enabled = True

    conn = FakeConn(columns={t: ["id"] for t in ALLOWED_TABLES}, pms="rentvine")
    monkeypatch.setattr(at, "get_settings", lambda: _S)
    monkeypatch.setattr(
        at, "current_memory_context", lambda: type("C", (), {"company_id": _COMPANY})()
    )
    monkeypatch.setattr(at, "get_pool", lambda: FakePool(conn))
    out = json.loads(asyncio.run(at.describe_mirror_schema_impl()))
    assert "helixis_tasks" not in out["tables"] and "helixis_tasks" in out["not_synced"]
    assert "helixis_leases" in out["tables"] and "helixis_leases" not in out["not_synced"]
    for table in out["not_synced"]:
        refused = _run(f"SELECT COUNT(*) FROM {table}", FakeConn(columns={table: ["id"]}))
        assert refused.error_kind == "not_synced", table
