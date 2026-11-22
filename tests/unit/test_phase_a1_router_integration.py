"""End-to-end test: a real-shape Task.Created webhook posted at the
receiver runs through enrichment + planning + storage in one go.

We mount the webhooks router on a minimal FastAPI app, mock the
secret lookup + asyncpg pool + Buildium API factory + LLM completion,
then POST a signed envelope and assert that:
  - the request returns 200,
  - enrichment ran (Buildium calls observed),
  - the planner ran (LLM call observed, surface validated),
  - the queue row was updated via ``store_enrichment`` and marked
    ``done``.

This is the seam tests for Phase A.1 — it's the thing that catches
wiring regressions when a future PR refactors the dispatch path.
"""

from __future__ import annotations

import json
import time
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from helixis.webhooks.router import router as webhooks_router
from helixis.webhooks.signature import compute_signature

SECRET = "whsec_phase_a1"
ACCOUNT_ID = 652932
COMPANY_ID = "5ca1ab1e-0000-4000-8000-000000000004"


@dataclass
class _FakeEnqueueResult:
    id: Any
    duplicate: bool


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(webhooks_router)
    return TestClient(app)


def _settings():
    from helixis.settings import BuildiumSettings, Settings

    s = Settings(
        env="test",
        memory={"enabled": False},
        api={"host": "127.0.0.1", "port": 8000},
        llm={
            "default_model": "gemini/gemini-2.0-flash",
            "google_api_key": "fake-key",
            "models": {"reasoning": {"model": "gemini/gemini-2.0-flash"}},
        },
    )
    s.buildium = BuildiumSettings(signature_freshness_seconds=3900)
    return s


def _signed_headers(body: bytes) -> dict[str, str]:
    ts = str(int(time.time()))
    sig = compute_signature(SECRET, ts, body)
    return {
        "buildium-webhook-timestamp": ts,
        "buildium-webhook-signature": sig,
        "Content-Type": "application/json",
    }


async def _fake_enqueue(_pool: Any, **_kwargs: Any) -> _FakeEnqueueResult:
    return _FakeEnqueueResult(id=uuid4(), duplicate=False)


async def _fake_lookup(account_id: int):
    return (COMPANY_ID, SECRET) if account_id == ACCOUNT_ID else None


# ── Fake Buildium API (4 endpoints) ────────────────────────────


class _FakeClient:
    def __init__(self, responses: dict[tuple[str, str], Any]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, str]] = []

    async def request(
        self,
        method: str,
        path: str,
        *,
        query: Any = None,
        body: Any = None,
        schema: Any = None,
    ) -> Any:
        self.calls.append((method, path))
        return self._responses.get((method, path), [])


class _FakeApi:
    def __init__(self, client: _FakeClient) -> None:
        self.client = client


# ── Fake LLM ───────────────────────────────────────────────────


_LLM_RESPONSE = {
    "summary": (
        "Tenant Mei Chen reported a kitchen sink leak at 1208 Oak St "
        "(unit 3B); the task is New at Normal priority."
    ),
    "actions": [
        {
            "tool_id": "update_buildium_task",
            "label": "Raise to High priority",
            "intent": "Raise priority to High",
            "parameters": {"priority": "High"},
        },
        {
            "tool_id": "send_tenant_message",
            "label": "Confirm window with Mei",
            "intent": "Ask Mei to confirm a 2-hour service window tomorrow",
            "parameters": {
                "tenant_id": 9001,
                "subject": "Quick check on the sink leak",
                "body": "Hi Mei — can we send a plumber tomorrow between 9-11am?",
            },
        },
    ],
    "freeform_placeholder": "e.g. 'send Eli Plumbing tomorrow afternoon'",
}


@dataclass
class _FakeMessage:
    content: str


@dataclass
class _FakeChoice:
    message: _FakeMessage


@dataclass
class _FakeResponse:
    choices: list[_FakeChoice]


async def _fake_completion(**_kwargs: Any) -> _FakeResponse:
    _fake_completion.calls += 1  # type: ignore[attr-defined]
    return _FakeResponse(
        choices=[_FakeChoice(message=_FakeMessage(content=json.dumps(_LLM_RESPONSE)))]
    )


_fake_completion.calls = 0  # type: ignore[attr-defined]


