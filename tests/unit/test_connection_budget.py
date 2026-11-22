"""The connection budget was derived from a real outage and lived only in a
comment — and the fix for that outage turned a diagnosable 500 into a hang.

TWO DEFECTS, ONE INCIDENT.

**The budget could be raised back with nothing failing.** Supabase's
SESSION-mode pooler caps the whole PROJECT at 15 clients, shared by the web
service, the worker, mem0's own pool and whoever has a psql open. On
2026-07-25 a dogfood burst hit that cap and 500'd ``/agent/run`` with
``EMAXCONNSESSION`` (Sentry PYTHON-FASTAPI-31): web(10) + worker(10) + mem0 +
CI had oversubscribed it. The fix — web 6, worker 4 — shipped as two integers
with the reasoning in a comment beside them. A comment cannot fail (gotcha
48), so restoring asyncpg's default of 10 broke no test and silently
re-oversubscribed the pooler. The cap and the reservations are constants now
and the arithmetic is checked here.

**And the fix converted the failure into an unbounded WAIT.** asyncpg's
``Pool.acquire()`` defaults to ``timeout=None``. Once the pool is capped, the
seventh concurrent request under a spike waits FOREVER — holding its worker
slot, logging nothing. The browser's own 45s timeout ends it, so the only
artefact anywhere is a client-side abort with the server-side cause invisible.
That is strictly worse than the outage it replaced: the 500 named itself.

⚠ **THE OVERRIDE IS A SUBCLASS FOR THE REASON GOTCHAS 25/32/81/94/95 ALL END
AT.** There are ~200 explicit ``pool.acquire()`` calls in this tree and a
two-hundred-and-first is one PR away; a ``timeout=`` per call site is the
fan-out where the caller who forgets is silently exempt. One override also
picks up ``pool.fetchrow(...)`` and friends, because asyncpg implements those
as ``async with self.acquire()`` with no timeout.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import pathlib
import re

import asyncpg
import pytest

from helixis.db import engine
from helixis.db.engine import (
    ACQUIRE_TIMEOUT_S,
    _asyncpg_pool_defaults,
    _TimeoutDefaultingPool,
)
from helixis.settings import (
    POOLER_CLIENT_CAP,
    RESERVED_FOR_MEM0,
    RESERVED_FOR_OPS,
    Settings,
)

REPO = pathlib.Path(__file__).resolve().parents[2]
DSN = "postgresql://u:p@127.0.0.1:1/db"


def _pool() -> _TimeoutDefaultingPool:
    """The object ``create_pool`` builds, without connecting to anything.

    ⚠ Must be called with a current event loop — ``Pool.__init__`` reaches for
    ``get_event_loop()`` before it ever opens a socket, so constructing at
    module scope or in a plain sync test raises ``There is no current event
    loop``. Nothing here connects; the DSN points at a closed port on purpose.
    """
    return _TimeoutDefaultingPool(
        DSN, **_asyncpg_pool_defaults(), min_size=1, max_size=2, statement_cache_size=0
    )


# --------------------------------------------------------------------------
# The budget
# --------------------------------------------------------------------------


def test_the_pool_budget_fits_under_the_pooler_cap() -> None:
    """THE LOAD-BEARING ONE. Raising either pool back to asyncpg's default of
    10 re-creates the 2026-07-25 outage, and before this test it broke
    nothing."""
    s = Settings().supabase
    spent = s.pool_max_web + s.pool_max_worker + RESERVED_FOR_MEM0 + RESERVED_FOR_OPS
    assert spent <= POOLER_CLIENT_CAP, (
        f"the connection budget oversubscribes the pooler: web {s.pool_max_web} + "
        f"worker {s.pool_max_worker} + mem0 {RESERVED_FOR_MEM0} + ops "
        f"{RESERVED_FOR_OPS} = {spent} against a cap of {POOLER_CLIENT_CAP}. "
        "This is exactly the arithmetic that 500'd /agent/run with "
        "EMAXCONNSESSION (Sentry PYTHON-FASTAPI-31). Raise the pooler's own "
        "pool_size first, or move it to transaction mode."
    )


def test_the_budget_leaves_headroom_rather_than_landing_exactly_on_the_cap() -> None:
    """Spending the cap to the last client is not a budget — the reservations
    are estimates of somebody else's usage, so a figure that only just fits
    fails the first time mem0 opens a third connection."""
    s = Settings().supabase
    spent = s.pool_max_web + s.pool_max_worker + RESERVED_FOR_MEM0 + RESERVED_FOR_OPS
    assert POOLER_CLIENT_CAP - spent >= 1, (
        f"no headroom: {spent} of {POOLER_CLIENT_CAP} spent, and the mem0/ops "
        "figures are estimates rather than limits anyone enforces"
    )


def test_both_processes_get_a_workable_share() -> None:
    """The other direction, so the arithmetic above cannot be satisfied by
    starving one side to zero."""
    s = Settings().supabase
    assert s.pool_max_web >= 4, "the web service cannot serve concurrent turns on this"
    assert s.pool_max_worker >= 2, "the worker claims batches concurrently"


# --------------------------------------------------------------------------
# The acquire timeout
# --------------------------------------------------------------------------


def test_acquire_waits_bounded_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """The defect itself: without this, exhaustion hangs instead of erroring."""
    seen: list[float | None] = []

    async def _spy(self, timeout):  # noqa: ANN001, ANN202
        seen.append(timeout)
        raise RuntimeError("stop before connecting")

    monkeypatch.setattr(asyncpg.Pool, "_acquire", _spy)

    async def _go() -> None:
        with pytest.raises(RuntimeError):
            await _pool().acquire()

    asyncio.run(_go())
    assert seen == [ACQUIRE_TIMEOUT_S], (
        f"acquire() passed {seen} to asyncpg rather than the default bound. "
        "timeout=None means wait forever, which is the hang this exists to stop."
    )


def test_an_explicit_timeout_still_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """A caller who has measured a reason to wait longer keeps it, and an
    explicit ``0`` is not silently replaced — ``0`` is falsy, so a naive
    ``timeout or DEFAULT`` would override the one value that means
    'do not wait at all'."""
    seen: list[float | None] = []

    async def _spy(self, timeout):  # noqa: ANN001, ANN202
        seen.append(timeout)
        raise RuntimeError("stop")

    monkeypatch.setattr(asyncpg.Pool, "_acquire", _spy)

    async def _go() -> None:
        pool = _pool()
        for kwargs in ({"timeout": 2.5}, {"timeout": 0}):
            with pytest.raises(RuntimeError):
                await pool.acquire(**kwargs)

    asyncio.run(_go())
    assert seen == [2.5, 0]


def test_the_convenience_shortcuts_inherit_the_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    """``pool.fetchrow(...)`` is the shape most of this tree actually uses, and
    asyncpg implements it as ``async with self.acquire()`` with no timeout —
    READ from the installed source rather than assumed (gotcha 79). If a future
    asyncpg passes its own timeout through, this goes red and the override
    needs re-checking."""
    seen: list[float | None] = []

    async def _spy(self, timeout):  # noqa: ANN001, ANN202
        seen.append(timeout)
        raise RuntimeError("stop")

    monkeypatch.setattr(asyncpg.Pool, "_acquire", _spy)

    async def _go() -> None:
        with pytest.raises(RuntimeError):
            await _pool().fetchrow("select 1")

    asyncio.run(_go())
    assert seen == [ACQUIRE_TIMEOUT_S], (
        f"pool.fetchrow() reached asyncpg with {seen}; the subclass no longer "
        "covers the convenience methods, so most callers are unbounded again"
    )


def test_the_timeout_lands_inside_the_clients_own_give_up_time() -> None:
    """BOTH SIDES (gotcha 28). The point of the bound is that the SERVER
    produces a diagnosable error before the browser aborts — otherwise all
    anybody ever sees is a client-side 'Failed to fetch' and the cause stays
    invisible, which is the defect rather than the fix."""
    api = (REPO / "frontend" / "src" / "services" / "api.ts").read_text()
    m = re.search(r"DEFAULT_TIMEOUT_MS\s*=\s*([\d_]+)", api)
    assert m, "could not read DEFAULT_TIMEOUT_MS from services/api.ts — the scan is stale"
    client_s = int(m.group(1).replace("_", "")) / 1000
    assert ACQUIRE_TIMEOUT_S < client_s / 2, (
        f"the pool waits {ACQUIRE_TIMEOUT_S}s against a client that gives up at "
        f"{client_s}s. The server has to fail first, with room for the rest of "
        "the request, or the only artefact is the browser's abort."
    )
    assert ACQUIRE_TIMEOUT_S >= 2, (
        "too tight — a healthy query holds a connection for milliseconds, so a "
        "bound this low turns an ordinary burst into errors"
    )


# --------------------------------------------------------------------------
# The wiring
# --------------------------------------------------------------------------


def test_create_pool_actually_builds_the_bounded_class() -> None:
    """A behavioural test of the subclass cannot see a ``create_pool`` that
    quietly goes back to ``asyncpg.create_pool(...)`` — every acquire test
    above would still pass while production returned to an unbounded wait.
    That is gotchas 62/66/69/103's class, so this walks the call site."""
    tree = ast.parse(inspect.getsource(engine))
    fn = next(
        n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "create_pool"
    )
    built = {
        n.func.id for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    } | {
        f"{n.func.value.id}.{n.func.attr}"
        for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and isinstance(n.func.value, ast.Name)
    }
    assert "_TimeoutDefaultingPool" in built, (
        "create_pool no longer constructs the bounded pool — every acquire in "
        "the process is back to waiting forever"
    )
    assert "asyncpg.create_pool" not in built, (
        "create_pool is calling asyncpg's constructor again, which returns a "
        "plain Pool and silently drops the timeout default"
    )


