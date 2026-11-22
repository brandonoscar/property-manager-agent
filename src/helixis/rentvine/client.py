"""Read-only HTTP client for one company's Rentvine account. ADR 0010, Phase 4.

WHAT THIS IS
    A thin, per-company, READ-ONLY client. It opens with credentials loaded
    through ``rentvine/credentials.py`` and has **no env fallback** — gotcha 70
    is the standing record of what that costs: the Buildium write harness fell
    back to env credentials pointing at a different account and confidently
    verified the wrong books.

WHAT IS @unverified HERE
    Nothing in this module has been run against a Rentvine server. Every
    assumption that a live call would settle carries a literal ``@unverified``
    tag naming what would settle it, because an untagged guess is exactly the
    failure Phase 4 exists to avoid (gotcha 79: one wrong path segment meant no
    customer could complete A2P registration, invisibly, because every mock
    answered whatever path it was handed).

READ-ONLY BY CONSTRUCTION, NOT BY CARE
    :meth:`RentvineClient.get` is the only request method and it hardcodes the
    verb. There is no parameter that can change it, so a later edit asking this
    class to write has to add a method rather than pass an argument — which is
    a diff a reviewer sees. Same shape as ``scripts/rentvine_probe.py``.
"""

from __future__ import annotations

import asyncio
import base64
import email.utils
import logging
import math
import random
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from helixis.buildium.client import _get_rate_bucket
from helixis.observability.pms_calls import log_pms_call
from helixis.rentvine import transport
from helixis.rentvine._row_envelopes import ROW_COMPANIONS, ROW_ENVELOPE
from helixis.rentvine.credentials import resolved_base_url
from helixis.settings import RentvineSettings

logger = logging.getLogger(__name__)

#: Hard ceiling on pages fetched for one collection.
#:
#: Rentvine returns ``pagination-total-pages`` on 40 of the 44 paginating
#: operations, so on those the loop knows when to stop. This ceiling is the
#: backstop for the other six (the exports — see
#: ``transport.OPERATIONS_WITHOUT_PAGINATION_HEADERS``) and for a server that
#: answers past the end in some way the spec does not describe.
#:
#: ✅ SETTLED 2026-09-24 on demoaccount41: the headers ARRIVE and mean what
#: they say. ``GET /leases`` sent ``pagination-total-items: 28``,
#: ``-total-pages: 2`` with 25 rows, and the mirror held exactly the 28 ids.
#: They arrive even on routes the spec says cannot be paged, which is what
#: :class:`RentvineTruncatedError` reads.
MAX_PAGES = 200

#: Rows per page.
#:
#: ⚠ @unverified — the spec declares ``pageSize`` on 44 operations and states no
#: maximum. 100 is a conservative guess that will not trip an undocumented cap;
#: a live probe should raise it if the server accepts more, because page count
#: is the whole cost of a sync.
PAGE_SIZE = 100

#: How long to wait before each retry of a RETRYABLE read, in seconds.
#:
#: ⚠ **THIS SCHEDULE IS OUR OWN GUESS — RENTVINE DOCUMENTS NO RATE LIMIT AT
#: ALL.** ``transport.classify_status`` records that the spec contains zero
#: occurrences of ``429``, ``ratelimit`` or ``retry-after``, so there is no
#: published ceiling to aim at and nothing here was probed. What makes the
#: guess safe is the direction it errs in: waiting too long costs wall clock on
#: a sync that runs unattended, and not waiting at all costs a whole mirror
#: table (see :meth:`RentvineClient._get_with_retry`).
#:
#: ⚠ **THE SCHEDULE IS ALSO THE BOUND.** Attempts are ``len(RETRY_BACKOFF_S) +
#: 1``, derived rather than declared, so a separate attempt count cannot drift
#: out of step with the waits it is supposed to describe (the
#: ``maintenance_status.OPEN_STATUSES`` / ``geo.intel_freshness.RECRAWL_AFTER``
#: precedent — compute the second number from the first).
RETRY_BACKOFF_S: tuple[float, ...] = (0.5, 2.0, 5.0)

#: How long to back off after a 429 that names no ``Retry-After``.
#:
#: Slower than :data:`RETRY_BACKOFF_S` on purpose: a 429 is Rentvine asking for
#: LESS load, and ``design/rentvine-terms-rules.md`` rule 1 makes every 429
#: binding. Shorter than this schedule and a throttled sync becomes a burst.
RATE_LIMIT_BACKOFF_S: tuple[float, ...] = (10.0, 30.0)

#: The longest ``Retry-After`` a single request will wait out in place.
#:
#: ``Retry-After`` is a standard HTTP response header (RFC 9110), not something
#: Rentvine has documented, so it is honoured WHEN PRESENT and never required.
#:
#: ⚠ **A LONGER ASK IS NEVER SHORTENED — THE REQUEST STOPS INSTEAD.** This used
#: to be a clamp: a server asking for 120 seconds got 30 and a second request,
#: which is working around a throttle (terms rule 2). Now an ask above this
#: stops the request, the table is reported stale, and the account is HELD
#: (:func:`_hold_account`) so nothing else in this process sends to it until
#: the server's time is up.
#:
#: ⚠ It must stay at or above the longest entry of both backoff schedules, or a
#: server asking for less than our own wait would be refused a retry we would
#: have made anyway. Pinned as an invariant rather than two literals (gotcha 109).
MAX_RETRY_AFTER_S = 30.0

#: What an unreadable ``Retry-After`` (junk, negative, not finite) is taken to
#: ask for. Above :data:`MAX_RETRY_AFTER_S`, so the request stops and the account
#: is held: a wait we cannot read is never treated as no wait.
UNREADABLE_RETRY_AFTER_S = 60.0

#: Random extra wait on every backoff, as a fraction of it. Only ever ADDED, so
#: jitter can lengthen a wait the server asked for and never shorten one.
JITTER_FRACTION = 0.25

#: Accounts Rentvine has asked this process to leave alone:
#: ``{account_code: time.monotonic() deadline}``.
#:
#: ⚠ PER PROCESS. The web and the worker each keep their own, so a hold set by
#: the worker's sync does not stop the web's Refresh button. Recorded as a
#: known gap in ``design/rentvine-terms-compliance.md``.
_holds: dict[str, float] = {}


def _now() -> float:
    return time.monotonic()


def _jitter(delay: float) -> float:
    """``delay`` plus up to :data:`JITTER_FRACTION` more. Never less."""
    return delay * (1.0 + random.uniform(0.0, JITTER_FRACTION))  # noqa: S311 — not crypto


