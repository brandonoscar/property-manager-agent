"""Telemetry writer for orchestrator runs.

Feeds the ``/dev/runs`` inspection page (PR #141) and the
narrative-truth assertion / replay surfaces coming in PRs #143 and
#144 of the Trust series. Every helper here is best-effort: a failure
to write telemetry MUST NOT take down the user's agent run. The
orchestrator stream loop is the only caller.

Discipline mirrors ``write_activity``: wrap the DB work in
``try/except Exception``, ``logger.exception(...)`` on failure, never
re-raise. Tool args and outputs pass through ``_redact`` before write
so emails / phone numbers / credential-like fields don't land in the
operational store as plaintext.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import asyncpg

logger = logging.getLogger(__name__)


# ── Redaction ─────────────────────────────────────────────────────────
#
# Mask before write, not before read, so a future bug in the read path
# can't accidentally surface raw secrets even from older rows. Cheap
# regex / string ops — runs once per tool call, not per token.

_SECRET_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "password",
        "token",
        "secret",
        "api_key",
        "apikey",
        "client_secret",
        "refresh_token",
        "access_token",
        "authorization",
    }
)

_EMAIL_RE = re.compile(r"([A-Za-z0-9._%+-]{1,64})@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
_PHONE_RE = re.compile(r"(?<!\d)(\+?\d[\d\s\-().]{7,}\d)(?!\d)")


def _mask_email(match: re.Match[str]) -> str:
    local, domain = match.group(1), match.group(2)
    if len(local) <= 2:
        return f"{local[0]}***@{domain}"
    return f"{local[:2]}***@{domain}"


def _mask_phone(match: re.Match[str]) -> str:
    raw = match.group(1)
    digits = re.sub(r"\D", "", raw)
    if len(digits) < 4:
        return raw
    return f"***-***-{digits[-4:]}"


def _redact_string(value: str) -> str:
    """Apply email and phone masks to a free-text value."""
    value = _EMAIL_RE.sub(_mask_email, value)
    value = _PHONE_RE.sub(_mask_phone, value)
    return value


def _redact(obj: Any) -> Any:
    """Recursively mask PII / secrets in tool args and outputs.

    Strategy:
      * dict — recurse, but values whose key matches a secret-name set
        are replaced wholesale with ``"***"`` instead of partial masks.
        Partial masks would let an attacker reconstruct the secret
        from many calls.
      * list / tuple — recurse element-wise.
      * str — apply email + phone regex passes.
      * everything else — pass through unchanged.

    Returns a NEW object; never mutates the input. The orchestrator
    needs the original args/output to keep streaming to the user.
    """
    if isinstance(obj, dict):
        out: dict[Any, Any] = {}
        for key, value in obj.items():
            key_lower = str(key).lower() if key is not None else ""
            if key_lower in _SECRET_FIELD_NAMES:
                out[key] = "***"
            else:
                out[key] = _redact(value)
        return out
    if isinstance(obj, (list, tuple)):
        redacted = [_redact(v) for v in obj]
        return redacted if isinstance(obj, list) else tuple(redacted)
    if isinstance(obj, str):
        return _redact_string(obj)
    return obj


# ── Row shape ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class OrchestratorRunRow:
    """Read-path shape for ``/dev/runs``. Mirrors the SELECT columns.

    JSONB fields land as Python dicts/lists; asyncpg already decodes
    them when the column type is jsonb and a codec is registered.
    """

    id: str
    company_id: str
    user_id: str | None
    session_id: str
    source: str
    user_query: str
    started_at: datetime
    completed_at: datetime | None
    latency_ms: int | None
    status: str
    error_message: str | None
    error_type: str | None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    final_text: str | None = None
    handoffs: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "company_id": self.company_id,
            "user_id": self.user_id,
            "session_id": self.session_id,
            "source": self.source,
            "user_query": self.user_query,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "latency_ms": self.latency_ms,
            "status": self.status,
            "error_message": self.error_message,
            "error_type": self.error_type,
            "tool_calls": self.tool_calls,
            "final_text": self.final_text,
            "handoffs": self.handoffs,
            "raw": self.raw,
        }


def _row_to_dataclass(row: asyncpg.Record) -> OrchestratorRunRow:
    """Convert an asyncpg row into the dataclass shape.

    JSONB columns come back as raw strings when ``statement_cache_size=0``
    is set on the pool (see ``db/engine.py``) — parse here so callers
    never have to think about it.
    """

    def _maybe_json(value: Any, default: Any) -> Any:
        if value is None:
            return default
        if isinstance(value, (dict, list)):
            return value
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return default

    return OrchestratorRunRow(
        id=str(row["id"]),
        company_id=str(row["company_id"]),
        user_id=str(row["user_id"]) if row["user_id"] else None,
        session_id=row["session_id"],
        source=row["source"],
        user_query=row["user_query"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        latency_ms=row["latency_ms"],
        status=row["status"],
        error_message=row["error_message"],
        error_type=row["error_type"],
        tool_calls=_maybe_json(row["tool_calls"], []),
        final_text=row["final_text"],
        handoffs=_maybe_json(row["handoffs"], []),
        raw=_maybe_json(row["raw"], None),
    )


# ── Writers ───────────────────────────────────────────────────────────


async def get_company_for_session(pool: asyncpg.Pool, session_id: str) -> str | None:
    """Return the company_id that owns ``session_id`` (from its run record), or
    ``None`` if no run row exists yet. A session belongs to one company, so any
    matching row's company answers ownership. Used by ``/agent/cancel`` to keep
    one tenant from cancelling another's live run. Best-effort: returns ``None``
    (caller falls through to current behavior) on absence or DB error.

    ⚠ **The ``str()`` is the whole function's correctness, not a tidy-up.**
    ``orchestrator_runs.company_id`` is a Postgres ``uuid``, so asyncpg decodes
    it to ``asyncpg.pgproto.pgproto.UUID`` — a ``uuid.UUID`` subclass, NOT a
    string. Every caller compares the result against ``user.company_ids``,
    which comes from a **PostgREST JSON** read in ``auth.py`` and is therefore
    a list of plain ``str``. ``UUID(...) in ["<the same uuid>"]`` is **False**,
    always::

        v = pgproto.UUID(...)          # 5ca1ab1e-…
        str(v) == "5ca1ab1e-…"         # True
        v in ["5ca1ab1e-…"]            # False   <-- the production comparison

    So the annotation on this function was not merely imprecise; it described
    a value it never returned, and three ownership checks silently inverted:

    * ``POST /agent/run``  (``owner is not None and owner not in …``)
      404'd **every turn in a thread that already had a run row** — i.e. every
      follow-up. A brand-new thread has no row, so it worked, which is exactly
      why this looked intermittent rather than total.
    * ``GET /agent/watch`` and ``POST /agent/cancel``
      (``owner is None or owner not in …``) 404'd **always**, so the
      dropped-stream reattach and the stop button could never work, and
      multiplayer live-watch was dead on arrival.

    ⚠ **Every ownership test passed throughout**, because the fake pool in
    ``test_agent_ownership.py`` returns ``{"company_id": "<a str>"}`` — a fake
    answers whatever shape it is handed (gotchas 69/79). The regression below
    feeds the REAL decoded type.

    ⚠ ``helixis_pending_confirmations.company_id`` is ``text``, so the confirm
    route's own check was never affected — measured, not assumed. Do not
    "fix" that one too.
    """
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT company_id FROM public.orchestrator_runs WHERE session_id = $1 LIMIT 1",
                session_id,
            )
        if row is None:
            return None
        owner = row["company_id"]
        return str(owner) if owner is not None else None
    except Exception:  # noqa: BLE001 — ownership check must never break cancel
        logger.debug("get_company_for_session failed (non-fatal)", exc_info=True)
        return None


async def begin_run(
    pool: asyncpg.Pool,
    *,
    company_id: str,
    user_id: str | None,
    session_id: str,
    source: str,
    user_query: str,
) -> UUID | None:
    """Insert a 'running' row. Returns the new run id, or None on failure.

    The orchestrator stores the returned id in a local variable and
    threads it through the stream loop; later calls to ``record_*`` /
    ``complete_run`` no-op when the id is None, so a failure here
    silently disables telemetry for that one run instead of breaking
    the user's response.
    """
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO public.orchestrator_runs
                    (company_id, user_id, session_id, source, user_query, status)
                VALUES ($1, $2::uuid, $3, $4, $5, 'running')
                RETURNING id
                """,
                company_id,
                user_id,
                session_id,
                source,
                user_query,
            )
            return row["id"] if row else None
    except Exception:  # noqa: BLE001
        logger.exception(
            "orchestrator_runs.begin_run failed (company_id=%s session_id=%s source=%s)",
            company_id,
            session_id,
            source,
        )
        return None


