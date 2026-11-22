"""Signing a device out has to stop the token that device is holding.

WHAT BROKE
----------
`DELETE /auth/sessions/{id}` deleted the GoTrue row, which ends the ability to
REFRESH — and did nothing to the access token already loaded in that browser.
A Supabase access token is a stateless JWT: signature plus `exp`, nothing
revocable. Measured on helixis-test 2026-09-09, the tightest gap between
consecutive `auth.refresh_tokens` rows for one session is **3,481s**, so the
lifetime is Supabase's default hour and a signed-out laptop kept full API
access for up to that long.

The route's own comment and `frontend/src/services/sessions.ts` both said
revoking "genuinely ends that session rather than hiding it from a list".
True of refresh, false of the token in hand — gotcha 48, a comment that cannot
fail. `api/session_liveness.py` is what makes it true.

WHY THIS FILE IS THE ONLY COVERAGE
----------------------------------
⚠ **No existing test can see this check, and none ever will.** The predicate
skips a token carrying no `session_id` claim, and an AST sweep of every
`jwt.encode(...)` in `tests/` finds **zero** hand-minted tokens that carry
one. So the whole suite is a no-op against the new path: convenient (nothing
broke) and exactly the state gotcha 35 warns about — a case that never runs is
not a passing case. Everything here mints the claim deliberately.

⚠ **The call-site walks are not decoration.** A predicate nothing calls is the
`comms_triage` failure (gotcha 62), repeated at gotchas 66 and 69. The
behavioural tests below drive `get_current_user`, which covers ONE caller;
`get_ws_user` is the other and a behavioural test of the first says nothing
about it.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from helixis.api import auth as auth_mod
from helixis.api.session_liveness import SessionCheckUnavailableError, session_revoked

USER = "11111111-1111-1111-1111-111111111111"
SESSION = "22222222-2222-2222-2222-222222222222"

_AUTH_SRC = Path("src/helixis/api/auth.py")


def _pool(fetchval_result: object = 1, *, raises: Exception | None = None) -> MagicMock:
    """A pool shaped the way asyncpg's actually is.

    ⚠ `MagicMock().acquire().__aenter__()` already yields an AsyncMock, so
    every method on the connection is awaitable — reach into that tree and
    override only `fetchval`, never hand-build the connection (gotcha 69).
    """
    pool = MagicMock()
    conn = pool.acquire.return_value.__aenter__.return_value
    if raises is not None:
        conn.fetchval = AsyncMock(side_effect=raises)
    else:
        conn.fetchval = AsyncMock(return_value=fetchval_result)
    return pool


# ── the predicate ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_deleted_session_row_reads_as_revoked() -> None:
    with patch("helixis.api.session_liveness.get_pool", return_value=_pool(None)):
        assert await session_revoked(SESSION, USER) is True


@pytest.mark.asyncio
async def test_a_live_session_row_is_not_revoked() -> None:
    with patch("helixis.api.session_liveness.get_pool", return_value=_pool(1)):
        assert await session_revoked(SESSION, USER) is False


@pytest.mark.asyncio
async def test_the_lookup_is_scoped_to_the_calling_user_too() -> None:
    """`user_id` is in the WHERE clause, not compared afterwards.

    A token whose `session_id` names somebody ELSE's row has no honest
    reading, and the direction that serves it is the one with no defence.
    """
    pool = _pool(1)
    with patch("helixis.api.session_liveness.get_pool", return_value=pool):
        await session_revoked(SESSION, USER)
    conn = pool.acquire.return_value.__aenter__.return_value
    sql, *args = conn.fetchval.await_args.args
    assert "auth.sessions" in sql
    assert "user_id" in sql, (
        "the user predicate is the difference between checking THIS user's "
        "session and checking whether any session with that id exists"
    )
    assert args == [SESSION, USER]


@pytest.mark.asyncio
async def test_a_token_with_no_session_claim_is_allowed_without_touching_the_db() -> None:
    """`session_id` is not a documented-guaranteed claim, so absence has to
    degrade to today's behaviour — and it must not cost a query, which is what
    keeps the existing suite both green and fast."""
    pool = _pool(None)
    with patch("helixis.api.session_liveness.get_pool", return_value=pool) as get_pool:
        assert await session_revoked(None, USER) is False
        assert await session_revoked("", USER) is False
    assert get_pool.call_count == 0


@pytest.mark.asyncio
async def test_no_pool_fails_closed() -> None:
    """Re-pointed 2026-11-11 from ``test_no_pool_fails_open`` (the founder's
    call: the check fails closed). Not knowing is never "not revoked"."""
    with patch("helixis.api.session_liveness.get_pool", return_value=None):
        with pytest.raises(SessionCheckUnavailableError):
            await session_revoked(SESSION, USER)


@pytest.mark.asyncio
async def test_an_uninitialised_pool_fails_closed() -> None:
    """The REAL ``get_pool`` never returns None: it raises RuntimeError until
    ``init_pool`` ran. So the None branch above was the only "no pool" a test
    reached, while the case that happens raised straight out of the check (a
    500). ``get_pool`` itself is left real; only the global it reads is emptied."""
    with patch("helixis.db.engine._pool", None):
        with pytest.raises(SessionCheckUnavailableError):
            await session_revoked(SESSION, USER)


@pytest.mark.asyncio
async def test_a_failing_query_fails_closed_and_says_so(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Re-pointed 2026-11-11 from ``test_a_failing_query_fails_open_and_says_so``.
    Failing open let anyone who could break the lookup keep a signed-out
    device's token; it now raises, which both callers answer with a 503. A
    check that has stopped working is gotcha 17's class, so it still warns."""
    with (
        patch(
            "helixis.api.session_liveness.get_pool",
            return_value=_pool(raises=RuntimeError("pool exploded")),
        ),
        caplog.at_level(logging.WARNING, logger="helixis.api.session_liveness"),
    ):
        with pytest.raises(SessionCheckUnavailableError):
            await session_revoked(SESSION, USER)
    assert any("session liveness" in r.getMessage() for r in caplog.records), (
        "a check that stopped working has to be visible; silence here is "
        "indistinguishable from the check passing"
    )


