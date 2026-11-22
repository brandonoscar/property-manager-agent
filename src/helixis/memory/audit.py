"""Memory audit log — every Mem0 add/search/delete writes a row.

The audit table (``memory_audit``) is RLS-scoped per company so each company
can later read its own access trail. Writes go through the global asyncpg
pool from ``helixis.db.engine``.

Failures here MUST NOT break the underlying memory operation. The audit row
is best-effort — log + continue. The memory call's success or failure is
authoritative.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

logger = logging.getLogger(__name__)

AuditAction = Literal["add", "search", "delete", "clear", "delete_company", "operational"]

# Truncation limits keep audit rows small. Memory contents and search queries
# can be arbitrarily long; audit is for traceability, not full data retention.
_CONTENT_TRUNCATE = 500
_QUERY_TRUNCATE = 500


async def write_audit(
    *,
    company_id: str,
    user_id: str,
    action: AuditAction,
    memory_id: str | None = None,
    query: str | None = None,
    content: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Insert one row into ``memory_audit``.

    Parameters mirror the table columns exactly. ``user_id`` must be a
    Supabase auth.users(id) UUID. ``company_id`` is a public.companies(id)
    text key.

    All exceptions are swallowed and logged — never re-raised. The caller
    owns the underlying memory operation; an audit failure cannot regress
    a successful memory write.
    """
    if not company_id or not user_id:
        # No audit context to attribute the row to. Silently skip — this is
        # the case for cron/system memory writes where there is no human user.
        return

    pool = _get_pool_or_none()
    if pool is None:
        # Pool not initialised (tests, local dev without Postgres). Audit is
        # best-effort; surface at debug level only.
        logger.debug("memory_audit skipped: no asyncpg pool")
        return

    truncated_content = content[:_CONTENT_TRUNCATE] if content else None
    truncated_query = query[:_QUERY_TRUNCATE] if query else None
    metadata_json = json.dumps(metadata) if metadata is not None else None

    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO memory_audit
                    (company_id, user_id, action, memory_id, query, content, metadata)
                VALUES ($1, $2::uuid, $3, $4, $5, $6, $7::jsonb)
                """,
                company_id,
                user_id,
                action,
                memory_id,
                truncated_query,
                truncated_content,
                metadata_json,
            )
    except Exception:  # noqa: BLE001 — audit must never break callers
        logger.exception(
            "Failed to write memory_audit row (company=%s action=%s)",
            company_id,
            action,
        )


async def write_operational_audit(
    *,
    company_id: str,
    content: str,
    memory_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Audit a SYSTEM-derived operational-memory write (#534 Stage B).

    Distinct from :func:`write_audit` because operational signals have no human
    user to attribute — they're derived deterministically by the worker from
    the Buildium mirror. Logs ``action='operational'`` with ``user_id=NULL``
    (both enabled by migration 0055). Best-effort: never raises, mirroring
    :func:`write_audit`. ``company_id`` alone gates the write (no user_id
    requirement, unlike the human path)."""
    if not company_id:
        return

    pool = _get_pool_or_none()
    if pool is None:
        logger.debug("memory_audit (operational) skipped: no asyncpg pool")
        return

    truncated_content = content[:_CONTENT_TRUNCATE] if content else None
    metadata_json = json.dumps(metadata) if metadata is not None else None

    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO memory_audit
                    (company_id, user_id, action, memory_id, query, content, metadata)
                VALUES ($1, NULL, 'operational', $2, NULL, $3, $4::jsonb)
                """,
                company_id,
                memory_id,
                truncated_content,
                metadata_json,
            )
    except Exception:  # noqa: BLE001 — audit must never break callers
        logger.exception("Failed to write operational memory_audit row (company=%s)", company_id)


def _get_pool_or_none() -> Any | None:
    """Return the asyncpg pool, or None if it isn't initialised yet."""
    try:
        from helixis.db.engine import get_pool

        return get_pool()
    except (ImportError, RuntimeError):
        return None