async def record_tool_call(
    pool: asyncpg.Pool,
    *,
    run_id: UUID,
    name: str,
    args: Any,
    started_at: datetime,
) -> None:
    """Append a tool-call stub to ``tool_calls`` jsonb.

    The stub is the call's start. ``record_tool_output`` later updates
    the same element with output, duration, and error. The pair is
    matched by name + ordinal (last unfinished entry with the same
    name) rather than call_id so storage is independent of the SDK's
    call_id shape — see PR #102 for why fixture-based assumptions
    about call_id break.
    """
    if run_id is None:
        return
    try:
        stub = {
            "name": name,
            "args": _redact(args),
            "started_at": started_at.isoformat(),
        }
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE public.orchestrator_runs
                   SET tool_calls = tool_calls || $2::jsonb
                 WHERE id = $1
                """,
                run_id,
                json.dumps(stub),
            )
    except Exception:  # noqa: BLE001
        logger.exception(
            "orchestrator_runs.record_tool_call failed (run_id=%s name=%s)",
            run_id,
            name,
        )


async def record_tool_output(
    pool: asyncpg.Pool,
    *,
    run_id: UUID,
    name: str,
    output: Any,
    duration_ms: int,
    error: str | None,
) -> None:
    """Update the most recent matching tool-call stub with its output.

    Uses a SQL CTE that finds the last element of ``tool_calls`` whose
    ``name`` matches and which has no ``output`` yet, then rewrites
    that single element. Falls back to appending a standalone output
    record if no matching stub is found — keeps the audit trail honest
    even if ``tool_called`` was missed for some reason.
    """
    if run_id is None:
        return
    try:
        payload = {
            "output": _redact(output),
            "duration_ms": duration_ms,
            "error": error,
        }
        async with pool.acquire() as conn:
            # Match-by-name + missing-output: find the latest unfinished
            # call with this name, merge the output payload into it.
            # ``jsonb_set`` with the path returned by the lateral
            # subquery does the rewrite in place.
            updated = await conn.fetchval(
                """
                WITH target AS (
                    SELECT ord
                      FROM (
                        SELECT (ord - 1) AS ord, elem
                          FROM public.orchestrator_runs r,
                               LATERAL jsonb_array_elements(r.tool_calls)
                                   WITH ORDINALITY AS t(elem, ord)
                         WHERE r.id = $1
                           AND elem->>'name' = $2
                           AND NOT (elem ? 'output')
                      ) t
                     ORDER BY ord DESC
                     LIMIT 1
                )
                UPDATE public.orchestrator_runs
                   SET tool_calls = jsonb_set(
                       tool_calls,
                       ARRAY[(SELECT ord FROM target)::text],
                       -- ``ord`` comes from WITH ORDINALITY, which Postgres
                       -- types as bigint. The jsonb ``->`` operator only has
                       -- an int4 overload, so an uncast index raises
                       -- ``operator does not exist: jsonb -> bigint`` (#5).
                       (tool_calls -> (SELECT ord FROM target)::int) || $3::jsonb,
                       false
                   )
                 WHERE id = $1
                   AND EXISTS (SELECT 1 FROM target)
                RETURNING (SELECT ord FROM target)
                """,
                run_id,
                name,
                json.dumps(payload),
            )
            if updated is None:
                # No matching stub found — append a standalone record so
                # the output isn't lost. Distinguishable from a paired
                # call because ``started_at`` is absent.
                fallback = {"name": name, **payload, "orphaned": True}
                await conn.execute(
                    """
                    UPDATE public.orchestrator_runs
                       SET tool_calls = tool_calls || $2::jsonb
                     WHERE id = $1
                    """,
                    run_id,
                    json.dumps(fallback),
                )
    except Exception:  # noqa: BLE001
        logger.exception(
            "orchestrator_runs.record_tool_output failed (run_id=%s name=%s)",
            run_id,
            name,
        )


async def record_handoff(
    pool: asyncpg.Pool,
    *,
    run_id: UUID,
    from_agent: str,
    to_agent: str,
) -> None:
    """Append an inter-agent handoff to ``handoffs`` jsonb."""
    if run_id is None:
        return
    try:
        entry = {
            "from_agent": from_agent,
            "to_agent": to_agent,
            "at": datetime.now(UTC).isoformat(),
        }
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE public.orchestrator_runs
                   SET handoffs = handoffs || $2::jsonb
                 WHERE id = $1
                """,
                run_id,
                json.dumps(entry),
            )
    except Exception:  # noqa: BLE001
        logger.exception(
            "orchestrator_runs.record_handoff failed (run_id=%s %s->%s)",
            run_id,
            from_agent,
            to_agent,
        )


