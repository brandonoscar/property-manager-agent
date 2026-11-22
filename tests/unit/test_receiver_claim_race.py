"""Receiver-claims-at-enqueue — the double-dispatch race fix.

Live-observed on applicant 5045 (2026-07-05): the receiver enqueued the row
``pending``, then ran the handler inline for ~2.5s (mirror handlers fetch
from Buildium); the worker's poll claimed the still-pending row mid-flight
and ran the SAME handler in parallel → duplicate activity lines + doubled
Buildium GETs. The queue itself was innocent (attempts=1, one claimant) —
the race was between the receiver's inline pass and the worker's poll.

Fix contract pinned here:
1. ``enqueue(claim_by=...)`` inserts the fresh row already ``running`` so
   the worker's ``status='pending'`` filter never sees it.
2. ``release_claim`` hands a running row back to pending — and ONLY a
   running row (a done row can never be demoted back into the queue).
3. The receiver: claims at enqueue, marks done on success, releases on
   inline-dispatch failure, and SKIPS inline dispatch entirely for
   redeliveries (duplicate=True).
"""

from __future__ import annotations

import json
import time
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from helixis.webhooks.queue import enqueue, release_claim
from helixis.webhooks.router import router as webhooks_router
from helixis.webhooks.signature import compute_signature

SECRET = "whsec_claimrace"
ACCOUNT_ID = 652932
COMPANY_ID = "5ca1ab1e-0000-4000-8000-000000000004"

#: ``claim_by`` is ``$8`` in the enqueue INSERT — 0-indexed, args[7].
CLAIM_ARG = 7


# ── queue helpers: exact SQL shape ───────────────────────────────


class _CapturingConn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any]:
        self.calls.append((sql, args))
        return {"id": uuid4()}

    async def execute(self, sql: str, *args: Any) -> None:
        self.calls.append((sql, args))


def _pool_with(conn: _CapturingConn) -> MagicMock:
    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    return pool


@pytest.mark.asyncio
async def test_enqueue_claim_by_inserts_claimed_running_row() -> None:
    conn = _CapturingConn()
    result = await enqueue(
        _pool_with(conn),
        event_name="Applicant.Created",
        event_time=datetime.now(UTC),
        account_id=ACCOUNT_ID,
        entity_ids={"ApplicantId": 5045},
        payload={"EventName": "Applicant.Created", "ApplicantId": 5045},
        company_id=COMPANY_ID,
        claim_by="receiver",
    )
    assert not result.duplicate
    sql, args = conn.calls[0]
    # The insert must set status/claimed_at/claimed_by from the claim arg.
    assert "status, claimed_at, claimed_by" in sql
    assert "ELSE 'running'" in sql
    # ⚠ Address the claim arg by its PLACEHOLDER ($8 → args[7]), never as
    # args[-1]. "Last" was only ever incidentally true: #0080 appended a $9
    # (inbox eligibility) and both these assertions started reading a bool.
    assert args[CLAIM_ARG] == "receiver"


@pytest.mark.asyncio
async def test_enqueue_without_claim_stays_pending() -> None:
    conn = _CapturingConn()
    await enqueue(
        _pool_with(conn),
        event_name="Applicant.Created",
        event_time=datetime.now(UTC),
        account_id=ACCOUNT_ID,
        entity_ids={},
        payload={"EventName": "Applicant.Created", "ApplicantId": 1},
        company_id=COMPANY_ID,
    )
    _sql, args = conn.calls[0]
    # No claim → NULL claim arg → the CASE resolves to 'pending'.
    assert args[CLAIM_ARG] is None


@pytest.mark.asyncio
async def test_release_claim_only_demotes_running_rows() -> None:
    conn = _CapturingConn()
    await release_claim(_pool_with(conn), uuid4())
    sql, _args = conn.calls[0]
    assert "SET status = 'pending'" in sql
    assert "claimed_at = NULL" in sql
    assert "claimed_by = NULL" in sql
    # The guard: a done row must never be demoted back into the queue.
    assert "status = 'running'" in sql


