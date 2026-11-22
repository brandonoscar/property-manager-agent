"""Rentvine's request-rate ceiling is Buildium's limiter, at Rentvine's own rate.

Founder decision 2026-09-24 (``design/rentvine-terms-compliance.md`` §3.1):
the same process-wide, per-account token bucket Buildium uses
(``buildium.client._TokenBucket``), at ``rentvine.requests_per_sec`` (4 by
default). Before this the setting had no reader at all: nothing capped how
fast the client sent, only how many requests ran at once.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import pytest

from helixis.buildium import client as bdclient
from helixis.rentvine import client as rvclient
from helixis.settings import RentvineSettings

pytestmark = pytest.mark.rentvine_rate_limit


@pytest.fixture(autouse=True)
def _fresh_buckets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bdclient, "_rate_buckets", {})
    monkeypatch.setattr(rvclient, "RETRY_BACKOFF_S", (0.0,) * len(rvclient.RETRY_BACKOFF_S))


def _settings(account: str = "acme", rate: float = 4.0) -> RentvineSettings:
    return RentvineSettings(
        account_code=account, api_key="k", api_secret="s", requests_per_sec=rate
    )


def _client(handler, **kw: Any) -> rvclient.RentvineClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), auth=("k", "s"))
    return rvclient.RentvineClient(_settings(**kw), client=http)


def test_the_bucket_reads_the_setting_and_is_shared_per_account() -> None:
    a1 = rvclient._rate_bucket_for(_settings("acme"))
    a2 = rvclient._rate_bucket_for(_settings("acme"))
    b = rvclient._rate_bucket_for(_settings("other"))
    assert a1 is a2 and a1 is not b  # one budget per account, across clients
    assert a1._rate == 4.0
    # Never the Buildium bucket of a Buildium client_id that happens to match.
    assert bdclient._get_rate_bucket("acme", 9.0) is not a1


def test_the_default_rate_is_rentvines_own_4_not_buildiums_9() -> None:
    assert RentvineSettings().requests_per_sec == 4.0


@pytest.mark.asyncio
async def test_every_attempt_takes_a_token_retries_and_the_write_included(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    taken: list[str] = []

    class _Counting:
        async def acquire(self) -> None:
            taken.append("t")

    monkeypatch.setattr(rvclient, "_rate_bucket_for", lambda _s: _Counting())
    replies = iter([503, 503, 200, 200])
    client = _client(lambda _r: httpx.Response(next(replies), json={"ok": True}))
    await client.get("/properties")
    assert len(taken) == 3  # two failures and the success
    await client.post(
        "/maintenance/work-orders/{workOrderID}", json_body={}, path_params={"workOrderID": 1}
    )
    assert len(taken) == 4


@pytest.mark.asyncio
async def test_requests_are_actually_paced() -> None:
    """At 50/s with a one-second burst of 50, 60 requests cannot finish in
    under (60 - 50) / 50 = 0.2s."""
    client = _client(lambda _r: httpx.Response(200, json=[]), rate=50.0)
    started = time.monotonic()
    for _ in range(60):
        await client.get("/properties")
    assert time.monotonic() - started >= 0.18