def test_the_bounded_pool_is_still_an_asyncpg_pool() -> None:
    """~200 call sites and every type annotation say ``asyncpg.Pool``. The
    subclass has to satisfy them or this is a much larger change than it
    looks."""

    async def _go() -> None:
        assert isinstance(_pool(), asyncpg.Pool)

    asyncio.run(_go())


def test_asyncpg_still_supplies_every_kwarg_its_constructor_demands() -> None:
    """``Pool.__init__`` requires five kwargs ``create_pool`` fills in, and
    hardcoding today's values would freeze them across an upgrade — so they are
    read off asyncpg's own signature. If an upgrade adds a required kwarg with
    no default, construction breaks AT BOOT; this says so here instead."""
    defaults = _asyncpg_pool_defaults()
    required = {
        name
        for name, p in inspect.signature(asyncpg.Pool.__init__).parameters.items()
        if p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is inspect.Parameter.empty
    }
    missing = sorted(required - set(defaults) - {"min_size", "max_size"})
    assert missing == [], (
        f"asyncpg.Pool now demands {missing}, which create_pool's signature does "
        "not default — the bounded pool will not construct and the process will "
        "fail to boot"
    )

    async def _go() -> None:
        _pool()  # constructs, so the mirror is complete today

    asyncio.run(_go())
