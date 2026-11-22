"""Feedback route (BR #19) — durable insert + best-effort notice and capture."""

from __future__ import annotations

import time
from contextlib import ExitStack, asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
from fastapi.testclient import TestClient

from helixis.main import create_app
from helixis.settings import Settings
from tests.unit._supabase_tokens import TEST_ISSUER, TEST_SUPABASE_URL

TEST_JWT_SECRET = "test-secret-key-for-unit-tests-only-not-production"
COMPANY_ID = "5ca1ab1e-0000-4000-8000-000000000004"
ME_USER_ID = "00000000-0000-0000-0000-000000000001"


def _token() -> str:
    return jwt.encode(
        {
            "sub": ME_USER_ID,
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


def _settings() -> Settings:
    return Settings(
        env="test",
        auth={"jwt_secret": TEST_JWT_SECRET},
        supabase={"url": TEST_SUPABASE_URL},
        memory={"enabled": False},
    )


def _pool(conn: Any) -> MagicMock:
    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    return pool


def _post(app: Any, body: dict[str, Any]) -> Any:
    with TestClient(app) as c:
        return c.post(
            "/api/v1/feedback", json=body, headers={"Authorization": f"Bearer {_token()}"}
        )


def _env(settings: Settings, conn: Any):
    return (
        patch("helixis.api.routes.feedback.get_pool", return_value=_pool(conn)),
        patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID]),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    )


def test_feedback_inserts_row_and_captures_event() -> None:
    settings = _settings()
    conn = MagicMock()
    # ⚠ RE-POINTED, not relaxed (2026-09-08). The insert moved from `execute`
    # to `fetchrow ... RETURNING id`, because the row id is the pointer the
    # Sentry notice carries — nothing read this table, so a report reached
    # nobody. A required-shape change failing its callers closed is the guard
    # working (gotcha 77).
    conn.fetchrow = AsyncMock(return_value={"id": "55555555-5555-5555-5555-555555555555"})
    with ExitStack() as stack:
        for cm in _env(settings, conn):
            stack.enter_context(cm)
        capture = stack.enter_context(patch("helixis.api.routes.feedback.ph_capture"))
        notice = stack.enter_context(patch("helixis.api.routes.feedback.capture_notice"))
        app = create_app(settings)
        resp = _post(app, {"category": "bug", "message": "The rent table clipped", "view": "chat"})
    assert resp.status_code == 200 and resp.json() == {"ok": True}
    sql, *args = conn.fetchrow.await_args.args
    assert "INSERT INTO helixis_feedback" in sql
    # ⚠ RE-POINTED, not relaxed (2026-09-05). The row gained four diagnostic
    # columns and this exact-list assertion failed its caller closed, which is
    # the guard working — a report that cannot name the build or the run is the
    # gap those columns exist to close. The tail is all None here because this
    # request carries no session: the browser sent no thread, so no run is
    # resolvable, and the honest stored value is absent rather than invented.
    assert args == [
        COMPANY_ID,
        ME_USER_ID,
        "bug",
        "The rent table clipped",
        "chat",
        None,  # session_id — none sent
        None,  # run_id — nothing to resolve without a session
        None,  # app_version
        None,  # user_agent
    ]
    capture.assert_called_once()
    assert capture.call_args.args[1] == "feedback submitted"

    # A bug report is a WARNING so it rises above info noise, and the notice
    # carries the row to READ — never the person's own words, which is the
    # line the PostHog capture above already draws.
    notice.assert_called_once()
    assert notice.call_args.kwargs["level"] == "warning"
    assert notice.call_args.kwargs["op"] == "feedback.submitted"
    assert "55555555-5555-5555-5555-555555555555" in notice.call_args.args[0]
    assert "The rent table clipped" not in notice.call_args.args[0]
    assert "The rent table clipped" not in str(notice.call_args.kwargs["tags"])


def test_feedback_rejects_bad_category_and_blank_message() -> None:
    settings = _settings()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"id": "55555555-5555-5555-5555-555555555555"})
    with ExitStack() as stack:
        for cm in _env(settings, conn):
            stack.enter_context(cm)
        app = create_app(settings)
        assert _post(app, {"category": "rant", "message": "hello there"}).status_code == 400
        assert _post(app, {"category": "bug", "message": "   "}).status_code == 400
    conn.fetchrow.assert_not_awaited()


def test_feedback_capture_failure_never_fails_the_submission() -> None:
    settings = _settings()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"id": "55555555-5555-5555-5555-555555555555"})
    with ExitStack() as stack:
        for cm in _env(settings, conn):
            stack.enter_context(cm)
        stack.enter_context(
            patch("helixis.api.routes.feedback.ph_capture", side_effect=RuntimeError("ph down"))
        )
        # ⚠ BOTH best-effort seams broken at once. Either one alone leaves the
        # other to carry the request, so a test that breaks only one cannot
        # tell "the report survives its diagnostics" from "the report survives
        # THIS diagnostic".
        stack.enter_context(
            patch(
                "helixis.api.routes.feedback.capture_notice",
                side_effect=RuntimeError("sentry down"),
            )
        )
        app = create_app(settings)
        resp = _post(app, {"category": "idea", "message": "vendor texting please"})
    assert resp.status_code == 200  # the row landed; neither seam is load-bearing
    conn.fetchrow.assert_awaited_once()
