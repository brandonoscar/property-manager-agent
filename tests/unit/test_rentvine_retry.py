"""A retryable Rentvine failure must not cost a whole mirror table.

``transport.classify_status`` has computed ``retryable`` since it was written,
``RentvineAPIError`` carried it, and nothing read it: ``sync.py`` caught every
failure the same way, logged a warning, and recorded the table as failed. So a
429 halfway through a sync and a 401 were the same event, and the operator's
next step — ``scripts/rentvine_reconcile.py`` — would report a mirror-vs-API
mismatch that reads as a sync BUG rather than a blip. The eighth instance of the
``comms_triage`` shape (gotcha 62): a value produced carefully and consumed by
nobody.

⚠ **Both directions, always.** A retry that fires on everything is worse than
none — it turns a 401 into four 401s and a restricted-key 403 into a sync that
takes four times as long to tell the customer to fix their role. So every
"retries" test here has a "does NOT retry" twin (gotchas 38/51), and the counts
are asserted rather than the outcome, because the outcome is identical either
way once the attempts run out.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
from pathlib import Path

import httpx
import pytest

from helixis.rentvine import client as rvclient
from helixis.settings import RentvineSettings

# A real documented read and a real documented write. Using templates the spec
# does not declare would make `transport.validate_query` raise before a single
# byte moved, and the test would pass for a reason unrelated to retrying.
READ = "/properties"
WRITE = "/accounting/bills"

#: The SHIPPED schedule, captured before any fixture can patch it.
#:
#: ⚠ The autouse fixture below zeroes the waits so the suite does not sleep, and
#: the two invariant tests at the bottom are ABOUT the shipped values. Reading
#: the live module attribute there measured the patched tuple: one test failed
#: honestly and the other — the clamp invariant — passed VACUOUSLY against
#: ``max(()) == 0.0``, which is the direction that reports coverage it does not
#: have. Whichever numbers ship, these are them.
SHIPPED_BACKOFF = rvclient.RETRY_BACKOFF_S
SHIPPED_RATE_LIMIT_BACKOFF = rvclient.RATE_LIMIT_BACKOFF_S


def _settings(**over) -> RentvineSettings:
    return RentvineSettings(enabled=True, account_code="acme", api_key="k", api_secret="s", **over)


def _client(handler, **over) -> rvclient.RentvineClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), auth=("k", "s"))
    return rvclient.RentvineClient(_settings(**over), client=http)


def _responder(*statuses: int, headers: dict[str, str] | None = None):
    """A handler that answers each call with the next status, then 200s.

    Returns ``(handler, calls)`` where ``calls`` is a live list the test reads
    AFTER the run — the attempt COUNT is the whole assertion in most of these,
    since a retried failure and an un-retried one produce the same exception.
    """
    calls: list[str] = []
    queue = list(statuses)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if queue:
            status = queue.pop(0)
            return httpx.Response(status, text="nope", headers=headers or {})
        return httpx.Response(200, json=[])

    return handler, calls


@pytest.fixture(autouse=True)
def _no_real_sleeping(monkeypatch):
    """Keep the real ATTEMPT COUNT and drop the real waits.

    Patching the schedule rather than ``asyncio.sleep`` is deliberate: the bound
    is ``len(RETRY_BACKOFF_S) + 1``, so a schedule of the same LENGTH exercises
    exactly the production attempt count while costing no wall clock. Stubbing
    the sleep instead would leave the test asserting against a mechanism rather
    than the data the mechanism reads.
    """
    monkeypatch.setattr(rvclient, "RETRY_BACKOFF_S", (0.0,) * len(rvclient.RETRY_BACKOFF_S))
    monkeypatch.setattr(
        rvclient, "RATE_LIMIT_BACKOFF_S", (0.0,) * len(rvclient.RATE_LIMIT_BACKOFF_S)
    )


class _Clock:
    """A clock that moves when the client sleeps, and records each sleep.

    Holds are deadlines on ``rvclient._now``. With ``asyncio.sleep`` stubbed and
    the real clock left alone, time never passes and every hold looks eternal.
    """

    def __init__(self) -> None:
        self.now = 1000.0
        self.waits: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    c = _Clock()
    monkeypatch.setattr(rvclient, "_now", lambda: c.now)
    monkeypatch.setattr(rvclient.asyncio, "sleep", c.sleep)
    monkeypatch.setattr(rvclient, "_jitter", lambda delay: delay)
    return c


# ── it retries what the classifier calls retryable ───────────────────────────


def test_a_429_is_retried_and_the_read_succeeds() -> None:
    """The headline case, and the one the spec cannot warn about.

    Rentvine's OpenAPI contains zero occurrences of ``429``, which is an absence
    of documentation rather than a promise of no limit — so it classifies as
    UNDOCUMENTED/retryable, and a full sandbox sync (~66 per-property unit calls
    plus six collections) is exactly the shape that finds one.
    """
    handler, calls = _responder(429)
    client = _client(handler)
    rows = asyncio.run(client.get_rows("/leases"))
    assert rows == []
    assert len(calls) == 2, "a 429 must cost one more attempt, not the table"


def test_a_transient_5xx_is_retried() -> None:
    handler, calls = _responder(503, 502)
    client = _client(handler)
    asyncio.run(client.get_rows("/leases"))
    assert len(calls) == 3


def test_the_paging_read_retries_too() -> None:
    """`_get_with_headers` is a SECOND entry point and needs its own coverage.

    It is the one the collection sync actually uses, so a fix that reached only
    :meth:`get` would leave the six paged tables exposed — gotcha 25's fan-out,
    arriving as two near-identical methods.
    """
    handler, calls = _responder(429)
    client = _client(handler)
    page = asyncio.run(client.get_page(READ, page=1))
    assert page.rows == []
    assert len(calls) == 2


def test_a_transport_error_is_retried() -> None:
    """A connection that never reached Rentvine carries no meaning about the
    account, so the client classifies it retryable — and now acts on that."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if len(calls) == 1:
            raise httpx.ConnectError("reset", request=request)
        return httpx.Response(200, json=[])

    asyncio.run(_client(handler).get_rows("/leases"))
    assert len(calls) == 2


