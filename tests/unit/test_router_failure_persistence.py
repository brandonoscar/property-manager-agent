"""Receiver-side enrichment/planner failure persistence (PR #69).

Audit Finding #5: two prod ``Task.Created`` rows reached
``status='done'`` with ``action_surface=NULL`` and ``last_error=NULL``
— silent planner failures. The receiver's ``except Exception`` only
logged the exception and set a local ``enrichment_ok = False`` flag,
so the row had no diagnostic trail.

PR #69 added ``mark_enrichment_failed`` (a new queue helper) and
wires it from the receiver's except block. The row now ends up
``status='pending'``, ``last_error=<repr(exc)>``, ``attempts+=1``,
``available_at = now() + 1 minute``.

These tests pin:
1. The queue helper's exact UPDATE shape (no SQL drift).
2. The router behaviour: when enrichment raises, ``mark_enrichment_failed``
   is called with the right kwargs AND ``mark_done`` is not.
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

from helixis.webhooks.queue import mark_enrichment_failed
from helixis.webhooks.router import router as webhooks_router
from helixis.webhooks.signature import compute_signature

SECRET = "whsec_pr69"
ACCOUNT_ID = 652932
COMPANY_ID = "5ca1ab1e-0000-4000-8000-000000000004"


# ── Helper #1 — queue function direct test ──────────────────────


@pytest.mark.asyncio
async def test_mark_enrichment_failed_executes_expected_update() -> None:
    """Assert the exact SQL + parameters the helper sends to asyncpg.
    Locks the shape so a future refactor can't silently change the
    behaviour (e.g. drop the attempts increment, change to status=done,
    skip the available_at bump)."""
    event_id = uuid4()
    error = "RuntimeError('planner exhausted retries')"

    captured: dict[str, Any] = {}

    class _FakeConn:
        async def execute(self, sql: str, *args: Any) -> None:
            captured["sql"] = sql
            captured["args"] = args

    @asynccontextmanager
    async def _acquire():
        yield _FakeConn()

    fake_pool = MagicMock()
    fake_pool.acquire = _acquire

    await mark_enrichment_failed(fake_pool, event_id, error=error)

    sql = captured["sql"]
    args = captured["args"]

    # SQL must hit buildium_events, leave status pending, persist
    # last_error, increment attempts, and bump available_at.
    assert "UPDATE buildium_events" in sql
    assert "status       = 'pending'" in sql
    assert "last_error   = $2" in sql
    assert "attempts     = attempts + 1" in sql
    assert "available_at = now() + interval '1 minute'" in sql
    assert "WHERE id = $1" in sql

    # Args: id then error in that exact order — matches the $1/$2 in SQL.
    assert args == (event_id, error)


# ── Helper #2 — full router integration ──────────────────────────


@dataclass
class _FakeEnqueueResult:
    id: Any
    duplicate: bool


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(webhooks_router)
    return TestClient(app)


def _mock_settings():
    from helixis.settings import BuildiumSettings, Settings

    s = Settings(
        env="test",
        memory={"enabled": False},
        api={"host": "127.0.0.1", "port": 8000},
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


class _FailingHandler:
    """Stand-in for ``EnrichmentHandler`` whose enricher raises.

    Mirrors the prod failure mode that left two rows silently broken
    (the planner raised after exhausting transient retries; we don't
    care which exception type — the receiver's catch is broad).

    PR #83 — router now calls ``.enricher.enrich()`` and
    ``.planner.plan()`` separately. This handler raises on enrich,
    which surfaces in the same ``mark_enrichment_failed`` path the
    legacy ``handler.run()`` raise used to.
    """

    class _RaisingEnricher:
        async def enrich(self, _event: dict[str, Any], _company_id: str) -> Any:
            raise RuntimeError("planner exhausted transient retries: simulated 502")

    class _UnusedPlanner:
        async def plan(self, *_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("planner should not be reached when enricher raises")

    enricher = _RaisingEnricher()
    planner = _UnusedPlanner()

    async def run(self, _event: dict[str, Any], _company_id: str) -> Any:
        # Kept for legacy callers; PR #83's router doesn't call this.
        raise RuntimeError("planner exhausted transient retries: simulated 502")


def test_enrichment_failure_persists_last_error_and_leaves_row_pending(
    client: TestClient,
) -> None:
    """Closes PR #69's silent-failure gap.

    Acceptance:
    * Receiver still returns 200 (Buildium SLA preserved).
    * ``mark_enrichment_failed`` is awaited with the row id and an
      ``error`` kwarg starting with the exception's repr.
    * ``mark_done`` is NOT awaited — the row must stay pending so the
      worker can retry on its next pass.
    """
    body = json.dumps(
        {
            "EventName": "Task.Created",
            "EventDateTime": "2026-05-16T18:00:00Z",
            "AccountId": ACCOUNT_ID,
            "TaskId": 99999,
        },
        separators=(",", ":"),
    ).encode()

    mark_failed_mock = AsyncMock()
    mark_done_mock = AsyncMock()
    dispatch_mock = AsyncMock(return_value=None)
    # PR #89 — receiver no longer calls coalesce_action_surface. The
    # planner lives in the worker now; the receiver only writes the
    # deterministic surface (via store_routing_decision) before
    # returning 200. Pre-PR-#89 we patched coalesce here to assert it
    # wasn't called when enrichment failed; with the LLM branch gone
    # there's nothing to patch.
    from unittest.mock import MagicMock

    routing_decision_mock = MagicMock()
    routing_decision_mock.action_type = "assign_to_user"
    routing_decision_mock.assigned_buildium_user_id = None
    routing_decision_mock.assigned_helixis_user_id = None
    routing_decision_mock.surface = "inbox"
    routing_decision_mock.reason = "stub"

    with ExitStack() as stack:
        stack.enter_context(patch("helixis.webhooks.router.get_settings", _mock_settings))
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
                new=AsyncMock(),
            )
        )
        stack.enter_context(
            patch(
                "helixis.webhooks.router.mark_enrichment_failed",
                new=mark_failed_mock,
            )
        )
        stack.enter_context(
            patch(
                "helixis.webhooks.router.get_enrichment_handler",
                new=lambda event_name: _FailingHandler() if event_name == "Task.Created" else None,
            )
        )

        resp = client.post(
            "/webhooks/buildium",
            content=body,
            headers=_signed_headers(body),
        )

    # SLA preserved.
    assert resp.status_code == 200

    # Failure was persisted — exactly the diagnostic trail the audit
    # said was missing.
    mark_failed_mock.assert_awaited_once()
    kwargs = mark_failed_mock.await_args.kwargs
    assert "error" in kwargs
    assert "planner exhausted transient retries" in kwargs["error"]
    # The error string is repr(exc) per router.py — must include the
    # exception class name so future audits can group failures.
    assert kwargs["error"].startswith("RuntimeError(")

    # Row must stay pending for the worker to retry. PR #89 — the
    # planner overlay branch (which used to be the reason coalesce
    # might or might not fire here) is gone entirely; the receiver
    # only writes the deterministic surface via store_routing_decision.
    mark_done_mock.assert_not_awaited()


def test_mark_done_failure_still_returns_200(client: TestClient) -> None:
    """A transient DB failure on the FINAL mark_done UPDATE must not
    500 the receiver: the event is already durably enqueued and fully
    processed, and every non-2xx burns Buildium retry budget (20
    consecutive suspend the subscription). The row simply stays
    ``pending`` and the worker re-runs the cheap legacy dispatch."""
    body = json.dumps(
        {
            "EventName": "Task.Created",
            "EventDateTime": "2026-05-16T18:00:00Z",
            "AccountId": ACCOUNT_ID,
            "TaskId": 4242,
        },
        separators=(",", ":"),
    ).encode()

    mark_done_mock = AsyncMock(side_effect=RuntimeError("db blip on final update"))

    with ExitStack() as stack:
        stack.enter_context(patch("helixis.webhooks.router.get_settings", _mock_settings))
        stack.enter_context(patch("helixis.webhooks.router.get_pool", return_value=object()))
        stack.enter_context(
            patch(
                "helixis.webhooks.router.load_webhook_secret_by_account_id",
                new=AsyncMock(side_effect=_fake_lookup),
            )
        )
        stack.enter_context(patch("helixis.webhooks.router.enqueue", new=_fake_enqueue))
        stack.enter_context(
            patch("helixis.webhooks.router.dispatch_event", new=AsyncMock(return_value=None))
        )
        stack.enter_context(patch("helixis.webhooks.router.mark_done", new=mark_done_mock))
        stack.enter_context(
            patch(
                "helixis.webhooks.router.route_event",
                new=AsyncMock(side_effect=RuntimeError("skip routing for this test")),
            )
        )
        # No enrichment handler — keeps the test focused on mark_done.
        stack.enter_context(
            patch("helixis.webhooks.router.get_enrichment_handler", new=lambda _: None)
        )

        resp = client.post(
            "/webhooks/buildium",
            content=body,
            headers=_signed_headers(body),
        )

    assert resp.status_code == 200
    mark_done_mock.assert_awaited_once()
