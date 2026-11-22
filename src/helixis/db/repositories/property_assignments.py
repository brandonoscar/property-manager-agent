"""Repository for ``property_assignments`` and admin-resolution lookups (PR #80).

Routing reads two things from Postgres:

1. The ``property_assignments`` row that matches a given
   ``(company_id, buildium_property_id, category)`` triple, or the
   company-wide default (``buildium_property_id IS NULL``) when no
   property-specific row exists.
2. The company admin's ``user_id`` to fall back to when no assignment
   resolves to a user — every event has to end up in *someone's*
   Inbox.

Both helpers are deliberately read-only — writes happen via the
properties API routes (PR #81 territory). v1 only needs the read
path so the webhook router can make a routing decision.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PropertyAssignment:
    """One row from ``property_assignments``."""

    id: str
    company_id: str
    buildium_property_id: int | None
    category: str
    action_type: str
    action_config: dict[str, Any]


async def load_property_assignment(
    pool: asyncpg.Pool,
    *,
    company_id: str,
    buildium_property_id: int | None,
    category: str,
) -> PropertyAssignment | None:
    """Fetch the assignment row matching the triple, or ``None``.

    Callers should attempt the property-bound row first; if it's
    ``None`` they then re-call with ``buildium_property_id=None`` to
    get the company-wide default row for that category. The router
    encodes this two-step lookup; this helper does not — it returns
    exactly the row that matches the arguments.
    """
    try:
        if buildium_property_id is None:
            row = await pool.fetchrow(
                """
                SELECT id::text                AS id,
                       company_id::text        AS company_id,
                       buildium_property_id,
                       category,
                       action_type,
                       action_config
                  FROM public.property_assignments
                 WHERE company_id = $1::uuid
                   AND buildium_property_id IS NULL
                   AND category = $2
                 LIMIT 1
                """,
                company_id,
                category,
            )
        else:
            row = await pool.fetchrow(
                """
                SELECT id::text                AS id,
                       company_id::text        AS company_id,
                       buildium_property_id,
                       category,
                       action_type,
                       action_config
                  FROM public.property_assignments
                 WHERE company_id = $1::uuid
                   AND buildium_property_id = $2
                   AND category = $3
                 LIMIT 1
                """,
                company_id,
                buildium_property_id,
                category,
            )
    except asyncpg.exceptions.UndefinedTableError:
        # Migration 0014 hasn't been applied yet — treat as "no
        # assignment configured" so inbound webhooks keep flowing
        # via the admin-fallback path.
        logger.warning("property_assignments table not provisioned yet; apply migration 0014.")
        return None

    if row is None:
        return None

    return PropertyAssignment(
        id=row["id"],
        company_id=row["company_id"],
        buildium_property_id=row["buildium_property_id"],
        category=row["category"],
        action_type=row["action_type"],
        action_config=_coerce_jsonb(row["action_config"]),
    )


_ADMIN_USER_CACHE: dict[str, str | None] = {}


async def resolve_admin_user_id(
    company_id: str,
    *,
    pool: asyncpg.Pool | None = None,
) -> str | None:
    """Return the admin user_id for a company, or ``None`` if none exists.

    "Admin" in v1 means the first ``company_users`` row with
    ``role IN ('owner', 'admin')`` for the company. PR #74's bootstrap
    path inserts an ``owner`` row on signup, so every company has at
    least one.

    Cached per-process — admin identity doesn't change often and the
    webhook router calls this on the hot path. The cache is keyed by
    company_id so a multi-tenant deployment doesn't leak rows.
    """
    if company_id in _ADMIN_USER_CACHE:
        return _ADMIN_USER_CACHE[company_id]

    if pool is None:
        # Lazy import — the pool helper lives in a module that
        # initializes settings; importing it at file load slows test
        # collection enough to matter.
        from helixis.db.engine import get_pool

        pool = get_pool()

    try:
        row = await pool.fetchrow(
            """
            SELECT user_id::text AS user_id
              FROM public.company_users
             WHERE company_id = $1::uuid
               AND role IN ('owner', 'admin')
             ORDER BY role = 'owner' DESC, user_id
             LIMIT 1
            """,
            company_id,
        )
    except asyncpg.exceptions.UndefinedTableError:
        logger.warning("company_users table not provisioned; can't resolve admin.")
        return None

    user_id = row["user_id"] if row else None
    _ADMIN_USER_CACHE[company_id] = user_id
    return user_id


def _clear_admin_cache_for_tests() -> None:
    """Tests only — drop the admin cache between cases."""
    _ADMIN_USER_CACHE.clear()


def _coerce_jsonb(value: Any) -> dict[str, Any]:
    """asyncpg returns JSONB as ``str`` by default unless a codec is
    registered. Coerce to dict so callers don't have to."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        import json

        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


