"""Buildium HTTP client.

Implements Buildium's Open API v1 transport with:
- client-id / client-secret header auth
- a process-wide request-RATE limiter (token bucket, keyed by client_id) that
  holds Buildium's 10 req/sec-per-account cap across the per-call client
  instances, plus a per-instance concurrency cap (semaphore)
- exponential backoff with jitter on 429 / 5xx / network errors
- cursor-style pagination helper
- raises BuildiumApiError on non-retriable failures
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import AsyncIterator, Callable
from typing import Any, TypeVar
from urllib.parse import urlencode

import httpx
from pydantic import BaseModel

from helixis.buildium.errors import (
    BuildiumApiError,
    BuildiumRetryableError,
    QueryTooLargeError,
)
from helixis.observability.pms_calls import log_pms_call, template_of
from helixis.settings import BuildiumSettings

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

_API_VERSION = "/v1"
_MAX_QUERY_LENGTH = 4000  # Buildium caps at 4096; leave headroom.
_RETRY_STATUSES = {429, 502, 503, 504}
_MAX_RETRIES = 5
_BASE_BACKOFF = 0.25
_MAX_BACKOFF = 8.0  # cap the full-jitter ceiling regardless of attempt count
_DEFAULT_PAGE_SIZE = 1000


class _TokenBucket:
    """Async token bucket for request-rate limiting.

    ``rate`` tokens are replenished per second up to ``capacity``. ``acquire``
    blocks until a token is available, then consumes one. The internal lock is
    held across the wait so concurrent callers are serialized into proper
    spacing — exactly the behaviour we want for a shared rate ceiling.
    """

    def __init__(self, rate: float, capacity: float) -> None:
        self._rate = rate
        self._capacity = capacity
        self._tokens = capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(
                    self._capacity, self._tokens + (now - self._updated) * self._rate
                )
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                # Sleep just long enough for the next token to accrue.
                await asyncio.sleep((1.0 - self._tokens) / self._rate)


# Process-wide registry of rate limiters keyed by Buildium client_id. The 10
# req/sec limit is per Buildium ACCOUNT, and BuildiumClient is constructed
# per-call in ~11 places, so the limiter MUST live at module scope (not on the
# instance) to bound the global rate. Access is sync with no await between the
# get and the set, so it is safe under single-threaded asyncio without a lock.
_rate_buckets: dict[str, _TokenBucket] = {}


def _get_rate_bucket(client_id: str, rate: float) -> _TokenBucket:
    bucket = _rate_buckets.get(client_id)
    if bucket is None:
        # capacity == rate → allow up to a 1-second burst, then steady ``rate``.
        bucket = _TokenBucket(rate, capacity=max(rate, 1.0))
        _rate_buckets[client_id] = bucket
    return bucket


class BuildiumClient:
    """Async Buildium API client.

    Use as an async context manager or call aclose() explicitly.
    """

    def __init__(
        self,
        settings: BuildiumSettings,
        *,
        http_client: httpx.AsyncClient | None = None,
        company_id: str | None = None,
    ) -> None:
        self._settings = settings
        #: For the per-call log only. Falls back to the request's company.
        self._company_id = company_id
        self._semaphore = asyncio.Semaphore(settings.concurrency)
        # Shared, process-wide rate ceiling keyed by account (client_id).
        self._rate_bucket = _get_rate_bucket(settings.client_id, settings.requests_per_sec)
        self._owns_client = http_client is None
        self._client = http_client or self._new_http_client()
        #: Context-manager nesting depth. Only the OUTERMOST exit closes.
        self._depth = 0

    def _new_http_client(self) -> httpx.AsyncClient:
        """Build the underlying transport. Factored out so a closed client can
        be rebuilt with byte-for-byte the same configuration."""
        settings = self._settings
        return httpx.AsyncClient(
            base_url=settings.base_url.rstrip("/") + _API_VERSION,
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={
                "x-buildium-client-id": settings.client_id,
                "x-buildium-client-secret": settings.client_secret,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

    def _ensure_open(self) -> None:
        """Rebuild the transport if a previous ``async with`` closed it.

        ⚠ THIS IS THE FIX FOR THE WRITE PATH, AND IT IS NOT DEFENSIVE PADDING.
        ``httpx.AsyncClient`` cannot be reopened -- entering a closed one and
        issuing a request raises ``RuntimeError: Cannot send a request, as the
        client has been closed.`` Every confirm-gated write does at least two
        separate ``async with client:`` blocks on the SAME client: one to read
        the record (often inside ``_gate_label``, which derives the confirm
        card from the record -- the Whitfield fix), and one inside the
        ``execute()`` closure that ``run_guarded`` calls after approval.

        The first closed the transport; the second raised. ``run_guarded``
        caught it as an ambiguous failure and reaped the claim
        ``abandoned_unknown``, which is never retried. Measured 2026-08-30:
        every gated write in the reversible and additive tiers failed this way
        (``ambiguous:RuntimeError``), the sole exception being
        ``create_work_order``, which writes BEFORE it verifies -- so its write
        landed and only the read-back raised (``verify_error:RuntimeError``).

        The agent then had a bare RuntimeError to explain and told users there
        had been a "connection error", which is why this read as a Buildium or
        network problem for two runs.

        ⚠ An INJECTED client is never rebuilt. Tests pass their own transport
        and silently swapping it for a real one would turn a unit test into a
        live API call.
        """
        if self._owns_client and self._client.is_closed:
            self._client = self._new_http_client()

    async def aclose(self) -> None:
        if self._owns_client and not self._client.is_closed:
            await self._client.aclose()

    async def __aenter__(self) -> BuildiumClient:
        # ⚠ Re-entrant by NESTING COUNT, so an inner helper's `async with`
        # cannot close the transport out from under its caller.
        self._depth += 1
        self._ensure_open()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self._depth = max(0, self._depth - 1)
        if self._depth == 0:
            await self.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: Any | None = None,
        schema: type[T] | None = None,
    ) -> Any:
        """Issue an authenticated request with retries."""
        url = self._build_url(path, query)
        request_args: dict[str, Any] = {}
        if body is not None:
            request_args["json"] = body

        attempt = 0
        while True:
            attempt += 1
            # Rate gate (per-account, process-wide) BEFORE the concurrency gate,
            # so retries also respect Buildium's 10 req/sec ceiling.
            await self._rate_bucket.acquire()
            async with self._semaphore:
                try:
                    # The execute() closure runs AFTER the outer context
                    # exited, so the nesting count cannot save it -- the
                    # transport is rebuilt here instead.
                    self._ensure_open()
                    started = time.monotonic()
                    response = await self._client.request(method, url, **request_args)
                except (httpx.TransportError, httpx.TimeoutException) as exc:
                    self._log_call(method, path, None, "transport_error", attempt, started)
                    self._log_retry(method, url, attempt, f"network: {exc}")
                    if attempt >= _MAX_RETRIES:
                        raise BuildiumApiError(
                            status=0,
                            message=f"Network error after {attempt} attempts: {exc}",
                            path=path,
                        ) from exc
                    await self._sleep_backoff(attempt)
                    continue

            self._log_call(
                method,
                path,
                response.status_code,
                "ok" if response.status_code < 400 else f"http_{response.status_code}",
                attempt,
                started,
            )
            if response.status_code in _RETRY_STATUSES:
                if attempt >= _MAX_RETRIES:
                    raise BuildiumApiError(
                        status=response.status_code,
                        message=self._scrub(response.text) or "retryable error exhausted retries",
                        path=path,
                        request_id=response.headers.get("x-request-id"),
                    )
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                self._log_retry(method, url, attempt, f"HTTP {response.status_code}")
                await self._sleep_backoff(attempt, retry_after=retry_after)
                continue

            if response.status_code >= 400:
                raise BuildiumApiError(
                    status=response.status_code,
                    message=self._scrub(response.text) or response.reason_phrase,
                    path=path,
                    request_id=response.headers.get("x-request-id"),
                )

            if not response.content:
                return None

            try:
                data = response.json()
            except ValueError as exc:
                # A 2xx with a non-JSON / truncated body: without this, the raw
                # json.JSONDecodeError (a ValueError) escapes request() and — the
                # agent tools only catch BuildiumApiError — propagates uncaught
                # into the agent loop. Convert it to the same structured error so
                # the tool degrades gracefully instead of crashing the turn.
                raise BuildiumApiError(
                    status=response.status_code,
                    message=f"Buildium returned an unreadable (non-JSON) response body: {exc}",
                    path=path,
                    request_id=response.headers.get("x-request-id"),
                ) from exc
            if schema is not None:
                if isinstance(data, list):
                    return [schema.model_validate(item) for item in data]
                return schema.model_validate(data)
            return data

    async def paginate(
        self,
        path: str,
        query: dict[str, Any] | None,
        schema: type[T] | None = None,
        *,
        page_size: int = _DEFAULT_PAGE_SIZE,
    ) -> AsyncIterator[list[T]]:
        """Yield pages of a list endpoint until a short page signals end-of-data.

        ``schema=None`` paginates a schemaless (raw-dict) collection — same
        page-until-short loop, no Pydantic validation. Every collection read
        must route through here (or ``list_all``) so no list silently
        truncates at Buildium's first page (page-size default).
        """
        offset = 0
        base_query = dict(query or {})
        while True:
            page_query = {**base_query, "limit": page_size, "offset": offset}
            page = await self.request("GET", path, query=page_query, schema=schema)
            if not isinstance(page, list):
                raise BuildiumApiError(
                    status=0,
                    message=f"Expected list from {path}, got {type(page).__name__}",
                    path=path,
                )
            yield page
            if len(page) < page_size:
                return
            offset += page_size

    async def list_all(
        self,
        path: str,
        query: dict[str, Any] | None,
        schema: type[T] | None = None,
        *,
        page_size: int = _DEFAULT_PAGE_SIZE,
    ) -> list[T]:
        """Collect every page into a flat list. Use paginate() for streaming.

        ``schema=None`` collects a schemaless (raw-dict) collection.
        """
        items: list[T] = []
        async for page in self.paginate(path, query, schema, page_size=page_size):
            items.extend(page)
        return items

    def _build_url(self, path: str, query: dict[str, Any] | None) -> str:
        if not path.startswith("/"):
            path = "/" + path
        if not query:
            return path
        encoded = urlencode(_flatten_query(query), doseq=True)
        if len(encoded) > _MAX_QUERY_LENGTH:
            raise QueryTooLargeError(
                f"Query string {len(encoded)} chars exceeds safe limit of {_MAX_QUERY_LENGTH}; "
                "caller must chunk the filters.",
            )
        return f"{path}?{encoded}"

    def _scrub(self, text: str) -> str:
        """Buildium's error body with this client's credentials masked.

        The body becomes the error's message, which the agent tools and the
        backfill log and the connection test returns to the page. A server or
        proxy that echoes the request headers would otherwise put the client
        id and secret in all of those. The longer value is masked first, so one
        credential containing the other cannot leave a fragment. Guard:
        ``test_buildium_no_secrets_in_errors.py``.
        """
        values = {self._settings.client_secret, self._settings.client_id} - {""}
        for value in sorted(values, key=len, reverse=True):
            text = text.replace(value, "***")
        return text

    async def _sleep_backoff(self, attempt: int, *, retry_after: float | None = None) -> None:
        await asyncio.sleep(_backoff_delay(attempt, retry_after=retry_after))

    def _log_call(
        self,
        method: str,
        path: str,
        status: int | None,
        outcome: str,
        attempt: int,
        started: float,
    ) -> None:
        """The per-call audit line, the same one Rentvine writes
        (``observability.pms_calls``; founder decision 2026-09-24). The path is
        templated, so record ids and query values stay out; the account is the
        Buildium AccountId, never ``client_id`` (half of the credential)."""
        log_pms_call(
            pms="buildium",
            company_id=self._company_id,
            account=str(self._settings.account_id) if self._settings.account_id else None,
            method=method,
            endpoint=template_of(path),
            status=status,
            outcome=outcome,
            attempt=attempt,
            duration_ms=(time.monotonic() - started) * 1000,
        )

    def _log_retry(self, method: str, url: str, attempt: int, reason: str) -> None:
        logger.warning(
            "Buildium request retry %d/%d: %s %s (%s)",
            attempt,
            _MAX_RETRIES,
            method,
            url,
            reason,
        )


def _flatten_query(query: dict[str, Any]) -> list[tuple[str, str]]:
    """Convert dict (possibly containing lists) into a flat param list."""
    items: list[tuple[str, str]] = []
    for key, value in query.items():
        if value is None:
            continue
        if isinstance(value, (list, tuple, set)):
            for v in value:
                items.append((key, str(v)))
        elif isinstance(value, bool):
            items.append((key, "true" if value else "false"))
        else:
            items.append((key, str(value)))
    return items


def _backoff_delay(
    attempt: int,
    *,
    retry_after: float | None = None,
    jitter: Callable[[float, float], float] = random.uniform,
) -> float:
    """Seconds to wait before retry ``attempt`` (1-indexed). A positive
    ``Retry-After`` wins; otherwise full-jitter exponential backoff —
    ``jitter(0, min(cap, base * 2**(attempt-1)))`` — bounded by ``_MAX_BACKOFF``.
    Pure (``jitter`` injectable) so the backoff curve is unit-testable without
    sleeping or patching the clock."""
    if retry_after is not None and retry_after > 0:
        return retry_after
    upper = min(_MAX_BACKOFF, _BASE_BACKOFF * (2 ** (max(1, attempt) - 1)))
    return jitter(0.0, upper)


def _parse_retry_after(header: str | None) -> float | None:
    if not header:
        return None
    try:
        return max(0.0, float(header))
    except ValueError:
        return None


__all__ = ["BuildiumClient", "BuildiumRetryableError"]
