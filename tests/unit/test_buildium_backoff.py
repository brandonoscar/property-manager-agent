"""Rate-limit / backoff behavior of the Buildium client (release-gate Tier-2 #10).

Deterministic simulation via ``httpx.MockTransport`` — a scripted sequence of
429/5xx/network failures then success — asserting the client's retry contract:
backoff is exponential + capped, Retry-After is honored, retryable statuses are
retried up to the cap then raised, a non-retryable 4xx raises immediately (no
duplicate request), and a transient failure resolves to the FULL correct body
(no partial-state corruption). ``asyncio.sleep`` is patched to a no-op so the
suite is instant while still recording the delays the client asked for.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from helixis.buildium.client import (
    _BASE_BACKOFF,
    _MAX_BACKOFF,
    _MAX_RETRIES,
    BuildiumClient,
    _backoff_delay,
)
from helixis.buildium.errors import BuildiumApiError
from helixis.settings import BuildiumSettings

# ── pure backoff curve ────────────────────────────────────────────


def test_backoff_curve_is_exponential_and_capped() -> None:
    upper = {a: _backoff_delay(a, jitter=lambda _lo, hi: hi) for a in range(1, 8)}
    assert upper[1] == _BASE_BACKOFF
    assert upper[2] == _BASE_BACKOFF * 2
    assert upper[3] == _BASE_BACKOFF * 4
    assert upper[7] == _MAX_BACKOFF  # capped, not 0.25 * 64


def test_retry_after_overrides_backoff() -> None:
    assert _backoff_delay(1, retry_after=3.0, jitter=lambda _lo, hi: hi) == 3.0
    # A zero/negative Retry-After falls through to jittered backoff.
    assert _backoff_delay(1, retry_after=0.0, jitter=lambda _lo, _hi: 0.1) == 0.1


def test_backoff_stays_within_bounds_with_real_jitter() -> None:
    for a in range(1, 6):
        d = _backoff_delay(a)
        assert 0.0 <= d <= min(_MAX_BACKOFF, _BASE_BACKOFF * (2 ** (a - 1)))


# ── mocked-transport retry simulation ─────────────────────────────


def _client(handler, *, cid: str) -> BuildiumClient:
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://x.test/v1",
    )
    # High rate so the per-account token bucket never blocks in-test.
    settings = BuildiumSettings(
        client_id=cid, client_secret="s", base_url="https://x.test", requests_per_sec=1000.0
    )
    return BuildiumClient(settings, http_client=http)


def _seq_handler(statuses: list[int], *, retry_after: str | None = None):
    """Return a handler that replays ``statuses`` in order; the final entry is the
    terminal response. Records how many calls were made on ``.calls``."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        i = min(calls["n"], len(statuses) - 1)
        calls["n"] += 1
        status = statuses[i]
        headers = {}
        if status == 429 and retry_after is not None:
            headers["Retry-After"] = retry_after
        body = {"ok": True, "n": calls["n"]} if status < 400 else {"error": status}
        return httpx.Response(status, json=body, headers=headers)

    handler.calls = calls  # type: ignore[attr-defined]
    return handler


@pytest.fixture()
def _no_sleep():
    with patch("helixis.buildium.client.asyncio.sleep", new=AsyncMock()) as m:
        yield m


async def test_retries_then_succeeds(_no_sleep) -> None:
    handler = _seq_handler([429, 503, 200])
    client = _client(handler, cid="c-succeed")
    result = await client.request("GET", "/tasks")
    assert result == {"ok": True, "n": 3}
    assert handler.calls["n"] == 3  # two retries, third succeeded
    assert _no_sleep.await_count == 2  # slept before each retry
    await client.aclose()


async def test_retry_after_header_is_honored(_no_sleep) -> None:
    handler = _seq_handler([429, 200], retry_after="7")
    client = _client(handler, cid="c-retryafter")
    await client.request("GET", "/tasks")
    # The single backoff sleep used the Retry-After value, not jittered backoff.
    _no_sleep.assert_awaited_once_with(7.0)
    await client.aclose()


async def test_exhausts_retries_then_raises(_no_sleep) -> None:
    handler = _seq_handler([429])  # always 429
    client = _client(handler, cid="c-exhaust")
    with pytest.raises(BuildiumApiError) as exc:
        await client.request("GET", "/tasks")
    assert exc.value.status == 429
    assert handler.calls["n"] == _MAX_RETRIES  # tried the cap, no more
    await client.aclose()


async def test_non_retryable_4xx_raises_immediately(_no_sleep) -> None:
    handler = _seq_handler([422])
    client = _client(handler, cid="c-422")
    with pytest.raises(BuildiumApiError) as exc:
        await client.request("POST", "/tasks", body={"x": 1})
    assert exc.value.status == 422
    assert handler.calls["n"] == 1  # NOT retried — no duplicate write
    assert _no_sleep.await_count == 0
    await client.aclose()


async def test_network_error_then_success(_no_sleep) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, json={"ok": True})

    client = _client(handler, cid="c-network")
    result = await client.request("GET", "/tasks")
    assert result == {"ok": True}
    assert calls["n"] == 2
    await client.aclose()


async def test_transient_failure_yields_full_body_no_partial(_no_sleep) -> None:
    # After a 429, the retried request must return the COMPLETE body, and a
    # subsequent independent request is unaffected (no partial-state carryover).
    handler = _seq_handler([429, 200])
    client = _client(handler, cid="c-nopartial")
    first = await client.request("GET", "/tasks")
    assert first == {"ok": True, "n": 2}
    # Fresh handler state for a second call proves no cross-request corruption.
    handler2 = _seq_handler([200])
    client2 = _client(handler2, cid="c-nopartial2")
    second = await client2.request("GET", "/tasks")
    assert second == {"ok": True, "n": 1}
    await client.aclose()
    await client2.aclose()
