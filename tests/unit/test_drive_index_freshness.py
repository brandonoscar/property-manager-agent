"""A three-week-old Drive index read as the current state of the Drive.

`drive_index` carries `indexed_at` on every row — `not null default now()`
since migration 0063 — and `_search_index` selected five columns and not that
one. So `drive_find_files` presented whatever the last successful daily
enumeration happened to contain, with nothing anywhere saying when that was
(coverage checklist #117).

Gotchas 41/50/51's sentence again: the field was on the row, fetched, and
thrown away. It arrives here on the one fact that decides whether the whole
list can be trusted — a document created last week is absent from a stale index
for a reason that has nothing to do with the query, and gotcha 42 is the
standing record of this product reporting a Drive as empty when it was not.

⚠ **Stated on a HIT as well as on an empty result.** The tempting shape is to
warn only when nothing matched, and it is the wrong one: a stale index that
DOES return rows presents an old title list as current and the model quotes it
without a word. Warn-when-empty, flatter-when-found is exactly the asymmetry the
mirror rollups carried (checklist #41).

⚠ **`None` is not "fresh".** An unreadable stamp means nobody checked, which is
a different claim from "checked, it is current" (gotchas 55/57/70).
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

from helixis.agents.drive_lookup_tools import index_freshness, render_results

_MODULE = Path("src/helixis/agents/drive_lookup_tools.py")

_NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


def _row(title: str = "SOP-01 Maintenance Triage") -> dict:
    return {
        "file_id": "id-1",
        "title": title,
        "mime_type": "application/vnd.google-apps.document",
        "path": "/Operations",
        "modified_at": "2026-05-01",
    }


# ── the sentence itself ──────────────────────────────────────────────


def test_a_fresh_index_says_when_without_alarm() -> None:
    line = index_freshness(_NOW - timedelta(hours=6), now=_NOW)
    assert line == "Index last refreshed 2026-09-09."
    assert "⚠" not in line


def test_one_missed_daily_run_is_still_fresh() -> None:
    """The job runs daily, so a day-old index is normal operation. Flagging it
    would put the warning on ordinary use and get it skipped (gotcha 52)."""
    assert "⚠" not in index_freshness(_NOW - timedelta(days=1, hours=12), now=_NOW)


def test_a_stale_index_says_so_and_says_what_it_means() -> None:
    line = index_freshness(_NOW - timedelta(days=21), now=_NOW)
    assert "⚠" in line
    assert "21 days ago" in line
    assert "says nothing about the user's Drive" in line, (
        "a stale-index warning that does not say what it implies is just doubt "
        "(gotcha 46) — the point is that absence here is not absence there"
    )


def test_an_unknown_stamp_is_not_reported_as_fresh() -> None:
    """⚠ The direction that matters. Rendering None as current tells the model
    the listing was verified when nobody looked."""
    line = index_freshness(None)
    assert "could not be read" in line
    assert "out of date" in line
    assert "last refreshed" not in line


def test_a_naive_stamp_is_read_as_utc_not_crashed() -> None:
    """asyncpg returns tz-aware, but a fake pool or a re-parsed value may not.
    A freshness line that raises takes the whole lookup down with it."""
    naive = (_NOW - timedelta(days=10)).replace(tzinfo=None)
    assert "10 days ago" in index_freshness(naive, now=_NOW)


def test_the_threshold_is_what_stops_a_zero_day_warning() -> None:
    """⚠ Rewritten after a mutation proved the point. A `max(1, age.days)`
    floor was in here and could not bind for any input: the stale branch is
    only reached past two days, so `age.days` is already >= 2. That is
    redundant code, not a blind test (gotcha 58) — the THRESHOLD is the real
    guarantee, so it is what gets pinned."""
    just_past = index_freshness(_NOW - timedelta(days=2, seconds=1), now=_NOW)
    assert "0 days ago" not in just_past and "2 days ago" in just_past
    assert "⚠" not in index_freshness(_NOW - timedelta(days=2), now=_NOW)


# ── where the specialist sees it ─────────────────────────────────────


def test_a_hit_carries_the_freshness_line() -> None:
    out = render_results(
        "sop", [_row()], total_indexed=209, limit=8, indexed_at=_NOW - timedelta(days=21), now=_NOW
    )
    assert "21 days ago" in out
    assert "SOP-01 Maintenance Triage" in out, "the rows are still rendered"


def test_an_empty_result_carries_it_too() -> None:
    """The empty branch already refuses to call a miss an absence; the stale
    stamp is the second, independent reason it might be a miss."""
    out = render_results(
        "nothing", [], total_indexed=209, limit=8, indexed_at=_NOW - timedelta(days=21), now=_NOW
    )
    assert "21 days ago" in out
    assert "do NOT tell the user the document does not exist" in out


def test_a_fresh_hit_is_not_decorated_with_a_warning() -> None:
    out = render_results(
        "sop", [_row()], total_indexed=209, limit=8, indexed_at=_NOW - timedelta(hours=3), now=_NOW
    )
    assert "Index last refreshed 2026-09-09." in out
    assert "days ago" not in out


def test_the_existing_contract_is_untouched() -> None:
    out = render_results("sop", [_row()], total_indexed=209, limit=8, indexed_at=_NOW, now=_NOW)
    assert "209 indexed file(s)" in out
    assert "TITLE matches from the daily index" in out
    assert "file_id:" in out


# ── it is actually read from the database ────────────────────────────


def _fn(name: str) -> ast.AST:
    tree = ast.parse(_MODULE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {_MODULE}")


def test_the_query_selects_the_stamp() -> None:
    """⚠ A behavioural test of `index_freshness` cannot see a query that
    stopped selecting the column — every caller would hand it None and the tool
    would report "could not be read" on a perfectly healthy index, forever
    (gotchas 62/66/69).

    ⚠ And it must read the SQL STRING, not the function. Found by MUTATION:
    dropping `indexed_at` from the SELECT left the name present elsewhere in
    the body (`r["indexed_at"]`), so a scan of the unparsed function passed
    against the exact defect it targets — gotcha 45's shape, one scope in.
    """
    sql = " ".join(
        str(n.value)
        for n in ast.walk(_fn("_search_index"))
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and "SELECT" in n.value
    )
    assert sql, "_search_index no longer issues a SELECT"
    assert "indexed_at" in sql, (
        f"the query is {sql!r} — it no longer selects indexed_at, so every "
        "lookup reports the index age as unknown on a healthy index"
    )


def test_the_impl_passes_the_stamp_to_the_renderer() -> None:
    src = ast.unparse(_fn("_drive_find_files_impl"))
    assert "indexed_at=indexed_at" in src, (
        "the stamp is read and then dropped before rendering — the exact "
        "fetched-and-thrown-away shape this change exists to close"
    )


def test_the_header_is_built_through_the_helper() -> None:
    """Pins that the freshness line reaches the header rather than being
    computed and discarded."""
    src = ast.unparse(_fn("render_results"))
    assert "index_freshness" in src


def test_the_stale_threshold_is_more_than_a_day() -> None:
    """⚠ Pinned because the job is DAILY: a threshold at or below one day puts
    the warning on ordinary operation, which is how a signal gets ignored
    (gotcha 52)."""
    from helixis.agents.drive_lookup_tools import _STALE_INDEX_AFTER

    assert _STALE_INDEX_AFTER > timedelta(days=1)


# ── which stamp answers the question ─────────────────────────────────


def test_a_mixed_corpus_reports_the_stalest_row() -> None:
    """⚠ Found by MUTATION, and it changed the code rather than the test.

    The index is refreshed per (company, composio_user_id) — Drive is a
    PERSONAL connection — so one colleague's broken OAuth leaves their share of
    the corpus weeks behind while another's refreshes nightly. Reporting the
    NEWEST stamp would call that company current and flatter exactly the case
    the warning exists for. The question being asked is "can absence from this
    list be trusted", and the answer is bounded by the oldest thing in it.
    """
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch

    fresh, stale = _NOW - timedelta(hours=2), _NOW - timedelta(days=21)
    conn = MagicMock()
    conn.fetch = AsyncMock(
        return_value=[
            {**_row("SOP-01 Triage"), "indexed_at": fresh},
            {**_row("SOP-02 Dispatch"), "indexed_at": stale},
        ]
    )

    class _CM:
        async def __aenter__(self) -> object:
            return conn

        async def __aexit__(self, *_a: object) -> bool:
            return False

    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=lambda: _CM())
    with patch("helixis.db.engine.get_pool", return_value=pool):
        from helixis.agents.drive_lookup_tools import _search_index

        _rows, total, stamp = asyncio.run(_search_index("c1", ["sop"], 8))
    assert total == 2
    assert stamp == stale, (
        "the freshness stamp reports the NEWEST refresh, so a company with one "
        "broken Drive connection reads as fully current"
    )
