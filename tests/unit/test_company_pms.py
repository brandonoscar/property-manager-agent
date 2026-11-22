"""Which PMS is a company on? The answer a1's trigger makes askable.

ADR 0010 Decision (a) puts BOTH systems' record ids in one identity column
under ``UNIQUE (company_id, buildium_id)``, so a company holding two
connections could have two records silently overwrite each other (finding
R3-7). Migration 0100's ``assert_one_pms_per_company`` trigger is what forbids
that — and it is also what makes this a function returning ONE string rather
than a set every caller has to disambiguate.
"""

from __future__ import annotations

import ast
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from helixis.db.company_pms import DEFAULT_PMS, company_pms

REPO = Path(__file__).resolve().parents[2]
_COMPANY = "11111111-1111-1111-1111-111111111111"


class _Conn:
    def __init__(self, row: Any = None, raises: Exception | None = None) -> None:
        self.row = row
        self.raises = raises
        self.sql: str | None = None

    async def fetchrow(self, sql: str, *_args: Any) -> Any:
        self.sql = sql
        if self.raises is not None:
            raise self.raises
        return self.row


class _Pool:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn

    def acquire(self):
        @asynccontextmanager
        async def _acq():
            yield self.conn

        return _acq()


def _ask(conn: _Conn, company: str = _COMPANY) -> str:
    return asyncio.run(company_pms(_Pool(conn), company))


def test_it_answers_both_systems() -> None:
    assert _ask(_Conn({"pms": "rentvine"})) == "rentvine"
    assert _ask(_Conn({"pms": "buildium"})) == "buildium"


def test_it_asks_the_credentials_table_not_the_mirror() -> None:
    """A company mid-onboarding has no mirrored rows and still has a PMS; a
    company that disconnected has rows and no connection. The credentials row
    is the fact — the mirror is its consequence."""
    conn = _Conn({"pms": "buildium"})
    _ask(conn)
    assert conn.sql is not None
    assert "company_rentvine_credentials" in conn.sql
    assert "company_id = $1::uuid" in conn.sql, "the query must be company-scoped (gotcha 47)"


def test_every_failure_falls_back_to_buildium_rather_than_raising() -> None:
    """Fail-open, deliberately — and the docstring says which callers may reuse
    that. The one caller today is the analyst's column filter, where a refusal
    would take the analyst down for a Buildium customer over a transient
    catalog read."""
    assert _ask(_Conn(raises=RuntimeError("pool exhausted"))) == DEFAULT_PMS
    assert _ask(_Conn(row=None)) == DEFAULT_PMS
    assert _ask(_Conn({"pms": None})) == DEFAULT_PMS
    assert _ask(_Conn({"pms": ""})) == DEFAULT_PMS


def test_no_company_never_touches_the_pool() -> None:
    conn = _Conn({"pms": "rentvine"})
    assert _ask(conn, company="") == DEFAULT_PMS
    assert conn.sql is None


def test_the_default_is_buildium_and_not_unknown() -> None:
    """ "unknown" would make every caller invent its own fallback, which is how
    one fact ends up with three answers (gotchas 25/73)."""
    assert DEFAULT_PMS == "buildium"


def test_the_two_readers_of_this_answer_both_call_it() -> None:
    """A behavioural test cannot see a caller that quietly stops asking and
    hardcodes 'buildium' instead (gotchas 62/66/69/103)."""
    for rel in ("src/helixis/agents/analysis_tools.py", "src/helixis/analysis/executor.py"):
        tree = ast.parse((REPO / rel).read_text())
        calls = {
            n.func.id
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        assert "company_pms" in calls, f"{rel} stopped asking which PMS the company is on"