def test_a_server_supplied_retry_after_reaches_the_actual_wait(clock: _Clock) -> None:
    """⚠ End to end, because the two halves being right proves nothing.

    ``_retry_after_seconds`` and ``_retry_delay`` are both unit-tested above and
    both stayed green against a client that passed ``retry_after=None`` at the
    one call site that reads a real response — producer correct, consumer
    correct, nothing connecting them (gotcha 28). Observing the WAIT is the
    point here, so recording the sleep is the measurement rather than a stubbed
    mechanism.
    """
    handler, _calls = _responder(429, headers={"Retry-After": "7"})
    asyncio.run(_client(handler).get_rows("/leases"))
    assert clock.waits == [7.0], "the server's own number never reached the backoff"


# ── and refuses to retry what it must not ────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "why"),
    [
        (401, "the credential is wrong and four attempts cannot make it right"),
        (403, "a role-restricted key is a supported state, not a blip"),
        (404, "no such record is an answer, not a failure to reach one"),
        (400, "we sent something malformed; resending it is the same request"),
    ],
)
def test_a_permanent_failure_is_raised_on_the_first_attempt(status: int, why: str) -> None:
    handler, calls = _responder(status)
    client = _client(handler)
    with pytest.raises(rvclient.RentvineAPIError) as caught:
        asyncio.run(client.get_rows("/leases"))
    assert caught.value.status == status
    assert len(calls) == 1, why


def test_the_write_method_never_retries() -> None:
    """⚠ **THE ONE ASYMMETRY THAT MATTERS.**

    Rentvine has no PUT and no PATCH, so :meth:`post` is every write this client
    can make. Gotcha 70 records that a 2xx is not the write; the mirror image is
    that a failure is not proof the write did NOT land, so resending it is how a
    duplicate reaches a landlord's books. Reads are idempotent by construction,
    which is the entire reason a retry is safe on one side and not the other.
    """
    handler, calls = _responder(503)
    client = _client(handler)
    with pytest.raises(rvclient.RentvineAPIError):
        asyncio.run(client.post(WRITE, json_body={"amount": 1}))
    assert len(calls) == 1, "a failed write was resent — that is a duplicate bill"


# ── the bound, and what it raises when it is spent ───────────────────────────


def test_a_persistent_failure_raises_the_original_error() -> None:
    """`sync.py` puts ``str(exc)`` in ``result.failed`` and an operator reads it.

    Wrapping it in "retries exhausted" would lose the status and the classified
    meaning, which is the 401-vs-403 distinction gotcha 102 is about.
    """
    handler, calls = _responder(*([503] * 20))
    client = _client(handler)
    with pytest.raises(rvclient.RentvineAPIError) as caught:
        asyncio.run(client.get_rows("/leases"))
    assert caught.value.status == 503
    assert caught.value.retryable is True
    assert len(calls) == len(rvclient.RETRY_BACKOFF_S) + 1


