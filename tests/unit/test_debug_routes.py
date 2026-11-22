"""Tests for ``/debug/sentry`` token gating + the release-SHA resolver.

The route's job is to raise a deterministic exception ONLY when the
correct token is provided. Every other shape (missing token, wrong
token, env var unset) must return 404 — leaking the route's
existence to scanners would defeat the point.
"""

from __future__ import annotations

import subprocess
from contextlib import asynccontextmanager
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from helixis.main import create_app
from helixis.observability.sentry_setup import _resolve_release_sha
from helixis.settings import Settings


@pytest.fixture
def app_client():
    settings = Settings(
        env="test",
        auth={"jwt_secret": "test-secret-key-for-unit-tests-only"},
        memory={"enabled": False},
        llm={"default_model": "openai/gpt-4o", "openai_api_key": "test-key"},
    )
    app = create_app(settings)
    app.state.memory_manager = MagicMock(available=False)
    app.state.composio_client = MagicMock(available=False)

    @asynccontextmanager
    async def noop_lifespan(app):
        yield

    app.router.lifespan_context = noop_lifespan
    return app


def test_debug_sentry_404_when_token_env_unset(monkeypatch: pytest.MonkeyPatch, app_client) -> None:
    """Even a request with a ``?token=`` value should 404 when the
    server-side env var isn't configured. Don't leak the route."""
    monkeypatch.delenv("SENTRY_TEST_TOKEN", raising=False)
    with TestClient(app_client) as c:
        resp = c.get("/api/v1/debug/sentry?token=anything")
    assert resp.status_code == 404


def test_debug_sentry_404_when_token_mismatches(
    monkeypatch: pytest.MonkeyPatch, app_client
) -> None:
    monkeypatch.setenv("SENTRY_TEST_TOKEN", "correct-token")
    with TestClient(app_client) as c:
        resp = c.get("/api/v1/debug/sentry?token=wrong")
    assert resp.status_code == 404


def test_debug_sentry_404_when_token_missing_from_query(
    monkeypatch: pytest.MonkeyPatch, app_client
) -> None:
    monkeypatch.setenv("SENTRY_TEST_TOKEN", "correct-token")
    with TestClient(app_client) as c:
        resp = c.get("/api/v1/debug/sentry")
    assert resp.status_code == 404


def test_debug_sentry_500s_on_correct_token(monkeypatch: pytest.MonkeyPatch, app_client) -> None:
    """Correct token → the route fires its error path and the server
    returns a structured 500.

    The catch-all CORS-on-500 middleware (#63) now converts the raised
    ``SentryTestError`` into a JSONResponse instead of letting it
    propagate — which is exactly the production behavior, so we assert on
    the 500 the browser actually sees, not on a bubbled exception. The
    raw exception name must not leak to the client.
    """
    monkeypatch.setenv("SENTRY_TEST_TOKEN", "correct-token")
    with TestClient(app_client) as c:
        resp = c.get("/api/v1/debug/sentry?token=correct-token")
    assert resp.status_code == 500
    assert resp.json()["error"] == "internal_server_error"
    assert "SentryTestError" not in resp.text


def test_debug_sentry_token_strips_surrounding_whitespace(
    monkeypatch: pytest.MonkeyPatch, app_client
) -> None:
    """Pasted-with-trailing-newline tokens still match — same defense
    as the env-var strip in ``settings.py``."""
    monkeypatch.setenv("SENTRY_TEST_TOKEN", "  correct-token\n")
    with TestClient(app_client) as c:
        resp = c.get("/api/v1/debug/sentry?token=correct-token")
    assert resp.status_code == 500


# ── Release SHA resolver ─────────────────────────────────────────────


def test_release_sha_prefers_helixis_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HELIXIS_RELEASE_SHA", "abc123")
    monkeypatch.setenv("RENDER_GIT_COMMIT", "should-be-ignored")
    assert _resolve_release_sha() == "abc123"


def test_release_sha_falls_back_to_render_git_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HELIXIS_RELEASE_SHA", raising=False)
    monkeypatch.setenv("RENDER_GIT_COMMIT", "def456")
    # Even if the local repo has a real HEAD, the env var wins.
    assert _resolve_release_sha() == "def456"


def test_release_sha_strips_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defense against newline-pasted SHA values, matching the
    settings-wide ``str_strip_whitespace`` posture."""
    monkeypatch.setenv("HELIXIS_RELEASE_SHA", "  abc123\n")
    monkeypatch.delenv("RENDER_GIT_COMMIT", raising=False)
    assert _resolve_release_sha() == "abc123"


def test_release_sha_returns_unknown_when_git_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If both env vars are unset AND ``git`` can't be run, the
    resolver returns the literal string ``"unknown"`` (not raises,
    not None). That string surfaces in Sentry's UI as
    ``release: unknown`` — operationally clearer than an absent tag,
    because the triager sees the failure case instead of wondering
    whether the deploy just wasn't tagged."""
    monkeypatch.delenv("HELIXIS_RELEASE_SHA", raising=False)
    monkeypatch.delenv("RENDER_GIT_COMMIT", raising=False)

    def boom(*_args, **_kwargs):
        raise FileNotFoundError("no git binary")

    with patch.object(subprocess, "run", side_effect=boom):
        assert _resolve_release_sha() == "unknown"


def test_release_sha_returns_unknown_on_git_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HELIXIS_RELEASE_SHA", raising=False)
    monkeypatch.delenv("RENDER_GIT_COMMIT", raising=False)

    fake_result = subprocess.CompletedProcess(args=[], returncode=128, stdout="", stderr="")
    with patch.object(subprocess, "run", return_value=fake_result):
        assert _resolve_release_sha() == "unknown"
