"""GET /api/v1/dev/runs (and /{run_id}) — RLS + feature-flag tests (PR #141).

Cross-tenant requests must return 404 (not 200 with empty data, and
not a different error code that distinguishes existence). The feature
flag must also 404 — not 403 — so the route's presence isn't leaked
to unauthorised probes.
"""

from __future__ import annotations

import time
from datetime import UTC
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import jwt
from fastapi.testclient import TestClient

from helixis.db.repositories.orchestrator_runs import OrchestratorRunRow
from helixis.main import create_app
from helixis.settings import Settings
from tests.unit._supabase_tokens import TEST_ISSUER, TEST_SUPABASE_URL

TEST_JWT_SECRET = "test-secret-key-for-unit-tests-only-not-production"
COMPANY_ID = "5ca1ab1e-0000-4000-8000-000000000004"
ME_USER_ID = "00000000-0000-0000-0000-000000000001"


def _make_token(sub: str = ME_USER_ID) -> str:
    return jwt.encode(
        {
            "sub": sub,
            "email": "brandon@example.com",
            "role": "authenticated",
            "aud": "authenticated",
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
            "iss": TEST_ISSUER,
        },
        TEST_JWT_SECRET,
        algorithm="HS256",
    )


def _settings_with_dev_flag(flag: bool) -> Settings:
    return Settings(
        env="test",
        auth={"jwt_secret": TEST_JWT_SECRET},
        supabase={"url": TEST_SUPABASE_URL},
        memory={"enabled": False},
        dev={"show_runs_page": flag},
    )


def _make_row(run_id: str, company_id: str) -> OrchestratorRunRow:
    from datetime import datetime

    return OrchestratorRunRow(
        id=run_id,
        company_id=company_id,
        user_id=None,
        session_id="s",
        source="chat",
        user_query="hi",
        started_at=datetime.now(UTC),
        completed_at=None,
        latency_ms=None,
        status="succeeded",
        error_message=None,
        error_type=None,
        tool_calls=[],
        final_text=None,
        handoffs=[],
        raw=None,
    )


def _client_with(settings: Settings, fetch_recent=None, fetch_one=None) -> TestClient:
    pool = MagicMock()
    fetch_recent = fetch_recent or AsyncMock(return_value=[])
    fetch_one = fetch_one or AsyncMock(return_value=None)

    async def fake_resolve(_user_id: str) -> list[str]:
        return [COMPANY_ID]

    cm = patch.multiple(
        "helixis.api.routes.dev",
        get_pool=lambda: pool,
        orchestrator_runs=MagicMock(
            fetch_recent_runs=fetch_recent,
            fetch_run=fetch_one,
        ),
    )
    cm.__enter__()
    patch("helixis.api.auth._resolve_company_ids", side_effect=fake_resolve).__enter__()
    patch("helixis.api.auth.get_settings", return_value=settings).__enter__()
    patch("helixis.main.get_settings", return_value=settings).__enter__()
    app = create_app(settings)
    return TestClient(app)


# ── Feature-flag gating ──────────────────────────────────────────────