async def complete_run(
    pool: asyncpg.Pool,
    *,
    run_id: UUID,
    status: str,
    final_text: str | None,
    error_message: str | None,
    error_type: str | None,
    latency_ms: int,
) -> None:
    """Finalize a run row on success, failure, or cancellation.

    The orchestrator's normal exit path, its except blocks, and its
    cancel path all funnel through here. ``status`` must be one of
    'succeeded', 'failed', 'cancelled'.
    """
    if run_id is None:
        return
    truncated_final = final_text[:16000] if final_text else None
    truncated_err = error_message[:1000] if error_message else None
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE public.orchestrator_runs
                   SET status        = $2,
                       final_text    = $3,
                       error_message = $4,
                       error_type    = $5,
                       latency_ms    = $6,
                       completed_at  = now()
                 WHERE id = $1
                """,
                run_id,
                status,
                truncated_final,
                truncated_err,
                error_type,
                latency_ms,
            )
    except Exception:  # noqa: BLE001
        logger.exception(
            "orchestrator_runs.complete_run failed (run_id=%s status=%s)",
            run_id,
            status,
        )


async def record_run_stats(
    pool: asyncpg.Pool,
    *,
    run_id: UUID,
    stats: dict[str, Any],
) -> None:
    """Persist a run's diagnostics (stop reason + budget ledgers) into ``raw``.

    Why this exists: ``stop_reason``, ``call_budget.summary`` and
    ``specialist_budget.summary`` were emitted to Langfuse and NOWHERE else.
    Battery v4 could only diagnose the wall-clock losses by reading traces by
    hand, and no one could answer "how often does a production run die at the
    deadline?" at all — the number existed for exactly one run at a time, in a
    third-party tool.

    Writing it beside the run makes the question a query. It also makes the
    battery self-contained: the harness reads what the run actually did instead
    of inferring it from tool-call counts, which is precisely how a concurrency
    hole survived a whole battery (gotcha 25).

    Merged into ``raw`` rather than replacing it, and best-effort like every
    other writer here — diagnostics must never fail a run that worked.
    """
    if run_id is None or not stats:
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE public.orchestrator_runs
                   SET raw = coalesce(raw, '{}'::jsonb) || $2::jsonb
                 WHERE id = $1
                """,
                run_id,
                json.dumps(_redact(stats)),
            )
    except Exception:  # noqa: BLE001 — telemetry never breaks a run
        logger.exception("orchestrator_runs.record_run_stats failed (run_id=%s)", run_id)