def _hold_account(account: str, seconds: float) -> None:
    """Refuse to send to ``account`` for ``seconds``. Extends, never shortens."""
    _holds[account] = max(_holds.get(account, 0.0), _now() + seconds)


def _held_for(account: str) -> float | None:
    """Seconds left on ``account``'s hold, or ``None`` when it may be called."""
    deadline = _holds.get(account)
    if deadline is None:
        return None
    remaining = deadline - _now()
    if remaining <= 0:
        _holds.pop(account, None)
        return None
    return remaining


def _rate_bucket_for(settings: RentvineSettings) -> Any:
    """The per-account request-rate ceiling, the SAME limiter Buildium uses.

    Founder decision 2026-09-24 (``design/rentvine-terms-compliance.md`` §3.1):
    match Buildium's mechanism, at Rentvine's own ``requests_per_sec`` (4 by
    default, below Buildium's 9, because Rentvine publishes no limit at all).
    Process-wide and keyed by account, like Buildium's, so every client built
    for one Rentvine account shares one budget. Every attempt takes a token,
    retries included.
    """
    return _get_rate_bucket(f"rentvine:{settings.account_code}", settings.requests_per_sec)


def _held_error(remaining: float) -> RentvineAPIError:
    return RentvineAPIError(
        429,
        transport.Failure.UNDOCUMENTED,
        False,
        f"not sent: Rentvine asked Occupella to wait {math.ceil(remaining)}s more (Retry-After)",
        retry_after=remaining,
        sent=False,
    )


# ── circuit breaker (terms rule 25, rule 28) ─────────────────────────────
#
# Founder go-ahead 2026-10-01 for the design in
# ``design/rentvine-terms-compliance.md`` §3.2 (deferred 2026-09-24):
#
# * OPENS after :data:`BREAKER_FAILURES` consecutive outage failures (a 5xx, a
#   transport error, or a 429) within :data:`BREAKER_WINDOW_S`.
# * While OPEN for :data:`BREAKER_OPEN_S`, nothing is sent: reads and writes
#   both raise :class:`RentvineUnavailableError` ("Rentvine unavailable").
# * Then HALF-OPEN: exactly one READ is let through as a probe. Any answer from
#   Rentvine closes it (a 404 is Rentvine working); an outage failure re-opens
#   it for another :data:`BREAKER_OPEN_S`. Writes are refused until it closes,
#   because a write is never the thing that tests whether Rentvine is back.
#
# ⚠ Only requests that were actually SENT count. A refusal into a Retry-After
# hold or into this breaker sent nothing and says nothing about Rentvine.
#
# ⚠ PER PROCESS, like :data:`_holds`: the web and the worker each keep their
# own. Recorded beside the holds gap in the compliance doc.

#: Consecutive outage failures that open the breaker.
BREAKER_FAILURES = 5
#: The window those failures must fall inside, in seconds.
BREAKER_WINDOW_S = 300.0
#: How long the breaker stays open before a probe is allowed, in seconds.
BREAKER_OPEN_S = 600.0
#: A probe that never reported back (a cancelled task, a crash between send
#: and record) is abandoned after this long, so half-open can't wedge shut.
BREAKER_PROBE_STALE_S = 120.0


@dataclass
class _Breaker:
    #: ``time.monotonic()`` of each outage failure since the last answer.
    failures: list[float] = field(default_factory=list)
    #: When OPEN ends. ``None`` is CLOSED.
    open_until: float | None = None
    #: When the half-open probe was let through, while it is in flight.
    probe_started: float | None = None


#: ``{account_code: _Breaker}``. Per process; see above.
_breakers: dict[str, _Breaker] = {}


def _is_outage(exc: RentvineAPIError) -> bool:
    """A failure that says Rentvine itself is struggling, not that we asked
    for something it refused. 4xx other than 429 is Rentvine WORKING."""
    return exc.status == 0 or exc.status == 429 or exc.status >= 500


def _breaker_admit(account: str, *, probe: bool) -> None:
    """Raise :class:`RentvineUnavailableError` unless a request may be sent.

    ``probe`` is True for reads, which may be the one half-open probe, and
    False for writes, which are refused until the breaker has CLOSED.
    """
    b = _breakers.get(account)
    if b is None or b.open_until is None:
        return
    now = _now()
    if now < b.open_until:
        raise RentvineUnavailableError(b.open_until - now)
    # Half-open.
    if not probe:
        raise RentvineUnavailableError(None)
    if b.probe_started is not None and now - b.probe_started < BREAKER_PROBE_STALE_S:
        raise RentvineUnavailableError(None)
    b.probe_started = now


def _breaker_record(account: str, exc: RentvineAPIError | None) -> None:
    """Record the outcome of a request that was SENT. ``None`` is success."""
    b = _breakers.setdefault(account, _Breaker())
    now = _now()
    probing = b.probe_started is not None
    if exc is None or not _is_outage(exc):
        # Rentvine answered. That closes a half-open breaker and breaks any run
        # of consecutive failures.
        b.failures.clear()
        b.open_until = None
        b.probe_started = None
        return
    if probing:
        b.probe_started = None
        b.failures.clear()
        b.open_until = now + BREAKER_OPEN_S
        logger.warning(
            "rentvine %s: probe failed, circuit stays open %.0fs", account, BREAKER_OPEN_S
        )
        return
    b.failures = [t for t in b.failures if now - t <= BREAKER_WINDOW_S]
    b.failures.append(now)
    if len(b.failures) >= BREAKER_FAILURES:
        b.failures.clear()
        b.open_until = now + BREAKER_OPEN_S
        logger.warning(
            "rentvine %s: %d consecutive failures, circuit open %.0fs",
            account,
            BREAKER_FAILURES,
            BREAKER_OPEN_S,
        )


class RentvineAPIError(Exception):
    """A request reached Rentvine and came back unusable.

    Carries the classified meaning rather than a bare status, so callers act on
    *what it means* — a 403 is a role problem the customer fixes in Rentvine, a
    401 is a credential problem they fix by reconnecting, and telling them the
    wrong one sends them to redo something that works (gotcha 102).
    """

    def __init__(
        self,
        status: int,
        failure: transport.Failure,
        retryable: bool,
        detail: str,
        *,
        retry_after: float | None = None,
        sent: bool = True,
    ):
        self.status = status
        self.failure = failure
        self.retryable = retryable
        self.detail = detail
        #: Seconds the SERVER asked us to wait, when it said so. ``None`` is
        #: the ordinary case and means "it did not say", never "wait zero".
        self.retry_after = retry_after
        #: ``False`` ONLY when Occupella refused before anything left the
        #: process (a Retry-After hold, the circuit breaker). A caller guarding
        #: a write reads it to tell "provably not sent, safe to retry" from
        #: "may have landed" (``agents/rentvine_tools``). Default ``True``:
        #: claiming not-sent wrongly would invite a double write.
        self.sent = sent
        super().__init__(f"rentvine {status} ({failure.value}): {detail[:200]}")


