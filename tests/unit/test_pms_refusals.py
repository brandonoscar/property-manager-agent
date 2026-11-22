"""A write a vendor refused with a 403, remembered for a day (``pms.refusals``).

Propertyware and Rent Manager share it. The behaviour each tool gets from it
is tested with the tools (``test_propertyware_tools``, ``test_rentmanager_writes``);
this file holds the rule itself and the column both sides rely on.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from helixis.pms import refusals
from helixis.propertyware import credentials as pw_credentials
from helixis.rentmanager import credentials as rm_credentials

REPO = Path(__file__).resolve().parents[2]
COMPANY = "00000000-0000-4000-8000-0000000000c1"
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


class _Pool:
    def __init__(self, capabilities: Any = None) -> None:
        self.capabilities = capabilities
        self.sent: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        self.sent.append((sql, args))
        return None if self.capabilities is None else {"capabilities": self.capabilities}

    async def execute(self, sql: str, *args: Any) -> str:
        self.sent.append((sql, args))
        return "UPDATE 1"


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        (
            json.dumps({refusals.REFUSED_KEY: {"POST /x": "2026-10-05T11:00:00Z"}}),
            NOW - timedelta(hours=1),
        ),
        (
            {refusals.REFUSED_KEY: {"POST /x": "2026-10-05T11:00:00+00:00"}},
            NOW - timedelta(hours=1),
        ),
        # A stamp with no zone is read as UTC, never as the machine's local time.
        ({refusals.REFUSED_KEY: {"POST /x": "2026-10-05T11:00:00"}}, NOW - timedelta(hours=1)),
        ({refusals.REFUSED_KEY: {"POST /y": "2026-10-05T11:00:00Z"}}, None),
        ({refusals.REFUSED_KEY: {"POST /x": "yesterday"}}, None),
        ({}, None),
        ("not json", None),
        (None, None),
    ],
)
async def test_a_refusal_is_read_as_its_instant_and_anything_else_as_none(
    stored: Any, expected: datetime | None
) -> None:
    pool = _Pool(stored)
    assert (
        await refusals.refused_at(pool, "company_rentmanager_credentials", COMPANY, "POST /x")
        == expected
    )


def test_a_refusal_is_remembered_for_the_window_and_not_a_moment_longer() -> None:
    window = refusals.REFUSAL_REMEMBERED
    assert refusals.still_refused(NOW - window + timedelta(seconds=1), now=NOW)
    assert not refusals.still_refused(NOW - window, now=NOW)
    assert not refusals.still_refused(None, now=NOW)


async def test_the_operation_is_bound_never_written_into_the_sql() -> None:
    pool = _Pool()
    operation = "POST /x'; DROP TABLE companies; --"
    await refusals.remember_refused(pool, "company_rentmanager_credentials", COMPANY, operation)
    ((sql, args),) = pool.sent
    assert operation not in sql and args == (COMPANY, operation)
    assert "$2::text" in sql and "WHERE company_id = $1::uuid" in sql
    assert sql.strip().startswith("UPDATE public.company_rentmanager_credentials")


@pytest.mark.parametrize("table", ["", "public.x", "x; DROP TABLE y", "x y"])
async def test_a_table_name_that_is_not_an_identifier_is_refused(table: str) -> None:
    with pytest.raises(ValueError):
        await refusals.refused_at(_Pool(), table, COMPANY, "POST /x")
    with pytest.raises(ValueError):
        await refusals.remember_refused(_Pool(), table, COMPANY, "POST /x")


@pytest.mark.parametrize("credentials", [pw_credentials, rm_credentials], ids=["pw", "rm"])
def test_every_table_the_memory_writes_has_the_column(credentials: Any) -> None:
    """Rule 15: the SQL above names ``capabilities`` on each connector's own
    table; migration 0107 must create it there, with an empty default."""
    (path,) = (REPO / "supabase/migrations").glob("0107_*.sql")
    migration = path.read_text(encoding="utf-8")
    table = re.search(
        rf"create table if not exists public\.{credentials.TABLE} \((.*?)\n\);", migration, re.S
    )
    assert table, f"0107 creates no {credentials.TABLE}"
    assert re.search(r"\bcapabilities\s+jsonb not null default '\{\}'::jsonb", table.group(1))


def test_propertyware_keeps_its_names_on_the_shared_rule() -> None:
    """Its tools and tests read these names from its own credentials module."""
    assert pw_credentials.REFUSED_KEY == refusals.REFUSED_KEY
    assert pw_credentials.REFUSAL_REMEMBERED == refusals.REFUSAL_REMEMBERED
    assert pw_credentials.still_refused is refusals.still_refused
