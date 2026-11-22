"""GET /api/v1/activity — Team Activity feed (PR #70).

Replaces the legacy demo-seeded ActivityPage with a real backend feed.
Rows come from ``helixis_activity_log``; the row's ``user_id`` joins to
``auth.users`` for the actor's display name/email so the frontend can
show "Brandon Oscar" instead of a uuid.

Scoping:
- ``scope=team`` (default) — every row for the requester's active
  company, including the requester's own rows (Brandon's call: include
  self with subtle treatment, not hide).
- ``scope=mine`` — only the requester's own rows.

Pagination is keyset over ``created_at``. The client passes the oldest
``created_at`` it has as ``cursor`` and gets the next page descending
from that timestamp (exclusive). ``next_cursor`` is the last row's
``created_at`` on the response, or ``null`` when the page is short.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import AwareDatetime, BaseModel

from helixis.api.auth import AuthenticatedUser, get_current_user
from helixis.api.rate_limit import connectors_limiter
from helixis.db.engine import get_pool

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/activity", tags=["activity"])


_LIMIT_DEFAULT = 50
_LIMIT_MAX = 100

# Category → a SQL predicate over (source, action_type). The values are
# CONSTANT, allow-listed fragments (never user input), so they're inlined
# into the WHERE clause without a bound parameter — that keeps the
# positional parameter order stable for the keyset pagination below.
# Underscores are LIKE wildcards but no action_type collides, so the plain
# prefix match is safe and readable.
_CATEGORY_PREDICATES: dict[str, str] = {
    "crm": "action_type LIKE 'crm\\_%'",  # crm_reply_sent / _call_placed / _stage_moved / …
    "buildium": "source = 'webhook'",  # mirrored Buildium events
    "assistant": "source = 'chat'",  # agent / chat-driven actions
    "team": "action_type LIKE 'member\\_%'",  # invites / removals / role changes
    "scheduled": "source = 'scheduled'",  # background cycles
}
ActivityCategory = Literal["all", "crm", "buildium", "assistant", "team", "scheduled"]


class Actor(BaseModel):
    """The user that produced the activity row. ``is_self`` lets the
    frontend prefix with "You" for the requester's own actions."""

    id: str
    name: str | None
    email: str | None
    is_self: bool


class ActivityRow(BaseModel):
    """One row in the feed. Shape mirrors ``helixis_activity_log`` plus
    the joined actor block."""

    id: str
    actor: Actor
    source: str
    source_label: str | None
    action_type: str
    description: str
    property: str | None
    tenant: str | None
    created_at: str


class ActivityPage(BaseModel):
    rows: list[ActivityRow]
    next_cursor: str | None


def _require_company(user: AuthenticatedUser) -> str:
    """Pick the active company for v1 (single-membership users)."""
    if not user.company_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No company associated with this account.",
        )
    return user.company_ids[0]


async def _fetch_actor_names(
    pool: asyncpg.Pool, user_ids: list[str]
) -> dict[str, tuple[str | None, str | None]]:
    """Join the ``user_id`` set to ``auth.users`` for display names.

    Uses a direct asyncpg fetch with the supabase service role (the
    asyncpg pool itself runs as the DB owner so it can see ``auth.users``
    — RLS only applies on Supabase REST). Falls back to email-only when
    ``raw_user_meta_data.name`` is unset.

    Returns ``{user_id: (name, email)}`` for every id that exists in
    ``auth.users``. Missing ids are absent from the map; callers default
    to ``(None, None)`` in the response so the frontend can render
    "Unknown user" rather than 500ing.
    """
    if not user_ids:
        return {}
    rows = await pool.fetch(
        """
        SELECT id::text AS id,
               email,
               (raw_user_meta_data ->> 'name')::text AS display_name
          FROM auth.users
         WHERE id::text = ANY($1)
        """,
        user_ids,
    )
    return {row["id"]: (row["display_name"], row["email"]) for row in rows}


