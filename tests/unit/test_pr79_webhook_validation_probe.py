"""PR #79 — accept Buildium's pre-save webhook validation probe.

Brandon couldn't save a webhook subscription in Buildium UI; the red
"Something went wrong on our end saving the webhook" banner came up
every time. Root cause: Buildium probes the destination URL with an
UNSIGNED request before saving the subscription (because the secret
hasn't been written on their side yet — nothing to sign with). Our
router 401'd the probe ``Missing signature headers`` → Buildium
concluded the URL was unreachable → aborted save.

PR #79 short-circuits unsigned requests whose body doesn't look like
a real Buildium envelope (no ``AccountId`` + ``EventName``) to
200 OK. Unsigned requests that DO carry a real envelope still 401 —
those are hostile, not probes.

This file pins both branches plus regressions on the signed flow.
"""

from __future__ import annotations

import json
import time
from contextlib import ExitStack
from dataclasses import dataclass
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from helixis.webhooks.router import _looks_like_validation_probe
from helixis.webhooks.router import router as webhooks_router
from helixis.webhooks.signature import compute_signature

SECRET = "whsec_pr79"
ACCOUNT_ID = 4242
COMPANY_ID = "company-pr79"


@dataclass
class _FakeEnqueueResult:
    id: object
    duplicate: bool


@pytest.fixture
def app() -> FastAPI:
    app = FastAPI()
    app.include_router(webhooks_router)
    return app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def _real_envelope(**extra: object) -> bytes:
    payload: dict[str, object] = {
        "EventName": "Lease.Created",
        "EventDateTime": "2026-05-17T19:43:00.0000000Z",
        "AccountId": ACCOUNT_ID,
        "LeaseId": 123,
    }
    payload.update(extra)
    return json.dumps(payload, separators=(",", ":")).encode()


def _signed_headers(body: bytes, secret: str = SECRET) -> dict[str, str]:
    ts = str(int(time.time()))
    sig = compute_signature(secret, ts, body)
    return {
        "buildium-webhook-timestamp": ts,
        "buildium-webhook-signature": sig,
        "Content-Type": "application/json",
    }


def _mock_settings() -> object:
    from helixis.settings import BuildiumSettings, Settings

    s = Settings(
        env="test",
        memory={"enabled": False},
        api={"host": "127.0.0.1", "port": 8000},
    )
    s.buildium = BuildiumSettings(signature_freshness_seconds=3900)
    return s


async def _fake_enqueue(_pool: object, **_kwargs: object) -> _FakeEnqueueResult:
    return _FakeEnqueueResult(id=uuid4(), duplicate=False)


async def _fake_lookup_known(account_id: int) -> tuple[str, str] | None:
    if account_id == ACCOUNT_ID:
        return (COMPANY_ID, SECRET)
    return None


class _RouterPatches:
    """Same shape as ``test_buildium_router._RouterPatches`` — stubs the
    Postgres pool + per-company secret lookup + dispatch/enqueue so
    the probe-path tests don't pull in a DB."""

    def __enter__(self) -> _RouterPatches:
        self._stack = ExitStack()
        self._stack.enter_context(patch("helixis.webhooks.router.get_settings", _mock_settings))
        self._stack.enter_context(patch("helixis.webhooks.router.get_pool", return_value=object()))
        self._stack.enter_context(
            patch(
                "helixis.webhooks.router.load_webhook_secret_by_account_id",
                new=AsyncMock(side_effect=_fake_lookup_known),
            )
        )
        self._stack.enter_context(patch("helixis.webhooks.router.enqueue", new=_fake_enqueue))
        self._stack.enter_context(
            patch("helixis.webhooks.router.dispatch_event", new=AsyncMock(return_value=None))
        )
        self._stack.enter_context(patch("helixis.webhooks.router.mark_done", new=AsyncMock()))
        return self

    def __exit__(self, *exc: object) -> None:
        self._stack.__exit__(*exc)


# ── Probe-path acceptance (the 4 happy cases) ──────────────────────


def test_unsigned_empty_post_returns_200(client: TestClient) -> None:
    """The minimum Buildium-UI probe: empty body, no signature headers.
    Pre-PR-#79 this 401'd; PR #79 makes it 200 so the subscription
    save proceeds."""
    with _RouterPatches():
        resp = client.post("/webhooks/buildium", content=b"")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["message"] == "webhook endpoint live"