async def reap_stale_runs(
    pool: asyncpg.Pool,
    *,
    stale_after_seconds: int = 3600,
) -> int:
    """Close out runs stuck in ``running`` past ``stale_after_seconds``.

    A process death mid-run (deploy, OOM, crash) leaves the telemetry row in
    ``running`` forever — ``complete_run`` never fires, so ``/dev/runs`` shows
    phantom in-flight work and any "currently running" count is wrong. The
    worker's janitor calls this on the same cadence as the queue's
    stale-``running`` rescue (worker.py) and moves such rows to the documented
    terminal vocabulary: ``status='failed'`` with ``error_type='AbandonedRun'``
    so readers can tell a reaped row from a real failure. ``latency_ms`` is
    left NULL — the true duration is unknowable.

    The threshold (default 1h) sits far above the orchestrator's own
    wall-clock cutoff, so a legitimately long run can never be reaped —
    only rows whose writer is provably gone. Best-effort: returns the number
    of rows reaped, 0 on any error.
    """
    try:
        async with pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE public.orchestrator_runs
                   SET status        = 'failed',
                       error_type    = 'AbandonedRun',
                       error_message = 'reaped: stuck in running past '
                                       || $1::text || 's (process died mid-run)',
                       completed_at  = now()
                 WHERE status = 'running'
                   AND started_at < now() - ($1::text || ' seconds')::interval
                """,
                str(stale_after_seconds),
            )
        # asyncpg returns e.g. "UPDATE 3".
        try:
            return int(result.split()[-1])
        except (ValueError, IndexError, AttributeError):
            return 0
    except Exception:  # noqa: BLE001 — janitor path, must never crash the worker
        logger.exception("orchestrator_runs.reap_stale_runs failed")
        return 0


# ── Readers ───────────────────────────────────────────────────────────


async def fetch_recent_runs(
    pool: asyncpg.Pool,
    *,
    company_id: str,
    limit: int = 100,
) -> list[OrchestratorRunRow]:
    """Read path for ``/dev/runs``. Returns runs newest-first.

    The route layer enforces ``company_id`` matches the authenticated
    user's membership before calling this. RLS on the table is the
    second line of defense (and the one that protects raw-SQL paths).
    """
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, company_id, user_id, session_id, source,
                       user_query, started_at, completed_at, latency_ms,
                       status, error_message, error_type, tool_calls,
                       final_text, handoffs, raw
                  FROM public.orchestrator_runs
                 WHERE company_id = $1
                 ORDER BY started_at DESC
                 LIMIT $2
                """,
                company_id,
                limit,
            )
            return [_row_to_dataclass(r) for r in rows]
    except Exception:  # noqa: BLE001
        logger.exception(
            "orchestrator_runs.fetch_recent_runs failed (company_id=%s)",
            company_id,
        )
        return []


async def fetch_run(
    pool: asyncpg.Pool,
    *,
    company_id: str,
    run_id: UUID,
) -> OrchestratorRunRow | None:
    """Detail-view fetch. Company-scoped — never returns cross-tenant rows.

    A missing row OR a row for a different company both return None.
    The route layer treats None as 404 so the existence of another
    company's run isn't leaked through differential responses.
    """
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, company_id, user_id, session_id, source,
                       user_query, started_at, completed_at, latency_ms,
                       status, error_message, error_type, tool_calls,
                       final_text, handoffs, raw
                  FROM public.orchestrator_runs
                 WHERE id = $1
                   AND company_id = $2
                """,
                run_id,
                company_id,
            )
            return _row_to_dataclass(row) if row else None
    except Exception:  # noqa: BLE001
        logger.exception(
            "orchestrator_runs.fetch_run failed (run_id=%s company_id=%s)",
            run_id,
            company_id,
        )
        return None