@router.get("")
async def list_activity(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    scope: Literal["team", "mine"] = Query(default="team"),
    category: ActivityCategory = Query(default="all"),
    actor: str | None = Query(default=None),
    # ⚠ TYPED, not ``str``. This value is bound to a ``$N::timestamptz``
    # parameter below, and asyncpg REFUSES a str there — it raises
    # ``DataError: expected a datetime.date or datetime.datetime instance``,
    # which is a 500 on the live "Load more" button (Sentry
    # PYTHON-FASTAPI-3G). The round trip is exact: ``next_cursor`` below is
    # ``datetime.isoformat()``, which pydantic parses straight back.
    #
    # ⚠ AWARE, not bare ``datetime``. asyncpg's timestamptz encoder does
    # ``obj.astimezone(utc)`` (READ from pgproto/codecs/datetime.pyx, not
    # recalled), and on a NAIVE value ``astimezone()`` assumes the server's
    # local zone — so a cursor with no offset would silently move the page
    # boundary by the container's UTC offset instead of failing. Demanding
    # the offset costs nothing: every cursor we emit carries one.
    cursor: AwareDatetime | None = Query(default=None),
    limit: int = Query(default=_LIMIT_DEFAULT, ge=1, le=_LIMIT_MAX),
) -> ActivityPage:
    """Paginated feed of recent activity rows.

    ``scope=team`` returns every row for the user's active company.
    ``scope=mine`` filters to the requester's own ``user_id``.
    ``category`` narrows to one action bucket (crm / buildium / assistant /
    team / scheduled); ``all`` (default) applies no category filter.
    ``actor`` narrows to one teammate's ``user_id`` (the person filter);
    ``scope=mine`` is shorthand for ``actor=<self>``. ``cursor`` is the
    ISO8601 ``created_at`` of the oldest row on the previous page — it must
    carry a UTC offset, which every ``next_cursor`` we emit does; the
    response is strictly older than the cursor.
    """
    await connectors_limiter.check_by_user(request, user)
    company_id = _require_company(user)
    pool = get_pool()

    where_clauses = ["company_id = $1"]
    params: list[Any] = [company_id]
    # Person filter: scope=mine pins to self; otherwise an explicit actor.
    actor_filter = user.user_id if scope == "mine" else actor
    if actor_filter:
        where_clauses.append(f"user_id::text = ${len(params) + 1}")
        params.append(actor_filter)
    if category != "all":
        # Constant, allow-listed fragment — no bound parameter (keeps the
        # positional order below stable).
        where_clauses.append(_CATEGORY_PREDICATES[category])
    if cursor is not None:
        where_clauses.append(f"created_at < ${len(params) + 1}::timestamptz")
        params.append(cursor)

    sql = f"""
        SELECT id::text                AS id,
               user_id::text           AS user_id,
               source,
               source_label,
               action_type,
               description,
               property,
               tenant,
               created_at
          FROM helixis_activity_log
         WHERE {" AND ".join(where_clauses)}
         ORDER BY created_at DESC
         LIMIT ${len(params) + 1}
    """  # noqa: S608 — clause set is allow-listed above, not user input
    params.append(limit)

    try:
        rows = await pool.fetch(sql, *params)
    except asyncpg.exceptions.UndefinedTableError:
        logger.warning(
            "helixis_activity_log table not provisioned yet — returning empty page. "
            "Apply migrations 0009 + 0010."
        )
        return ActivityPage(rows=[], next_cursor=None)
    except asyncpg.exceptions.UndefinedColumnError as exc:
        logger.warning("activity query column missing: %s", exc)
        return ActivityPage(rows=[], next_cursor=None)

    if not rows:
        return ActivityPage(rows=[], next_cursor=None)

    actor_ids = sorted({r["user_id"] for r in rows if r["user_id"] is not None})
    actor_lookup = await _fetch_actor_names(pool, actor_ids)

    out_rows: list[ActivityRow] = []
    for r in rows:
        uid = r["user_id"]
        name, email = actor_lookup.get(uid, (None, None)) if uid else (None, None)
        out_rows.append(
            ActivityRow(
                id=r["id"],
                actor=Actor(
                    id=uid or "system",
                    name=name,
                    email=email,
                    is_self=bool(uid) and uid == user.user_id,
                ),
                source=r["source"] or "manual",
                source_label=r["source_label"],
                action_type=r["action_type"] or "",
                description=r["description"] or "",
                property=r["property"],
                tenant=r["tenant"],
                created_at=r["created_at"].isoformat() if r["created_at"] else "",
            )
        )

    # Next cursor is the oldest row's created_at — exclusive, so the
    # caller passes it back without overlap on the next page.
    next_cursor: str | None = None
    if len(out_rows) == limit:
        next_cursor = out_rows[-1].created_at

    return ActivityPage(rows=out_rows, next_cursor=next_cursor)


@router.get("/actors")
async def list_activity_actors(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[Actor]:
    """Distinct people who have produced activity for the company — powers
    the person filter dropdown so it only lists real participants (not the
    whole member roster). Sorted by display name; the requester is flagged
    ``is_self`` so the frontend can float "You" to the top.

    Best-effort: a missing table (fresh DB) yields an empty list rather
    than a 500, mirroring the feed endpoint.
    """
    await connectors_limiter.check_by_user(request, user)
    company_id = _require_company(user)
    pool = get_pool()

    try:
        rows = await pool.fetch(
            """
            SELECT DISTINCT user_id::text AS user_id
              FROM helixis_activity_log
             WHERE company_id = $1
               AND user_id IS NOT NULL
            """,
            company_id,
        )
    except (
        asyncpg.exceptions.UndefinedTableError,
        asyncpg.exceptions.UndefinedColumnError,
    ) as exc:
        logger.warning("activity actors query failed (table/column missing): %s", exc)
        return []

    ids = [r["user_id"] for r in rows if r["user_id"]]
    if not ids:
        return []

    lookup = await _fetch_actor_names(pool, ids)
    actors = [
        Actor(
            id=uid,
            name=lookup.get(uid, (None, None))[0],
            email=lookup.get(uid, (None, None))[1],
            is_self=uid == user.user_id,
        )
        for uid in ids
    ]
    # Stable order: name, then email, then id — self floats via the flag,
    # not the sort, so the roster stays alphabetical.
    actors.sort(key=lambda a: (a.name or a.email or a.id).lower())
    return actors


__all__ = ["router"]
