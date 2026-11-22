"""One HTTP client per (company, system) for the connectors built on
``helixis.pms`` (Rent Manager, Propertyware).

It copies the behaviour of Rentvine's proven client (``rentvine/client.py``)
and shares none of its code (design P1):

* **Documented calls only.** Every request is checked against the
  connector's operation index (verb, path, query parameters) before anything
  is sent. An unknown filter is refused, never sent: a vendor that ignores it
  answers about the whole account with a 200 (gotcha 24).
* **Reads retry, writes never do.** :meth:`PmsHttpClient.get` retries while
  the classifier calls a failure retryable. :meth:`PmsHttpClient.send_once`
  sends a write at most once: a failure is not proof the write did not land
  (gotcha 70's mirror image), so a duplicate in a landlord's books is the cost
  of a retry. There is no delete method, and DELETE is refused.
* **Every vendor wait is binding** (``pms.rate``) and **a failing vendor is
  left alone** (``pms.breaker``).
* **One audit line per attempt** (``observability.pms_calls``): the endpoint
  TEMPLATE, never a record id, a value, a header or a body.
* **Errors are scrubbed** of every credential before they are shown or logged.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, TypeVar

import httpx

from helixis.observability.pms_calls import log_pms_call
from helixis.pms import breaker, rate, registry
from helixis.pms.auth import Auth
from helixis.pms.errors import (
    Failure,
    PmsApiError,
    PmsUnavailableError,
    RefusedBeforeSendingError,
    classify_status,
)

#: Path parameter values that may be put into a URL: ids and simple codes.
#: Anything else (a slash, a dot-dot, a query) is refused before sending.
_PATH_VALUE_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

T = TypeVar("T")


async def _sleep(seconds: float) -> None:
    """Every wait goes through here, so a test can record waits without
    sleeping and without patching ``asyncio`` for everyone else."""
    await asyncio.sleep(seconds)


def operation_key(method: str, template: str) -> str:
    """The index key, e.g. ``GET /Properties/{PropertyID}``."""
    return f"{method.strip().upper()} /{template.strip().lstrip('/')}"


@dataclass(frozen=True)
class ClientConfig:
    """Everything a connector decides about its vendor's HTTP surface."""

    #: The ``pms.registry`` key: logs, holds and the breaker are keyed by it.
    pms: str
    #: The vendor account label (Rent Manager's corp id, Propertyware's system
    #: id). Not a secret: it is in the URL or a header on every call.
    account: str
    #: ``https://`` only.
    base_url: str
    #: ``operation_key -> declared query parameters``. A call outside it is
    #: refused before sending.
    operations: Mapping[str, frozenset[str]]
    requests_per_sec: float
    #: Requests in flight at once for this client.
    concurrency: int
    timeout_seconds: float = 30.0
    #: Headers read, in order, for how long the vendor asked us to wait.
    hold_headers: tuple[str, ...] = ("retry-after",)
    #: The hold a 429 earns when it names no wait.
    bare_429_hold_s: float = rate.RATE_LIMIT_BACKOFF_S[0]
    #: The connector's own status meanings, over ``errors.DEFAULT_MEANINGS``.
    status_meanings: Mapping[int, tuple[Failure, bool]] = field(default_factory=dict)
    #: Keys that must never appear in a body or a query (Rent Manager's
    #: ``saveOptions``). Refused before sending, in any letter case: a .NET
    #: API binds JSON names case-insensitively, so ``SAVEOPTIONS`` would land.
    forbidden_keys: frozenset[str] = frozenset()
    #: The vendor's quota headers, when known.
    rate_headers: rate.RateHeaders | None = None
    #: ``(status, body text) -> seconds to hold, or None``: a connector's own
    #: outage signal (Rent Manager's maintenance window).
    outage_hold: Callable[[int, str], float | None] | None = None
    #: The verbs :meth:`PmsHttpClient.send_once` may use. Never DELETE.
    write_methods: frozenset[str] = frozenset({"POST", "PUT", "PATCH"})
    #: Sent on every request, the login included. Left out of the repr: a
    #: connector may carry a secret here (Rent Manager's partner token), and
    #: this config is printed wherever a client is logged or fails.
    extra_headers: Mapping[str, str] = field(
        default_factory=lambda: {"Accept": "application/json"}, repr=False
    )


@dataclass(frozen=True)
class PmsResponse:
    status: int
    #: The parsed JSON body (a leading UTF-8 BOM is stripped), or ``None``
    #: for an empty body.
    body: Any
    headers: Mapping[str, str]


def parse_body(raw: bytes) -> Any:
    """JSON from ``raw``, tolerating a UTF-8 BOM (Rent Manager, ``@unverified``).
    ``None`` for an empty body; ``ValueError`` for one that is not JSON."""
    text = raw.decode("utf-8-sig", errors="strict")
    if not text.strip():
        return None
    return json.loads(text)


