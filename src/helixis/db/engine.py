"""Asyncpg connection pool management.

The Helixis backend reuses the Supabase Postgres database (via
``HELIXIS_SUPABASE__DB_URL``). We keep a single global pool for the
process and expose init/shutdown hooks for FastAPI's lifespan.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Mapping, Sequence

import asyncpg

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None


class SchemaVerificationError(RuntimeError):
    """The pool cannot see one or more columns the application requires.

    Almost always indicates a stale PgBouncer/asyncpg view of the
    schema after a recent migration — the database has the column,
    but the pool's bound backend session does not. Restart the
    process to force fresh backend bindings.
    """


#: How long a caller may wait for a free connection before the request FAILS
#: rather than hanging.
#:
#: ⚠ THE HANG IS WORSE THAN THE OUTAGE IT REPLACED. The 2026-07-25 burst
#: exhausted the pooler and returned ``EMAXCONNSESSION`` 500s — ugly, and
#: diagnosable: it named itself in Sentry. Capping the pool (settings
#: ``pool_max_web``/``pool_max_worker``) stopped the oversubscription and
#: converted the failure into an UNBOUNDED WAIT, because asyncpg's
#: ``Pool.acquire()`` defaults to ``timeout=None``. Under a spike the seventh
#: request now waits forever, holds its worker slot, and logs nothing — the
#: browser's own 45s timeout is the only thing that ends it, so all anybody
#: ever sees is a client-side abort with the server-side cause invisible.
#:
#: The value is chosen by a RELATIONSHIP, not picked (gotcha 96): a healthy
#: query holds a connection for milliseconds, so ten seconds of queueing means
#: the pool is genuinely saturated rather than briefly busy; and it must land
#: well inside the frontend's ``DEFAULT_TIMEOUT_MS`` (45s, services/api.ts) so
#: the SERVER produces the error first and Sentry gets the cause instead of the
#: symptom. ``test_connection_budget.py`` reads both sides.
ACQUIRE_TIMEOUT_S = 10.0


def _asyncpg_pool_defaults() -> dict[str, object]:
    """The kwargs ``asyncpg.create_pool`` would have supplied for us.

    ``Pool.__init__`` requires several of these outright, and hardcoding
    today's values would pin them across an asyncpg upgrade. Reading them off
    the installed signature means a changed default reaches us the way it
    reaches every other asyncpg user. Only the names we set ourselves are
    withheld; ``connect_kwargs`` is VAR_KEYWORD and has no default, so it drops
    out on its own.
    """
    ours = {"dsn", "min_size", "max_size"}
    return {
        name: param.default
        for name, param in inspect.signature(asyncpg.create_pool).parameters.items()
        if name not in ours and param.default is not inspect.Parameter.empty
    }


class _TimeoutDefaultingPool(asyncpg.Pool):
    """An ``asyncpg.Pool`` whose ``acquire()`` waits bounded by default.

    ⚠ ONE OVERRIDE COVERS THE WHOLE SURFACE, which is why this is a subclass
    rather than a ``timeout=`` at each call site. There are ~200 explicit
    ``pool.acquire()`` calls in this tree and a two-hundred-and-first is one PR
    away — the fan-out gotchas 25/32/81/94/95 all end at, where the caller that
    forgets is silently exempt. asyncpg's convenience methods reach it too:
    ``Pool.fetchrow`` is ``async with self.acquire() as con`` with no timeout
    (READ from the installed 0.31.0 source, not recalled — gotcha 79), so
    ``pool.fetchrow(...)`` inherits the bound without knowing about it.

    An EXPLICIT ``timeout=`` still wins: a caller who has measured a reason to
    wait longer keeps it, and passing ``0`` or a float is unchanged behaviour.
    """

    def acquire(self, *, timeout: float | None = None):  # type: ignore[override] # noqa: ANN201
        return super().acquire(timeout=ACQUIRE_TIMEOUT_S if timeout is None else timeout)


async def create_pool(dsn: str, *, min_size: int = 1, max_size: int = 10) -> asyncpg.Pool:
    """Create a new pool. Caller owns its lifecycle."""
    # PR #96 — ``statement_cache_size=0`` disables asyncpg's per-
    # connection prepared-statement cache. We do this because:
    #
    #   1. Supabase fronts Postgres with PgBouncer in transaction-
    #      pooling mode. Prepared statements are per-backend-session
    #      and don't survive a backend swap, so any cached plan can
    #      become stale or unreachable after PgBouncer rebinds the
    #      backend.
    #   2. Recently-migrated columns (e.g. PR #92's summary_* set)
    #      created a confusing partial-data symptom in production —
    #      ``summary_last_attempted_at`` populated but
    #      ``summary_text`` and ``summary_attempts`` null/0 in the
    #      JSON response — that's consistent with a stale prepared
    #      plan referring to a pre-migration column set.
    #
    # The per-call parse-and-plan cost is negligible for our query
    # volume; defensive cache-off is the right default here.
    # ⚠ Constructed directly rather than through ``asyncpg.create_pool``,
    # which hardcodes ``return Pool(...)`` and exposes no ``pool_class`` hook
    # (read from 0.31.0's own source). Two shapes were PROBED and rejected
    # before this one, both of which would have shipped a defect:
    #   * passing the five required kwargs by hand — ``Pool.__init__`` demands
    #     ``max_queries`` / ``max_inactive_connection_lifetime`` / ``loop`` /
    #     ``connection_class`` / ``record_class``, and writing today's values
    #     down FREEZES them, so an asyncpg release that changes a default
    #     leaves us silently on the old one (gotcha 48's shape);
    #   * ``pool.__class__ = _TimeoutDefaultingPool`` after the fact —
    #     ``TypeError: __class__ assignment: object layout differs``, because
    #     ``Pool`` carries ``__slots__``.
    # So the defaults are READ OFF asyncpg's own signature and cannot drift.
    return await _TimeoutDefaultingPool(
        dsn,
        **_asyncpg_pool_defaults(),
        min_size=min_size,
        max_size=max_size,
        statement_cache_size=0,
    )


async def init_pool(dsn: str, *, min_size: int = 1, max_size: int = 10) -> asyncpg.Pool:
    """Initialize the process-global pool. Idempotent."""
    global _pool
    if _pool is not None:
        return _pool
    if not dsn:
        raise RuntimeError("HELIXIS_SUPABASE__DB_URL is empty; cannot initialize Postgres pool.")
    _pool = await create_pool(dsn, min_size=min_size, max_size=max_size)
    logger.info("Postgres pool initialized (min=%d, max=%d)", min_size, max_size)
    return _pool


def get_pool() -> asyncpg.Pool:
    """Return the global pool. Raises if init_pool hasn't been called."""
    if _pool is None:
        raise RuntimeError("Postgres pool not initialized; call init_pool() first.")
    return _pool