async def list_assignments(
    pool: asyncpg.Pool,
    *,
    company_id: str,
) -> list[PropertyAssignment]:
    """Return every ``property_assignments`` row for the company.

    Used by the Properties API to render the grid in one round-trip
    instead of N (one per cell).
    """
    try:
        rows = await pool.fetch(
            """
            SELECT id::text                AS id,
                   company_id::text        AS company_id,
                   buildium_property_id,
                   category,
                   action_type,
                   action_config
              FROM public.property_assignments
             WHERE company_id = $1::uuid
            """,
            company_id,
        )
    except asyncpg.exceptions.UndefinedTableError:
        logger.warning("property_assignments table not provisioned yet; apply migration 0014.")
        return []

    return [
        PropertyAssignment(
            id=row["id"],
            company_id=row["company_id"],
            buildium_property_id=row["buildium_property_id"],
            category=row["category"],
            action_type=row["action_type"],
            action_config=_coerce_jsonb(row["action_config"]),
        )
        for row in rows
    ]


async def upsert_assignment(
    pool: asyncpg.Pool,
    *,
    company_id: str,
    buildium_property_id: int | None,
    category: str,
    action_type: str,
    action_config: dict[str, Any],
    updated_by: str | None = None,
) -> PropertyAssignment:
    """Insert or update the row for ``(company, property, category)``.

    ``buildium_property_id=None`` is the "(no property)" default row.
    Partial unique indexes on the table guarantee at most one row
    per (company, category) for the default and at most one row per
    (company, property, category) for property-specific rows.

    Returns the persisted row.
    """
    import json as _json

    config_json = _json.dumps(action_config)

    if buildium_property_id is None:
        row = await pool.fetchrow(
            """
            INSERT INTO public.property_assignments
                (company_id, buildium_property_id, category,
                 action_type, action_config, updated_by, updated_at)
            VALUES ($1::uuid, NULL, $2, $3, $4::jsonb, $5::uuid, now())
            ON CONFLICT (company_id, category)
            WHERE buildium_property_id IS NULL
            DO UPDATE SET action_type    = EXCLUDED.action_type,
                          action_config  = EXCLUDED.action_config,
                          updated_by     = EXCLUDED.updated_by,
                          updated_at     = now()
            RETURNING id::text                AS id,
                      company_id::text        AS company_id,
                      buildium_property_id,
                      category,
                      action_type,
                      action_config
            """,
            company_id,
            category,
            action_type,
            config_json,
            updated_by,
        )
    else:
        row = await pool.fetchrow(
            """
            INSERT INTO public.property_assignments
                (company_id, buildium_property_id, category,
                 action_type, action_config, updated_by, updated_at)
            VALUES ($1::uuid, $2, $3, $4, $5::jsonb, $6::uuid, now())
            ON CONFLICT (company_id, buildium_property_id, category)
            WHERE buildium_property_id IS NOT NULL
            DO UPDATE SET action_type    = EXCLUDED.action_type,
                          action_config  = EXCLUDED.action_config,
                          updated_by     = EXCLUDED.updated_by,
                          updated_at     = now()
            RETURNING id::text                AS id,
                      company_id::text        AS company_id,
                      buildium_property_id,
                      category,
                      action_type,
                      action_config
            """,
            company_id,
            buildium_property_id,
            category,
            action_type,
            config_json,
            updated_by,
        )

    # ``RETURNING`` always populates here — no ON CONFLICT DO NOTHING
    # branch — so the row guard is just for type narrowing.
    assert row is not None  # noqa: S101
    return PropertyAssignment(
        id=row["id"],
        company_id=row["company_id"],
        buildium_property_id=row["buildium_property_id"],
        category=row["category"],
        action_type=row["action_type"],
        action_config=_coerce_jsonb(row["action_config"]),
    )


async def delete_assignment(
    pool: asyncpg.Pool,
    *,
    company_id: str,
    buildium_property_id: int | None,
    category: str,
) -> bool:
    """Delete one cell's assignment. Returns True if a row was deleted.

    Brandon's UI uses this when the user picks "(Unassigned — falls
    back to admin)" — clearing a cell means deleting the row, NOT
    setting ``action_config`` to ``{}``, so the router can cleanly
    fall through to the default-row → admin chain.
    """
    if buildium_property_id is None:
        result = await pool.execute(
            """
            DELETE FROM public.property_assignments
             WHERE company_id = $1::uuid
               AND buildium_property_id IS NULL
               AND category = $2
            """,
            company_id,
            category,
        )
    else:
        result = await pool.execute(
            """
            DELETE FROM public.property_assignments
             WHERE company_id = $1::uuid
               AND buildium_property_id = $2
               AND category = $3
            """,
            company_id,
            buildium_property_id,
            category,
        )
    try:
        return int(result.split(" ")[-1]) > 0
    except (ValueError, IndexError):
        return False


__all__ = [
    "PropertyAssignment",
    "_clear_admin_cache_for_tests",
    "delete_assignment",
    "list_assignments",
    "load_property_assignment",
    "resolve_admin_user_id",
    "upsert_assignment",
]