# ── router: claim lifecycle end-to-end ───────────────────────────


@dataclass
class _FakeEnqueueResult:
    id: UUID
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


async def _fake_lookup(account_id: int):
    return (COMPANY_ID, SECRET) if account_id == ACCOUNT_ID else None


def _post_event(
    client: TestClient,
    *,
    duplicate: bool,
    dispatch_mock: AsyncMock,
) -> tuple[Any, AsyncMock, AsyncMock, AsyncMock]:
    body = json.dumps(
        {
            "EventName": "Applicant.Created",
            "EventDateTime": "2026-07-05T21:30:03Z",
            "AccountId": ACCOUNT_ID,
            "ApplicantId": 5045,
        },
        separators=(",", ":"),
    ).encode()

    enqueue_mock = AsyncMock(return_value=_FakeEnqueueResult(id=uuid4(), duplicate=duplicate))
    mark_done_mock = AsyncMock()
    release_mock = AsyncMock()

    with ExitStack() as stack:
        stack.enter_context(patch("helixis.webhooks.router.get_settings", _mock_settings))
        stack.enter_context(patch("helixis.webhooks.router.get_pool", return_value=object()))
        stack.enter_context(
            patch(
                "helixis.webhooks.router.load_webhook_secret_by_account_id",
                new=AsyncMock(side_effect=_fake_lookup),
            )
        )
        stack.enter_context(patch("helixis.webhooks.router.enqueue", new=enqueue_mock))
        stack.enter_context(patch("helixis.webhooks.router.dispatch_event", new=dispatch_mock))
        stack.enter_context(patch("helixis.webhooks.router.mark_done", new=mark_done_mock))
        stack.enter_context(patch("helixis.webhooks.router.release_claim", new=release_mock))
        stack.enter_context(
            patch("helixis.webhooks.router.get_enrichment_handler", new=lambda _n: None)
        )

        resp = client.post(
            "/webhooks/buildium",
            content=body,
            headers=_signed_headers(body),
        )

    return resp, enqueue_mock, mark_done_mock, release_mock


def test_receiver_claims_at_enqueue_and_marks_done_on_success(client: TestClient) -> None:
    dispatch_mock = AsyncMock(return_value=None)
    resp, enqueue_mock, mark_done_mock, release_mock = _post_event(
        client, duplicate=False, dispatch_mock=dispatch_mock
    )
    assert resp.status_code == 200
    # The row is inserted ALREADY CLAIMED so the worker can't double-run it.
    assert enqueue_mock.await_args.kwargs["claim_by"] == "receiver"
    dispatch_mock.assert_awaited_once()
    mark_done_mock.assert_awaited_once()
    release_mock.assert_not_awaited()


def test_receiver_releases_claim_when_inline_dispatch_fails(client: TestClient) -> None:
    dispatch_mock = AsyncMock(side_effect=RuntimeError("handler crashed"))
    resp, _enqueue_mock, mark_done_mock, release_mock = _post_event(
        client, duplicate=False, dispatch_mock=dispatch_mock
    )
    # SLA preserved, claim handed back to the worker for retry.
    assert resp.status_code == 200
    mark_done_mock.assert_not_awaited()
    release_mock.assert_awaited_once()


def test_redelivery_skips_inline_dispatch_entirely(client: TestClient) -> None:
    """A Buildium redelivery must not re-run the handler inline — the
    original delivery either finished (row done) or is with the worker."""
    dispatch_mock = AsyncMock(return_value=None)
    resp, _enqueue_mock, mark_done_mock, release_mock = _post_event(
        client, duplicate=True, dispatch_mock=dispatch_mock
    )
    assert resp.status_code == 200
    dispatch_mock.assert_not_awaited()
    mark_done_mock.assert_not_awaited()
    release_mock.assert_not_awaited()
