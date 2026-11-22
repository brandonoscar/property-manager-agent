"""POST /api/v1/auth/bootstrap — idempotent self-heal of the
company + company_users pair (PR #74 Bug 3a).

The endpoint exists to recover users whose ``handle_new_user``
trigger didn't fire on signup (pre-PR-#69 broken trigger, race
conditions, manual auth.users inserts). Tests pin:

1. Auth required.
2. Both upserts fire with the expected SQL shape (ON CONFLICT DO
   NOTHING — matches the trigger semantics, so the call is safe
   to run repeatedly).
3. Endpoint never accepts a user_id from the body (multi-tenant
   discipline — the scope is the authenticated user, full stop).
4. Pool unavailable → 503, never 500.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import MagicMock, patch

import jwt
import pytest
from fastapi.testclient import TestClient

from helixis.main import create_app
from helixis.settings import Settings
from tests.unit._supabase_tokens import TEST_ISSUER, TEST_SUPABASE_URL

TEST_JWT_SECRET = "test-secret-key-for-unit-tests-only-not-production"
USER_ID = "00000000-0000-0000-0000-000000000001"


def _token(sub: str = USER_ID, email: str = "brandon@example.com") -> str:
    return jwt.encode(
        {
            "sub": sub,
            "email": email,
            "role": "authenticated",
            "aud": "authenticated",
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
            "iss": TEST_ISSUER,
        },
        TEST_JWT_SECRET,
        algorithm="HS256",
    )


@pytest.fixture
def test_settings() -> Settings:
    return Settings(
        env="test",
        auth={"jwt_secret": TEST_JWT_SECRET},
        supabase={"url": TEST_SUPABASE_URL},
        memory={"enabled": False},
    )


def _build_pool() -> tuple[Any, list[tuple[str, tuple[Any, ...]]]]:
    """Stand in for asyncpg pool. Captures every (sql, args) tuple
    that ``conn.execute`` sees so tests can pin the upsert shape."""
    captured: list[tuple[str, tuple[Any, ...]]] = []

    class _FakeConn:
        async def execute(self, sql: str, *args: Any) -> None:
            captured.append((sql, args))

    @asynccontextmanager
    async def _acquire():
        yield _FakeConn()

    pool = MagicMock()
    pool.acquire = _acquire
    return pool, captured


def _make_client(
    test_settings: Settings,
    *,
    pool: Any | None,
) -> TestClient:
    from helixis.api.rate_limit import connectors_limiter

    connectors_limiter.counter._log.clear()

    async def fake_resolve(_user_id: str) -> list[str]:
        # The bootstrap endpoint is the answer when this returns empty —
        # the test bypasses the company-membership scope check.
        return []

    patches = [
        patch("helixis.api.routes.auth.get_pool", return_value=pool),
        patch("helixis.api.auth._resolve_company_ids", side_effect=fake_resolve),
        patch("helixis.api.auth.get_settings", return_value=test_settings),
        patch("helixis.main.get_settings", return_value=test_settings),
    ]
    for p in patches:
        p.start()
    app = create_app(test_settings)
    client = TestClient(app)
    original_close = client.close

    def close_with_patches() -> None:
        for p in reversed(patches):
            p.stop()
        original_close()

    client.close = close_with_patches  # type: ignore[method-assign]
    return client


def test_bootstrap_requires_authentication(test_settings: Settings) -> None:
    pool, _ = _build_pool()
    client = _make_client(test_settings, pool=pool)
    try:
        resp = client.post("/api/v1/auth/bootstrap")
        assert resp.status_code in (401, 403)
    finally:
        client.close()


def test_bootstrap_upserts_company_and_membership(test_settings: Settings) -> None:
    """Happy path — both INSERTs fire, ON CONFLICT DO NOTHING, scoped
    by the authenticated user."""
    pool, captured = _build_pool()
    client = _make_client(test_settings, pool=pool)
    try:
        resp = client.post(
            "/api/v1/auth/bootstrap",
            headers={"Authorization": f"Bearer {_token()}"},
        )
    finally:
        client.close()

    assert resp.status_code == 200
    body = resp.json()
    assert body == {"company_id": USER_ID}

    assert len(captured) == 2, f"expected 2 upserts, got {len(captured)}"

    companies_sql, companies_args = captured[0]
    cu_sql, cu_args = captured[1]

    # Companies upsert: ON CONFLICT (id) DO NOTHING, args = (user_id, display_name)
    assert "INSERT INTO public.companies" in companies_sql
    assert "ON CONFLICT (id) DO NOTHING" in companies_sql
    assert companies_args[0] == USER_ID
    # display_name = email local-part
    assert companies_args[1] == "brandon"

    # Membership upsert: ON CONFLICT (user_id, company_id) DO NOTHING
    assert "INSERT INTO public.company_users" in cu_sql
    assert "ON CONFLICT (user_id, company_id) DO NOTHING" in cu_sql
    # Role hardcoded — never user-supplied. 'admin' is the reconciled top
    # role (ADR 0005 / migration 0034); the legacy 'owner' violates the
    # tightened company_users_role_check and 500'd fresh-user onboarding.
    assert "'admin'" in cu_sql
    assert "'owner'" not in cu_sql
    assert cu_args == (USER_ID, USER_ID)


def test_bootstrap_uses_personal_workspace_fallback_for_missing_email(
    test_settings: Settings,
) -> None:
    """If the JWT has no email or the local-part is empty, the display
    name falls back to 'Personal Workspace' — matches the trigger's
    same fallback in migration 0012."""
    pool, captured = _build_pool()
    client = _make_client(test_settings, pool=pool)
    try:
        # JWT with an explicit empty email-local-part case is hard to
        # encode (Supabase requires email present). Test via an email
        # whose local part is the empty string — "@example.com".
        token = _token(email="@example.com")
        resp = client.post(
            "/api/v1/auth/bootstrap",
            headers={"Authorization": f"Bearer {token}"},
        )
    finally:
        client.close()

    assert resp.status_code == 200
    companies_args = captured[0][1]
    assert companies_args[1] == "Personal Workspace"


def test_bootstrap_returns_503_when_pool_unavailable(test_settings: Settings) -> None:
    """Defensive: tests / scripts without a DB pool get a truthful
    503, never a 500 traceback."""
    client = _make_client(test_settings, pool=None)
    try:
        resp = client.post(
            "/api/v1/auth/bootstrap",
            headers={"Authorization": f"Bearer {_token()}"},
        )
    finally:
        client.close()
    assert resp.status_code == 503


def test_bootstrap_db_error_returns_handled_500(test_settings: Settings) -> None:
    """A DB failure must surface as a handled JSON 500 — an unhandled
    exception skips CORSMiddleware, so browser clients (the onboarding
    wizard) see an opaque "blocked by CORS" failure instead of the real
    error (the 2026-07-24 role_check incident)."""

    class _FailConn:
        async def execute(self, sql: str, *args: Any) -> None:
            raise RuntimeError("violates check constraint")

    @asynccontextmanager
    async def _acquire():
        yield _FailConn()

    pool = MagicMock()
    pool.acquire = _acquire

    client = _make_client(test_settings, pool=pool)
    try:
        resp = client.post(
            "/api/v1/auth/bootstrap",
            headers={"Authorization": f"Bearer {_token()}"},
        )
    finally:
        client.close()

    assert resp.status_code == 500
    assert "bootstrap failed" in resp.json()["detail"].lower()


def test_bootstrap_rejects_user_id_in_body(test_settings: Settings) -> None:
    """Multi-tenant discipline: the scope is the authenticated user.
    The route MUST NOT accept a user_id from the body — that's a
    classic privilege-escalation surface. Posting a body should
    either be ignored or rejected, but never accepted."""
    pool, captured = _build_pool()
    client = _make_client(test_settings, pool=pool)
    try:
        resp = client.post(
            "/api/v1/auth/bootstrap",
            headers={"Authorization": f"Bearer {_token()}"},
            json={"user_id": "11111111-1111-1111-1111-111111111111"},
        )
    finally:
        client.close()

    # Still 200 (route ignores body), and the companies upsert used
    # the authenticated user_id from the JWT — never the body's
    # value.
    assert resp.status_code == 200
    assert resp.json() == {"company_id": USER_ID}
    assert captured[0][1][0] == USER_ID  # NOT the attacker-supplied uuid