class RentvineTruncatedError(RentvineAPIError):
    """An unpaged read that the server's OWN headers say came back short.

    ⚠ **MEASURED 2026-09-24 on demoaccount41, and it turned a false PASS into
    this class.** ``GET /leases`` and ``GET /properties/{propertyID}/units``
    declare no ``page`` in the spec, yet the server pages them anyway and says
    so in the response: ``/leases`` sent 25 rows with ``pagination-total-items:
    28``, ``pagination-total-pages: 2``; a 120-unit property's units sent 20.
    The sync and Phase 8's "independent" count read the same way, so they
    agreed with each other and Phase 8 scored the units RECONCILED while 100
    were missing.

    Carries what DID arrive (:attr:`rows`, all real) and what the server says
    exists (:attr:`server_total`), so a caller can write the rows it has and
    report the rest as stale. A caller that does neither gets an error, never a
    short list that reads as the whole (gotchas 26/55).
    """

    def __init__(self, template: str, rows: list[dict[str, Any]], server_total: int | None):
        self.rows = rows
        self.server_total = server_total
        held = f"{server_total}" if server_total is not None else "more (total-pages > 1)"
        super().__init__(
            200,
            transport.Failure.UNDOCUMENTED,
            False,
            f"{template}: Rentvine sent {len(rows)} rows and says it holds {held}. The spec "
            f"declares no paging here, so the rest cannot be asked for.",
        )


class RentvineUnavailableError(RentvineAPIError):
    """Occupella's circuit breaker refused to send (terms rules 25 and 28).

    ``remaining`` is seconds until a probe is allowed, or ``None`` while the
    probe is deciding. Nothing was sent, so nothing changed in Rentvine.
    Not retryable: the retry loop must stop rather than wait it out.
    """

    def __init__(self, remaining: float | None):
        self.remaining = remaining
        when = (
            f"for about {max(1, math.ceil(remaining / 60))} more minute(s)"
            if remaining is not None
            else "while it checks whether Rentvine is back"
        )
        super().__init__(
            503,
            transport.Failure.UNAVAILABLE,
            False,
            f"Rentvine unavailable: not sent. Rentvine kept failing, so Occupella "
            f"paused calls to it {when}.",
            sent=False,
        )


def _retry_after_seconds(headers: httpx.Headers) -> float | None:
    """Read ``Retry-After`` as seconds to wait, or ``None`` when it asks for none.

    Both legal forms count, because every ``Retry-After`` is binding (terms
    rule 1): delay-seconds, and an HTTP-date. The date used to be skipped as
    "a second thing to get wrong", which meant a server that said "come back at
    07:28" was answered on our own schedule instead.

    ``None`` is "no wait asked": no header, ``0``, or a date already past. It is
    never returned for a header we cannot read. Junk, a negative, or a value
    that is not finite reads as :data:`UNREADABLE_RETRY_AFTER_S`, which stops
    the request and holds the account rather than treating the unknown as zero.
    """
    raw = headers.get("retry-after")
    if raw is None:
        return None
    raw = raw.strip()
    try:
        seconds = float(raw)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(raw)
        except (TypeError, ValueError, IndexError):
            return UNREADABLE_RETRY_AFTER_S
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - datetime.now(UTC)).total_seconds()
        return seconds if seconds > 0 else None
    if not math.isfinite(seconds) or seconds < 0:
        return UNREADABLE_RETRY_AFTER_S
    return seconds if seconds > 0 else None


def _schedule_for(exc: RentvineAPIError) -> tuple[float, ...]:
    """The backoff a failure earns. A 429 that names no wait gets the slower
    one; retryability itself is the classifier's call, never decided here."""
    if exc.status == 429 and exc.retry_after is None:
        return RATE_LIMIT_BACKOFF_S
    return RETRY_BACKOFF_S


def _retry_delay(exc: RentvineAPIError, fallback: float) -> float | None:
    """How long to wait before the next attempt, or ``None`` to stop.

    The server's own number wins when it sent one, and can never get LESS than
    our fallback (waiting longer is never the harmful direction under a rate
    limit). An ask above :data:`MAX_RETRY_AFTER_S` is ``None``: the request
    stops rather than coming back before the server said it could.
    """
    asked = exc.retry_after
    if asked is None:
        return fallback
    if asked > MAX_RETRY_AFTER_S:
        return None
    return max(asked, fallback)


@dataclass(frozen=True)
class Page:
    """One page of a collection, and whether another follows."""

    rows: list[dict[str, Any]]
    #: ⚠ Read this with :attr:`total_pages` in mind. When Rentvine sent
    #: ``pagination-total-pages`` this is a FACT ("page N of M"); when it did
    #: not, it degrades to the heuristic and means only "this page was full, so
    #: there MIGHT be another" (``transport.page_looked_complete``).
    may_have_more: bool
    #: Rentvine's own page count, or ``None`` when it did not say.
    total_pages: int | None = None
    #: Rentvine's own row count across all pages, or ``None``.
    total_items: int | None = None

    @property
    def stop_condition_was_documented(self) -> bool:
        """Whether the loop is following Rentvine or guessing.

        Worth surfacing rather than hiding: a sync that guessed its way to the
        end is a different claim from one the server confirmed, and a caller
        reporting "you have N properties" should know which it has.

        ⚠ **THIS IS THE PER-PAGE ANSWER AND IS NOT THE ONE A CALLER WANTS.**
        :attr:`Collection.stop_was_documented` is the collection-level claim,
        and it is deliberately WIDER: a run that reconciled against
        ``pagination-total-items`` also stopped on a server fact, even on a
        page that carried no ``total-pages``. Reading this one directly from
        outside the paging loop would under-claim on exactly the collections
        the reconcile makes strongest.
        """
        return self.total_pages is not None


