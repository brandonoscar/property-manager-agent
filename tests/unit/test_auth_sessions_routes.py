"""The sessions list and the per-device revoke.

⚠ NOT EXECUTED LOCALLY. This container's Python crypto stack is broken
(`_cffi_backend` missing; `cryptography`'s pyo3 bindings panic on import), so
`fastapi`/`pyjwt` cannot load here and nothing in this file ran before it was
committed. CI is what proves it. Said plainly rather than implied, because
gotcha 23's rule is that a test nobody has executed can be born broken and
stay red forever without anyone noticing — the honest state is "written,
unrun", not "passing".

`device_label`'s own tests DID run (19 passed, 2 ordering mutations caught) —
it is stdlib-only.

What is worth pinning here is the SECURITY shape, not the happy path:

  · the list is scoped to the caller and cannot be asked about anyone else;
  · the revoke's `user_id` predicate lives in the WHERE clause, so another
    user's session id simply does not match — a Python-side check after the
    fact would be one refactor away from being dropped;
  · a session that is not yours is a 404, identical to one that does not
    exist, so the endpoint is not an id-enumeration oracle;
  · `is_current` is NULL — never False — when the token carries no
    `session_id` claim, because False reads as "some other device" and invites
    somebody to revoke the session they are sitting in.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from helixis.api.auth import AuthenticatedUser
from helixis.api.routes.auth import list_sessions, revoke_session

USER = "11111111-1111-1111-1111-111111111111"
OTHER_USER = "22222222-2222-2222-2222-222222222222"
SESSION_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
SESSION_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

CHROME_MAC = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


def _now() -> datetime:
    return datetime(2026, 9, 8, 12, 0, tzinfo=UTC)


def _pool(*, fetch_rows: list[dict[str, Any]] | None = None, deleted: Any = None) -> Any:
    """A pool whose CONNECTION answers, matching the routes' own access shape.

    ⚠ Gotcha 73: these routes use `pool.acquire()` + `conn.fetch` /
    `conn.fetchval`. A fake that only models `pool.fetch` would raise inside
    the route and every test here would fail for a reason unrelated to what
    it is checking.
    """
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=fetch_rows or [])
    conn.fetchval = AsyncMock(return_value=deleted)

    pool = MagicMock()

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool.acquire = _acquire
    pool.conn = conn  # so a test can assert on the SQL and its arguments
    return pool


def _user(session_id: str | None = SESSION_A) -> AuthenticatedUser:
    return AuthenticatedUser(user_id=USER, email="a@b.com", session_id=session_id)


@pytest.fixture()
def pooled(monkeypatch: pytest.MonkeyPatch):
    def _install(pool: Any) -> Any:
        monkeypatch.setattr("helixis.api.routes.auth.get_pool", lambda: pool)
        return pool

    return _install


@pytest.mark.asyncio
async def test_the_list_is_scoped_to_the_caller_in_sql(pooled) -> None:
    pool = pooled(_pool(fetch_rows=[]))
    await list_sessions(user=_user())

    sql, *args = pool.conn.fetch.call_args.args
    assert "auth.sessions" in sql
    assert "user_id = $1" in sql, "the scope must be IN the query, not applied after"
    assert args == [USER]


@pytest.mark.asyncio
async def test_rows_are_humanised_and_the_current_one_is_marked(pooled) -> None:
    pooled(
        _pool(
            fetch_rows=[
                {
                    "id": SESSION_A,
                    "user_agent": CHROME_MAC,
                    "created_at": _now(),
                    "updated_at": _now(),
                },
                {
                    "id": SESSION_B,
                    "user_agent": None,
                    "created_at": _now(),
                    "updated_at": None,
                },
            ]
        )
    )
    result = await list_sessions(user=_user(session_id=SESSION_A))

    assert result.current_identified is True
    assert result.sessions[0].device == "Chrome on macOS"
    assert result.sessions[0].is_current is True
    # A missing user agent is named, not blank.
    assert result.sessions[1].device == "Unknown device"
    assert result.sessions[1].is_current is False


@pytest.mark.asyncio
async def test_last_seen_falls_back_to_created_when_updated_is_null(pooled) -> None:
    """Measured on helixis-test: `refreshed_at` is populated on 4 of 58 rows
    and `not_after` on 0 of 58, so neither can carry this. `updated_at` is
    58/58 — but a NULL must still render a real time rather than crash."""
    pooled(
        _pool(
            fetch_rows=[
                {
                    "id": SESSION_A,
                    "user_agent": CHROME_MAC,
                    "created_at": _now(),
                    "updated_at": None,
                }
            ]
        )
    )
    result = await list_sessions(user=_user())
    assert result.sessions[0].last_seen == _now().isoformat()


@pytest.mark.asyncio
async def test_no_session_claim_reports_unknown_not_false(pooled) -> None:
    """The direction that matters. False would label the session the user is
    USING as somebody else's, which is an invitation to sign themselves out
    while trying to be careful."""
    pooled(
        _pool(
            fetch_rows=[
                {
                    "id": SESSION_A,
                    "user_agent": CHROME_MAC,
                    "created_at": _now(),
                    "updated_at": _now(),
                }
            ]
        )
    )
    result = await list_sessions(user=_user(session_id=None))

    assert result.current_identified is False
    assert result.sessions[0].is_current is None
    assert result.sessions[0].is_current is not False


@pytest.mark.asyncio
async def test_revoke_scopes_to_the_caller_in_sql(pooled) -> None:
    """The whole security of the endpoint. Without the `user_id` predicate a
    caller could pass any session id and sign out any user on the deployment."""
    pool = pooled(_pool(deleted=SESSION_B))
    await revoke_session(session_id=SESSION_B, user=_user())

    sql, *args = pool.conn.fetchval.call_args.args
    assert "delete from auth.sessions" in sql.lower()
    assert "user_id = $2" in sql
    assert args == [SESSION_B, USER]


@pytest.mark.asyncio
async def test_revoking_someone_elses_session_is_a_404_not_a_403(pooled) -> None:
    """A row that is not the caller's does not match, so the delete returns
    nothing — and "not yours" is reported identically to "does not exist".
    A 403 would confirm the id is real, which is a free enumeration oracle."""
    from fastapi import HTTPException

    pooled(_pool(deleted=None))
    with pytest.raises(HTTPException) as caught:
        await revoke_session(session_id=SESSION_B, user=_user())
    assert caught.value.status_code == 404


@pytest.mark.asyncio
async def test_no_pool_is_a_503_on_both_routes(pooled) -> None:
    from fastapi import HTTPException

    pooled(None)
    for call in (
        list_sessions(user=_user()),
        revoke_session(session_id=SESSION_A, user=_user()),
    ):
        with pytest.raises(HTTPException) as caught:
            await call
        assert caught.value.status_code == 503


def test_the_user_id_is_never_taken_from_the_request() -> None:
    """An AST-free but load-bearing check: neither route may accept a user id
    as a parameter. If one ever does, the scoping above becomes advisory."""
    import inspect

    for fn in (list_sessions, revoke_session):
        params = set(inspect.signature(fn).parameters)
        assert "user_id" not in params, f"{fn.__name__} must never take a user_id"
