"""Guarded execution for analyst SQL (ADR 0007, slice 1).

Runs an agent-written, guard-approved query with layered containment that is
enforceable and testable IN CODE (this repo's tenancy model scopes in
application SQL, not database roles — see mirror_tools.py):

1. **Read-only at the database**: the query runs inside a Postgres
   ``READ ONLY`` transaction — the server itself refuses any write, no
   matter what slipped past the guard.
2. **Tenancy via CTE shadowing**: for every table the query references, the
   executor prepends a company-scoped CTE of the SAME NAME
   (``WITH helixis_bills AS (SELECT * FROM public.helixis_bills WHERE
   company_id = $1), …``). Unqualified references inside the agent's query
   resolve to the shadow, so other tenants' rows are structurally invisible.
   The guard makes this airtight by rejecting schema-qualified names (which
   would bypass a shadow) and CTE names that collide with mirror tables.
3. **Statement timeout** (``SET LOCAL``) and the guard's outer row cap.

The result carries the exact executed SQL so the agent's REVERSE_REPORT
citation is the real computation, not a paraphrase. Failures never raise into
the agent loop — they come back as structured errors the model can read and
self-correct from (bad column name → Postgres says so → retry).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from helixis.analysis.sql_guard import MAX_ROWS, guard_sql, hidden_columns, visible_columns
from helixis.db.company_pms import company_pms
from helixis.db.pms_coverage import Coverage, coverage_for

logger = logging.getLogger(__name__)

#: Server-side cap per query — generous for reporting, fatal for runaways.
STATEMENT_TIMEOUT_MS = 5_000

#: Cells longer than this are truncated in the returned rows (a memo column
#: should never blow up the agent's context window).
_MAX_CELL_CHARS = 500

#: Postgres error text returned to the model is capped — enough to
#: self-correct a bad column name, not enough to flood the context.
_MAX_ERROR_CHARS = 400


@dataclass
class AnalysisResult:
    """Outcome of one guarded query, JSON-safe for the tool boundary."""

    ok: bool
    #: Machine-readable failure class: "" | "rejected_by_guard" |
    #: "query_error" | "no_company" | "analysis_unavailable" | "db_unavailable"
    #: | "not_synced" (a table this company's PMS does not feed, or not yet)
    error_kind: str = ""
    error: str = ""
    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    row_count: int = 0
    #: True when exactly MAX_ROWS came back — the cap may have clipped data.
    truncated: bool = False
    #: The SQL that actually ran (scoped + capped) — the citation.
    executed_sql: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "error_kind": self.error_kind,
            "error": self.error,
            "columns": self.columns,
            "rows": self.rows,
            "row_count": self.row_count,
            "truncated": self.truncated,
            "executed_sql": self.executed_sql,
        }


def compose_scoped_sql(
    normalized_sql: str,
    tables: tuple[str, ...],
    visible: Mapping[str, Sequence[str]] | None = None,
) -> str:
    """Wrap ``normalized_sql`` in company-scoped shadow CTEs + the row cap.

    Produces::

        SELECT * FROM (
            WITH <t1> AS (SELECT * FROM public.<t1> WHERE company_id = $1::uuid),
                 <t2> AS (…),
                 <agent's own CTEs, when its query started with WITH>
            <agent's SELECT>
        ) AS _analyst LIMIT MAX_ROWS

    ``$1`` is bound to the company id by the executor — the agent's SQL was
    already rejected by the guard if it contained any ``$``.

    ⚠ **``visible`` is the OTHER HALF of the analyst's hidden-column set, and
    filtering the describer alone does not close it** (ADR 0010 Decision (a)).
    A model writing ``SELECT *`` gets every column of the shadow CTE back
    regardless of what the schema tool told it, so a hidden column has to be
    absent from the CTE itself. When a table's entry is present the CTE projects
    that list explicitly; a table with nothing hidden keeps ``*``, so its
    ``executed_sql`` — the REVERSE_REPORT citation — is byte-identical to what
    it was before the column existed.

    ⚠ **PostgreSQL has NO ``SELECT * EXCEPT (...)``** (probed — Phase 2.5 round
    2, F-11), which is why a positive list is needed at all, and why that list
    must be COMPUTED from a live catalog read rather than written down here: a
    hardcoded list is a second copy of the schema that drifts on the next
    migration.
    """
    cols = dict(visible or {})
    shadows = ",\n".join(
        f"{t} AS (SELECT {_projection(cols.get(t))} FROM public.{t} WHERE company_id = $1::uuid)"
        for t in tables
    )
    body = normalized_sql.strip()
    if body.lower().startswith("with"):
        # Merge the agent's CTE list into ours: WITH s1 …, s2 …, <theirs> …
        body = body[len("with") :].lstrip()
        inner = f"WITH {shadows},\n{body}"
    else:
        inner = f"WITH {shadows}\n{body}"
    return f"SELECT * FROM (\n{inner}\n) AS _analyst LIMIT {MAX_ROWS}"


def _projection(columns: Sequence[str] | None) -> str:
    """``*``, or an explicit quoted column list.

    ⚠ ``None`` and an EMPTY list are deliberately the same answer — ``*``. An
    empty list would compose to ``SELECT  FROM``, a syntax error on every query
    touching that table, so a catalog read that came back with nothing would
    take the analyst down rather than hide one column. Falling back to ``*``
    costs the concealment of a constant-valued discriminator; refusing costs the
    customer their analyst.
    """
    if not columns:
        return "*"
    return ", ".join(f'"{c}"' for c in columns)


async def _visible_columns_for(
    conn: Any, tables: tuple[str, ...], pms: str
) -> dict[str, tuple[str, ...]]:
    """Per-table visible column lists, read LIVE, for the tables that need one.

    ⚠ Only tables that actually HAVE something hidden get an entry. Everything
    else is left to ``*`` on purpose, so the executed SQL a Buildium customer
    sees cited is unchanged for every table migration 0101 did not touch.
    """
    hidden = hidden_columns(pms)
    if not tables or not hidden:
        return {}
    rows = await conn.fetch(
        """
        SELECT table_name, column_name
          FROM information_schema.columns
         WHERE table_schema = 'public'
           AND table_name = ANY($1::text[])
         ORDER BY table_name, ordinal_position
        """,
        list(tables),
    )
    by_table: dict[str, list[str]] = {}
    for r in rows:
        by_table.setdefault(r["table_name"], []).append(r["column_name"])
    return {
        t: visible_columns(cols, pms) for t, cols in by_table.items() if hidden.intersection(cols)
    }


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    text = value if isinstance(value, str) else str(value)
    if len(text) > _MAX_CELL_CHARS:
        return text[: _MAX_CELL_CHARS - 1] + "…"
    return text


def _not_synced(cov: Coverage, tables: Sequence[str]) -> str:
    """What the model is told instead of an empty result. It names the tables
    and forbids the reading that an empty one invites."""
    return (
        cov.not_sent("these records", "them")
        + f" Not synced for this company: {', '.join(tables)}. Do not answer as if"
        " they were empty or zero; say the data isn't available in Occupella."
    )


async def run_analysis_query(sql: str, company_id: str, pool: Any) -> AnalysisResult:
    """Guard, scope, and execute one agent-written query.

    Never raises for query-level problems — guard rejections and Postgres
    errors come back as structured results the agent can read and correct.
    ``pool`` is an asyncpg pool (injected for testability).
    """
    if not company_id:
        return AnalysisResult(
            ok=False,
            error_kind="no_company",
            error="No company context for this session.",
        )

    verdict = guard_sql(sql)
    if not verdict.allowed or verdict.normalized_sql is None:
        return AnalysisResult(
            ok=False,
            error_kind="rejected_by_guard",
            error=f"Query rejected: {verdict.reason}",
        )

    # ⚠ A table this company's PMS never fills (or has not finished filling)
    # is EMPTY, and an empty table answers COUNT(*) with 0. The analyst would
    # report "you have no overdue tasks" for a Rentvine company, which has no
    # tasks synced at all (rule 14). Same rule as every mirror reader
    # (``pms_coverage``), checked before anything runs.
    cov = await coverage_for(pool, company_id)
    unseen = [t for t in verdict.tables if not cov.sees(t)]
    if unseen:
        return AnalysisResult(ok=False, error_kind="not_synced", error=_not_synced(cov, unseen))

    pms = await company_pms(pool, company_id)
    executed = compose_scoped_sql(verdict.normalized_sql, verdict.tables)
    try:
        async with pool.acquire() as conn:
            # ⚠ Composed INSIDE the connection, because the visible column list
            # is a live catalog read (see `_visible_columns_for`). The
            # `executed` above is the fallback that survives a catalog read
            # failing — `*`, exactly today's SQL — so a transient failure costs
            # the concealment and never the query.
            try:
                visible = await _visible_columns_for(conn, verdict.tables, pms)
            except Exception as exc:  # noqa: BLE001 — degrade, never fail the query
                logger.warning("analyst column filter unavailable, falling back to *: %s", exc)
            else:
                executed = compose_scoped_sql(verdict.normalized_sql, verdict.tables, visible)
            # READ ONLY at the server: even a query that fooled every textual
            # check cannot write. SET LOCAL scopes the timeout to this
            # transaction only.
            async with conn.transaction(readonly=True):
                await conn.execute(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}")
                records = await conn.fetch(executed, company_id)
    except Exception as exc:  # noqa: BLE001 — surface as data, never crash the agent loop
        message = str(exc) or type(exc).__name__
        if len(message) > _MAX_ERROR_CHARS:
            message = message[: _MAX_ERROR_CHARS - 1] + "…"
        logger.warning("analysis query failed for company=%s: %s", company_id, message)
        return AnalysisResult(
            ok=False,
            error_kind="query_error",
            error=message,
            executed_sql=executed,
        )

    rows = [{k: _json_safe(v) for k, v in dict(r).items()} for r in records]
    columns = list(rows[0].keys()) if rows else []
    return AnalysisResult(
        ok=True,
        columns=columns,
        rows=rows,
        row_count=len(rows),
        truncated=len(rows) >= MAX_ROWS,
        executed_sql=executed,
    )


__all__ = [
    "STATEMENT_TIMEOUT_MS",
    "AnalysisResult",
    "compose_scoped_sql",
    "run_analysis_query",
]