def test_unsigned_probe_with_test_body_returns_200(client: TestClient) -> None:
    """Some webhook validation tools send a small probe body like
    ``{"test": true}``. Without signature headers and without an
    ``AccountId`` + ``EventName`` envelope, we still treat it as a
    probe."""
    with _RouterPatches():
        resp = client.post(
            "/webhooks/buildium",
            json={"test": True},
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_unsigned_probe_with_non_json_body_returns_200(client: TestClient) -> None:
    """A plain-text probe body (``hello``) isn't a real envelope —
    treat as probe, 200."""
    with _RouterPatches():
        resp = client.post(
            "/webhooks/buildium",
            content=b"hello",
            headers={"Content-Type": "text/plain"},
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_unsigned_probe_with_account_only_returns_200(client: TestClient) -> None:
    """A JSON body that has AccountId but NO EventName isn't a complete
    Buildium envelope — still treat as a probe. The full-envelope
    rejection (the security case) is covered by
    ``test_unsigned_real_envelope_still_401``."""
    with _RouterPatches():
        resp = client.post(
            "/webhooks/buildium",
            json={"AccountId": 1},
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


# ── Probe-path rejection (the security cases) ──────────────────────


def test_unsigned_real_envelope_still_401(client: TestClient) -> None:
    """**CRITICAL SECURITY TEST.** If a request body looks like a real
    Buildium webhook envelope (carries both ``AccountId`` AND
    ``EventName``) but has NO signature headers, it must still 401.
    Accepting these would let an attacker fire forged events at us;
    Buildium's real traffic always signs."""
    body = _real_envelope()
    with _RouterPatches():
        resp = client.post(
            "/webhooks/buildium",
            content=body,
            headers={"Content-Type": "application/json"},
        )
    assert resp.status_code == 401


def test_unsigned_oversized_body_still_401(client: TestClient) -> None:
    """A 5KB unsigned body is suspicious — real probes are tiny. Fall
    through to the 401 path so we don't silently accept a large
    hostile payload as a probe."""
    big_payload = json.dumps({"junk": "x" * 5000}).encode()
    assert len(big_payload) > 4096
    with _RouterPatches():
        resp = client.post(
            "/webhooks/buildium",
            content=big_payload,
            headers={"Content-Type": "application/json"},
        )
    assert resp.status_code == 401


def test_signed_real_envelope_unchanged_behavior(client: TestClient) -> None:
    """Regression on the happy path: a properly signed real envelope
    still flows through the existing signature + dispatch path and
    returns 200 with the event metadata. PR #79 only short-circuits
    the unsigned probe branch."""
    body = _real_envelope()
    with _RouterPatches():
        resp = client.post(
            "/webhooks/buildium",
            content=body,
            headers=_signed_headers(body),
        )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["event_name"] == "Lease.Created"
    assert payload["duplicate"] is False


def test_signed_with_invalid_signature_still_401(client: TestClient) -> None:
    """A signed request whose signature doesn't verify still 401's.
    PR #79 must not weaken signature checks on the signed branch."""
    body = _real_envelope()
    headers = _signed_headers(body)
    # Corrupt the signature so it can't verify. We flip the first character to
    # a *different* base64 char: the old ``"A" + sig[1:]`` was a no-op roughly
    # 1/64 of the time — whenever the freshly-computed (timestamp-dependent)
    # signature already began with "A" — which left a VALID signature, let the
    # request through to routing, and flaked this 401 assertion into a 200.
    sig = headers["buildium-webhook-signature"]
    headers["buildium-webhook-signature"] = ("B" if sig[0] != "B" else "A") + sig[1:]
    with _RouterPatches():
        resp = client.post("/webhooks/buildium", content=body, headers=headers)
    assert resp.status_code == 401


def test_get_still_405(client: TestClient) -> None:
    """We did NOT add a GET handler. Buildium's spec is POST-only;
    a GET to ``/webhooks/buildium`` continues to 405."""
    resp = client.get("/webhooks/buildium")
    assert resp.status_code == 405


# ── _looks_like_validation_probe unit tests (the helper itself) ────


def test_probe_helper_empty_body_is_probe() -> None:
    assert _looks_like_validation_probe(b"") is True


def test_probe_helper_plain_text_is_probe() -> None:
    assert _looks_like_validation_probe(b"hello world") is True


def test_probe_helper_random_json_object_is_probe() -> None:
    assert _looks_like_validation_probe(b'{"test": true}') is True


def test_probe_helper_account_id_only_is_probe() -> None:
    """Real envelopes need BOTH AccountId and EventName; missing
    either makes it a probe."""
    assert _looks_like_validation_probe(b'{"AccountId": 1}') is True
    assert _looks_like_validation_probe(b'{"EventName": "X"}') is True


def test_probe_helper_full_envelope_is_not_probe() -> None:
    """The critical negation — a real envelope must NOT be classified
    as a probe."""
    body = json.dumps({"AccountId": 42, "EventName": "Lease.Created", "LeaseId": 1}).encode()
    assert _looks_like_validation_probe(body) is False


def test_probe_helper_oversized_body_is_not_probe() -> None:
    """4KB+ unsigned bodies fall through to 401 — never classified
    as probes."""
    big = json.dumps({"junk": "x" * 5000}).encode()
    assert _looks_like_validation_probe(big) is False


def test_probe_helper_event_alias_is_not_probe() -> None:
    """Some Buildium event payloads use ``Event`` instead of
    ``EventName`` (the v1/v2 webhook shape divergence). Accept
    either as the "envelope" marker."""
    body = json.dumps({"AccountId": 42, "Event": "Lease.Created"}).encode()
    assert _looks_like_validation_probe(body) is False


def test_probe_helper_top_level_array_is_probe() -> None:
    """A JSON array (not a dict) can't be a real envelope — Buildium
    always nests fields in a top-level object."""
    assert _looks_like_validation_probe(b"[1, 2, 3]") is True


def test_probe_helper_non_utf8_bytes_is_probe() -> None:
    """Bytes that don't decode as UTF-8 → can't be a real envelope."""
    assert _looks_like_validation_probe(b"\xff\xfe\xfd") is True
