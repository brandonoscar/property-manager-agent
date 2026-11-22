"""Auth self-heal endpoint (PR #74 Bug 3).

Background. The ``public.handle_new_user`` trigger
(migration ``0012_fix_handle_new_user_trigger.sql``) auto-creates a
personal company + ``company_users`` row on every ``auth.users``
insert. Users who signed up while the trigger was broken (pre-0012)
or in any signup-time race condition end up without a
``company_users`` row, and the frontend's ``fetchCompanyIds`` returns
``[]``, which lands the chat at the ``"No company selected."`` toast.

This endpoint is the self-heal path: idempotent upsert of the
personal-company + membership pair, scoped strictly to the calling
user via ``Depends(get_current_user)`` (never accepts a ``user_id``
in the request body). Uses ``INSERT ... ON CONFLICT DO NOTHING`` to
match the trigger's exact semantics — running this against a user
who already has a row is a no-op.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from helixis.api.auth import AuthenticatedUser, get_current_user
from helixis.api.device_label import describe_device
from helixis.db.engine import get_pool

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


class BootstrapResponse(BaseModel):
    """Always returns the user-as-company-id, which is the canonical
    personal-company id the ``handle_new_user`` trigger uses."""

    company_id: str


@router.post("/bootstrap", response_model=BootstrapResponse)
async def bootstrap_company(
    user: AuthenticatedUser = Depends(get_current_user),
) -> BootstrapResponse:
    """Idempotently ensure the calling user has a paired companies row
    + company_users row.

    Frontend self-heal path: ``ensureCompanyProvisioned()`` calls this
    when ``fetchCompanyIds`` returns empty for an otherwise-valid auth
    session. Always returns ``user_id`` as the canonical personal-
    company id (matches the trigger's ``NEW.id``-as-companies.id
    convention from migration 0012).
    """
    pool = get_pool()
    if pool is None:
        # Tests / scripts without a pool: 503 is the truthful answer.
        # Production always has a pool by the time the API serves a
        # request.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database pool not initialised.",
        )

    company_id = user.user_id
    display_name = (user.email or "").split("@")[0] or "Personal Workspace"

    try:
        async with pool.acquire() as conn:
            # Two upserts mirror the trigger's body verbatim so the
            # self-heal and the signup-time path produce byte-identical
            # rows. ON CONFLICT DO NOTHING makes this safe to call on
            # every cold-start of the chat surface.
            await conn.execute(
                "INSERT INTO public.companies (id, name) VALUES ($1::uuid, $2) "
                "ON CONFLICT (id) DO NOTHING",
                company_id,
                display_name,
            )
            # 'admin' — the reconciled top role (ADR 0005 / migration 0034).
            # The legacy 'owner' now violates company_users_role_check, which
            # 500'd every fresh-user bootstrap after 0034 landed.
            await conn.execute(
                "INSERT INTO public.company_users (user_id, company_id, role) "
                "VALUES ($1::uuid, $2::uuid, 'admin') "
                "ON CONFLICT (user_id, company_id) DO NOTHING",
                user.user_id,
                company_id,
            )
    except Exception:
        # Raise a handled 500 instead of letting the exception escape the
        # middleware stack — an unhandled error skips CORSMiddleware, so
        # browser clients (the onboarding wizard) see an opaque "CORS
        # blocked" failure instead of the real one.
        logger.exception("Bootstrap upsert failed for user=%s", user.user_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Workspace bootstrap failed — see server logs.",
        )

    logger.info(
        "Bootstrap upsert ran for user=%s — company_id=%s",
        user.user_id,
        company_id,
    )
    return BootstrapResponse(company_id=company_id)


# ── Active sessions ──────────────────────────────────────────────────
#
# "Which devices am I signed in on, and how do I kick one off?" — the
# question a manager asks after losing a laptop, and one the product had no
# answer to. `signOutEverywhere` is the blunt instrument; this is the scalpel.
#
# ⚠ Reads `auth.sessions` DIRECTLY. GoTrue publishes no list-my-sessions API,
# and that table is the same one it reads to decide whether a refresh token is
# still good — so deleting a row here genuinely ends that session rather than
# merely hiding it from a list.
#
# ⚠ THAT SENTENCE WAS HALF TRUE UNTIL 2026-09-09, and the half it missed is
# the case this endpoint exists for. Deleting the row ends REFRESH; the access
# token already loaded in that browser is a stateless JWT and kept full API
# access until its own `exp` — measured at Supabase's default hour (the
# tightest observed gap in `auth.refresh_tokens` is 3,481s). So "sign my lost
# laptop out" left the laptop working for up to an hour. `api/session_
# liveness.py` is what closes it: `get_current_user` asks whether this row
# still exists on every request. Delete that check and the hour comes back
# with nothing failing to say so.
# Measured on helixis-test 2026-09-08 before
# building anything (gotcha 53 — check POPULATION, not presence, 58 rows /
# 10 users):
#
#     user_agent   58/58     ip           58/58
#     created_at   58/58     updated_at   58/58     aal  58/58
#     refreshed_at  4/58  ← so "last active" reads updated_at, never this
#     not_after     0/58  ← NEVER set, so no expiry is rendered at all
#
# Rendering an expiry off `not_after` would print "expires: never" for every
# session on the account — a confident claim about a column GoTrue does not
# populate (gotcha 55).


class ActiveSession(BaseModel):
    """One row of the sessions list, already humanised for display."""

    id: str
    device: str
    #: Truthfully the last time GoTrue TOUCHED the row, which is the closest
    #: thing to "last active" that exists here. Named for what it is.
    last_seen: str
    signed_in_at: str
    #: Whether this row is the browser making the request. `None` means the
    #: token carried no `session_id` claim, so the honest answer is "cannot
    #: tell" — never `False`, which would read as "some OTHER device" and
    #: invite somebody to revoke the session they are sitting in.
    is_current: bool | None = None


class ActiveSessionsResponse(BaseModel):
    sessions: list[ActiveSession]
    #: Surfaced so the client can say "we can't highlight this device" rather
    #: than silently rendering a list with nothing marked, which looks broken.
    current_identified: bool


@router.get("/sessions", response_model=ActiveSessionsResponse)
async def list_sessions(
    user: AuthenticatedUser = Depends(get_current_user),
) -> ActiveSessionsResponse:
    """Every live session for the CALLING user, newest activity first."""
    pool = get_pool()
    if pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database pool not initialised.",
        )

    try:
        async with pool.acquire() as conn:
            # Scoped to the caller's own user_id — never a parameter, so no
            # request can ask about somebody else's devices. (auth.sessions is
            # not a `helixis_*` table and carries no company_id; the tenant
            # boundary here is the auth user, gotcha 47.)
            rows = await conn.fetch(
                """
                select id::text as id,
                       user_agent,
                       created_at,
                       updated_at
                  from auth.sessions
                 where user_id = $1::uuid
                 order by updated_at desc nulls last
                """,
                user.user_id,
            )
    except Exception:
        logger.exception("Session list failed for user=%s", user.user_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Couldn't read your active sessions — see server logs.",
        )

    known = user.session_id
    return ActiveSessionsResponse(
        current_identified=known is not None,
        sessions=[
            ActiveSession(
                id=row["id"],
                device=describe_device(row["user_agent"]),
                last_seen=(row["updated_at"] or row["created_at"]).isoformat(),
                signed_in_at=row["created_at"].isoformat(),
                is_current=(row["id"] == known) if known else None,
            )
            for row in rows
        ],
    )


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_session(
    session_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> None:
    """End ONE session. Signing out a device you no longer hold.

    ⚠ The `user_id` predicate is the whole security of this endpoint. Without
    it a caller could pass any session id and sign out any user on the
    deployment — so it is in the WHERE clause, not checked in Python
    afterwards, and a row belonging to somebody else simply does not match.
    A 404 for both "no such session" and "not yours" is deliberate: telling an
    attacker which session ids exist is a free enumeration oracle.
    """
    pool = get_pool()
    if pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database pool not initialised.",
        )

    try:
        async with pool.acquire() as conn:
            deleted = await conn.fetchval(
                """
                delete from auth.sessions
                 where id = $1::uuid
                   and user_id = $2::uuid
                returning id::text
                """,
                session_id,
                user.user_id,
            )
    except Exception:
        logger.exception("Session revoke failed for user=%s session=%s", user.user_id, session_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Couldn't end that session — see server logs.",
        )

    if deleted is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No such session.",
        )

    logger.info("Session revoked for user=%s", user.user_id)


__all__ = ["router"]
