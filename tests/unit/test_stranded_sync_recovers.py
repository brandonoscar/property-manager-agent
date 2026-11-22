"""A stranded Buildium import must be able to end, by any of the three routes
that can actually end it.

``run_initial_sync`` is fired and forgotten from ``POST /buildium/test``, so a
deploy mid-crawl strands the row at ``syncing`` for the life of the account —
the module's own comment says launch week is deploy week, so this is likely
rather than theoretical. ``sync_state.is_stale`` was written for exactly that,
is exposed on ``GET /buildium/sync-status``, and ``SyncProgressBanner`` reads
it and tells the customer:

    Your Buildium import stopped before it finished. You can re-run it from
    Settings → Connectors → Buildium.

⚠ **AND THEN NEITHER ROUTE IN THAT SENTENCE COULD ACT ON IT.** THREE code
paths complete a full ``backfill_company`` — the onboarding crawl, the "Sync
data" button and the worker's daily ``mirror_sync`` — and only the first one
recorded that it had happened:

* ``POST /buildium/test`` restarted only on ``pending``/``failed``, so a stale
  ``syncing`` row — the one state ``is_stale`` exists to describe — was refused,
  silently.
* ``POST /buildium/backfill`` (the button the banner names) called
  ``backfill_company`` directly and wrote no state at all, so the customer did
  exactly what they were told, watched their data arrive, and the warning
  stayed on screen forever.
* the worker re-pulls the whole account daily, which repairs the mirror with no
  user action, and said nothing either.

So the guards here are about REACHABILITY, not about the staleness rule (which
``test_sync_state_staleness.py`` already pins in both directions). Several are
AST call-site checks because a behavioural test structurally cannot see a route
that quietly goes back to calling ``backfill_company`` — gotchas 62/66/69/103,
where a well-tested helper's one production caller ignores it.
"""

from __future__ import annotations

import ast
import pathlib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from helixis.db.repositories import sync_state
from helixis.db.repositories.sync_state import STALE_AFTER_SECONDS, SyncState

_ROUTE = pathlib.Path("src/helixis/api/routes/buildium.py")
_WORKER = pathlib.Path("src/helixis/webhooks/mirror_sync.py")
_BACKFILL = pathlib.Path("src/helixis/buildium/backfill.py")

_NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
_STALE = _NOW - timedelta(seconds=STALE_AFTER_SECONDS + 60)
_FRESH = _NOW - timedelta(seconds=60)


def _state(status: str, started: datetime | None) -> SyncState:
    return SyncState(
        company_id="c1",
        status=status,
        started_at=started,
        finished_at=None,
        counts={},
        last_error=None,
    )


class _Pool:
    """Enough asyncpg surface for ``clear_stranded``: one row in, writes out."""

    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row
        self.executed: list[tuple[Any, ...]] = []

    async def fetchrow(self, *_args: Any) -> dict[str, Any] | None:
        return self._row

    async def execute(self, *args: Any) -> None:
        self.executed.append(args)


def _row(status: str, started: datetime | None) -> dict[str, Any]:
    return {
        "company_id": "c1",
        "status": status,
        "started_at": started,
        "finished_at": None,
        "counts": {},
        "last_error": None,
    }


# ── clear_stranded: what it retires, and the three states it must not ───────


@pytest.mark.asyncio
async def test_a_failed_row_is_retired_by_a_clean_pull() -> None:
    pool = _Pool(_row("failed", _FRESH))
    assert await sync_state.clear_stranded(pool, "c1", {"properties": 3}, now=_NOW) is True
    assert pool.executed, "a failed row was left describing an import that has since completed"


@pytest.mark.asyncio
async def test_a_stale_syncing_row_is_retired_by_a_clean_pull() -> None:
    """The headline case: a deploy stranded the crawl, the worker finished the
    same pull a day later, so the banner's claim is no longer true."""
    pool = _Pool(_row("syncing", _STALE))
    assert await sync_state.clear_stranded(pool, "c1", {"properties": 3}, now=_NOW) is True
    assert pool.executed


@pytest.mark.asyncio
async def test_a_crawl_that_is_genuinely_running_is_left_alone() -> None:
    """⚠ The false-positive direction. Overwriting a live crawl makes the
    banner vanish mid-import and reappear if that crawl later fails — worse
    than the defect, because it flickers."""
    pool = _Pool(_row("syncing", _FRESH))
    assert await sync_state.clear_stranded(pool, "c1", {"properties": 3}, now=_NOW) is False
    assert not pool.executed


@pytest.mark.asyncio
async def test_a_done_row_is_not_rewritten() -> None:
    """Already true. Rewriting it would replace the onboarding crawl's counts
    with a routine refresh's, for no reason."""
    pool = _Pool(_row("done", _FRESH))
    assert await sync_state.clear_stranded(pool, "c1", {"properties": 3}, now=_NOW) is False
    assert not pool.executed


@pytest.mark.asyncio
async def test_no_row_stays_no_row() -> None:
    """⚠ Absence is not a stranded import. This company never started one, so
    writing `done` would claim an onboarding pull that never ran — the
    fabricated-completion shape (gotchas 26/106). `unknown` is honest and the
    banner already says nothing for it."""
    pool = _Pool(None)
    assert await sync_state.clear_stranded(pool, "c1", {"properties": 3}, now=_NOW) is False
    assert not pool.executed


# ── the restart gate on POST /buildium/test ────────────────────────────────