def _keys_in(value: Any) -> set[str]:
    """Every dict key anywhere in a JSON-shaped value."""
    found: set[str] = set()
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, Mapping):
            found.update(str(k) for k in item)
            stack.extend(item.values())
        elif isinstance(item, list | tuple):
            stack.extend(item)
    return found


class PmsHttpClient:
    """A client bound to ONE company's account on ONE system."""

    def __init__(
        self,
        config: ClientConfig,
        auth: Auth,
        *,
        company_id: str | None = None,
        client: httpx.AsyncClient | None = None,
    ):
        if not config.base_url.startswith("https://"):
            raise ValueError("a PMS base URL must be https")
        if "DELETE" in {m.upper() for m in config.write_methods}:
            raise ValueError("no connector may send DELETE")
        self._config = config
        self._auth = auth
        self._company_id = company_id
        self._key = f"{config.pms}:{config.account}"
        self.name = registry.name_of(config.pms) or config.pms
        self._base = config.base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=config.timeout_seconds, headers=dict(config.extra_headers)
        )
        self._gate = asyncio.Semaphore(max(1, config.concurrency))

    @property
    def pms(self) -> str:
        """The registry key of the system this client calls."""
        return self._config.pms

    @property
    def company_id(self) -> str | None:
        """The company whose credentials this client carries. ``pms.gated_write``
        refuses a client bound to another company: a write against the wrong
        books would verify cleanly against them (gotcha 70)."""
        return self._company_id

    async def __aenter__(self) -> PmsHttpClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ── building a request ────────────────────────────────────────────

    def _prepare(
        self,
        method: str,
        template: str,
        path_params: Mapping[str, Any] | None,
        query: Mapping[str, Any] | None,
        body: Any,
    ) -> tuple[str, dict[str, Any]]:
        key = operation_key(method, template)
        declared = self._config.operations.get(key)
        if declared is None:
            raise RefusedBeforeSendingError(f"{key} is not a documented {self.name} call")
        params = dict(query or {})
        unknown = sorted(k for k in params if k not in declared)
        if unknown:
            raise RefusedBeforeSendingError(
                f"{key} does not declare {unknown}; an ignored filter would answer "
                f"about the whole account"
            )
        never = {k.casefold() for k in self._config.forbidden_keys}
        forbidden = {k for k in set(params) | _keys_in(body) if k.casefold() in never}
        if forbidden:
            raise RefusedBeforeSendingError(f"{key} may never carry {sorted(forbidden)}")
        values: dict[str, str] = {}
        for name, value in (path_params or {}).items():
            text = str(value)
            if not _PATH_VALUE_RE.match(text):
                raise RefusedBeforeSendingError(f"{name} is not an id that may go in a path")
            values[name] = text
        try:
            # Always formatted: a placeholder with no value must be refused,
            # never sent as a literal "{PropertyID}".
            path = template.format(**values)
        except (KeyError, IndexError) as exc:
            raise RefusedBeforeSendingError(f"{key} is missing a path value: {exc}") from exc
        return f"{self._base}/{path.lstrip('/')}", params

    def _scrub(self, text: str) -> str:
        for secret in self._auth.secrets():
            if secret:
                text = text.replace(secret, "***")
        return text

    def _log(
        self, method: str, template: str, status: int | None, outcome: str, attempt: int, t0: float
    ) -> None:
        log_pms_call(
            pms=self._config.pms,
            company_id=self._company_id,
            account=self._config.account,
            method=method,
            endpoint=template,
            status=status,
            outcome=outcome,
            attempt=attempt,
            duration_ms=(time.monotonic() - t0) * 1000,
        )

    def _held_error(self, remaining: float) -> PmsApiError:
        return PmsApiError(
            429,
            Failure.UNDOCUMENTED,
            False,
            f"not sent: {self.name} asked Occupella to wait {int(remaining) + 1}s more",
            retry_after=remaining,
            sent=False,
        )

    def _error_from(self, resp: httpx.Response) -> PmsApiError:
        failure, retryable = classify_status(resp.status_code, self._config.status_meanings)
        return PmsApiError(
            resp.status_code,
            failure,
            retryable,
            self._scrub(resp.text),
            retry_after=rate.retry_after_seconds(resp.headers, self._config.hold_headers),
        )

    def _note_holds(self, resp: httpx.Response, exc: PmsApiError | None) -> None:
        """Hold the account for as long as the vendor asked, for its quota,
        and for a connector-named outage. Holds only ever extend."""
        if exc is not None:
            if exc.retry_after is not None:
                rate.hold(self._key, exc.retry_after)
            elif exc.status == 429:
                rate.hold(self._key, self._config.bare_429_hold_s)
            if self._config.outage_hold is not None:
                extra = self._config.outage_hold(exc.status, exc.detail)
                if extra:
                    rate.hold(self._key, extra)
        quota = rate.low_quota_hold(resp.headers, self._config.rate_headers)
        if quota:
            rate.hold(self._key, quota)

    async def _send(
        self,
        method: str,
        url: str,
        query: dict[str, Any],
        headers: Mapping[str, str],
        body: Any,
    ) -> httpx.Response:
        try:
            return await self._client.request(
                method, url, params=query or None, headers=dict(headers), json=body
            )
        except httpx.HTTPError as exc:
            # Never reached the vendor's answer: says nothing about the account.
            # ⚠ ``sent`` stays True: for a write this is "may have landed".
            raise PmsApiError(
                0, Failure.UNDOCUMENTED, True, f"transport: {type(exc).__name__}"
            ) from exc

    # ── reads ─────────────────────────────────────────────────────────

    async def _wait_out_hold(self) -> None:
        """Wait out a short hold; refuse, without sending, during a long one."""
        remaining = rate.held_for(self._key)
        if remaining is None:
            return
        if remaining > rate.MAX_RETRY_AFTER_S:
            raise self._held_error(remaining)
        await _sleep(remaining)

    async def get(
        self,
        template: str,
        *,
        path_params: Mapping[str, Any] | None = None,
        query: Mapping[str, Any] | None = None,
    ) -> PmsResponse:
        """ONE documented GET, retried while the classifier calls the failure
        retryable. A 401 on a refreshable auth logs in once and replays once.

        ⚠ The concurrency slot is held across backoff, so a throttled read is
        back-pressure on the whole sync, not a gap a sibling fills.
        """
        url, params = self._prepare("GET", template, path_params, query, None)
        attempt = 0
        sends = 0
        refreshed = False
        async with self._gate:
            while True:
                await self._wait_out_hold()
                try:
                    breaker.admit(self._key, self.name, probe=True)
                except PmsUnavailableError:
                    self._log("GET", template, None, "circuit_open", sends + 1, time.monotonic())
                    raise
                headers = await self._auth.headers(self)
                await rate.bucket(self._key, self._config.requests_per_sec).acquire()
                sends += 1
                t0 = time.monotonic()
                try:
                    resp = await self._send("GET", url, params, headers, None)
                except PmsApiError as exc:
                    breaker.record(self._key, exc)
                    self._log("GET", template, None, "transport_error", sends, t0)
                    if attempt >= len(rate.RETRY_BACKOFF_S):
                        raise
                    await _sleep(rate.jitter(rate.RETRY_BACKOFF_S[attempt]))
                    attempt += 1
                    continue
                if resp.status_code == 401 and self._auth.refreshable and not refreshed:
                    # The vendor answered, so the breaker hears an answer.
                    breaker.record(self._key, None)
                    self._log("GET", template, 401, Failure.CREDENTIALS.value, sends, t0)
                    refreshed = True
                    await self._auth.refresh(self, headers)
                    continue
                if resp.status_code >= 400:
                    err = self._error_from(resp)
                    breaker.record(self._key, err)
                    self._log("GET", template, resp.status_code, err.failure.value, sends, t0)
                    self._note_holds(resp, err)
                    if err.failure is Failure.CREDENTIALS and refreshed:
                        self._auth.mark_broken()
                    schedule = rate.schedule_for(err.status, err.retry_after)
                    if (
                        not err.retryable
                        or attempt >= len(rate.RETRY_BACKOFF_S)
                        or attempt >= len(schedule)
                    ):
                        raise err
                    wait = rate.retry_delay(err.retry_after, schedule[attempt])
                    if wait is None:
                        # The vendor asked for longer than one request waits;
                        # the hold keeps every other request off it too.
                        raise err
                    attempt += 1
                    await _sleep(rate.jitter(wait))
                    continue
                breaker.record(self._key, None)
                self._note_holds(resp, None)
                self._log("GET", template, resp.status_code, "ok", sends, t0)
                try:
                    body = parse_body(resp.content)
                except ValueError as exc:
                    raise PmsApiError(
                        resp.status_code,
                        Failure.UNDOCUMENTED,
                        False,
                        f"{self.name} answered {template} with a body that is not JSON",
                    ) from exc
                return PmsResponse(resp.status_code, body, dict(resp.headers))

    # ── writes ────────────────────────────────────────────────────────

    def _refuse_if_held_or_open(self, method: str, template: str) -> None:
        """A write never waits out a hold and is never the breaker's probe:
        it runs inside a person's approval, so it refuses at once."""
        remaining = rate.held_for(self._key)
        if remaining is not None:
            self._log(method, template, None, "held", 1, time.monotonic())
            raise self._held_error(remaining)
        try:
            breaker.admit(self._key, self.name, probe=False)
        except PmsUnavailableError:
            self._log(method, template, None, "circuit_open", 1, time.monotonic())
            raise

    @staticmethod
    async def _unsent_if_auth_fails(step: Awaitable[T]) -> T:
        """Run a login step for a write. A failure here means the WRITE never
        left the process (the vendor refused the login, or a 401 refused the
        write itself), so the error says ``sent=False``: a guard that read the
        login's own status as "may have landed" would block a safe retry and
        tell the person to check for a change that never happened."""
        try:
            return await step
        except PmsApiError as exc:
            exc.sent = False
            raise

    async def send_once(
        self,
        method: str,
        template: str,
        *,
        json_body: Any = None,
        path_params: Mapping[str, Any] | None = None,
        query: Mapping[str, Any] | None = None,
    ) -> PmsResponse:
        """The ONE write method: POST, PUT or PATCH, sent at most once.

        ⚠ Never retried. A 5xx or a transport error may have landed, so it is
        raised as it is (``sent=True``) and the caller's read-back decides.
        The one replay is after a 401 on a refreshable auth: the vendor
        refused before processing, so the write did not happen.

        A 2xx whose body is not JSON returns ``body=None`` rather than raising:
        a 2xx is not the write either way, and the read-back decides.
        """
        verb = method.strip().upper()
        if verb not in {m.upper() for m in self._config.write_methods}:
            raise RefusedBeforeSendingError(f"{verb} is not a write {self.name} may be sent")
        url, params = self._prepare(verb, template, path_params, query, json_body)
        replayed = False
        async with self._gate:
            self._refuse_if_held_or_open(verb, template)
            while True:
                headers = await self._unsent_if_auth_fails(self._auth.headers(self))
                await rate.bucket(self._key, self._config.requests_per_sec).acquire()
                t0 = time.monotonic()
                try:
                    resp = await self._send(verb, url, params, headers, json_body)
                except PmsApiError as exc:
                    breaker.record(self._key, exc)
                    self._log(verb, template, None, "transport_error", 2 if replayed else 1, t0)
                    raise
                if resp.status_code == 401 and self._auth.refreshable and not replayed:
                    breaker.record(self._key, None)
                    self._log(verb, template, 401, Failure.CREDENTIALS.value, 1, t0)
                    replayed = True
                    await self._unsent_if_auth_fails(self._auth.refresh(self, headers))
                    continue
                break
        attempt = 2 if replayed else 1
        if resp.status_code >= 400:
            err = self._error_from(resp)
            breaker.record(self._key, err)
            self._log(verb, template, resp.status_code, err.failure.value, attempt, t0)
            self._note_holds(resp, err)
            if err.failure is Failure.CREDENTIALS and replayed:
                self._auth.mark_broken()
            raise err
        breaker.record(self._key, None)
        self._note_holds(resp, None)
        self._log(verb, template, resp.status_code, "ok", attempt, t0)
        try:
            body = parse_body(resp.content)
        except ValueError:
            body = None
        return PmsResponse(resp.status_code, body, dict(resp.headers))

    # ── login (auth bootstrap only) ───────────────────────────────────

    async def login(self, template: str, *, json_body: Any) -> PmsResponse:
        """The login call a :class:`~helixis.pms.auth.SessionTokenAuth` makes.

        Sent without auth headers, never retried, refused into a hold. It does
        not take a concurrency slot or ask the breaker: it only runs inside a
        request that already did both, and taking the slot again would
        deadlock a one-at-a-time client. Its outcome still counts for the
        breaker.
        """
        url, params = self._prepare("POST", template, None, None, json_body)
        remaining = rate.held_for(self._key)
        if remaining is not None:
            self._log("POST", template, None, "held", 1, time.monotonic())
            raise self._held_error(remaining)
        await rate.bucket(self._key, self._config.requests_per_sec).acquire()
        t0 = time.monotonic()
        try:
            resp = await self._send("POST", url, params, {}, json_body)
        except PmsApiError as exc:
            breaker.record(self._key, exc)
            self._log("POST", template, None, "transport_error", 1, t0)
            raise
        if resp.status_code >= 400:
            err = self._error_from(resp)
            breaker.record(self._key, err)
            self._log("POST", template, resp.status_code, err.failure.value, 1, t0)
            self._note_holds(resp, err)
            raise err
        breaker.record(self._key, None)
        self._log("POST", template, resp.status_code, "ok", 1, t0)
        try:
            body = parse_body(resp.content)
        except ValueError as exc:
            raise PmsApiError(
                resp.status_code,
                Failure.UNDOCUMENTED,
                False,
                f"{self.name}'s login answered with a body that is not JSON",
            ) from exc
        return PmsResponse(resp.status_code, body, dict(resp.headers))


__all__ = [
    "ClientConfig",
    "PmsHttpClient",
    "PmsResponse",
    "operation_key",
    "parse_body",
]