@dataclass(frozen=True)
class Collection:
    """Every row of a paged collection, and whether the END was a server fact.

    ⚠ **THE SECOND FIELD IS WHY THIS TYPE EXISTS, and it exists because the
    first version of this module returned a bare ``list``.**
    ``SyncResult.confirmed_complete`` is documented as *"tables whose
    collection Rentvine confirmed a total for, versus tables where the loop
    guessed the end from a short page"*, and ``sync.py`` added the table on any
    fetch that did not raise — so the flag made the opposite of the
    distinction it claims, and :attr:`Page.stop_condition_was_documented`, the
    property built to answer exactly this, had no production reader at all
    (``grep`` found the definition and two test assertions). Third instance in
    this package of a value computed, carried, and read by nobody — gotcha 147
    is the retry flag and gotcha 148 the mapped date.

    ⚠ **RETURNED AS A TYPE RATHER THAN LEFT TO A CONVENTION.** A sibling
    ``iter_all_with_provenance`` would be two homes for the paging loop
    (gotcha 25), and a ``list`` subclass carrying an attribute is lost silently
    by any ``list(...)`` copy — present in the source, absent in effect
    (gotchas 39/97). Making the caller unwrap ``.rows`` is what forces the
    completeness question to be answered rather than assumed, which is
    gotcha 91's reasoning about the reminder sweep's required ``today``.
    """

    rows: list[dict[str, Any]]
    #: ``True`` only when Rentvine itself said where the collection ends —
    #: either it sent ``pagination-total-pages`` and the loop followed it, or
    #: it sent ``pagination-total-items`` and :meth:`RentvineClient.iter_all`
    #: reconciled the row count against it. ``False`` means the loop stopped on
    #: ``transport.page_looked_complete``, which is a guess from a short page.
    stop_was_documented: bool


