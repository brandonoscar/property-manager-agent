"""Fault injection for the recovery suites (5 + 10).

You cannot make a real Buildium sandbox return 500 / 429 on command, so the only
faithful way to test OUR recovery logic is to inject the fault at the HTTP layer
BENEATH the real client and let the shipped code react. ``BuildiumClient`` takes
an ``http_client``, so we hand it an ``httpx.AsyncClient`` backed by a mock
transport — no monkeypatching of our own code, and the client's real retry /
backoff / error-mapping loop (#592) runs exactly as in production.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import patch

import httpx

if TYPE_CHECKING:
    from helixis.buildium.client import BuildiumClient
    from helixis.settings import BuildiumSettings

_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@dataclass
class _Counter:
    calls: int = 0
    write_calls: int = 0


def _handler(
    counter: _Counter,
    *,
    status: int,
    fail_first_n: int | None,
    only_writes: bool,
    success_payload: dict | list,
    timeout: bool,
):
    def handle(request: httpx.Request) -> httpx.Response:
        counter.calls += 1
        is_write = request.method.upper() in _WRITE_METHODS
        if is_write:
            counter.write_calls += 1
        applies = is_write or not only_writes
        should_fail = applies and (fail_first_n is None or counter.calls <= fail_first_n)
        if should_fail:
            if timeout:
                raise httpx.TimeoutException("injected timeout", request=request)
            headers = {"Retry-After": "0"} if status == 429 else {}
            return httpx.Response(status, headers=headers, text=f"injected {status}")
        return httpx.Response(200, json=success_payload)

    return handle


@dataclass
class FaultInjection:
    """A faulty BuildiumClient plus the call counter, as a context manager."""

    client: BuildiumClient
    counter: _Counter

    async def __aenter__(self) -> FaultInjection:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.client.aclose()


def faulty_buildium_client(
    settings: BuildiumSettings,
    *,
    status: int = 500,
    fail_first_n: int | None = None,
    only_writes: bool = True,
    success_payload: dict | list | None = None,
    timeout: bool = False,
) -> FaultInjection:
    """Build a real ``BuildiumClient`` whose transport injects faults.

    ``fail_first_n=None`` fails forever; an int fails the first N calls then
    returns 200 (transient recovery). ``status=429`` drives the client's real
    backoff loop; ``status=500`` raises immediately (500 is not retryable). Set
    ``timeout=True`` to raise ``httpx.TimeoutException`` (the network-error path).
    """
    from helixis.buildium.client import _API_VERSION, BuildiumClient

    counter = _Counter()
    handler = _handler(
        counter,
        status=status,
        fail_first_n=fail_first_n,
        only_writes=only_writes,
        success_payload=success_payload if success_payload is not None else {"Id": 1},
        timeout=timeout,
    )
    # The client issues RELATIVE paths ("/workorders") and relies on the
    # AsyncClient's base_url to make them absolute — mirror that here so the mock
    # transport receives a resolvable URL (the host is irrelevant; it's mocked).
    base = (settings.base_url or "https://apisandbox.buildium.com").rstrip("/") + _API_VERSION
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=base)
    client = BuildiumClient(settings, http_client=http_client)
    return FaultInjection(client=client, counter=counter)


@contextlib.contextmanager
def no_backoff_sleep() -> Iterator[None]:
    """Zero the client's backoff delay so a retry-loop test runs fast WITHOUT
    changing the loop itself — we still exercise the real retry count, just not
    the wall-clock sleeps. Patches ``_backoff_delay`` (the delay calculator), so
    the retry decision logic is untouched."""
    with patch("helixis.buildium.client._backoff_delay", return_value=0.0):
        yield
