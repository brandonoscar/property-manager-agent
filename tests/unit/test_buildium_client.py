"""Unit tests for the Buildium HTTP client.

Uses httpx MockTransport so we never make real network calls.
"""

from __future__ import annotations

import httpx
import pytest

from helixis.buildium.client import BuildiumClient
from helixis.buildium.errors import BuildiumApiError, QueryTooLargeError
from helixis.buildium.schemas import WorkOrder
from helixis.settings import BuildiumSettings


def _settings(**overrides) -> BuildiumSettings:
    base = {
        "base_url": "https://apisandbox.buildium.com",
        "client_id": "cid",
        "client_secret": "csecret",
        "webhook_secret": "whsec",
        "account_id": 1,
        "concurrency": 4,
    }
    base.update(overrides)
    return BuildiumSettings(**base)


def _client(transport: httpx.MockTransport) -> BuildiumClient:
    settings = _settings()
    http = httpx.AsyncClient(
        base_url=settings.base_url.rstrip("/") + "/v1",
        transport=transport,
        headers={
            "x-buildium-client-id": settings.client_id,
            "x-buildium-client-secret": settings.client_secret,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    return BuildiumClient(settings, http_client=http)


async def test_injects_auth_headers() -> None:
    captured: dict[str, str] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["client-id"] = request.headers.get("x-buildium-client-id", "")
        captured["client-secret"] = request.headers.get("x-buildium-client-secret", "")
        return httpx.Response(200, json={"ok": True})

    async with _client(httpx.MockTransport(handler)) as client:
        await client.request("GET", "/ping")

    assert captured["client-id"] == "cid"
    assert captured["client-secret"] == "csecret"


async def test_retries_on_429_then_succeeds() -> None:
    attempts = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(
            200,
            json={
                "Id": 1,
                "PropertyId": 10,
                "UnitId": 20,
                "Title": "HVAC",
                "Status": "New",
                "Priority": "High",
            },
        )

    async with _client(httpx.MockTransport(handler)) as client:
        result = await client.request("GET", "/workorders/1", schema=WorkOrder)

    assert attempts["n"] == 3
    assert isinstance(result, WorkOrder)
    assert result.id == 1


async def test_retries_on_503_then_gives_up_with_api_error() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="boom")

    async with _client(httpx.MockTransport(handler)) as client:
        with pytest.raises(BuildiumApiError) as exc_info:
            await client.request("GET", "/workorders/1")

    assert exc_info.value.status == 503


async def test_non_retriable_error_raises_immediately() -> None:
    attempts = {"n": 0}

    async def handler(_request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(404, json={"Message": "not found"})

    async with _client(httpx.MockTransport(handler)) as client:
        with pytest.raises(BuildiumApiError) as exc_info:
            await client.request("GET", "/workorders/1")

    assert attempts["n"] == 1
    assert exc_info.value.status == 404


async def test_malformed_json_body_raises_api_error_not_valueerror() -> None:
    """A 2xx with a non-JSON body must surface as a BuildiumApiError (which the
    agent tools catch and degrade on), NOT a raw ValueError/JSONDecodeError that
    escapes request() and crashes the agent turn."""

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>502 Bad Gateway</html>")

    async with _client(httpx.MockTransport(handler)) as client:
        with pytest.raises(BuildiumApiError) as exc_info:
            await client.request("GET", "/rentals")

    assert "non-JSON" in str(exc_info.value) or "unreadable" in str(exc_info.value)


async def test_query_too_large_raises() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    async with _client(httpx.MockTransport(handler)) as client:
        # Construct a query that definitely blows past 4000 chars.
        huge = {"ids": [str(i) for i in range(5000)]}
        with pytest.raises(QueryTooLargeError):
            await client.request("GET", "/rentals", query=huge)


async def test_paginate_stops_on_short_page() -> None:
    # Page 1: 1000 items, Page 2: 5 items → stop after page 2.
    calls: list[int] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("offset", 0))
        calls.append(offset)
        count = 1000 if offset == 0 else 5
        payload = [
            {
                "Id": offset + i,
                "PropertyId": 1,
                "UnitId": None,
                "Title": "t",
                "Status": "New",
            }
            for i in range(count)
        ]
        return httpx.Response(200, json=payload)

    async with _client(httpx.MockTransport(handler)) as client:
        items = await client.list_all("/workorders", None, WorkOrder)

    assert calls == [0, 1000]
    assert len(items) == 1005


async def test_network_error_retries_and_raises() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns fail")

    async with _client(httpx.MockTransport(handler)) as client:
        with pytest.raises(BuildiumApiError):
            await client.request("GET", "/ping")


async def test_retry_after_header_is_respected(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    # Patch asyncio.sleep inside the client module to observe backoff values.
    import helixis.buildium.client as client_module

    monkeypatch.setattr(client_module.asyncio, "sleep", fake_sleep)

    attempts = {"n": 0}

    async def handler(_request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(200, json={})

    async with _client(httpx.MockTransport(handler)) as client:
        await client.request("GET", "/ping")

    # First sleep should be exactly the Retry-After value.
    assert sleeps and sleeps[0] == pytest.approx(2.0)


async def test_token_bucket_enforces_rate() -> None:
    """Draining the capacity is instant; the next acquire waits ~1/rate."""
    import time

    from helixis.buildium.client import _TokenBucket

    bucket = _TokenBucket(rate=50.0, capacity=2.0)  # 2-token burst, then 50/s
    start = time.monotonic()
    await bucket.acquire()
    await bucket.acquire()
    burst_elapsed = time.monotonic() - start
    assert burst_elapsed < 0.05, "the initial burst should not block"

    # Third token must wait roughly one inter-token interval (1/50 = 20ms).
    mid = time.monotonic()
    await bucket.acquire()
    assert (time.monotonic() - mid) >= 0.01, "rate limit did not throttle the over-draw"


def test_rate_bucket_registry_is_shared_per_client_id() -> None:
    """The limiter is keyed by client_id so it spans per-call client instances."""
    from helixis.buildium.client import _get_rate_bucket

    a1 = _get_rate_bucket("acct-A", 9.0)
    a2 = _get_rate_bucket("acct-A", 9.0)
    b1 = _get_rate_bucket("acct-B", 9.0)
    assert a1 is a2, "same client_id must share one bucket"
    assert a1 is not b1, "different client_id must get distinct buckets"
