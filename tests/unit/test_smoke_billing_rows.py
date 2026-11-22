"""The every-company-has-a-billing-row invariant, and why it needed a schedule.

Gotcha 81 is the record: two individually-correct fail-open decisions compose
into a free unlimited account. Migration 0087 backfilled the companies alive
that morning, 0088 added a trigger so new ones get a row, and `gate.py` reads a
missing row as ALLOW — deliberately, because a database blip must not lock a
paying customer out. Each half is right. Together, a company with no row is
served forever and nothing anywhere says so.

`helixis_billing_missing` was created to be that alarm and was **queried by
nothing that runs**. The trigger it watches is `security definer` and swallows
its own exception by design (a signup that 500s is worse than a missing billing
row), so when it fails the entire signal is one Postgres `raise warning`.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helixis.settings import Settings
from helixis.smoke import checks as _checks


def _settings(db_url: str = "postgresql://x/y") -> Settings:
    return Settings(env="test", memory={"enabled": False}, supabase={"db_url": db_url})


def _pool(missing: int, sample: list[dict[str, str]] | None = None) -> MagicMock:
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=missing)
    conn.fetch = AsyncMock(return_value=sample or [])
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    pool.close = AsyncMock()
    return pool


def _with_pool(pool: Any):
    return patch("helixis.db.engine.create_pool", AsyncMock(return_value=pool))


@pytest.mark.asyncio
async def test_a_company_with_no_billing_row_fails() -> None:
    """THE DEFECT. Each such company is served unmetered, permanently, and the
    only other symptom is a log line."""
    with _with_pool(_pool(2, [{"id": "co-1"}, {"id": "co-2"}])):
        result = await _checks.check_billing_rows(_settings())
    assert result.status == "fail"
    assert "2 company" in result.detail
    # Naming a few makes it actionable without dumping a tenant list into a log.
    assert "co-1" in result.detail
    assert "unmetered" in result.detail


@pytest.mark.asyncio
async def test_the_healthy_state_passes() -> None:
    with _with_pool(_pool(0)):
        result = await _checks.check_billing_rows(_settings())
    assert result.status == "pass", result.detail


@pytest.mark.asyncio
async def test_no_database_url_skips_and_says_what_went_unchecked() -> None:
    """⚠ SKIP, not fail — `supabase_postgres` owns real DB health, so failing
    here would report one outage twice (the same reasoning `webhook_queue`
    already carries). The detail must name the gap or a skip reads as coverage,
    which is the LED-013 skip-floor rule.

    ⚠ The literal must be `"skipped"`. `"skip"` is a status nothing recognises:
    not passed, not failed, and INVISIBLE to `test_smoke_skips.py`'s AST guard,
    which scans for the real spelling — so it would slip past the one control
    built to stop a silent skip being added. Both new billing checks shipped
    with that typo and it was caught by reading `CheckResult.skipped`, not by
    any failing test.
    """
    with _with_pool(_pool(0)):
        result = await _checks.check_billing_rows(_settings(db_url=""))
    assert result.status == "skipped"
    assert "not checked" in result.detail


@pytest.mark.asyncio
async def test_an_unreadable_database_fails_rather_than_raising() -> None:
    """Nothing in the smoke may raise — a check that explodes takes the whole
    run with it and every other dependency goes unreported."""
    with patch("helixis.db.engine.create_pool", AsyncMock(side_effect=RuntimeError("no route"))):
        result = await _checks.check_billing_rows(_settings())
    assert result.status == "fail"
    assert "RuntimeError" in result.detail


@pytest.mark.asyncio
async def test_the_pool_is_closed_even_when_the_query_fails() -> None:
    """This check runs daily from a long-lived process. A pool leaked on the
    error path is a connection this deployment never gets back."""
    pool = _pool(0)
    conn = await pool.acquire.return_value.__aenter__()
    conn.fetchval = AsyncMock(side_effect=RuntimeError("boom"))
    with _with_pool(pool):
        result = await _checks.check_billing_rows(_settings())
    assert result.status == "fail"
    pool.close.assert_awaited_once()


def test_the_sample_query_selects_columns_the_view_has() -> None:
    """⚠ Reads BOTH sides, because the mocks above cannot. The sample query
    selected ``id`` from a view whose column is ``company_id``, so the real run
    raised UndefinedColumnError every day (smoke issue #1068) while every test
    here passed against a fake pool that answers any SQL."""
    import ast
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    view_sql = (root / "supabase/migrations/0088_billing_row_on_company_create.sql").read_text()
    body = re.search(
        r"create\s+or\s+replace\s+view\s+helixis_billing_missing\s+as\s+select\s+(.*?)\s+from\s",
        view_sql,
        re.IGNORECASE | re.DOTALL,
    )
    assert body is not None, "0088 no longer defines helixis_billing_missing"
    view_columns = {
        re.split(r"\s+as\s+", item.strip(), flags=re.IGNORECASE)[-1].split(".")[-1]
        for item in body.group(1).split(",")
    }

    tree = ast.parse(Path(_checks.__file__).read_text())
    queries = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "from public.helixis_billing_missing" in node.value
        and node.value.lstrip().lower().startswith("select")
    ]
    assert queries, "the smoke check no longer queries helixis_billing_missing"
    for sql in queries:
        selected = re.match(r"\s*select\s+(.*?)\s+from\s", sql, re.IGNORECASE | re.DOTALL)
        assert selected is not None, sql
        for item in selected.group(1).split(","):
            column = re.split(r"::|\s+as\s+|\(", item.strip(), flags=re.IGNORECASE)[0].strip()
            if column.lower() in {"count", "count(*)", "*", ""}:
                continue
            assert column in view_columns, (
                f"{sql!r} selects {column!r}; the view has {view_columns}"
            )