@pytest.mark.parametrize("schedule", [(0.0,), (0.0, 0.0), (0.0, 0.0, 0.0, 0.0)])
def test_the_schedule_is_the_bound(monkeypatch, schedule: tuple[float, ...]) -> None:
    """Attempts are DERIVED from the waits, so the two cannot drift apart.

    A separately-declared attempt count is a second number describing the same
    thing, and the day somebody lengthens one list the other keeps its own idea
    of how many tries that is.
    """
    monkeypatch.setattr(rvclient, "RETRY_BACKOFF_S", schedule)
    handler, calls = _responder(*([503] * 20))
    with pytest.raises(rvclient.RentvineAPIError):
        asyncio.run(_client(handler).get_rows("/leases"))
    assert len(calls) == len(schedule) + 1


# ── Retry-After, when the server sends one ───────────────────────────────────


_UNREADABLE = rvclient.UNREADABLE_RETRY_AFTER_S


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("12", 12.0),
        (" 12 ", 12.0),
        ("2.5", 2.5),
        ("0", None),
        # ⚠ RE-POINTED (rule 19). These used to read as "no wait" and fall back
        # to our own schedule. Every Retry-After is binding now (terms rule 1),
        # so a header we cannot read is a wait we cannot see, never zero: it
        # stops the request and holds the account.
        ("-5", _UNREADABLE),
        ("", _UNREADABLE),
        ("soon", _UNREADABLE),
        ("nan", _UNREADABLE),
        ("inf", _UNREADABLE),
    ],
)
def test_retry_after_is_read_as_seconds_or_as_unreadable(raw: str, expected: float | None) -> None:
    assert rvclient._retry_after_seconds(httpx.Headers({"Retry-After": raw})) == expected


def test_an_http_date_retry_after_is_honoured() -> None:
    """⚠ RE-POINTED: the date form used to be skipped, so a server that said
    "come back at 07:28" was answered on our own schedule instead."""
    from datetime import UTC, datetime, timedelta
    from email.utils import format_datetime

    later = format_datetime(datetime.now(UTC) + timedelta(seconds=90), usegmt=True)
    got = rvclient._retry_after_seconds(httpx.Headers({"Retry-After": later}))
    assert got is not None and 80 < got <= 90


def test_an_http_date_already_past_asks_for_no_wait() -> None:
    got = rvclient._retry_after_seconds(
        httpx.Headers({"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"})
    )
    assert got is None


def test_an_absent_retry_after_is_none_not_zero() -> None:
    """``None`` means "the server did not say", never "wait nothing"."""
    assert rvclient._retry_after_seconds(httpx.Headers({})) is None


def _err(retry_after: float | None) -> rvclient.RentvineAPIError:
    from helixis.rentvine import transport

    return rvclient.RentvineAPIError(
        429, transport.Failure.UNDOCUMENTED, True, "slow down", retry_after=retry_after
    )


def test_the_server_number_wins_when_it_asks_for_longer() -> None:
    assert rvclient._retry_delay(_err(9.0), fallback=2.0) == 9.0


def test_the_server_cannot_ask_for_less_than_our_fallback() -> None:
    """Waiting longer is never the harmful direction under a rate limit."""
    assert rvclient._retry_delay(_err(0.1), fallback=2.0) == 2.0


def test_a_retry_after_longer_than_we_wait_stops_instead_of_being_shortened() -> None:
    """⚠ RE-POINTED (rule 19). This used to assert the CLAMP: 86400 became 30
    and the request went again, which is coming back before the server said we
    could (terms rules 1 and 2). An unattended sync still never parks for a day:
    the request stops, and the table is reported stale."""
    assert rvclient._retry_delay(_err(86400.0), fallback=2.0) is None
    assert rvclient._retry_delay(_err(rvclient.MAX_RETRY_AFTER_S + 1), fallback=2.0) is None


def test_no_header_means_our_own_schedule() -> None:
    assert rvclient._retry_delay(_err(None), fallback=2.0) == 2.0


def test_the_cap_cannot_invert_the_clamp() -> None:
    """⚠ The invariant, not two literals asserted twice (gotcha 109).

    If the cap ever drops below the longest fallback, a server asking for MORE
    time silently receives less than we would have waited on our own — the
    clamp doing the opposite of its job, with every behavioural test above
    still green.
    """
    assert SHIPPED_BACKOFF, "an empty schedule means no retry at all"
    assert rvclient.MAX_RETRY_AFTER_S >= max(SHIPPED_BACKOFF)
    assert rvclient.MAX_RETRY_AFTER_S >= max(SHIPPED_RATE_LIMIT_BACKOFF)
    assert rvclient.UNREADABLE_RETRY_AFTER_S > rvclient.MAX_RETRY_AFTER_S, (
        "an unreadable Retry-After must stop the request, not be waited out"
    )


