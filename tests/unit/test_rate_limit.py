"""Tests for the rate-limiting system and security-header middleware."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from helixis.api.auth import AuthenticatedUser
from helixis.api.middleware import setup_middleware
from helixis.api.rate_limit import (
    RateLimiter,
    SlidingWindowCounter,
    _get_client_ip,
    agent_run_limiter,
    apply_settings,
)
from helixis.settings import RateLimitSettings, Settings

# ── SlidingWindowCounter ─────────────────────────────────────────────


def test_counter_allows_within_limit() -> None:
    counter = SlidingWindowCounter(requests=3, window_seconds=60)
    for _ in range(3):
        allowed, _, _ = counter.is_allowed("test")
        assert allowed


def test_counter_rejects_over_limit() -> None:
    counter = SlidingWindowCounter(requests=2, window_seconds=60)
    counter.is_allowed("test")
    counter.is_allowed("test")
    allowed, remaining, retry_after = counter.is_allowed("test")
    assert not allowed
    assert remaining == 0
    assert retry_after > 0


def test_counter_remaining_counts_down() -> None:
    counter = SlidingWindowCounter(requests=5, window_seconds=60)
    _, r1, _ = counter.is_allowed("test")
    _, r2, _ = counter.is_allowed("test")
    assert (r1, r2) == (4, 3)


def test_counter_separate_keys_are_independent() -> None:
    counter = SlidingWindowCounter(requests=1, window_seconds=60)
    a_allowed, _, _ = counter.is_allowed("user_a")
    b_allowed, _, _ = counter.is_allowed("user_b")
    assert a_allowed and b_allowed


def test_counter_rejected_requests_dont_consume_budget() -> None:
    """A rejected request must not be appended to the log — otherwise a
    client spamming an already-full bucket would extend their own lockout.
    """
    counter = SlidingWindowCounter(requests=1, window_seconds=60)
    counter.is_allowed("test")
    before_len = len(counter._log["test"])
    counter.is_allowed("test")  # rejected
    counter.is_allowed("test")  # rejected
    assert len(counter._log["test"]) == before_len


def test_counter_window_expiry() -> None:
    counter = SlidingWindowCounter(requests=1, window_seconds=0.1)
    counter.is_allowed("test")
    time.sleep(0.15)
    allowed, _, _ = counter.is_allowed("test")
    assert allowed


def test_counter_cleanup_removes_expired_keys() -> None:
    counter = SlidingWindowCounter(requests=5, window_seconds=0.05)
    counter.is_allowed("key_a")
    counter.is_allowed("key_b")
    time.sleep(0.1)
    pruned = counter.cleanup()
    assert pruned == 2
    assert "key_a" not in counter._log
    assert "key_b" not in counter._log


def test_counter_reconfigure_applies_new_limits() -> None:
    counter = SlidingWindowCounter(requests=1, window_seconds=60)
    counter.is_allowed("k")
    assert counter.is_allowed("k")[0] is False
    counter.reconfigure(requests=5, window_seconds=60)
    # Existing log carried over — one entry already recorded — but new
    # ceiling is 5, so the next call should be allowed.
    assert counter.is_allowed("k")[0] is True


# ── _get_client_ip ───────────────────────────────────────────────────


def test_client_ip_direct() -> None:
    request = MagicMock()
    request.headers = {}
    request.client.host = "192.168.1.1"
    assert _get_client_ip(request) == "192.168.1.1"


def test_client_ip_forwarded_picks_first() -> None:
    request = MagicMock()
    request.headers = {"X-Forwarded-For": "203.0.113.50, 70.41.3.18, 150.172.238.178"}
    request.client.host = "127.0.0.1"
    assert _get_client_ip(request) == "203.0.113.50"


def test_client_ip_no_client_object() -> None:
    request = MagicMock()
    request.headers = {}
    request.client = None
    assert _get_client_ip(request) == "unknown"


# ── RateLimiter ──────────────────────────────────────────────────────


@pytest.fixture
def limiter() -> RateLimiter:
    return RateLimiter(requests=2, window_seconds=60)


def _fake_request() -> MagicMock:
    request = MagicMock()
    request.headers = {}
    request.client.host = "127.0.0.1"
    request.state = MagicMock(spec=[])  # no pre-existing attributes
    return request


async def test_limiter_stashes_headers_on_state(limiter: RateLimiter) -> None:
    user = AuthenticatedUser(user_id="u1", company_ids=["c"])
    request = _fake_request()

    await limiter.check_by_user(request, user)

    headers = request.state.rate_limit_headers
    assert headers["X-RateLimit-Limit"] == "2"
    assert headers["X-RateLimit-Remaining"] == "1"


async def test_limiter_rejects_with_429_and_retry_after(
    limiter: RateLimiter,
) -> None:
    from fastapi import HTTPException

    user = AuthenticatedUser(user_id="u1", company_ids=["c"])
    await limiter.check_by_user(_fake_request(), user)
    await limiter.check_by_user(_fake_request(), user)

    with pytest.raises(HTTPException) as exc:
        await limiter.check_by_user(_fake_request(), user)

    assert exc.value.status_code == 429
    assert exc.value.headers["X-RateLimit-Limit"] == "2"
    assert exc.value.headers["X-RateLimit-Remaining"] == "0"
    assert int(exc.value.headers["Retry-After"]) >= 1


async def test_limiter_user_keys_isolated(limiter: RateLimiter) -> None:
    alice = AuthenticatedUser(user_id="alice", company_ids=["c"])
    bob = AuthenticatedUser(user_id="bob", company_ids=["c"])

    await limiter.check_by_user(_fake_request(), alice)
    await limiter.check_by_user(_fake_request(), alice)
    # Alice is now full. Bob should still be allowed.
    await limiter.check_by_user(_fake_request(), bob)


async def test_limiter_by_ip_uses_forwarded_header(
    limiter: RateLimiter,
) -> None:
    req_a = _fake_request()
    req_a.headers = {"X-Forwarded-For": "1.1.1.1"}
    req_b = _fake_request()
    req_b.headers = {"X-Forwarded-For": "2.2.2.2"}

    # Consume req_a's budget entirely — req_b must still be unaffected.
    await limiter.check_by_ip(req_a)
    await limiter.check_by_ip(req_a)
    await limiter.check_by_ip(req_b)


async def test_limiter_falls_back_to_ip_when_user_missing(
    limiter: RateLimiter,
) -> None:
    req = _fake_request()
    req.headers = {"X-Forwarded-For": "9.9.9.9"}
    # user=None path → keyed by IP.
    await limiter.check_by_user(req, None)
    headers = req.state.rate_limit_headers
    assert headers["X-RateLimit-Remaining"] == "1"


# ── apply_settings reconfigures module-level limiters ────────────────


def test_apply_settings_reconfigures_module_limiters() -> None:
    original = agent_run_limiter.requests

    apply_settings(RateLimitSettings(agent_run=7, agent_action=8, connectors=9, webhooks=10))
    assert agent_run_limiter.requests == 7

    # Restore defaults so later tests aren't affected.
    apply_settings(RateLimitSettings())
    assert agent_run_limiter.requests == original


# ── Middleware integration ───────────────────────────────────────────


def _build_middleware_app(env: str = "test") -> FastAPI:
    """Minimal app with just our middleware — no Composio / memory boot."""
    app = FastAPI()
    settings = Settings(
        env=env,
        memory={"enabled": False},
        api={
            "host": "127.0.0.1",
            "port": 8000,
            "cors_origins": ["*"],
        },
    )
    setup_middleware(app, settings)

    tight_limiter = RateLimiter(requests=2, window_seconds=60)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.get("/limited")
    async def limited(
        request: Request,
        _rl: None = Depends(tight_limiter.check_by_ip),
    ) -> dict:
        return {"ok": True}

    return app


def test_security_headers_on_every_response() -> None:
    client = TestClient(_build_middleware_app())
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "camera=()" in resp.headers["Permissions-Policy"]
    assert resp.headers["X-XSS-Protection"] == "0"


def test_hsts_only_in_production() -> None:
    test_app = TestClient(_build_middleware_app(env="test"))
    assert "Strict-Transport-Security" not in test_app.get("/health").headers

    prod_app = TestClient(_build_middleware_app(env="production"))
    assert "Strict-Transport-Security" in prod_app.get("/health").headers


def test_request_id_header_round_trips() -> None:
    client = TestClient(_build_middleware_app())
    resp = client.get("/health", headers={"X-Request-ID": "rid-abc"})
    assert resp.headers["X-Request-ID"] == "rid-abc"


def test_request_id_generated_when_missing() -> None:
    client = TestClient(_build_middleware_app())
    rid = client.get("/health").headers.get("X-Request-ID")
    assert rid and len(rid) > 8


def test_rate_limit_headers_propagate_to_response() -> None:
    client = TestClient(_build_middleware_app())
    resp = client.get("/limited")
    assert resp.status_code == 200
    assert resp.headers["X-RateLimit-Limit"] == "2"
    assert resp.headers["X-RateLimit-Remaining"] == "1"


def test_rate_limited_endpoint_returns_429_after_burst() -> None:
    client = TestClient(_build_middleware_app())
    r1 = client.get("/limited")
    r2 = client.get("/limited")
    r3 = client.get("/limited")
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r3.status_code == 429
    # 429 responses should still carry the rate-limit headers.
    assert r3.headers.get("X-RateLimit-Limit") == "2"
    assert r3.headers.get("Retry-After") is not None


def test_counter_thread_safety_under_concurrency() -> None:
    """Many concurrent claims against a shared counter should never
    over-count. Covers the Lock inside SlidingWindowCounter."""
    counter = SlidingWindowCounter(requests=100, window_seconds=60)

    async def hammer() -> int:
        allowed_count = 0
        for _ in range(50):
            ok, _, _ = counter.is_allowed("shared")
            if ok:
                allowed_count += 1
        return allowed_count

    async def main() -> list[int]:
        return await asyncio.gather(*(hammer() for _ in range(4)))

    results = asyncio.run(main())
    assert sum(results) == 100  # exactly the ceiling, no more