def test_runs_list_returns_404_when_flag_off():
    """Production-shaped config: show_runs_page=False → /dev/runs 404s
    even for an authenticated user. The flag is the only gate; the
    route doesn't even attempt the DB read."""
    settings = _settings_with_dev_flag(False)
    fetch_recent = AsyncMock(return_value=[])

    with (
        patch("helixis.api.routes.dev.get_pool", return_value=MagicMock()),
        patch(
            "helixis.api.routes.dev.orchestrator_runs.fetch_recent_runs",
            fetch_recent,
        ),
        patch(
            "helixis.api.auth._resolve_company_ids",
            side_effect=lambda _uid: [COMPANY_ID],
        ),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.get(
                "/api/v1/dev/runs",
                headers={"Authorization": f"Bearer {_make_token()}"},
            )
    assert resp.status_code == 404
    # Defense in depth — the writer must not be reached when the flag is off.
    fetch_recent.assert_not_called()


def test_run_detail_returns_404_when_flag_off():
    settings = _settings_with_dev_flag(False)
    fetch_one = AsyncMock(return_value=_make_row(str(uuid4()), COMPANY_ID))

    with (
        patch("helixis.api.routes.dev.get_pool", return_value=MagicMock()),
        patch("helixis.api.routes.dev.orchestrator_runs.fetch_run", fetch_one),
        patch(
            "helixis.api.auth._resolve_company_ids",
            side_effect=lambda _uid: [COMPANY_ID],
        ),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.get(
                f"/api/v1/dev/runs/{uuid4()}",
                headers={"Authorization": f"Bearer {_make_token()}"},
            )
    assert resp.status_code == 404
    # Critical: never reach the DB when the flag is off, even for a
    # row that genuinely exists. Otherwise enabling tracing later
    # could leak per-row existence through differential timings.
    fetch_one.assert_not_called()


# ── Cross-tenant isolation ───────────────────────────────────────────


def test_runs_list_only_returns_callers_company():
    """The route hands the caller's primary company_id to
    fetch_recent_runs. We verify the repo is invoked with the right
    scope — RLS handles the second layer of defense."""
    settings = _settings_with_dev_flag(True)
    fetch_recent = AsyncMock(return_value=[_make_row(str(uuid4()), COMPANY_ID)])

    with (
        patch("helixis.api.routes.dev.get_pool", return_value=MagicMock()),
        patch(
            "helixis.api.routes.dev.orchestrator_runs.fetch_recent_runs",
            fetch_recent,
        ),
        patch(
            "helixis.api.auth._resolve_company_ids",
            side_effect=lambda _uid: [COMPANY_ID],
        ),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.get(
                "/api/v1/dev/runs",
                headers={"Authorization": f"Bearer {_make_token()}"},
            )
    assert resp.status_code == 200
    fetch_recent.assert_awaited_once()
    kwargs = fetch_recent.await_args.kwargs
    assert kwargs["company_id"] == COMPANY_ID


def test_run_detail_returns_404_for_cross_tenant_id():
    """The repo's ``fetch_run`` returns None when company_id doesn't
    match (the SELECT has ``WHERE id = $1 AND company_id = $2``).
    The route maps None to 404 — never a discriminating message."""
    settings = _settings_with_dev_flag(True)
    # Simulate a row that exists in another company: fetch_run returns
    # None because the SQL filters by company_id, but the request itself
    # is well-formed and authenticated.
    fetch_one = AsyncMock(return_value=None)

    with (
        patch("helixis.api.routes.dev.get_pool", return_value=MagicMock()),
        patch("helixis.api.routes.dev.orchestrator_runs.fetch_run", fetch_one),
        patch(
            "helixis.api.auth._resolve_company_ids",
            side_effect=lambda _uid: [COMPANY_ID],
        ),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.get(
                f"/api/v1/dev/runs/{uuid4()}",
                headers={"Authorization": f"Bearer {_make_token()}"},
            )
    assert resp.status_code == 404
    # The repo WAS called — and was called with the caller's company_id,
    # so even if the run exists for a different tenant, the SQL filter
    # blocks it.
    fetch_one.assert_awaited_once()
    assert fetch_one.await_args.kwargs["company_id"] == COMPANY_ID


def test_unauthenticated_request_is_401():
    """No Authorization header — auth dependency fires before the
    dev-flag gate, so we get 401, not 404."""
    settings = _settings_with_dev_flag(True)
    with (
        patch("helixis.api.routes.dev.get_pool", return_value=MagicMock()),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.get("/api/v1/dev/runs")
    assert resp.status_code == 401


def test_user_without_company_membership_is_400():
    """Auth succeeds but the user has zero memberships — the route
    rejects rather than returning an empty list, so the error is
    actionable instead of looking like a quiet no-runs case."""
    settings = _settings_with_dev_flag(True)
    fetch_recent = AsyncMock(return_value=[])

    with (
        patch("helixis.api.routes.dev.get_pool", return_value=MagicMock()),
        patch(
            "helixis.api.routes.dev.orchestrator_runs.fetch_recent_runs",
            fetch_recent,
        ),
        patch(
            "helixis.api.auth._resolve_company_ids",
            side_effect=lambda _uid: [],
        ),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.get(
                "/api/v1/dev/runs",
                headers={"Authorization": f"Bearer {_make_token()}"},
            )
    assert resp.status_code == 400
    fetch_recent.assert_not_called()