def test_the_backoff_grows() -> None:
    """A flat schedule is a hot loop with extra steps."""
    assert list(SHIPPED_BACKOFF) == sorted(SHIPPED_BACKOFF)
    assert SHIPPED_BACKOFF[0] > 0, "a zero first wait is an immediate resend"
    assert list(SHIPPED_RATE_LIMIT_BACKOFF) == sorted(SHIPPED_RATE_LIMIT_BACKOFF)


def test_a_429_backs_off_more_slowly_than_a_blip() -> None:
    """A 429 is Rentvine asking for LESS load (terms rule 1)."""
    assert SHIPPED_RATE_LIMIT_BACKOFF[0] > SHIPPED_BACKOFF[0]
    assert len(SHIPPED_RATE_LIMIT_BACKOFF) <= len(SHIPPED_BACKOFF)


def test_jitter_only_ever_lengthens_a_wait() -> None:
    for _ in range(200):
        got = rvclient._jitter(10.0)
        assert 10.0 <= got <= 10.0 * (1 + rvclient.JITTER_FRACTION)


# ── a Retry-After holds the whole account, not one request ───────────────────
# The mocked throttle the rules' merge gate asks for (design/rentvine-terms-rules.md).


def test_a_429_without_retry_after_waits_on_the_slower_schedule(clock: _Clock, monkeypatch) -> None:
    monkeypatch.setattr(rvclient, "RATE_LIMIT_BACKOFF_S", (11.0, 33.0))
    monkeypatch.setattr(rvclient, "RETRY_BACKOFF_S", (0.5, 2.0, 5.0))
    handler, calls = _responder(429)
    asyncio.run(_client(handler).get_rows("/leases"))
    assert len(calls) == 2
    assert clock.waits == [11.0], "a 429 was retried on the blip schedule"


def test_a_long_retry_after_is_one_request_and_then_nothing(clock: _Clock) -> None:
    """Rentvine asks for 120s. We send nothing more to the account until then."""
    handler, calls = _responder(429, headers={"Retry-After": "120"})
    client = _client(handler)
    with pytest.raises(rvclient.RentvineAPIError) as first:
        asyncio.run(client.get_rows("/leases"))
    assert first.value.status == 429
    assert len(calls) == 1, "a 120s Retry-After was answered with a second request"
    assert clock.waits == []

    # A different read on the same account, a minute later: refused, NOT sent.
    clock.now += 60
    with pytest.raises(rvclient.RentvineAPIError) as held:
        asyncio.run(client.get_page(READ, page=1))
    assert len(calls) == 1
    assert held.value.retryable is False
    assert "not sent" in held.value.detail

    # Once the server's time is up, the account is callable again.
    clock.now += 61
    asyncio.run(client.get_rows("/leases"))
    assert len(calls) == 2


def test_a_short_hold_is_waited_out_before_the_next_send(clock: _Clock) -> None:
    rvclient._hold_account("acme", 5.0)
    handler, calls = _responder()
    asyncio.run(_client(handler).get_rows("/leases"))
    assert clock.waits == [pytest.approx(5.0)]
    assert len(calls) == 1


def test_a_write_is_never_sent_into_a_hold(clock: _Clock) -> None:
    rvclient._hold_account("acme", 5.0)
    handler, calls = _responder()
    with pytest.raises(rvclient.RentvineAPIError) as held:
        asyncio.run(_client(handler).post(WRITE, json_body={"amount": 1}))
    assert calls == [], "a write went out while Rentvine had asked us to wait"
    assert "not sent" in held.value.detail
    assert clock.waits == [], "a write must refuse at once, not sit inside a confirm"


def test_a_throttled_write_holds_the_account(clock: _Clock) -> None:
    handler, calls = _responder(429, headers={"Retry-After": "45"})
    client = _client(handler)
    with pytest.raises(rvclient.RentvineAPIError):
        asyncio.run(client.post(WRITE, json_body={"amount": 1}))
    with pytest.raises(rvclient.RentvineAPIError):
        asyncio.run(client.get_rows("/leases"))
    assert len(calls) == 1, "the read after a throttled write was sent anyway"


def test_a_hold_is_per_account(clock: _Clock) -> None:
    rvclient._hold_account("someone-else", 600.0)
    handler, calls = _responder()
    asyncio.run(_client(handler).get_rows("/leases"))
    assert len(calls) == 1 and clock.waits == []


