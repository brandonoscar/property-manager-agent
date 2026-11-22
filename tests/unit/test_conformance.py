"""Unit tests for the conformance probe's pure logic (no network)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from helixis.conformance.checks import (
    CONTRACT_ENDPOINTS,
    _is_egress_block,
    classify_buildium_test,
    classify_cors,
    classify_credentials_put,
    classify_endpoint_probe,
    classify_tested_persisted,
    probe_buildium_credentialed,
)
from helixis.main import create_app
from helixis.pms import registry
from helixis.settings import Settings


def test_egress_block_detected_by_deny_reason_header() -> None:
    resp = httpx.Response(403, headers={"x-deny-reason": "host_not_allowed"})
    assert _is_egress_block(resp) is True


def test_egress_block_detected_by_body() -> None:
    resp = httpx.Response(403, text="Host not in allowlist: example.com")
    assert _is_egress_block(resp) is True


def test_backend_403_is_not_egress_block() -> None:
    # A genuine backend auth 403 (no deny-reason header / allowlist body)
    # must NOT be mistaken for an egress denial.
    resp = httpx.Response(403, json={"detail": "Forbidden"})
    assert _is_egress_block(resp) is False


def test_401_is_not_egress_block() -> None:
    assert _is_egress_block(httpx.Response(401)) is False


@pytest.mark.parametrize(
    "status,expected_verdict",
    [
        (401, "pass"),  # wired + auth-protected
        (403, "pass"),
        (422, "pass"),  # wired; request validation reached
        (404, "fail"),  # route missing / path drifted
        (405, "fail"),  # method drifted from contract
        (500, "fail"),  # server error
        (502, "fail"),
        (200, "warn"),  # reachable but not auth-rejected
        (302, "warn"),  # unexpected
    ],
)
def test_classify_endpoint_probe(status: int, expected_verdict: str) -> None:
    verdict, detail = classify_endpoint_probe("POST", status)
    assert verdict == expected_verdict
    assert str(status) in detail


def test_classify_cors_allows_exact_origin() -> None:
    verdict, _ = classify_cors(
        "https://your-onboarding.vercel.app", "https://your-onboarding.vercel.app"
    )
    assert verdict == "pass"


def test_classify_cors_allows_wildcard() -> None:
    verdict, _ = classify_cors("*", "https://your-onboarding.vercel.app")
    assert verdict == "pass"


def test_classify_cors_missing_header_fails() -> None:
    verdict, detail = classify_cors(None, "https://your-onboarding.vercel.app")
    assert verdict == "fail"
    assert "CORS-blocked" in detail


def test_classify_cors_wrong_origin_fails() -> None:
    verdict, _ = classify_cors("https://your-app.vercel.app", "https://your-onboarding.vercel.app")
    assert verdict == "fail"


def test_contract_registry_covers_adr_0003() -> None:
    """Lock the registry to the ADR 0003 contract surface."""
    paths = {(m, p) for _, m, p in CONTRACT_ENDPOINTS}
    for required in [
        ("POST", "/api/v1/auth/bootstrap"),
        ("PUT", "/api/v1/buildium/credentials"),
        ("POST", "/api/v1/buildium/test"),
        ("PUT", "/api/v1/buildium/webhook-secret"),
        ("GET", "/api/v1/rentvine/credentials"),
        ("PUT", "/api/v1/rentvine/credentials"),
        ("POST", "/api/v1/rentvine/test"),
        ("GET", "/api/v1/rentmanager/credentials"),
        ("PUT", "/api/v1/rentmanager/credentials"),
        ("POST", "/api/v1/rentmanager/test"),
        ("GET", "/api/v1/propertyware/credentials"),
        ("PUT", "/api/v1/propertyware/credentials"),
        ("POST", "/api/v1/propertyware/test"),
        ("GET", "/api/v1/doorloop/credentials"),
        ("PUT", "/api/v1/doorloop/credentials"),
        ("POST", "/api/v1/doorloop/test"),
        ("GET", "/api/v1/reports/first-scan"),
        ("POST", "/api/v1/connectors/gmail/connect"),
        ("PATCH", "/api/v1/company"),
        ("POST", "/api/v1/company/invite"),
    ]:
        assert required in paths, f"contract endpoint {required} missing from probe registry"


def test_every_system_the_backend_knows_has_its_connect_routes_in_the_contract() -> None:
    """The wizard offers each system the backend has switched on, through the
    same three routes (helixis-onboarding ``src/lib/pms.ts``). Propertyware's
    were missing while Rent Manager's were listed, so the production probe
    could not see them drift. A system added to ``pms.registry`` fails here
    until its routes are in the contract."""
    paths = {(method, path) for _, method, path in CONTRACT_ENDPOINTS}
    for key in registry.PMS:
        for method, route in (("GET", "credentials"), ("PUT", "credentials"), ("POST", "test")):
            assert (method, f"/api/v1/{key}/{route}") in paths, f"{key}: {method} {route}"


def test_every_contract_endpoint_is_wired_and_turns_away_no_token() -> None:
    """What the production probe asks, asked of this build: each contract
    route exists with its method and refuses a caller with no token, under the
    same verdict (a 404 or 405 is a route the client repos call and this build
    dropped). Rent Manager, Propertyware and DoorLoop are probed switched off,
    as production runs them."""
    settings = Settings(
        env="test",
        auth={"jwt_secret": "test-secret-key-for-unit-tests-only-not-production"},
        memory={"enabled": False},
    )
    with patch("helixis.main.get_settings", return_value=settings):
        app = create_app(settings)
    client = TestClient(app)  # no ``with``: startup never runs, nothing dials a database
    assert not registry.enabled(registry.RENTMANAGER)
    assert not registry.enabled(registry.PROPERTYWARE)
    assert not registry.enabled(registry.DOORLOOP)
    for name, method, path in CONTRACT_ENDPOINTS:
        body = None if method == "GET" else {}
        status = client.request(method, path, json=body).status_code
        verdict, detail = classify_endpoint_probe(method, status)
        assert verdict == "pass", f"{name} ({method} {path}): {detail}"


# ── credentialed provisioning probe (P0 gate) ───────────────────


def test_classify_credentials_put() -> None:
    assert classify_credentials_put(200, True)[0] == "pass"
    assert classify_credentials_put(200, False)[0] == "fail"  # stored but not configured
    assert classify_credentials_put(401, None)[0] == "fail"  # token rejected
    assert classify_credentials_put(500, True)[0] == "fail"


def test_classify_buildium_test() -> None:
    assert classify_buildium_test(200, True, None)[0] == "pass"
    # ok:false is THE meaningful failure — Buildium rejected the creds.
    v, detail = classify_buildium_test(200, False, "invalid client secret")
    assert v == "fail" and "invalid client secret" in detail
    assert classify_buildium_test(401, None, None)[0] == "fail"
    assert classify_buildium_test(503, None, None)[0] == "fail"


def test_classify_tested_persisted() -> None:
    assert classify_tested_persisted(200, "2026-07-14T00:00:00Z")[0] == "pass"
    # 200 but no timestamp = the /test side effect didn't persist → real fail.
    assert classify_tested_persisted(200, None)[0] == "fail"
    # couldn't read back at all → warn, not fail (the ok:true gate already passed).
    assert classify_tested_persisted(500, None)[0] == "warn"


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _run_credentialed(handler: Callable[[httpx.Request], httpx.Response]) -> list:
    async def _go() -> list:
        async with _client(handler) as client:
            return await probe_buildium_credentialed(
                client, "https://x", "tok", client_id="ci", client_secret="cs"
            )

    return asyncio.run(_go())


def test_credentialed_happy_path_all_pass() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        p, m = req.url.path, req.method
        if m == "PUT" and p.endswith("/buildium/credentials"):
            return httpx.Response(200, json={"configured": True})
        if m == "POST" and p.endswith("/buildium/test"):
            return httpx.Response(
                200, json={"ok": True, "account_name": "Acme", "properties_count": 3}
            )
        if m == "GET" and p.endswith("/buildium/credentials"):
            return httpx.Response(
                200, json={"configured": True, "last_tested_at": "2026-07-14T00:00:00Z"}
            )
        return httpx.Response(404)

    results = _run_credentialed(handler)
    assert [r.name for r in results] == [
        "buildium.credentials.put.authenticated",
        "buildium.test.authenticated",
        "buildium.provisioning.persisted",
    ]
    assert all(r.status == "pass" for r in results)


def test_credentialed_test_ok_false_short_circuits() -> None:
    """ok:false (bad key) fails at the test step and skips the read-back."""

    def handler(req: httpx.Request) -> httpx.Response:
        p, m = req.url.path, req.method
        if m == "PUT" and p.endswith("/buildium/credentials"):
            return httpx.Response(200, json={"configured": True})
        if m == "POST" and p.endswith("/buildium/test"):
            return httpx.Response(200, json={"ok": False, "error": "Buildium rejected the request"})
        raise AssertionError("read-back should not be reached after a failed test")

    results = _run_credentialed(handler)
    assert len(results) == 2
    assert results[0].status == "pass"
    assert results[1].status == "fail" and "rejected" in results[1].detail


def test_credentialed_put_failure_short_circuits() -> None:
    """A rejected token at the store step stops before touching Buildium."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "PUT" and req.url.path.endswith("/buildium/credentials"):
            return httpx.Response(401, json={"detail": "invalid token"})
        raise AssertionError("no step should run after the credentials PUT fails")

    results = _run_credentialed(handler)
    assert len(results) == 1
    assert results[0].status == "fail"