@pytest.mark.asyncio
async def test_a_lookup_that_times_out_fails_closed() -> None:
    """``acquire()`` is bounded by default (db/engine.py); its TimeoutError is
    an outage like any other."""
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(side_effect=TimeoutError())
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    with patch("helixis.api.session_liveness.get_pool", return_value=pool):
        with pytest.raises(SessionCheckUnavailableError):
            await session_revoked(SESSION, USER)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("session_id", "user_id"),
    [("not-a-uuid", USER), (SESSION, "user-123"), ("' or 1=1 --", USER)],
)
async def test_an_id_that_is_not_a_uuid_is_revoked_without_a_query(
    session_id: str, user_id: str
) -> None:
    """Re-pointed 2026-11-11 from ``test_a_session_id_that_is_not_a_uuid_fails_open``.
    That shape reached the database, failed the ``::uuid`` cast and failed
    open. ``auth.sessions`` keys are uuids, so no live row can match: the
    honest answer is "revoked" (a final 401), not a 503 asking the client to
    retry something that can never succeed. And it costs no query."""
    pool = _pool(1)
    with patch("helixis.api.session_liveness.get_pool", return_value=pool) as get_pool:
        assert await session_revoked(session_id, user_id) is True
    assert get_pool.call_count == 0


# ── the HTTP caller ──────────────────────────────────────────────────


def _request(token: str = "tok") -> MagicMock:
    req = MagicMock()
    req.headers = {"Authorization": f"Bearer {token}"}
    return req


@pytest.mark.asyncio
async def test_get_current_user_refuses_a_revoked_device() -> None:
    claims = {"sub": USER, "email": "a@b.com", "role": "authenticated", "session_id": SESSION}
    with (
        patch.object(auth_mod, "_decode_jwt", return_value=claims),
        patch.object(auth_mod, "session_revoked", AsyncMock(return_value=True)),
        patch.object(auth_mod, "_resolve_company_ids", AsyncMock(return_value=["c"])) as resolve,
    ):
        with pytest.raises(HTTPException) as exc:
            await auth_mod.get_current_user(_request())
    assert exc.value.status_code == 401
    assert "signed out" in exc.value.detail.lower()
    assert resolve.await_count == 0, (
        "the check runs BEFORE _resolve_company_ids, which is a full HTTPS "
        "round trip — a signed-out token should not spend it"
    )