def test_an_unreadable_retry_after_stops_and_holds(clock: _Clock) -> None:
    handler, calls = _responder(429, headers={"Retry-After": "soon-ish"})
    client = _client(handler)
    with pytest.raises(rvclient.RentvineAPIError):
        asyncio.run(client.get_rows("/leases"))
    assert len(calls) == 1
    assert rvclient._held_for("acme") is not None


# ── wiring: a behavioural test cannot see a caller that stops asking ─────────


def _fn(name: str) -> ast.AsyncFunctionDef:
    tree = ast.parse(Path(rvclient.__file__).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found — re-point this guard")


def _calls(node: ast.AST) -> set[str]:
    out: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            fn = sub.func
            if isinstance(fn, ast.Attribute):
                out.add(fn.attr)
            elif isinstance(fn, ast.Name):
                out.add(fn.id)
    return out


@pytest.mark.parametrize("reader", ["get", "_get_with_headers"])
def test_every_read_path_goes_through_the_retry(reader: str) -> None:
    """Gotchas 62/66/69/103/121/136/138: a well-covered helper whose one
    production caller quietly stopped calling it is invisible to its own tests.
    """
    assert "_get_with_retry" in _calls(_fn(reader))


def test_the_write_path_does_not() -> None:
    assert "_get_with_retry" not in _calls(_fn("post"))


def test_the_gate_encloses_the_whole_retry_loop() -> None:
    """⚠ Structural, and no behavioural test in this file can see it.

    A request that KEEPS its concurrency slot while it backs off is what turns a
    rate-limit response into back-pressure on the whole sync. Swapping the two
    statements — ``while`` outside, ``async with`` inside — reads as a tidy-up
    and releases the slot between attempts, so a sibling walks in and hits the
    same limit a millisecond later: a busier client at exactly the moment the
    server asked for a quieter one.

    ⚠ **THE FIRST VERSION OF THIS GUARD ASSERTED THE WRONG PROPERTY, and a
    mutation is what said so.** It checked that the sleep sits lexically inside
    the ``async with`` — true of the correct code AND of the swapped version,
    because the ``except`` block is nested inside the gate either way. So the
    mutation that releases the slot per attempt passed it. The property is
    NESTING, not sleep position (gotcha 58's question, answered "the test").
    """
    fn = _fn("_get_with_retry")
    gates = [n for n in ast.walk(fn) if isinstance(n, ast.AsyncWith)]
    loops = [n for n in ast.walk(fn) if isinstance(n, ast.While)]
    assert len(gates) == 1, "expected exactly one concurrency gate — re-point this guard"
    assert len(loops) == 1, "expected exactly one retry loop — re-point this guard"
    assert loops[0] in ast.walk(gates[0]), (
        "the gate is acquired per attempt — a backing-off read releases its slot"
    )

    def sleeps(node: ast.AST) -> int:
        return sum(
            1
            for n in ast.walk(node)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "sleep"
        )

    assert sleeps(fn) >= 1, "the retry stopped waiting between attempts"
    assert sleeps(gates[0]) == sleeps(fn), "the backoff sleeps outside the concurrency gate"


def test_the_retry_reads_the_classifier_and_keeps_no_status_list() -> None:
    """⚠ A second status list here is the two-homes drift gotcha 25 exists to
    stop, and it would drift in the expensive direction: the classifier's rule
    is *everything undocumented is retryable, including 429*, so a hand-copied
    list is precisely what stops covering a status the vendor starts sending.
    """
    src = inspect.getsource(rvclient.RentvineClient._get_with_retry)
    body = "\n".join(line for line in src.splitlines() if not line.lstrip().startswith("#")).split(
        '"""'
    )
    # Index 2 is everything after the docstring — the comments inside it quote
    # status codes in order to explain them (gotchas 40/59/114).
    code = body[2] if len(body) > 2 else src
    assert ".retryable" in code
    for status in ("429", "503", "401", "403"):
        assert status not in code, f"{status} hardcoded — ask the classifier instead"


def test_a_throttled_write_tells_the_manager_when_to_try_again() -> None:
    """The agent's answer when a write was refused into a hold, or throttled:
    it says nothing was sent and names a wait, not "try again later"."""
    from helixis.agents.rentvine_tools import _sanitize

    msg = _sanitize(rvclient._held_error(42.2), "update the work order")
    assert "wasn't sent" in msg and "nothing was changed" in msg
    assert "43 seconds" in msg