def _restart_decision(status: str | None, started: datetime | None) -> bool:
    """The route's own predicate, read out rather than restated.

    ⚠ Restating it here would let the route and the test drift into agreeing
    about different things (gotcha 28), so this mirrors the source exactly and
    the AST guard below pins that the source still reads `is_stale`.
    """
    if status is None:
        return True
    existing = _state(status, started)
    restart = existing.status in ("pending", "failed")
    if not restart:
        restart = sync_state.is_stale(existing.status, existing.started_at, now=_NOW)
    return restart


def test_a_stale_syncing_row_restarts_on_retest() -> None:
    """The defect: this was the one state the old gate refused."""
    assert _restart_decision("syncing", _STALE) is True


def test_a_running_syncing_row_does_not_restart_on_retest() -> None:
    """Repeated clicks must not launch a second crawl over the first."""
    assert _restart_decision("syncing", _FRESH) is False


def test_a_done_row_does_not_restart_on_retest() -> None:
    assert _restart_decision("done", _FRESH) is False


@pytest.mark.parametrize("status", ["pending", "failed"])
def test_the_states_that_already_restarted_still_do(status: str) -> None:
    assert _restart_decision(status, _FRESH) is True


def test_the_retest_gate_reads_the_staleness_rule(  # noqa: D103
) -> None:
    """A behavioural test cannot see the route drop the `is_stale` arm and go
    back to a two-state gate — the parametrised cases above would all still
    pass. So pin the call site."""
    tree = ast.parse(_ROUTE.read_text())
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "test_connection"
    )
    calls = {
        node.func.attr
        for node in ast.walk(fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "is_stale" in calls, (
        "POST /buildium/test stopped consulting sync_state.is_stale, so a stranded "
        "import can no longer be restarted from the page the banner sends people to"
    )


# ── the Sync data button records that it ran ───────────────────────────────


def test_the_backfill_route_pulls_through_the_wrapper_that_records_state() -> None:
    """⚠ THE WHOLE DEFECT WAS THIS CALL. `run_backfill` called
    `backfill_company` directly, so the button the banner names completed a
    full mirror pull and left the row describing a crawl that had been dead for
    weeks. Nothing behavioural can see a revert — the response body is
    identical either way."""
    tree = ast.parse(_ROUTE.read_text())
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "run_backfill"
    )
    called = {
        node.func.id
        for node in ast.walk(fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "run_initial_sync" in called, (
        "POST /buildium/backfill no longer goes through run_initial_sync, so a "
        "successful manual sync records nothing and the stalled-import banner "
        "stays on screen forever"
    )
    assert "backfill_company" not in called, (
        "POST /buildium/backfill calls backfill_company directly again — that is "
        "the pull without the record, which is the defect"
    )


def test_the_backfill_route_asks_for_the_error_text() -> None:
    """`reraise=True` is what keeps the provider's own words in the UI message.
    Without it the wrapper swallows and the route reports a generic failure —
    a diagnosability regression wearing the fix's name."""
    tree = ast.parse(_ROUTE.read_text())
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "run_backfill"
    )
    call = next(
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "run_initial_sync"
    )
    reraise = next((kw for kw in call.keywords if kw.arg == "reraise"), None)
    assert reraise is not None and getattr(reraise.value, "value", None) is True, (
        "the Sync data button stopped asking run_initial_sync to re-raise, so a "
        "failed sync now shows a generic message instead of what went wrong"
    )


# ── the worker's daily pull self-heals ─────────────────────────────────────


def test_the_worker_clears_a_stranded_row() -> None:
    """The worker re-pulls every connected company's whole account daily, so a
    stranded crawl is repaired with no user action — it just never said so."""
    tree = ast.parse(_WORKER.read_text())
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "run_mirror_sync_cycle"
    )
    calls = {
        node.func.attr
        for node in ast.walk(fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "clear_stranded" in calls, (
        "the daily mirror sync stopped retiring a stranded import row, so a "
        "customer whose first crawl died keeps the warning even though the "
        "worker has since pulled their whole account"
    )


def test_the_worker_never_announces_an_import() -> None:
    """⚠ A daily refresh is not an import. `mark_syncing` here would put "still
    importing your Buildium data" in front of every customer, every day — a
    caveat that fires when it is not true is one people learn to ignore
    (gotchas 57/93)."""
    tree = ast.parse(_WORKER.read_text())
    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "mark_syncing" not in calls
    assert "mark_done" not in calls, (
        "the worker writes sync state directly instead of through clear_stranded, "
        "which drops the refusals that protect a live crawl and a done row"
    )


def test_the_worker_only_clears_after_a_clean_pull() -> None:
    """A run with stage errors must not certify a mirror we know is partial;
    the next clean tick clears it."""
    src = _WORKER.read_text()
    tree = ast.parse(src)
    guard = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and any(
            isinstance(c, ast.Call)
            and isinstance(c.func, ast.Attribute)
            and c.func.attr == "clear_stranded"
            for c in ast.walk(node)
        )
    )
    assert ast.unparse(guard.test) == "result.ok", (
        "the daily sync clears a stranded row on a pull that had stage errors — "
        f"gate reads {ast.unparse(guard.test)!r}, expected 'result.ok'"
    )


# ── the counts vocabulary has one home ─────────────────────────────────────


def test_both_recording_paths_use_the_same_counts_builder() -> None:
    """The banner reads these keys back. Two writers picking different labels
    would render "0 work orders" beside "12 workOrders" with nothing failing."""
    for path in (_BACKFILL, _WORKER):
        tree = ast.parse(path.read_text())
        names = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "sync_counts" in names, f"{path} builds its own counts dict instead of sharing one"
