"""A charge posted ten seconds ago is not in the mirror yet.

THE DEFECT, found 2026-09-10 checking coverage item 187 against the code. A
confirmed Buildium write reaches the mirror only when Buildium's webhook
arrives. Nothing invalidated or annotated the mirror in between — `call_budget`
clears the per-RUN memo, which is a different mechanism and does not touch the
Postgres tables the rollups read. So:

    "post a $2,400 charge to Aaron Tillman"   -> approved, written, verified
    "what's his balance now?"                 -> the mirror, without it

answered confidently, about money, seconds after the manager watched the change
get approved. Gotcha 26's absence-claim class with a number instead of an empty
list.

MEASURED on helixis-test (cross-tenant deliberately — webhook delivery lag is a
property of Buildium and our receiver, not of one company's books, gotcha 47).
`LeaseTransaction.Created`, n=248: p50 **1.1s**, p95 **9.7s**, p99 **14.1s**,
max **19.1s**.

⚠ **The raw p90 over all 298 rows reads 1,528 seconds and acting on it would
have been wrong.** That tail is ONE cluster — 50 events, a single hour, one
company, all 1,488-1,605s — a seeding backfill rather than delivery lag. The
window below is derived from the figures with that hour excluded. Two of this
repo's own rules met here: never characterise from one statistic (gotcha 37),
and say which numbers are measured and which are inferred (gotcha 47).

⚠ **What these tests can and cannot see.** `_recent_write_note` is a database
read, so its behaviour is exercised against a fake pool. What that cannot see
is a return path in `_freshness_line` that forgets to append it — the footer
would simply be absent, which is indistinguishable from "no recent write". That
half is an AST check over every `return` in the function (gotchas 62/66/69/94).
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from helixis.agents.mirror_tools import (
    RECENT_WRITE_WINDOW_SECONDS,
    _recent_write_note,
)
from helixis.agents.write_ledger import LABEL_PREFIX

_MIRROR = Path("src/helixis/agents/mirror_tools.py")

#: The measurement the window is derived from. Re-derive with the query in
#: gotcha 96 rather than nudging these.
MEASURED_MAX_LAG_SECONDS = 19.1
MEASURED_P99_LAG_SECONDS = 14.1


def _pool(row: dict[str, Any] | None, *, raises: bool = False) -> MagicMock:
    """A pool whose `acquire()` yields a connection returning `row`.

    ⚠ `MagicMock().acquire().__aenter__()` already yields an AsyncMock, so every
    method on it is awaitable — reach in and override only `fetchrow` rather
    than hand-building the connection, or every other await in the path breaks
    with `object MagicMock can't be used in 'await' expression` (gotcha 69).
    """
    pool = MagicMock()
    conn = pool.acquire.return_value.__aenter__.return_value
    if raises:
        conn.fetchrow.side_effect = RuntimeError("pool is gone")
    else:
        conn.fetchrow.return_value = row
    return pool


def _note(row: dict[str, Any] | None, **kw: Any) -> str:
    return asyncio.run(_recent_write_note(_pool(row, **kw), "co-1"))


# ── the note itself ──────────────────────────────────────────────────────


def test_a_write_seconds_ago_is_flagged() -> None:
    """The whole point. Before this, the reply reported the pre-write figure
    as current."""
    out = _note({"action_type": "buildium_post_charge"})
    assert out, "a write in the window produced no caveat at all"
    assert "may not be reflected" in out
    assert "20 seconds" in out


def test_no_recent_write_says_nothing() -> None:
    """⚠ The half that keeps the caveat worth reading. A footer that appears on
    every read is one the model learns to skip, and then it is worth nothing on
    the read that matters (gotcha 57's false-caveat rule)."""
    assert _note(None) == ""


def test_an_unreadable_ledger_says_nothing_rather_than_caveating_everything() -> None:
    """⚠ Deliberately the OPPOSITE posture to the consent ledger and the role
    floor (gotchas 55/72/89), and the asymmetry is the point: those refuse
    because acting without the fact is the harm. Here failing silent returns
    exactly today's behaviour for a 30-second window, while failing loud would
    caveat EVERY mirror read for the length of an outage."""
    assert _note(None, raises=True) == ""


def test_only_occupellas_own_writes_count() -> None:
    """⚠ A `source='webhook'` row is Buildium reporting somebody else's change,
    which by definition already came through the mirror. Counting those would
    fire the caveat on ordinary inbound traffic and it would never stop."""
    sql = _captured_sql()
    assert "source_label LIKE" in sql, (
        "the query no longer discriminates by label, so every webhook row now "
        "counts as a recent Occupella write and the caveat never turns off"
    )


def _captured_sql() -> str:
    pool = MagicMock()
    conn = pool.acquire.return_value.__aenter__.return_value
    seen: list[str] = []

    async def _fetchrow(query: str, *args: object) -> None:
        seen.append(query)
        seen.extend(str(a) for a in args)
        return None

    conn.fetchrow = _fetchrow
    asyncio.run(_recent_write_note(pool, "co-1"))
    return "\n".join(seen)


def test_the_query_is_company_scoped() -> None:
    """Every `helixis_*` read carries a company (gotcha 47). Unscoped, this
    would caveat one company's reads because a DIFFERENT company just wrote."""
    sql = _captured_sql()
    assert "company_id = $1::uuid" in sql
    assert "co-1" in sql


def test_the_label_prefix_is_imported_not_retyped() -> None:
    """⚠ gotcha 73: `"approved"` was spelled out in three modules, one
    character-drift away from silencing every white-label customer. The
    discriminator here has exactly one home — `write_ledger.LABEL_PREFIX` —
    and this asserts the value that actually reached the query came from it."""
    assert f"{LABEL_PREFIX} %" in _captured_sql()


# ── the window is a measurement, not a preference ────────────────────────


def test_the_window_covers_the_measured_worst_case() -> None:
    """⚠ A decision taken from a measurement needs a test that re-states it
    (gotcha 60), and this one cannot re-measure — CI has no database. So it
    pins the relationship instead: the window must clear the measured maximum,
    and shrinking it below that silently re-opens the gap for the slowest
    tenth of writes."""
    assert RECENT_WRITE_WINDOW_SECONDS >= MEASURED_MAX_LAG_SECONDS, (
        f"the window ({RECENT_WRITE_WINDOW_SECONDS}s) is under the measured "
        f"worst-case webhook lag ({MEASURED_MAX_LAG_SECONDS}s) — a charge can "
        "land in the mirror after the caveat has already stopped firing"
    )
    assert RECENT_WRITE_WINDOW_SECONDS < 300, (
        "the window has grown past a few minutes, which makes the caveat fire "
        "on reads long after the write landed — a caveat that is usually "
        "false is one the model learns to skip (gotcha 57)"
    )


def test_the_measurement_is_recorded_beside_the_constant() -> None:
    """The constant is only defensible while the numbers it came from are
    readable next to it. Prose cannot fail (gotcha 48), so this at least pins
    that the prose is still there to be read."""
    src = _MIRROR.read_text(encoding="utf-8")
    block = src.split("RECENT_WRITE_WINDOW_SECONDS")[0][-2000:]
    for needle in ("p99", "19.1", "backfill"):
        assert needle in block, (
            f"the derivation of the window no longer mentions {needle!r} — a "
            "constant with no recorded measurement is one the next reader nudges"
        )


# ── it is on every path the footer takes ─────────────────────────────────


def _fn(name: str) -> ast.AST:
    for node in ast.walk(ast.parse(_MIRROR.read_text(encoding="utf-8"))):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} is gone from mirror_tools.py")