@pytest.mark.asyncio
async def test_get_current_user_serves_a_live_device() -> None:
    claims = {"sub": USER, "email": "a@b.com", "role": "authenticated", "session_id": SESSION}
    with (
        patch.object(auth_mod, "_decode_jwt", return_value=claims),
        patch.object(auth_mod, "session_revoked", AsyncMock(return_value=False)),
        patch.object(auth_mod, "_resolve_company_ids", AsyncMock(return_value=["c"])),
    ):
        user = await auth_mod.get_current_user(_request())
    assert user.user_id == USER
    assert user.session_id == SESSION


@pytest.mark.asyncio
async def test_get_current_user_passes_the_claim_and_the_subject() -> None:
    """Both arguments, in that order. Passing the wrong subject would check a
    session against a user it does not belong to and refuse everybody."""
    claims = {"sub": USER, "role": "authenticated", "session_id": SESSION}
    spy = AsyncMock(return_value=False)
    with (
        patch.object(auth_mod, "_decode_jwt", return_value=claims),
        patch.object(auth_mod, "session_revoked", spy),
        patch.object(auth_mod, "_resolve_company_ids", AsyncMock(return_value=[])),
    ):
        await auth_mod.get_current_user(_request())
    assert spy.await_args.args == (SESSION, USER)


@pytest.mark.asyncio
async def test_get_current_user_answers_503_when_the_check_cannot_run() -> None:
    """Not a 401: the web app reads a 401 as a bad sign-in, and an outage is
    not one. Not a pass either. And the company lookup is not spent."""
    claims = {"sub": USER, "email": "a@b.com", "role": "authenticated", "session_id": SESSION}
    with (
        patch.object(auth_mod, "_decode_jwt", return_value=claims),
        patch.object(
            auth_mod,
            "session_revoked",
            AsyncMock(side_effect=SessionCheckUnavailableError("the session lookup failed")),
        ),
        patch.object(auth_mod, "_resolve_company_ids", AsyncMock(return_value=["c"])) as resolve,
    ):
        with pytest.raises(HTTPException) as exc:
            await auth_mod.get_current_user(_request())
    assert exc.value.status_code == 503
    assert exc.value.headers == {"Retry-After": "5"}
    assert "try again" in exc.value.detail.lower()
    assert resolve.await_count == 0


@pytest.mark.asyncio
async def test_a_request_through_the_app_gets_the_503() -> None:
    """End to end through a route's dependency, with the real predicate and a
    pool whose query fails: what a browser would receive."""
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()

    @app.get("/who")
    async def who(user: auth_mod.AuthenticatedUser = Depends(auth_mod.get_current_user)) -> dict:
        return {"user": user.user_id}

    claims = {"sub": USER, "role": "authenticated", "session_id": SESSION}
    with (
        patch.object(auth_mod, "_decode_jwt", return_value=claims),
        patch(
            "helixis.api.session_liveness.get_pool",
            return_value=_pool(raises=ConnectionError("database went away")),
        ),
        patch.object(auth_mod, "_resolve_company_ids", AsyncMock(return_value=[])),
    ):
        resp = TestClient(app).get("/who", headers={"Authorization": "Bearer tok"})
    assert resp.status_code == 503
    assert resp.headers["retry-after"] == "5"


# ── the websocket caller ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_ws_user_refuses_a_revoked_device_and_closes_the_socket() -> None:
    """A socket outlives a request, so the connection a signed-out device
    keeps is the one that would otherwise stay open longest."""
    claims = {"sub": USER, "role": "authenticated", "session_id": SESSION}
    ws = MagicMock()
    ws.close = AsyncMock()
    with (
        patch.object(auth_mod, "_decode_jwt", return_value=claims),
        patch.object(auth_mod, "session_revoked", AsyncMock(return_value=True)),
        patch.object(auth_mod, "_resolve_company_ids", AsyncMock(return_value=["c"])) as resolve,
    ):
        with pytest.raises(HTTPException) as exc:
            await auth_mod.get_ws_user(ws, token="tok")
    assert exc.value.status_code == 401
    ws.close.assert_awaited_once()
    assert resolve.await_count == 0