# ── Test ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_task_created_webhook_enriches_plans_and_stores(client: TestClient) -> None:
    body = json.dumps(
        {
            "EventName": "Task.Created",
            "EventDateTime": "2026-05-12T23:39:58Z",
            "AccountId": ACCOUNT_ID,
            "TaskId": 4001,
        },
        separators=(",", ":"),
    ).encode()

    fake_client = _FakeClient(
        {
            ("GET", "/tasks/4001"): {
                "Id": 4001,
                "Title": "Kitchen sink leaking",
                "Description": "Tenant reports drip under the sink.",
                "Status": "New",
                "Category": {"Name": "Plumbing"},
                "Priority": "Normal",
                "RequestedByUserEntity": {
                    "Id": 9001,
                    "FirstName": "Mei",
                    "LastName": "Chen",
                },
                "PropertyId": 31,
                "UnitId": 412,
            },
            ("GET", "/rentals/units/412"): {
                "Id": 412,
                "PropertyId": 31,
                "UnitNumber": "3B",
                "Address": {"AddressLine1": "1208 Oak St"},
                "MarketRent": 2200.0,
            },
            ("GET", "/leases"): [
                {
                    "Id": 7771,
                    "LeaseStatus": "Active",
                    "UnitId": 412,
                    "CurrentTenants": [
                        {
                            "Id": 9001,
                            "FirstName": "Mei",
                            "LastName": "Chen",
                            "Email": "mei@example.com",
                        }
                    ],
                }
            ],
            ("GET", "/tasks"): [
                {"Id": 4001, "Title": "Kitchen sink leaking", "Status": "New"},
            ],
        }
    )

    @asynccontextmanager
    async def fake_factory(_company_id: str):
        yield _FakeApi(fake_client)

    # Build a one-off enrichment handler bound to the fake api +
    # fake completion and substitute it into the registry's
    # ``get_enrichment_handler`` for the duration of the test.
    from helixis.agent.enrichment import BuildiumTaskEnricher
    from helixis.agent.planner import ActionPlanner
    from helixis.webhooks.handlers.enrichment_registry import EnrichmentHandler

    handler = EnrichmentHandler(
        enricher=BuildiumTaskEnricher(api_factory=fake_factory),
        planner=ActionPlanner(_settings(), completion_fn=_fake_completion),
    )

    # PR #89 — the receiver no longer calls the planner inline. We
    # assert enrichment still runs (PR #83 contract — receiver
    # persists enrichment + deterministic floor before returning
    # 200), and we assert the planner is NOT invoked here. The LLM
    # plan happens later in the worker; coalesce_action_surface lives
    # there now and is exercised by tests/unit/test_pr89_planner_worker.py.
    store_routing_mock = AsyncMock()
    mark_done_mock = AsyncMock()
    dispatch_mock = AsyncMock(return_value=None)
    routing_decision_mock = MagicMock()
    routing_decision_mock.action_type = "assign_to_user"
    routing_decision_mock.assigned_buildium_user_id = None
    routing_decision_mock.assigned_helixis_user_id = None
    routing_decision_mock.surface = "inbox"
    routing_decision_mock.reason = "stub"

    # Reset the planner's completion call counter so we can assert
    # zero calls below (the counter is a module-level fixture so it
    # carries state across tests in this file).
    _fake_completion.calls = 0  # type: ignore[attr-defined]

    with ExitStack() as stack:
        stack.enter_context(patch("helixis.webhooks.router.get_settings", _settings))
        stack.enter_context(patch("helixis.webhooks.router.get_pool", return_value=object()))
        stack.enter_context(
            patch(
                "helixis.webhooks.router.load_webhook_secret_by_account_id",
                new=AsyncMock(side_effect=_fake_lookup),
            )
        )
        stack.enter_context(patch("helixis.webhooks.router.enqueue", new=_fake_enqueue))
        stack.enter_context(patch("helixis.webhooks.router.dispatch_event", new=dispatch_mock))
        stack.enter_context(patch("helixis.webhooks.router.mark_done", new=mark_done_mock))
        stack.enter_context(
            patch(
                "helixis.webhooks.router.route_event",
                new=AsyncMock(return_value=routing_decision_mock),
            )
        )
        stack.enter_context(
            patch(
                "helixis.webhooks.router.load_staff_cache",
                new=AsyncMock(return_value={}),
            )
        )
        stack.enter_context(
            patch(
                "helixis.webhooks.router.store_routing_decision",
                new=store_routing_mock,
            )
        )
        stack.enter_context(
            patch(
                "helixis.webhooks.router.get_enrichment_handler",
                new=lambda event_name: handler if event_name == "Task.Created" else None,
            )
        )

        resp = client.post(
            "/webhooks/buildium",
            content=body,
            headers=_signed_headers(body),
        )

    assert resp.status_code == 200
    assert resp.json()["event_name"] == "Task.Created"

    # Enrichment ran — observed Buildium calls (task + unit +
    # leases + 2x history = 5 minimum).
    paths = [p for _, p in fake_client.calls]
    assert "/tasks/4001" in paths
    assert "/rentals/units/412" in paths
    assert "/leases" in paths
    assert paths.count("/tasks") >= 2  # unit-history + property-history

    # PR #89: planner did NOT run in the receiver. The worker will
    # claim this row and run the planner later. Exercised in
    # tests/unit/test_pr89_planner_worker.py.
    assert _fake_completion.calls == 0  # type: ignore[attr-defined]

    # The deterministic surface still lands on the row via
    # store_routing_decision, with the EnrichedEvent attached.
    store_routing_mock.assert_awaited_once()
    store_kwargs = store_routing_mock.await_args.kwargs
    assert store_kwargs["enrichment"] is not None
    assert store_kwargs["enrichment"]["task"]["id"] == 4001
    assert store_kwargs["enrichment"]["company_id"] == COMPANY_ID
    assert store_kwargs["deterministic_surface"] is not None
    assert "summary" in store_kwargs["deterministic_surface"]
    assert isinstance(store_kwargs["deterministic_surface"]["actions"], list)

    # Both enrichment and legacy dispatch succeeded → row marked done.
    mark_done_mock.assert_awaited_once()
    dispatch_mock.assert_awaited_once()