async def shutdown_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("Postgres pool closed")


async def verify_schema(
    pool: asyncpg.Pool,
    expected: Mapping[str, Sequence[str]],
) -> None:
    """Confirm the pool can see every ``(table, column)`` pair in ``expected``.

    PR #101 — boot-time gate that catches the class of bug where the
    schema is correctly migrated but the pool's bound PgBouncer
    backend sessions still hold a pre-migration view (a recurrence
    of the symptom PR #96 partially addressed at the asyncpg layer).

    The query goes through ``pool.acquire()`` rather than a side
    connection, so we are testing the pool's view of the schema and
    not a fresh connection's view. If any column is missing, raises
    ``SchemaVerificationError`` with a single-line message listing
    every missing ``(table, column)`` pair.

    Multi-tenant safe: queries ``information_schema.columns`` only,
    no per-tenant row reads, no ``company_id`` filter. Works on a
    freshly-provisioned tenant DB with zero rows.
    """
    if not expected:
        return

    # Single round-trip: pull every column for the union of tables we
    # care about, then diff in Python.
    table_names = list(expected.keys())
    query = """
        SELECT table_name, column_name
          FROM information_schema.columns
         WHERE table_schema = 'public'
           AND table_name = ANY($1::text[])
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(query, table_names)

    present: dict[str, set[str]] = {t: set() for t in table_names}
    for row in rows:
        present.setdefault(row["table_name"], set()).add(row["column_name"])

    missing: list[str] = []
    for table, columns in expected.items():
        missing_cols = [c for c in columns if c not in present.get(table, set())]
        if missing_cols:
            missing.append(f"{table}: {', '.join(missing_cols)}")

    if missing:
        raise SchemaVerificationError(
            "asyncpg pool is missing required columns — " + "; ".join(missing)
        )
