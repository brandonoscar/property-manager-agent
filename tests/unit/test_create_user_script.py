"""Tests for ``scripts/create_user.py``.

Mocks the Supabase Auth Admin API via ``httpx.MockTransport`` and the
asyncpg pool so we can exercise the orchestration end-to-end without
either real network or a real database.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

# Capture the real AsyncClient before any test patches it (same pattern
# as test_smoke_script.py).
_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _load_module():
    repo_root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "_helixis_create_user", repo_root / "scripts" / "create_user.py"
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_helixis_create_user"] = mod
    spec.loader.exec_module(mod)
    return mod


cu = _load_module()


def _fake_pool_yielding(conn: AsyncMock) -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=cm)
    pool.close = AsyncMock()
    # transaction() is a context manager too
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=tx)
    return pool


def _patched_admin_create(transport: httpx.MockTransport):
    """Wrap httpx.AsyncClient so create_user_user uses our MockTransport."""

    def _factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return _REAL_ASYNC_CLIENT(*args, **kwargs)  # type: ignore[arg-type]

    return patch.object(cu, "httpx", MagicMock(AsyncClient=_factory))


# ─────────────────────────────────────────────────────────────────────
# admin create
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_create_returns_id_on_2xx() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path.endswith("/auth/v1/admin/users")
        body = request.headers.get("Authorization", "")
        assert body.startswith("Bearer ")
        return httpx.Response(
            201,
            json={"id": "user-uuid-123", "email": "x@y.test"},
        )

    with _patched_admin_create(httpx.MockTransport(handler)):
        uid = await cu._admin_create_or_get_user(
            supabase_url="https://example.supabase.co",
            service_key="sk-test",
            email="x@y.test",
            password="pw",
        )
    assert uid == "user-uuid-123"


@pytest.mark.asyncio
async def test_admin_create_handles_user_alias_in_response() -> None:
    """Some Supabase versions wrap the user in {"user": {...}}."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"user": {"id": "user-uuid-456"}},
        )

    with _patched_admin_create(httpx.MockTransport(handler)):
        uid = await cu._admin_create_or_get_user(
            supabase_url="https://example.supabase.co",
            service_key="sk-test",
            email="x@y.test",
            password="pw",
        )
    assert uid == "user-uuid-456"


@pytest.mark.asyncio
async def test_admin_create_falls_back_to_list_on_422() -> None:
    """Email already registered → list endpoint is queried for the id."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            calls.append("create")
            return httpx.Response(
                422,
                json={"msg": "User already registered"},
            )
        if request.method == "GET":
            calls.append("list")
            return httpx.Response(
                200,
                json={
                    "users": [
                        {"id": "other-id", "email": "noise@example.com"},
                        {"id": "matched-id", "email": "x@y.test"},
                    ]
                },
            )
        return httpx.Response(500)

    with _patched_admin_create(httpx.MockTransport(handler)):
        uid = await cu._admin_create_or_get_user(
            supabase_url="https://example.supabase.co",
            service_key="sk-test",
            email="x@y.test",
            password="pw",
        )
    assert uid == "matched-id"
    assert calls == ["create", "list"]


@pytest.mark.asyncio
async def test_admin_create_raises_on_other_errors() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with _patched_admin_create(httpx.MockTransport(handler)):
        with pytest.raises(RuntimeError, match="500"):
            await cu._admin_create_or_get_user(
                supabase_url="https://example.supabase.co",
                service_key="sk-test",
                email="x@y.test",
                password="pw",
            )


# ─────────────────────────────────────────────────────────────────────
# tenancy inserts
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ensure_company_uses_on_conflict_do_nothing() -> None:
    conn = AsyncMock()
    await cu._ensure_company(conn, company_id="acme", company_name="Acme Realty")
    sql, *args = conn.execute.await_args.args
    assert "INSERT INTO public.companies" in sql
    assert "ON CONFLICT (id) DO NOTHING" in sql
    assert args == ["acme", "Acme Realty"]


@pytest.mark.asyncio
async def test_ensure_membership_uses_on_conflict_do_nothing() -> None:
    conn = AsyncMock()
    await cu._ensure_membership(
        conn,
        user_id="11111111-1111-1111-1111-111111111111",
        company_id="acme",
        role="owner",
    )
    sql, *args = conn.execute.await_args.args
    assert "INSERT INTO public.company_users" in sql
    assert "ON CONFLICT (user_id, company_id) DO NOTHING" in sql
    assert args == [
        "11111111-1111-1111-1111-111111111111",
        "acme",
        "owner",
    ]


# ─────────────────────────────────────────────────────────────────────
# env guard
# ─────────────────────────────────────────────────────────────────────


def test_require_env_exits_when_missing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for name in (
        "HELIXIS_SUPABASE__URL",
        "HELIXIS_SUPABASE__SERVICE_KEY",
        "HELIXIS_SUPABASE__DB_URL",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(SystemExit) as exc_info:
        cu._require_env()
    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert "HELIXIS_SUPABASE__URL" in err
    assert "HELIXIS_SUPABASE__SERVICE_KEY" in err


def test_require_env_rejects_placeholder(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("HELIXIS_SUPABASE__URL", "https://placeholder.supabase.co")
    monkeypatch.setenv("HELIXIS_SUPABASE__SERVICE_KEY", "sk-test")
    monkeypatch.setenv("HELIXIS_SUPABASE__DB_URL", "postgresql://test:test@localhost/test")

    with pytest.raises(SystemExit) as exc_info:
        cu._require_env()
    assert exc_info.value.code == 3
    err = capsys.readouterr().err
    assert "placeholder" in err.lower()