@pytest.mark.asyncio
async def test_get_ws_user_closes_with_try_again_when_the_check_cannot_run() -> None:
    """1013 is the standard "try again later" close, distinct from the 4001
    a signed-out device gets."""
    claims = {"sub": USER, "role": "authenticated", "session_id": SESSION}
    ws = MagicMock()
    ws.close = AsyncMock()
    with (
        patch.object(auth_mod, "_decode_jwt", return_value=claims),
        patch.object(
            auth_mod,
            "session_revoked",
            AsyncMock(side_effect=SessionCheckUnavailableError("no database pool")),
        ),
        patch.object(auth_mod, "_resolve_company_ids", AsyncMock(return_value=["c"])) as resolve,
    ):
        with pytest.raises(HTTPException) as exc:
            await auth_mod.get_ws_user(ws, token="tok")
    assert exc.value.status_code == 503
    ws.close.assert_awaited_once()
    assert ws.close.await_args.kwargs["code"] == 1013
    assert resolve.await_count == 0


# ── the wiring (a behavioural test cannot see a caller that stops asking) ──


def _awaits_session_revoked(fn: ast.AsyncFunctionDef) -> bool:
    return any(
        isinstance(n, ast.Call) and getattr(n.func, "id", None) == "session_revoked"
        for n in ast.walk(fn)
    )


@pytest.mark.parametrize("name", ["get_current_user", "get_ws_user"])
def test_both_entry_points_still_ask(name: str) -> None:
    """⚠ AST, not a substring scan: this module's own prose names the function
    in order to explain it, and a comment is never a Call node (gotcha 40).
    """
    tree = ast.parse(_AUTH_SRC.read_text(encoding="utf-8"))
    fns = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == name  # type: ignore[attr-defined]
    ]
    assert len(fns) == 1, f"{name} is not in {_AUTH_SRC} under that name any more"
    assert _awaits_session_revoked(fns[0]), (
        f"{name} no longer calls session_revoked, so a revoked device is "
        "served again — the hour-long window is back and nothing else fails"
    )


def _handles_unavailable(fn: ast.AsyncFunctionDef) -> bool:
    """An ``except SessionCheckUnavailableError`` whose body raises, inside ``fn``."""
    for node in ast.walk(fn):
        if not isinstance(node, ast.ExceptHandler) or node.type is None:
            continue
        names = node.type.elts if isinstance(node.type, ast.Tuple) else [node.type]
        if any(getattr(n, "id", None) == "SessionCheckUnavailableError" for n in names) and any(
            isinstance(b, ast.Raise) for b in ast.walk(node)
        ):
            return True
    return False


@pytest.mark.parametrize("name", ["get_current_user", "get_ws_user"])
def test_both_entry_points_turn_an_unchecked_session_into_a_refusal(name: str) -> None:
    """Gotcha 25: the 503 has to reach every caller, not only the one the
    behavioural tests drive. Without the handler the error escapes as a 500."""
    tree = ast.parse(_AUTH_SRC.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == name)
    assert _handles_unavailable(fn), f"{name} no longer answers an unchecked session with a 503"


def test_the_handler_guard_can_say_no() -> None:
    tree = ast.parse(
        "async def get_current_user(r):\n"
        "    try:\n        x = 1\n    except SessionCheckUnavailableError:\n        pass\n"
    )
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef))
    assert _handles_unavailable(fn) is False


def test_the_wiring_guard_can_say_no() -> None:
    """Proof it can return False, not just True. A walk that matched nothing
    would pass against every caller including one that dropped the check
    (gotcha 45)."""
    tree = ast.parse("async def get_current_user(r):\n    return 1\n")
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef))
    assert _awaits_session_revoked(fn) is False


def test_no_test_token_carries_a_session_claim() -> None:
    """The premise this file's own existence rests on, checked rather than
    assumed. The day a shared fixture starts minting `session_id`, every test
    using it begins hitting the real predicate — and would fail confusingly
    far from here unless that fixture also stands up a live session row.
    """
    offenders: list[str] = []
    for path in sorted(Path("tests").rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - a broken test file fails elsewhere
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "attr", None) == "encode"
                and node.args
                and "session_id" in ast.unparse(node.args[0])
            ):
                offenders.append(f"{path}:{node.lineno}")
    assert not offenders, (
        "a hand-minted JWT now carries a session_id claim, so session_revoked "
        f"will run for real in those tests: {offenders}. Either drop the claim "
        "or patch helixis.api.session_liveness.get_pool in that fixture."
    )