def test_every_exit_from_the_freshness_line_carries_the_note() -> None:
    """⚠ The half a behavioural test structurally cannot see. `_freshness_line`
    returns from THREE places — the no-sync-state fallback, the not-stale path
    and the stale path — and a note appended to only some of them leaves the
    others silently exempt. A missing footer is indistinguishable from "no
    recent write", so nothing would ever surface it (gotcha 94's shape, which
    is why that fix had to cover both of `run_guarded`'s exits).
    """
    returns = [n for n in ast.walk(_fn("_freshness_line")) if isinstance(n, ast.Return)]
    assert len(returns) == 3, (
        f"_freshness_line now returns from {len(returns)} places — a new exit "
        "needs the recent-write note too, or reads down that path silently "
        "lose the caveat"
    )
    for node in returns:
        assert "recent" in ast.unparse(node), (
            f"a return path in _freshness_line drops the recent-write note: {ast.unparse(node)!r}"
        )


def test_the_note_is_computed_once_rather_than_per_exit() -> None:
    """Three call sites would be three chances to forget, and three database
    reads on a path taken by every mirror tool."""
    calls = [
        n
        for n in ast.walk(_fn("_freshness_line"))
        if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_recent_write_note"
    ]
    assert len(calls) == 1, (
        f"_recent_write_note is called {len(calls)} times inside _freshness_line"
    )