class RentvineClient:
    """Read-only client bound to ONE company's credentials.

    Construct it from :func:`helixis.rentvine.credentials.load_rentvine_settings_for_company`.
    There is deliberately no constructor that reads global settings — a client
    that can be built without naming a company is a client that can be pointed
    at the wrong account.
    """

    def __init__(
        self,
        settings: RentvineSettings,
        *,
        client: httpx.AsyncClient | None = None,
        company_id: str | None = None,
    ):
        #: For the per-call log only. Falls back to the request's company.
        self._company_id = company_id
        if not settings.account_code:
            raise ValueError("RentvineClient requires an account_code; refusing to guess one")
        if not settings.api_key or not settings.api_secret:
            raise ValueError("RentvineClient requires both api_key and api_secret")
        self._settings = settings
        self._base = resolved_base_url(settings)
        self._owns_client = client is None
        # SETTLED 2026-09-23 against the live `demoaccount41` sandbox, WITH THE
        # CONTROL the marker this replaces asked for. `Basic(api_key,
        # api_secret)` returned 200 carrying real property rows;
        # `Basic(api_secret, api_key)` — the same credential pair, swapped —
        # returned 401. A one-sided probe could not have told a working
        # orientation from an account that accepts anything, so the swap IS the
        # evidence. Confirmed a second time by `POST /api/v1/rentvine/test`
        # going through this constructor and reporting 12 properties.
        # ⚠ Do NOT "tidy" this into `auth=(secret, key)`: the spec's
        # `securitySchemes.basicAuth` and the Launch-Engine reference client
        # both declare key-as-username, and all three now agree.
        self._client = client or httpx.AsyncClient(
            auth=(settings.api_key, settings.api_secret),
            timeout=settings.timeout_seconds,
            headers={"Accept": "application/json"},
        )
        self._gate = asyncio.Semaphore(max(1, settings.concurrency))

    async def __aenter__(self) -> RentvineClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def post(
        self,
        template: str,
        *,
        json_body: dict[str, Any] | None = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
        path_params: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> Any:
        """The ONE write method. Phase 6.

        Exactly one body: ``json_body``, or ``files`` for the one multipart
        operation (``POST /files``, ``{"file": (name, bytes, content type)}``).
        ``query`` is checked against the captured spec like a read's, so an
        undeclared parameter is refused before anything is sent.

        ⚠ **THIS CLASS WAS READ-ONLY BY CONSTRUCTION AND THIS IS THE DIFF A
        REVIEWER SEES.** The module docstring's claim was that a later edit
        asking it to write would have to ADD A METHOD rather than pass an
        argument — so the guarantee is not weakened by this existing, it is
        collected. What replaces it: there is exactly ONE write method, it
        hardcodes POST, `test_rentvine_sync` pins that no second one appears,
        and `test_rentvine_confirm_gate` pins that every caller of this one runs
        inside a confirm gate. (This used to name `test_rentvine_write_tool` for
        the second claim, which never checked it.)

        ⚠ **Rentvine has no PUT and no PATCH — updates are POST** (T1: 78 POST,
        18 DELETE, zero PUT/PATCH). So "is this a create or an update" is a
        property of the PATH, not the verb, and a caller cannot tell them apart
        from the method name. That is why the read-merge-write lives in
        ``rentvine/work_orders.py`` and not here.

        ⚠ The body is NOT validated against ``BODY_FIELDS`` here. The spec
        declares 38 fields with ``required: []`` on the work-order update while
        CREATE requires 8 — a permissive schema is not a promise, and a
        transport that quietly dropped an undeclared field would hide a real
        mismatch instead of surfacing it. Callers validate; this sends.
        """
        if (json_body is None) == (files is None):
            raise ValueError("post() sends exactly one body: json_body or files")
        query = transport.validate_query("POST", template, query or {})
        path = transport.build_path(template, **(path_params or {}))
        url = f"{self._base}{path}"
        body: dict[str, Any] = {"json": json_body} if files is None else {"files": files}
        async with self._gate:
            # ⚠ A write is never sent into a hold, and never waits one out
            # either: it runs inside a person's confirm, so it refuses at once
            # and says how long Rentvine asked for (terms rule 1).
            remaining = _held_for(self._settings.account_code)
            if remaining is not None:
                self._log_call("POST", template, None, "held", 1, time.monotonic())
                raise _held_error(remaining)
            # ⚠ Rule 28: while the breaker is open OR half-open, the gate
            # refuses writes. A write is never the probe.
            try:
                _breaker_admit(self._settings.account_code, probe=False)
            except RentvineUnavailableError:
                self._log_call("POST", template, None, "circuit_open", 1, time.monotonic())
                raise
            await _rate_bucket_for(self._settings).acquire()
            started = time.monotonic()
            try:
                resp = await self._client.post(url, params=query or None, **body)
            except httpx.HTTPError as exc:
                self._log_call("POST", template, None, "transport_error", 1, started)
                transport_exc = RentvineAPIError(
                    0, transport.Failure.UNDOCUMENTED, True, f"transport: {exc}"
                )
                _breaker_record(self._settings.account_code, transport_exc)
                raise transport_exc from exc
        _breaker_record(
            self._settings.account_code,
            None
            if resp.status_code < 400
            else RentvineAPIError(resp.status_code, transport.Failure.UNDOCUMENTED, False, ""),
        )
        outcome = "ok"
        if resp.status_code >= 400:
            outcome = transport.classify_status(resp.status_code)[0].value
        self._log_call("POST", template, resp.status_code, outcome, 1, started)
        if resp.status_code >= 400:
            failure, retryable = transport.classify_status(resp.status_code)
            retry_after = _retry_after_seconds(resp.headers)
            self._note_throttle(resp.status_code, retry_after)
            raise RentvineAPIError(
                resp.status_code,
                failure,
                retryable,
                self._scrub(resp.text),
                retry_after=retry_after,
            )
        try:
            return resp.json()
        except ValueError:
            # ⚠ A 2xx with an unreadable body is NOT a failure — gotcha 70's
            # rule pointed the other way: a 2xx is not the write either. The
            # caller's read-back verify is what decides, so this returns None
            # rather than raising and making a successful write look failed.
            return None

    async def get(
        self,
        template: str,
        *,
        path_params: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> Any:
        """Issue ONE documented GET. The verb is hardcoded — see the module docstring.

        ``template`` is the spec's path template (``/properties/{propertyID}``),
        not an already-built path, so ``transport`` can check it against the
        captured spec before anything leaves the process. An undocumented
        operation or an undeclared query parameter raises rather than being
        sent: a filter Rentvine ignores is worse than one it rejects, because a
        silently-ignored filter returns the WHOLE account and the caller cannot
        tell (gotcha 24 is exactly this, on Buildium).
        """
        resp = await self._get_with_retry(template, path_params=path_params, query=query)
        # ⚠ @unverified — the response ENVELOPE. The spec's error shape is
        # documented (`{"error": string}`) and its success shape is not: whether
        # a collection arrives as a bare JSON array or wrapped in an object is
        # not stated anywhere. Both readings are handled below rather than one
        # being guessed; probe B settles it.
        return resp.json()

    async def _get_once(self, url: str, query: dict[str, Any]) -> httpx.Response:
        """ONE GET, no retry. The verb is hardcoded — see the module docstring.

        Raises :class:`RentvineAPIError` for anything unusable, carrying the
        classification :func:`transport.classify_status` made rather than a bare
        status, so the layer above can act on MEANING instead of re-deciding
        which statuses are worth another attempt.
        """
        try:
            resp = await self._client.get(url, params=query or None)
        except httpx.HTTPError as exc:
            # Transport-level failure never reached Rentvine, so it carries
            # no meaning about the account. Retryable by definition.
            raise RentvineAPIError(
                0, transport.Failure.UNDOCUMENTED, True, f"transport: {exc}"
            ) from exc
        if resp.status_code >= 400:
            failure, retryable = transport.classify_status(resp.status_code)
            retry_after = _retry_after_seconds(resp.headers)
            self._note_throttle(resp.status_code, retry_after)
            raise RentvineAPIError(
                resp.status_code,
                failure,
                retryable,
                self._scrub(resp.text),
                retry_after=retry_after,
            )
        return resp

    def _log_call(
        self,
        method: str,
        template: str,
        status: int | None,
        outcome: str,
        attempt: int,
        started: float,
    ) -> None:
        """The per-call audit line (terms rule 22; ``observability.pms_calls``).
        The endpoint is the spec TEMPLATE, so no record id or value is logged."""
        log_pms_call(
            pms="rentvine",
            company_id=self._company_id,
            account=self._settings.account_code,
            method=method,
            endpoint=template,
            status=status,
            outcome=outcome,
            attempt=attempt,
            duration_ms=(time.monotonic() - started) * 1000,
        )

    def _scrub(self, text: str) -> str:
        """Rentvine's error body with this client's credentials masked.

        The body becomes the error's detail, which the retry log, the sync, the
        routes and the sync-state row all print. A server or proxy that echoes
        the request (the key, the secret, or the Basic header built from them,
        in either order) would otherwise put a working credential in the logs
        (terms rule 11).
        """
        key, secret = self._settings.api_key, self._settings.api_secret
        tokens = [
            base64.b64encode(f"{a}:{b}".encode()).decode()
            for a, b in ((key, secret), (secret, key))
        ]
        for value in (*tokens, secret, key):
            if value:
                text = text.replace(value, "***")
        return text

    def _note_throttle(self, status: int, retry_after: float | None) -> None:
        """Hold the account for as long as Rentvine asked (terms rule 1).

        A 429 with no ``Retry-After`` still holds, for the first step of
        :data:`RATE_LIMIT_BACKOFF_S`, so sibling requests queued behind the
        gate wait too instead of hitting the same limit a millisecond later.
        """
        if retry_after is not None:
            _hold_account(self._settings.account_code, retry_after)
        elif status == 429:
            _hold_account(self._settings.account_code, RATE_LIMIT_BACKOFF_S[0])

    async def _wait_out_hold(self) -> None:
        """Wait out a short hold; refuse, without sending, during a long one."""
        remaining = _held_for(self._settings.account_code)
        if remaining is None:
            return
        if remaining > MAX_RETRY_AFTER_S:
            raise _held_error(remaining)
        await asyncio.sleep(remaining)

    async def _get_with_retry(
        self,
        template: str,
        *,
        path_params: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """A GET, retried while the classifier calls the failure retryable.

        **WHY THIS EXISTS.** ``transport.classify_status`` computed
        ``retryable`` from the day it was written, ``RentvineAPIError`` carried
        it, and NOTHING read it — ``sync.py`` caught every failure identically,
        logged a warning and recorded the table as failed. So a 429 mid-sync and
        a 401 were the same event: ``helixis_units`` marked failed for the whole
        run, and ``scripts/rentvine_reconcile.py`` then reporting a mirror-vs-API
        mismatch that reads as a sync BUG rather than a transient blip. A full
        sandbox sync issues ~66 per-property unit calls plus six collections —
        exactly the shape that trips an undocumented limit — so the first person
        to run one would have chased a phantom.

        ⚠ **IT READS ``exc.retryable`` AND NEVER A STATUS LIST OF ITS OWN.** A
        second list here is the two-homes drift gotcha 25 exists to stop, and
        this one would drift in the expensive direction: the classifier's rule
        is *everything undocumented is retryable, including 429*, and a
        hand-copied list is exactly what stops covering a status the vendor
        starts sending.

        ⚠ **READS ONLY — :meth:`post` deliberately does not come through here.**
        Rentvine has no PUT and no PATCH, so that one method is every write this
        client can make, and a retried write that already landed is a duplicate
        in a landlord's books. Gotcha 70 is the standing record that a 2xx is not
        the write; the mirror image is that a FAILURE is not proof the write did
        not happen. Reads are idempotent by construction, which is the whole
        reason a retry is safe here and nowhere else.

        ⚠ **The concurrency gate is held ACROSS the backoff, not released
        between attempts.** A sleeping request keeping its slot is what turns a
        rate-limit response into back-pressure on the whole sync; releasing it
        would free the slot for a sibling to hit the same limit a millisecond
        later, which is a busier client at exactly the moment the server asked
        for a quieter one.
        """
        query = transport.validate_query("GET", template, dict(query or {}))
        path = transport.build_path(template, **(path_params or {}))
        url = f"{self._base}{path}"
        attempt = 0
        async with self._gate:
            while True:
                # ⚠ Before EVERY send, the first one included: a hold another
                # request set is binding on this one too (terms rule 1).
                await self._wait_out_hold()
                # ⚠ Before EVERY attempt, retries included: once this read's
                # own failures open the breaker, the next retry is refused
                # here instead of adding to the load (rule 25).
                try:
                    _breaker_admit(self._settings.account_code, probe=True)
                except RentvineUnavailableError:
                    self._log_call(
                        "GET", template, None, "circuit_open", attempt + 1, time.monotonic()
                    )
                    raise
                await _rate_bucket_for(self._settings).acquire()
                started = time.monotonic()
                try:
                    resp = await self._get_once(url, query)
                except RentvineAPIError as exc:
                    _breaker_record(self._settings.account_code, exc)
                    self._log_call(
                        "GET", template, exc.status or None, exc.failure.value, attempt + 1, started
                    )
                    schedule = _schedule_for(exc)
                    if (
                        not exc.retryable
                        or attempt >= len(RETRY_BACKOFF_S)
                        or attempt >= len(schedule)
                    ):
                        # ⚠ The ORIGINAL error, never a wrapper. `sync.py` puts
                        # `str(exc)` in `result.failed` and the operator reads
                        # it; burying the status behind "retries exhausted"
                        # loses the 401-vs-403 distinction gotcha 102 is about.
                        raise
                    wait = _retry_delay(exc, schedule[attempt])
                    if wait is None:
                        # Rentvine asked for longer than one request waits out.
                        # Stop; the hold keeps every other request off it too.
                        raise
                    delay = _jitter(wait)
                    attempt += 1
                    logger.info(
                        "rentvine %s: %s — retrying in %.1fs (attempt %d of %d)",
                        path,
                        exc,
                        delay,
                        attempt + 1,
                        len(RETRY_BACKOFF_S) + 1,
                    )
                    await asyncio.sleep(delay)
                    continue
                _breaker_record(self._settings.account_code, None)
                self._log_call("GET", template, resp.status_code, "ok", attempt + 1, started)
                return resp

    async def _get_with_headers(
        self,
        template: str,
        *,
        path_params: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> tuple[Any, dict[str, str]]:
        """As :meth:`get`, but keeps the response headers.

        Separate from :meth:`get` so the common case stays a one-liner; the
        pagination metadata only matters to the paging loop.
        """
        resp = await self._get_with_retry(template, path_params=path_params, query=query)
        return resp.json(), dict(resp.headers)

    async def get_rows(
        self,
        template: str,
        *,
        path_params: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Every row of a collection that **cannot be paged**, in one request.

        A handful of Rentvine collections declare no ``page``/``pageSize`` at all
        — ``GET /leases`` and ``GET /properties/{propertyID}/units`` among them —
        so :meth:`iter_all` cannot be used on them: ``transport.validate_query``
        refuses the pagination pair before a request is built, which is correct
        (a parameter Rentvine does not recognise is IGNORED, not rejected).

        ⚠ **THIS METHOD CANNOT ESTABLISH COMPLETENESS AND DOES NOT CLAIM TO.**
        There is no next page to ask for, so what comes back is whatever the
        server chose to send. Callers must not report the result as a total —
        see ``sync._sync_leases``, which deliberately leaves leases out of
        ``confirmed_complete``.

        ⚠ **BUT IT CAN TELL WHEN IT IS SHORT, AND THEN IT RAISES.** Measured
        2026-09-24: the server pages these routes anyway and sends
        ``pagination-total-items`` / ``-total-pages`` with the first page. When
        they say more exists, this raises :class:`RentvineTruncatedError`
        carrying the rows that did arrive.

        ⚠ It REFUSES a pageable operation, and that refusal is the point. Calling
        the unpaged read on an endpoint that pages returns the server's own
        default slice — 15, 20, 25 or 250 rows depending on the operation — with
        a 200 and no indication that anything was left behind. That is a
        fabricated total (gotchas 26/55), and it is the mistake this method's
        existence otherwise invites. So the two readers are mutually exclusive by
        construction: ``iter_all`` refuses the endpoints that cannot page, and
        this refuses the ones that can.
        """
        key = transport.operation_key("GET", template)
        declared = transport.QUERY_PARAMS.get(key)
        if declared is not None and transport.PAGE_PARAM in declared:
            raise RentvineAPIError(
                0,
                transport.Failure.BAD_REQUEST,
                False,
                f"{key} declares {transport.PAGE_PARAM} — use iter_all(). An "
                f"unpaged read of a pageable collection returns the server's "
                f"default page and nothing says so, which would report a slice "
                f"of the customer's data as the whole of it.",
            )
        body, headers = await self._get_with_headers(template, path_params=path_params, query=query)
        rows = _rows_from(body, key)
        # ⚠ The server pages these routes even though the spec says they cannot
        # be paged, and its headers say so (RentvineTruncatedError). Believe the
        # headers: a short read is raised, never returned as the whole.
        total_items = _int_header(headers, "pagination-total-items")
        total_pages = transport.total_pages_from(headers)
        if (total_items is not None and total_items > len(rows)) or (
            total_pages is not None and total_pages > 1
        ):
            raise RentvineTruncatedError(template, rows, total_items)
        return rows

    async def get_object(
        self,
        template: str,
        *,
        id_field: str,
        path_params: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """ONE record, unwrapped from whatever envelope it arrives in.

        The single-object envelope, measured 2026-10-02 on a work order: the
        record arrives WRAPPED under a key naming its type, beside scalar
        siblings ({"workOrder": {...}, "schedulingStatusID": ...}). Bare is
        handled too; a third shape RAISES rather than being guessed at, because
        a record read as empty would blank a mirror row that had real values.

        ⚠ The unwrap keys on ``id_field`` — a field the mapper already depends
        on — rather than on a wrapper NAME we would be inventing. That is the
        difference between reading the contract and writing one. Settled by a
        Phase 8 probe: one real GET names the shape for good.
        """
        body = await self.get(template, path_params=path_params, query=query)
        if not isinstance(body, dict):
            raise RentvineAPIError(
                0,
                transport.Failure.UNDOCUMENTED,
                False,
                f"{template} returned {type(body).__name__}, not an object",
            )
        if any(k.lower() == id_field.lower() for k in body):
            return body
        wrapped = [
            v
            for v in body.values()
            if isinstance(v, dict) and any(k.lower() == id_field.lower() for k in v)
        ]
        if len(wrapped) == 1:
            return wrapped[0]
        raise RentvineAPIError(
            0,
            transport.Failure.UNDOCUMENTED,
            False,
            f"{template} returned an object with no {id_field} at the top level and "
            f"{len(wrapped)} nested candidates — refusing to guess which is the record. "
            f"Keys: {sorted(body)[:12]}",
        )

    async def get_page(
        self,
        template: str,
        *,
        page: int,
        page_size: int = PAGE_SIZE,
        path_params: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        companion: str | None = None,
        nullable_companions: tuple[str, ...] = (),
        optional_companions: tuple[str, ...] = (),
    ) -> Page:
        """One page of a collection, preferring Rentvine's own page count.

        ``companion`` keeps one SIBLING record the row carries beside its
        subject, under its own name on the returned record. It exists for
        ``GET /properties/units/export``, whose rows are ``{"property", "unit"}``
        (probe K, gotcha 159): the unit alone has no ``propertyID``. Only a
        companion ``ROW_COMPANIONS`` declares for the operation is accepted, so
        this cannot be used to invent a join the spec does not describe.

        ``nullable_companions`` are siblings the spec declares MAY be null
        (``GET /accounting/payables/search``'s ``property`` is null for a
        manager-level charge). An explicit null is kept as ``None``; a row
        with the KEY missing still raises, because that is a shape the spec
        does not describe, not a documented null.

        ``optional_companions`` are siblings the spec declares but does not
        mark required, read beside a record whose own read must not depend on
        them (``balances`` on the live lease export): a missing key is carried
        as ``None`` and anything else as sent, for the mapper to judge; an
        unusable value is "unknown", never zero.
        """
        key = transport.operation_key("GET", template)
        for name in (
            *([companion] if companion is not None else []),
            *nullable_companions,
            *optional_companions,
        ):
            if name not in ROW_COMPANIONS.get(key, ()):
                raise RentvineAPIError(
                    0,
                    transport.Failure.BAD_REQUEST,
                    False,
                    f"{key} declares no {name!r} companion (ROW_COMPANIONS).",
                )
        params = dict(query or {})
        params.update(transport.page_params(page, page_size))
        body, headers = await self._get_with_headers(
            template, path_params=path_params, query=params
        )
        rows = _rows_from(body, key)
        if companion is not None:
            rows = _with_companion(rows, _body_rows(body), companion)
        for name in nullable_companions:
            rows = _with_companion(rows, _body_rows(body), name, nullable=True)
        for name in optional_companions:
            rows = _with_companion(rows, _body_rows(body), name, optional=True)
        total_pages = transport.total_pages_from(headers)
        total_items = _int_header(headers, "pagination-total-items")
        if total_pages is not None:
            # Rentvine said how many pages there are. Believe it.
            more = page < total_pages
        else:
            # The six header-less operations, or a server that did not send it.
            more = transport.page_looked_complete(len(rows), page_size)
        return Page(
            rows=rows,
            may_have_more=more,
            total_pages=total_pages,
            total_items=total_items,
        )

    async def iter_all(
        self,
        template: str,
        *,
        page_size: int = PAGE_SIZE,
        path_params: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        max_pages: int = MAX_PAGES,
        companion: str | None = None,
        nullable_companions: tuple[str, ...] = (),
        optional_companions: tuple[str, ...] = (),
    ) -> Collection:
        """Every row of a collection, paging until a page comes back short.

        ⚠ The loop stops on the first non-full page and accepts one wasted
        request at an exact-multiple boundary. That is the only safe rule while
        the stop condition is undocumented — see
        ``transport.page_looked_complete``.

        ⚠ It also stops at ``max_pages`` and says so LOUDLY rather than
        returning a quietly-truncated list, because a short list that looks
        complete is this product's worst failure mode (gotchas 26/55): a caller
        would report "you have 20,000 work orders" as if it were the total.

        ⚠ **IT RETURNS A :class:`Collection`, NOT A LIST, and the second field
        is the point** — see that class for what went wrong without it. The
        rule it carries is *either* server fact, never our heuristic: the last
        page named the page count, OR the server named the row count and the
        reconcile below agreed with it. An empty collection fetched with no
        headers at all is therefore ``stop_was_documented=False`` — "we think
        you have zero properties" is exactly the claim that must not launder
        into a confirmed total (gotchas 26/55/57).
        """
        out: list[dict[str, Any]] = []
        expected: int | None = None
        for page in range(1, max_pages + 1):
            got = await self.get_page(
                template,
                page=page,
                page_size=page_size,
                path_params=path_params,
                query=query,
                companion=companion,
                nullable_companions=nullable_companions,
                optional_companions=optional_companions,
            )
            out.extend(got.rows)
            if expected is None:
                expected = got.total_items
            if not got.may_have_more:
                # ⚠ RECONCILE when Rentvine told us the total. Phase 8's pass
                # criterion is that the mirror's row count equals the API's;
                # this is that check at the point where it is cheapest, and a
                # mismatch means the loop stopped early — which would otherwise
                # surface as a customer's portfolio quietly missing records.
                if expected is not None and len(out) != expected:
                    logger.warning(
                        "rentvine %s: collected %d rows but the server said "
                        "pagination-total-items=%d — treating the collection as "
                        "INCOMPLETE rather than reporting the short count",
                        template,
                        len(out),
                        expected,
                    )
                    raise RentvineAPIError(
                        0,
                        transport.Failure.UNDOCUMENTED,
                        True,
                        f"{template}: paged to the end with {len(out)} rows but "
                        f"pagination-total-items said {expected}. Refusing to "
                        f"return a short collection as if it were complete.",
                    )
                # ⚠ Reads the PAGE's own property rather than re-deriving
                # `total_pages is not None` here — a second spelling of the
                # same rule is the two-homes drift gotcha 25 exists to stop,
                # and this is the reader that property never had.
                return Collection(
                    rows=out,
                    stop_was_documented=got.stop_condition_was_documented or expected is not None,
                )
        raise RentvineAPIError(
            0,
            transport.Failure.UNDOCUMENTED,
            False,
            f"{template} still returned full pages at the {max_pages}-page ceiling "
            f"({len(out)} rows so far). Refusing to return a truncated collection "
            f"as if it were complete. Raise MAX_PAGES only after a live probe "
            f"establishes what the server does past the last page.",
        )


def _int_header(headers: dict[str, str], name: str) -> int | None:
    """A non-negative integer header, or ``None`` on anything else.

    Same posture as ``transport.total_pages_from``: never a guess. A junk value
    read as 0 would turn "we could not tell" into "you have nothing".
    """
    raw = headers.get(name) or {k.lower(): v for k, v in headers.items()}.get(name)
    if raw is None:
        return None
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _body_rows(body: Any) -> list[dict[str, Any]]:
    """Pull the row list out of a collection response BODY.

    ⚠ @unverified — see :meth:`RentvineClient.get`. The success envelope is
    undocumented, so BOTH plausible shapes are accepted: a bare array, or an
    object with exactly one list-valued key. What is NOT done is picking a key
    name by guesswork — an object with several list values raises, because
    choosing one would be inventing the contract. Probe A settles it with one
    call.
    """
    if isinstance(body, list):
        return [r for r in body if isinstance(r, dict)]
    if isinstance(body, dict):
        lists = {k: v for k, v in body.items() if isinstance(v, list)}
        if len(lists) == 1:
            only = next(iter(lists.values()))
            return [r for r in only if isinstance(r, dict)]
        if not lists:
            return []
        raise RentvineAPIError(
            0,
            transport.Failure.UNDOCUMENTED,
            False,
            f"collection response has {len(lists)} list-valued keys "
            f"({sorted(lists)}) and the envelope is undocumented — refusing to "
            f"guess which one holds the rows.",
        )
    return []


def _with_companion(
    records: list[dict[str, Any]],
    wire_rows: list[dict[str, Any]],
    companion: str,
    *,
    nullable: bool = False,
    optional: bool = False,
) -> list[dict[str, Any]]:
    """Each unwrapped record, with its row's ``companion`` sibling beside it.

    A row with no such sibling RAISES, for the reason ``_unwrap_rows`` does: a
    unit with no property would be written with ``property_id`` NULL, the
    broken relationship ADR 0010 Decision (a) exists to prevent. With
    ``nullable`` an explicit ``null`` is carried as ``None``; a MISSING key
    still raises. With ``optional`` a missing key is ``None`` and anything
    else is carried as sent: the subject's read never fails on the sibling,
    and the mapper decides whether what came is usable.
    """
    out: list[dict[str, Any]] = []
    for index, (record, wire) in enumerate(zip(records, wire_rows, strict=True)):
        sibling = wire.get(companion) if isinstance(wire, dict) else None
        if optional:
            # Kept as sent (an object or a list); the mapper judges its shape.
            out.append({**record, companion: sibling})
            continue
        if nullable and isinstance(wire, dict) and companion in wire and sibling is None:
            out.append({**record, companion: None})
            continue
        if not isinstance(sibling, dict):
            raise RentvineAPIError(
                0,
                transport.Failure.UNDOCUMENTED,
                False,
                f"row {index} carries no {companion!r} record beside its subject "
                f"(keys {sorted(wire) if isinstance(wire, dict) else type(wire).__name__}).",
            )
        out.append({**record, companion: sibling})
    return out


def _unwrap_rows(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    """Lift each row's record out of its per-row wrapper.

    ⚠ A ROW MISSING ITS DECLARED KEY RAISES, AND THAT IS THE WHOLE POINT.
    Returning the wrapper, or skipping the row, gives the mapper an object with
    no id; every record is then dropped for want of one and the sync reports a
    clean, complete fetch of an empty portfolio — a fabricated absence with a
    success verdict on it (gotchas 26/55/57). Raising turns the same mistake
    into one named failure on the first live call, which is the difference
    between a diagnosable bug and a customer whose mirror is silently empty.

    ⚠ The siblings are DROPPED here and deliberately not lost: ``/leases`` rows
    carry ``unit`` and ``/properties`` rows carry ``portfolio``, which is
    relationship material rather than noise. ``ROW_COMPANIONS`` names them, and
    a caller that wants one reads the un-unwrapped page rather than having this
    function guess how to merge two records into one.
    """
    out: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        record = row.get(key)
        if isinstance(record, dict):
            out.append(record)
            continue
        raise RentvineAPIError(
            0,
            transport.Failure.UNDOCUMENTED,
            False,
            f"row {index} does not carry the record under {key!r} — it has "
            f"{sorted(row)}. The spec says this collection wraps each row; "
            f"either it no longer does, or {key!r} is the wrong key for it. "
            f"Refusing to hand the wrapper to the mapper, which would read no "
            f"id off it and report the whole collection as empty.",
        )
    return out


def _rows_from(body: Any, operation: str | None = None) -> list[dict[str, Any]]:
    """The rows of a collection response, unwrapped for ``operation``.

    Two separate questions, and only one of them is undocumented. The BODY
    envelope — is the payload ``[...]`` or ``{"items": [...]}`` — is a guess
    the spec does not answer. The ROW envelope — is each element the record or
    a wrapper around it — the spec answers per operation, and
    ``_row_envelopes.ROW_ENVELOPE`` is that answer, generated from it.

    ⚠ An operation ABSENT from the map is WARNED about, not silently treated
    as bare. ``ROW_ENVELOPE.get()`` returns ``None`` for both, so without the
    membership test the two collapse — and they are different claims. Bare is
    "the spec says these rows are records". Absent is "the spec does not
    describe this as a collection at all", which means either the caller's
    template is wrong or the map is stale, and the rows are being handed on
    unwrapped on no evidence. That is precisely how ``/vendors`` was missed
    while the map was keyed on ``$ref``: it inlines its schema, so it fell out
    of the map and read as bare, with nothing anywhere saying so. The line it
    logs names the operation and the keys of the first row, because the only
    thing that can settle an unseen shape is having written down what arrived
    (gotcha 87).
    """
    rows = _body_rows(body)
    if operation is None or not rows:
        return rows
    if operation not in ROW_ENVELOPE:
        logger.warning(
            "rentvine: %s is not a collection in the captured spec, so the row "
            "envelope is unknown and %d row(s) are being passed through "
            "unwrapped. First row's keys: %s",
            operation,
            len(rows),
            sorted(rows[0]),
        )
        return rows
    key = ROW_ENVELOPE[operation]
    return rows if key is None else _unwrap_rows(rows, key)


__all__ = [
    "MAX_PAGES",
    "PAGE_SIZE",
    "Page",
    "RentvineAPIError",
    "RentvineClient",
    "RentvineTruncatedError",
    "RentvineUnavailableError",
]
