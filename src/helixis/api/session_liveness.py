"""Has the device this token rides on been signed out?

A Supabase access token is a stateless JWT — a signature and an ``exp``, and
nothing that can be taken back. So ``DELETE /auth/sessions/{id}`` ("sign this
device out") ends the ability to REFRESH and does nothing at all to the token
already sitting in that browser: it keeps full API access until it expires on
its own.

**Measured, not assumed** (helixis-test, 2026-09-09). The tightest gap between
consecutive rows in ``auth.refresh_tokens`` for one session is **3,481s** — a
client refreshing just before the boundary — so the access-token lifetime is
Supabase's default hour, and the revocation window was up to that.

The lost-laptop case is the entire reason that endpoint exists, and it was the
one case it could not serve. Both the route's own comment and
``frontend/src/services/sessions.ts`` said revoking "genuinely ends that
session rather than hiding it from a list"; that was true of the refresh path
and false of the token already in hand — gotcha 48's shape, a comment that
cannot fail and so went on being believed. This module is what makes the
sentence true, and deleting it puts the hour back without failing anything.

⚠ **No cache, deliberately.** A TTL cache is the obvious optimisation and it
re-opens a smaller copy of the very window this closes — one minute instead of
one hour — plus a cross-process invalidation problem the moment the web
service runs more than one worker. The saving is not real either:
``get_current_user`` already spends a full HTTPS round trip to Supabase REST
inside ``_resolve_company_ids`` on every request, so a primary-key lookup on a
pool the request is already holding is cheaper than the call it sits beside.

⚠ **Fails CLOSED since 2026-11-11** (it failed open before). ``session_revoked``
answers True when the row was POSITIVELY established to be gone, False when it
was positively found, and RAISES :class:`SessionCheckUnavailableError` when it
could not find out: no pool, or a query that raised or timed out. Both callers
in ``api/auth.py`` turn that into a 503 ("try again"), never a 401 and never a
pass, and the books' verifier lets it propagate, which the books answer as
"try again" too, so an outage never counts as a wrong password.

Why the change (the founder's call, 2026-11-11): failing open meant that
anyone who could make the lookup fail kept a signed-out device's token working,
the exact window this module closes. Failing closed costs availability while
the database is away: every signed-in request answers 503 until it is back. It
is a 503 and not a sign-out, so the web app keeps its session, says "try again
in a moment", and works again the moment the lookup does.

Two inputs still answer without the database, on purpose:

- **No ``session_id`` claim → not revoked.** Unchanged. Every token GoTrue
  issues carries one, so its absence can only come from something holding the
  project's signing key, which can already mint a token for anyone; refusing
  it would add no protection and would refuse every hand-minted test token.
- **A ``session_id`` or user id that is not a uuid → revoked.** ``auth.sessions``
  keys are uuids, so no live row can match; this used to reach the database,
  fail the cast and fail open. A 401 is the honest answer, and it is final,
  where a 503 would ask the client to retry something that can never succeed.
"""

from __future__ import annotations

import logging
from uuid import UUID

from helixis.db.engine import get_pool

logger = logging.getLogger(__name__)


class SessionCheckUnavailableError(RuntimeError):
    """Whether this device was signed out could not be found out: there is no
    database pool, or the lookup raised or timed out. Never a revocation and
    never a pass; the caller answers 503."""


def _is_uuid(value: str) -> bool:
    try:
        UUID(value)
    except (TypeError, ValueError, AttributeError):
        return False
    return True


async def session_revoked(session_id: str | None, user_id: str) -> bool:
    """True when ``auth.sessions`` positively has no such live row; False when
    it has one. Raises :class:`SessionCheckUnavailableError` when it can't tell.

    ``user_id`` is in the WHERE clause rather than compared afterwards, so a
    token whose ``session_id`` names a row belonging to somebody ELSE reads as
    revoked and is refused. That pairing can only come from a mismatch we do
    not understand, and serving it would be the one direction with no honest
    reading.
    """
    if not session_id:
        # Not a documented-guaranteed claim (see ``AuthenticatedUser``), and
        # every token GoTrue issues carries one. A token without one could only
        # come from a holder of the signing key, who can mint any token anyway,
        # so this stays as it shipped: unrevokable. It also keeps the existing
        # unit suite, whose hand-minted tokens carry no such claim, off the
        # database, which is why the dedicated tests exist at all.
        return False

    if not (_is_uuid(session_id) and _is_uuid(user_id)):
        # ``auth.sessions`` keys are uuids: no live row can carry this pair.
        return True

    try:
        pool = get_pool()
    except RuntimeError as exc:  # get_pool raises when init_pool never ran
        raise SessionCheckUnavailableError("no database pool") from exc
    if pool is None:
        raise SessionCheckUnavailableError("no database pool")

    try:
        async with pool.acquire() as conn:
            live = await conn.fetchval(
                """
                select 1
                  from auth.sessions
                 where id = $1::uuid
                   and user_id = $2::uuid
                """,
                session_id,
                user_id,
            )
    except Exception as exc:
        # Fail closed and say so out loud: a revocation check nobody can see
        # failing is the same silent degradation as gotcha 17's embedder.
        logger.warning(
            "Could not check session liveness for user=%s — refusing the request "
            "with a 503 until the database answers.",
            user_id,
            exc_info=True,
        )
        raise SessionCheckUnavailableError("the session lookup failed") from exc

    return live is None


__all__ = ["SessionCheckUnavailableError", "session_revoked"]