@pytest.mark.parametrize("tool", ["_portfolio_overview_impl", "_delinquency_report_impl"])
def test_the_footer_reaches_the_real_tools(tool: str) -> None:
    """Reads the OTHER side (gotcha 28). The contract test already requires
    every mirror read to emit a freshness line; this pins that the tools this
    change is FOR are still among them, so the note lands where a manager
    actually asks "did that go through?"."""
    src = _MIRROR.read_text(encoding="utf-8")
    assert f"def {tool}" in src, f"{tool} was renamed — re-point this guard"
    node = _fn(tool)
    called = {
        getattr(n.func, "id", "") or getattr(n.func, "attr", "")
        for n in ast.walk(node)
        if isinstance(n, ast.Call)
    }
    assert "_freshness_line" in called or "_not_synced" in called, (
        f"{tool} no longer reports its data's age, so a stale figure and a "
        "current one now read identically"
    )


def test_the_note_never_raises_out_of_a_read() -> None:
    """A footer must never cost the answer. Driven through a pool whose every
    call explodes, asserting the read still completes."""
    boom = MagicMock()
    boom.acquire.side_effect = RuntimeError("no pool")
    assert asyncio.run(_recent_write_note(boom, "co-1")) == ""

    # And with a connection that yields something unexpected rather than raising.
    weird = MagicMock()
    weird.acquire.return_value.__aenter__.return_value.fetchrow.return_value = SimpleNamespace()
    assert isinstance(asyncio.run(_recent_write_note(weird, "co-1")), str)


# ── the system it names ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("pms", "name"),
    [("rentvine", "Rentvine"), ("rentmanager", "Rent Manager"), ("propertyware", "Propertyware")],
)
def test_a_mirror_only_system_is_named_with_how_its_changes_arrive(
    monkeypatch: pytest.MonkeyPatch, pms: str, name: str
) -> None:
    """It said "this Buildium account" and "up to 20 seconds" to every company.
    Only Buildium's changes arrive by webhook; every other system's arrive
    with its next sync."""
    from helixis.agents import mirror_tools
    from helixis.db.pms_coverage import Coverage

    async def on(_pool: Any, _company_id: str) -> Coverage:
        return Coverage(pms)

    monkeypatch.setattr(mirror_tools, "coverage_for", on)
    out = _note({"action_type": f"{pms}_create_work_order"})
    assert f"this {name} account" in out and f"{name}'s next sync" in out
    assert "Buildium" not in out and "20 seconds" not in out
    assert "may not show the change" in out


def test_an_unknown_system_is_not_named(monkeypatch: pytest.MonkeyPatch) -> None:
    from helixis.agents import mirror_tools
    from helixis.db.pms_coverage import Coverage

    async def unknown(_pool: Any, _company_id: str) -> Coverage:
        return Coverage(None)

    monkeypatch.setattr(mirror_tools, "coverage_for", unknown)
    out = _note({"action_type": "send_text_message"})
    assert "may not be reflected" in out
    assert not any(n in out for n in ("Buildium", "Rentvine", "Rent Manager", "Propertyware"))
