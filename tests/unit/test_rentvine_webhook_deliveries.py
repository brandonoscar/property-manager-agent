"""The refused-delivery recorder (migration 0106) and the two things that read it.

Three properties pinned:
1. The writer stores a delivery verbatim minus credential headers, prunes in
   the same transaction, and NEVER raises: the receiver's 401 cannot depend on
   a diagnostics table.
2. The writer, the migration and the capture tool's reader agree on the
   columns (gotcha 28's both-sides rule, with SQL as one side).
3. The table is purged with the company's mirror: its rows are Rentvine data.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from helixis.buildium.purge import PURGE_TABLES
from helixis.rentvine import webhook_deliveries as wd

REPO = Path(__file__).resolve().parents[2]
MIGRATION = REPO / "supabase" / "migrations" / "0106_rentvine_webhook_deliveries.sql"
TABLE = "helixis_rentvine_webhook_deliveries"
_COMPANY = "11111111-1111-1111-1111-111111111111"
_BODY = b'{"workOrderID": 9, "note": "the bytes as sent"}'


def _pool(execute: Any = None) -> tuple[Any, Any]:
    conn = MagicMock()
    conn.execute = execute or AsyncMock(return_value="INSERT 0 1")
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool, conn


def _record(pool: Any, **overrides: Any) -> bool:
    kwargs: dict[str, Any] = {
        "company_id": _COMPANY,
        "method": "POST",
        "path": "/webhooks/rentvine/acme",
        "query": "timestamp=1&signature=ab-c%2Bd&token=a%20b+c",
        "headers": {
            "content-type": "application/json",
            "Authorization": "Bearer stranger",
            "Cookie": "sid=1",
            "proxy-authorization": "Basic x",
            "user-agent": "axios/0.28.1",
        },
        "body": _BODY,
        "reason": "Rentvine's signature scheme is not configured.",
    }
    kwargs.update(overrides)
    return asyncio.run(wd.record_refused_delivery(pool, **kwargs))


# ── the writer ───────────────────────────────────────────────────────────────


def test_it_stores_the_delivery_verbatim_and_prunes_in_one_transaction() -> None:
    pool, conn = _pool()
    assert _record(pool) is True

    conn.transaction.assert_called_once()
    (insert, *params), (prune, *prune_params) = (c.args for c in conn.execute.await_args_list)
    assert insert.lstrip().startswith("INSERT INTO public." + TABLE)
    company, method, path, query, headers_json, body, reason = params
    assert (company, method, path) == (_COMPANY, "POST", "/webhooks/rentvine/acme")
    # RAW, escapes and order intact, except the URL's `signature`, which
    # Rentvine fills with the signing key itself (gotcha 203).
    assert query == "timestamp=1&signature=[redacted]&token=a%20b+c", "the query must stay RAW"
    assert body == _BODY, "the body must be the bytes as sent"
    assert json.loads(headers_json) == {
        "content-type": "application/json",
        "user-agent": "axios/0.28.1",
    }, "credential headers are dropped in any case; the rest kept as sent"
    assert reason.startswith("Rentvine's signature scheme")

    assert prune.lstrip().startswith("DELETE FROM public." + TABLE)
    assert prune_params == [_COMPANY, wd.KEEP_PER_COMPANY]
    assert "ORDER BY id DESC" in prune, "the prune must keep the NEWEST rows"


def test_an_overlong_reason_is_cut_not_stored_whole() -> None:
    pool, conn = _pool()
    _record(pool, reason="x" * 5000)
    stored = conn.execute.await_args_list[0].args[7]
    assert 0 < len(stored) < 5000


def test_it_never_raises_and_never_logs_the_body(caplog) -> None:
    """asyncpg's data errors quote the offending parameter, and one of the
    parameters is a delivery body — so the log names the error TYPE only."""
    leaky = RuntimeError(f"invalid input for query argument $6: {_BODY!r}")
    pool, _conn = _pool(execute=AsyncMock(side_effect=leaky))
    with caplog.at_level(logging.WARNING, logger=wd.__name__):
        assert _record(pool) is False
    assert "RuntimeError" in caplog.text
    assert "the bytes as sent" not in caplog.text

    broken = MagicMock()
    broken.acquire = MagicMock(side_effect=TimeoutError("pool exhausted"))
    assert _record(broken) is False


def test_stored_headers_drops_credentials_in_any_case() -> None:
    kept = wd.stored_headers({"AUTHORIZATION": "a", "cookie": "c", "X-Ok": "y"})
    assert kept == {"X-Ok": "y"}


def test_the_signing_key_is_never_stored_wherever_it_was_sent() -> None:
    """Rentvine sends the key itself as ``?signature=`` (measured 2026-10-04).
    It is withheld by NAME, because before a key is saved there is nothing to
    compare with, and by VALUE wherever else a copy turns up."""
    key = "k3y-" + "q" * 20
    assert wd.stored_query(f"timestamp=1&signature={key}&token=aaa") == (
        "timestamp=1&signature=[redacted]&token=aaa"
    ), "withheld by name, with no key saved yet"
    assert wd.stored_query(f"a=1&other={key}&b=%20", secret=key) == (
        "a=1&other=[redacted]&b=%20"
    ), "withheld by value, and every other part stays raw"
    assert wd.stored_query("a=1&flag&b=2", secret=key) == "a=1&flag&b=2"
    assert wd.stored_headers({"X-Copy": key, "X-Ok": "y"}, secret=key) == {
        "X-Copy": wd.REDACTED,
        "X-Ok": "y",
    }

    pool, conn = _pool()
    _record(pool, query=f"signature={key}", headers={"X-Copy": key}, secret=key)
    stored_query, stored_headers = conn.execute.await_args_list[0].args[4:6]
    assert key not in stored_query and key not in stored_headers


# ── both sides of the SQL ────────────────────────────────────────────────────


def _migration_columns() -> set[str]:
    text = MIGRATION.read_text(encoding="utf-8")
    body = text.split(f"create table if not exists public.{TABLE} (", 1)[1].split("\n);", 1)[0]
    columns = set()
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("--") or line.startswith("references"):
            continue
        columns.add(line.split()[0])
    return columns


def _named_columns(sql: str, pattern: str) -> set[str]:
    match = re.search(pattern, sql, re.S)
    assert match, sql
    return {c.strip() for c in match.group(1).split(",")}


def test_the_writer_and_the_tool_use_only_columns_the_migration_creates() -> None:
    import importlib.util
    import sys

    created = _migration_columns()
    assert {"company_id", "query", "body", "headers"} <= created, created

    written = _named_columns(wd._INSERT, r"\(([^)]*)\)\s*VALUES")
    assert written <= created, written - created

    spec = importlib.util.spec_from_file_location(
        "_rv_capture_db", REPO / "scripts" / "rentvine_webhook_capture.py"
    )
    assert spec and spec.loader
    tool = importlib.util.module_from_spec(spec)
    sys.modules["_rv_capture_db"] = tool
    spec.loader.exec_module(tool)
    read = _named_columns(tool._DB_DELIVERIES_SQL, r"SELECT(.*?)FROM")
    assert read <= created, read - created
    # Everything the writer stores that the analysis needs, the tool reads.
    assert {"method", "path", "query", "headers", "body"} <= read


def test_the_migration_locks_the_table_and_ties_it_to_the_company() -> None:
    text = MIGRATION.read_text(encoding="utf-8")
    assert f"alter table public.{TABLE} enable row level security" in text
    assert "references public.companies(id) on delete cascade" in text
    assert "create policy" not in text.lower(), "backend-only: RLS on, no policy"


def test_the_rows_go_with_the_company_mirror_on_disconnect() -> None:
    """The payloads are Rentvine data (ADR 0010 a1: one mirror, one list)."""
    assert TABLE in PURGE_TABLES
