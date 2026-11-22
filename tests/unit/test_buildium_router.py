"""Integration test for the Buildium webhook router.

We mount just the webhook router on a minimal FastAPI app and stub the
Postgres pool + the per-company secret lookup so the whole path runs
without a database. The signing secret is now per-company (not a single
global env var) so the tests mock ``load_webhook_secret_by_account_id``
to return a fixed pair.
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

from helixis.webhooks.router import router as webhooks_router
from helixis.webhooks.signature import compute_signature

SECRET = "whsec_test"
ACCOUNT_ID = 42
COMPANY_ID = "company-A"


@dataclass
class FakeEnqueueResult:
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


def _make_body(**extra: object) -> bytes:
    payload: dict[str, object] = {
        "EventName": "Lease.Created",
        "EventDateTime": "2026-04-20T19:43:00.0000000Z",
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


def _mock_settings():
    from helixis.settings import BuildiumSettings, Settings

    s = Settings(
        env="test",
        memory={"enabled": False},
        api={"host": "127.0.0.1", "port": 8000},
    )
    s.buildium = BuildiumSettings(signature_freshness_seconds=3900)
    return s


async def _fake_enqueue(_pool, **_kwargs):
    return FakeEnqueueResult(id=uuid4(), duplicate=False)


async def _fake_lookup_known(account_id: int):
    if account_id == ACCOUNT_ID:
        return (COMPANY_ID, SECRET)
    return None


class _RouterPatches:
    """Bundle the router-side patches so each test can adjust just one
    knob without restating the whole stack."""

    def __init__(
        self,
        *,
        lookup=_fake_lookup_known,
        enqueue=_fake_enqueue,
        dispatch=None,
    ) -> None:
        self.lookup = lookup
        self.enqueue = enqueue
        self.dispatch = dispatch if dispatch is not None else AsyncMock(return_value=None)
        self.mark_done = AsyncMock()
        self._stack: ExitStack | None = None

    def __enter__(self):
        self._stack = ExitStack()
        self._stack.enter_context(patch("helixis.webhooks.router.get_settings", _mock_settings))
        self._stack.enter_context(patch("helixis.webhooks.router.get_pool", return_value=object()))
        self._stack.enter_context(
            patch(
                "helixis.webhooks.router.load_webhook_secret_by_account_id",
                new=AsyncMock(side_effect=self.lookup),
            )
        )
        self._stack.enter_context(patch("helixis.webhooks.router.enqueue", new=self.enqueue))
        self._stack.enter_context(
            patch("helixis.webhooks.router.dispatch_event", new=self.dispatch)
        )
        self._stack.enter_context(patch("helixis.webhooks.router.mark_done", new=self.mark_done))
        # Router tests exercise the ROUTING pipeline, not enrichment: the
        # registry now serves handlers for WorkOrder.*/Applicant.*/Task.*
        # (2026-07-07), and a live enricher would hit real credentials /
        # the planner LLM from inside the request. Stub it to None here;
        # enricher behavior is pinned in test_related_enrichers.py.
        self._stack.enter_context(
            patch("helixis.webhooks.router.get_enrichment_handler", return_value=None)
        )
        return self

    def __exit__(self, *exc):
        assert self._stack is not None
        self._stack.__exit__(*exc)


def test_valid_webhook_returns_200_and_dispatches(client: TestClient) -> None:
    body = _make_body()
    with _RouterPatches() as p:
        resp = client.post(
            "/webhooks/buildium",
            content=body,
            headers=_signed_headers(body),
        )
        assert resp.status_code == 200
        body_json = resp.json()
        assert body_json["event_name"] == "Lease.Created"
        assert body_json["duplicate"] is False
        p.dispatch.assert_awaited_once()
        args = p.dispatch.await_args.args
        assert args[0] == COMPANY_ID
        assert args[1] == "Lease.Created"
        # Successful sync dispatch marks the queue row done.
        p.mark_done.assert_awaited_once()


def test_missing_signature_returns_401(client: TestClient) -> None:
    body = _make_body()
    with _RouterPatches():
        resp = client.post(
            "/webhooks/buildium",
            content=body,
            headers={"Content-Type": "application/json"},
        )
    assert resp.status_code == 401


def test_tampered_body_returns_401(client: TestClient) -> None:
    body = _make_body()
    headers = _signed_headers(body)
    tampered = body.replace(b"123", b"999")
    with _RouterPatches():
        resp = client.post("/webhooks/buildium", content=tampered, headers=headers)
    assert resp.status_code == 401


def test_unknown_account_id_returns_200_silently(client: TestClient) -> None:
    """Per spec we'd rather acknowledge an event for an account we don't
    recognise than burn Buildium's retry budget on it. The response
    carries ``status: unknown_account`` so ops can distinguish real
    successes from absorbed unknowns.

    Audit Finding #10: also asserts the PostHog
    ``webhook_dropped_unknown_account`` metric fires, so misconfigured
    customers surface in analytics instead of disappearing silently."""
    body = _make_body(AccountId=9999)

    async def lookup(_aid: int):
        return None

    with (
        _RouterPatches(lookup=lookup) as p,
        patch("helixis.webhooks.router.capture") as capture_mock,
    ):
        resp = client.post(
            "/webhooks/buildium",
            content=body,
            headers=_signed_headers(body),
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "unknown_account"
        p.dispatch.assert_not_awaited()
        capture_mock.assert_called_once_with(
            distinct_id="system",
            event="webhook_dropped_unknown_account",
            properties={
                "account_id": 9999,
                "event_name": "Lease.Created",
                "source": "buildium_webhook",
            },
        )


def test_duplicate_event_still_returns_200(client: TestClient) -> None:
    body = _make_body()

    async def fake_enqueue_duplicate(_pool, **_kwargs):
        return FakeEnqueueResult(id=uuid4(), duplicate=True)

    with _RouterPatches(enqueue=fake_enqueue_duplicate) as p:
        resp = client.post(
            "/webhooks/buildium",
            content=body,
            headers=_signed_headers(body),
        )
        assert resp.status_code == 200
        assert resp.json()["duplicate"] is True
        # Duplicates skip mark_done — the original was already processed.
        p.mark_done.assert_not_awaited()


def test_handler_crash_is_swallowed_and_returns_200(client: TestClient) -> None:
    """If a synchronous handler raises, we MUST return 200 anyway —
    Buildium suspends subscriptions after 20 consecutive non-2xx
    responses. The queue row stays pending so the worker can retry."""
    body = _make_body()
    dispatch = AsyncMock(side_effect=RuntimeError("handler explosion"))
    with _RouterPatches(dispatch=dispatch) as p:
        resp = client.post(
            "/webhooks/buildium",
            content=body,
            headers=_signed_headers(body),
        )
        assert resp.status_code == 200
        dispatch.assert_awaited_once()
        # Crashed handler → row stays pending, mark_done NOT called.
        p.mark_done.assert_not_awaited()


def test_non_allowlisted_event_skips_routing_but_acks(client: TestClient) -> None:
    """An event outside the Inbox allowlist (``OwnershipAccountTransaction``
    — owner-ledger bookkeeping) must skip the PR #82/#83 routing+surface
    pipeline: no surface is built (no misleading admin-assignment), but
    the event is still enqueued for audit, dispatched, marked done, and
    200'd."""
    body = _make_body(EventName="OwnershipAccountTransaction.Created", TransactionId=2034723)
    route_event = AsyncMock()
    with (
        _RouterPatches() as p,
        patch("helixis.webhooks.router.route_event", new=route_event),
    ):
        resp = client.post(
            "/webhooks/buildium",
            content=body,
            headers=_signed_headers(body),
        )
        assert resp.status_code == 200
        assert resp.json()["event_name"] == "OwnershipAccountTransaction.Created"
        # Routing/surface pipeline is skipped for non-allowlisted events…
        route_event.assert_not_awaited()
        # …but the legacy dispatch still runs and the row is marked done.
        p.dispatch.assert_awaited_once()
        p.mark_done.assert_awaited_once()


def test_allowlisted_event_runs_routing(client: TestClient) -> None:
    """An allowlisted event (``WorkOrder.Created``) DOES run the
    routing+surface pipeline so it lights up the Inbox."""
    body = _make_body(EventName="WorkOrder.Created", WorkOrderId=555)
    route_event = AsyncMock()
    with (
        _RouterPatches() as p,
        patch("helixis.webhooks.router.route_event", new=route_event),
        patch("helixis.webhooks.router.store_routing_decision", new=AsyncMock()),
        patch("helixis.webhooks.router.load_staff_cache", new=AsyncMock(return_value={})),
        patch("helixis.webhooks.router.build_action_surface_from_routing", return_value={}),
    ):
        resp = client.post(
            "/webhooks/buildium",
            content=body,
            headers=_signed_headers(body),
        )
        assert resp.status_code == 200
        route_event.assert_awaited_once()
        p.mark_done.assert_awaited_once()


def test_stale_timestamp_within_buildium_retry_window_is_accepted(client: TestClient) -> None:
    """Buildium's third retry is 1 hour after the original send. The
    default 3900s freshness window covers that."""
    body = _make_body()
    # Sign with a 50-minute-old timestamp; should still verify.
    ts = str(int(time.time() - 50 * 60))
    sig = compute_signature(SECRET, ts, body)
    headers = {
        "buildium-webhook-timestamp": ts,
        "buildium-webhook-signature": sig,
        "Content-Type": "application/json",
    }
    with _RouterPatches():
        resp = client.post("/webhooks/buildium", content=body, headers=headers)
    assert resp.status_code == 200
