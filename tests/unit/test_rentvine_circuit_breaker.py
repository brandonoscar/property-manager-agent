"""The Rentvine circuit breaker (terms rules 25 and 28).

Founder go-ahead 2026-10-01 for the design in
``design/rentvine-terms-compliance.md`` §3.2: five consecutive outage failures
inside five minutes open it for ten minutes; while open nothing is sent; then
one read probes. Every test counts SENDS, because a refused request and a sent
one that failed raise the same kind of error.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from helixis.rentvine import client as rvclient
from helixis.rentvine import transport
from helixis.settings import RentvineSettings

READ = "/properties"
WRITE = "/accounting/bills"
ACCOUNT = "acme"


def _settings() -> RentvineSettings:
    return RentvineSettings(enabled=True, account_code=ACCOUNT, api_key="k", api_secret="s")


class _Server:
    """Answers each request with the next queued status, then 200s."""

    def __init__(self, *statuses: int) -> None:
        self.queue = list(statuses)
        self.sent: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(request.method)
        if self.queue:
            status = self.queue.pop(0)
            if status < 400:
                return httpx.Response(status, json=[])
            return httpx.Response(status, text="nope")
        return httpx.Response(200, json=[])

    def client(self) -> rvclient.RentvineClient:
        http = httpx.AsyncClient(transport=httpx.MockTransport(self.handler), auth=("k", "s"))
        return rvclient.RentvineClient(_settings(), client=http)


class _Clock:
    def __init__(self) -> None:
        self.now = 10_000.0

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def clock(monkeypatch) -> _Clock:
    """No real waits, and a clock the test can move. One attempt per read, so
    each failing read is exactly one sent failure (retries are pinned in
    their own test below with the shipped schedule length)."""
    c = _Clock()
    monkeypatch.setattr(rvclient, "_now", lambda: c.now)
    monkeypatch.setattr(rvclient.asyncio, "sleep", c.sleep)
    monkeypatch.setattr(rvclient, "_jitter", lambda delay: delay)
    # No retries. RATE_LIMIT_BACKOFF_S stays as shipped: a 429 still holds the
    # account for its first step, which the fake clock sleeps through.
    monkeypatch.setattr(rvclient, "RETRY_BACKOFF_S", ())
    return c


async def _read(c: rvclient.RentvineClient) -> Exception | None:
    try:
        await c.get(READ)
    except rvclient.RentvineAPIError as exc:
        return exc
    return None


async def _write(c: rvclient.RentvineClient) -> Exception | None:
    try:
        await c.post(WRITE, json_body={})
    except rvclient.RentvineAPIError as exc:
        return exc
    return None


def _run(coro):
    return asyncio.run(coro)


def test_five_outage_failures_open_it_and_the_sixth_read_is_not_sent():
    server = _Server(503, 503, 503, 503, 503)

    async def go():
        async with server.client() as c:
            for _ in range(5):
                await _read(c)
            return await _read(c)

    exc = _run(go())
    assert len(server.sent) == 5
    assert isinstance(exc, rvclient.RentvineUnavailableError)
    assert exc.failure is transport.Failure.UNAVAILABLE
    assert "Rentvine unavailable" in exc.detail
    assert exc.retryable is False


def test_four_failures_do_not_open_it():
    server = _Server(503, 503, 503, 503)

    async def go():
        async with server.client() as c:
            for _ in range(4):
                await _read(c)
            return await _read(c)

    assert _run(go()) is None
    assert len(server.sent) == 5


class _DownServer(_Server):
    """Every request fails in transport: Rentvine never answered."""

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(request.method)
        raise httpx.ConnectError("down")


@pytest.mark.parametrize("status", [500, 502, 429, 0])
def test_every_outage_kind_counts(status):
    server = _DownServer() if status == 0 else _Server(*([status] * 5))

    async def go():
        async with server.client() as c:
            for _ in range(5):
                await _read(c)
            return await _read(c)

    assert isinstance(_run(go()), rvclient.RentvineUnavailableError)
    assert len(server.sent) == 5


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_rentvine_answering_is_not_an_outage_and_breaks_the_run(status):
    # Four outages, then an answer, then four more: never five in a row.
    server = _Server(503, 503, 503, 503, status, 503, 503, 503, 503)

    async def go():
        async with server.client() as c:
            for _ in range(9):
                await _read(c)
            return await _read(c)

    assert _run(go()) is None
    assert len(server.sent) == 10


def test_a_success_resets_the_count():
    server = _Server(503, 503, 503, 503, 200, 503, 503, 503, 503)

    async def go():
        async with server.client() as c:
            for _ in range(9):
                await _read(c)
            return await _read(c)

    assert _run(go()) is None


def test_failures_older_than_the_window_do_not_count(clock):
    server = _Server(503, 503, 503, 503, 503)

    async def go():
        async with server.client() as c:
            for _ in range(4):
                await _read(c)
            clock.now += rvclient.BREAKER_WINDOW_S + 1
            await _read(c)  # the fifth, but the first four have aged out
            return await _read(c)

    assert _run(go()) is None
    assert len(server.sent) == 6


def test_while_open_a_write_is_refused_without_sending():
    server = _Server(503, 503, 503, 503, 503)

    async def go():
        async with server.client() as c:
            for _ in range(5):
                await _read(c)
            return await _write(c)

    exc = _run(go())
    assert isinstance(exc, rvclient.RentvineUnavailableError)
    assert server.sent.count("POST") == 0


def test_refusals_do_not_extend_the_open_period(clock):
    server = _Server(503, 503, 503, 503, 503)

    async def go():
        async with server.client() as c:
            for _ in range(5):
                await _read(c)
            for _ in range(20):  # refused, never sent
                await _read(c)
            clock.now += rvclient.BREAKER_OPEN_S + 1
            return await _read(c)  # the probe, answered 200

    assert _run(go()) is None
    assert len(server.sent) == 6


def test_after_the_open_period_one_read_probes_and_success_closes_it(clock):
    server = _Server(503, 503, 503, 503, 503)

    async def go():
        async with server.client() as c:
            for _ in range(5):
                await _read(c)
            clock.now += rvclient.BREAKER_OPEN_S + 1
            probe = await _read(c)
            after = [await _read(c) for _ in range(3)]
            write = await _write(c)
            return probe, after, write

    probe, after, write = _run(go())
    assert probe is None
    assert after == [None, None, None]
    assert write is None
    assert len(server.sent) == 5 + 1 + 3 + 1


def test_a_failed_probe_reopens_it_for_another_period(clock):
    server = _Server(503, 503, 503, 503, 503, 503)

    async def go():
        async with server.client() as c:
            for _ in range(5):
                await _read(c)
            clock.now += rvclient.BREAKER_OPEN_S + 1
            probe = await _read(c)  # sent, fails
            refused = await _read(c)  # open again, not sent
            clock.now += rvclient.BREAKER_OPEN_S - 10
            still = await _read(c)  # not yet
            return probe, refused, still

    probe, refused, still = _run(go())
    assert not isinstance(probe, rvclient.RentvineUnavailableError)
    assert isinstance(refused, rvclient.RentvineUnavailableError)
    assert isinstance(still, rvclient.RentvineUnavailableError)
    assert len(server.sent) == 6


def test_half_open_lets_one_read_through_and_refuses_writes_and_a_second_read(clock):
    rvclient._breakers[ACCOUNT] = rvclient._Breaker(open_until=clock.now - 1)
    rvclient._breaker_admit(ACCOUNT, probe=True)  # the probe takes the slot
    with pytest.raises(rvclient.RentvineUnavailableError):
        rvclient._breaker_admit(ACCOUNT, probe=True)
    with pytest.raises(rvclient.RentvineUnavailableError):
        rvclient._breaker_admit(ACCOUNT, probe=False)


def test_a_probe_that_never_reports_back_is_abandoned(clock):
    rvclient._breakers[ACCOUNT] = rvclient._Breaker(open_until=clock.now - 1)
    rvclient._breaker_admit(ACCOUNT, probe=True)
    clock.now += rvclient.BREAKER_PROBE_STALE_S + 1
    rvclient._breaker_admit(ACCOUNT, probe=True)  # a fresh probe, not wedged


def test_a_retried_read_stops_once_its_own_failures_open_it(monkeypatch):
    """With the SHIPPED attempt count, a read retries; the retry that would
    be the sixth send is refused instead of sent."""
    monkeypatch.setattr(rvclient, "RETRY_BACKOFF_S", (0.0, 0.0, 0.0))
    server = _Server(*([503] * 10))

    async def go():
        async with server.client() as c:
            first = await _read(c)  # 4 sends, all fail
            second = await _read(c)  # 5th send opens it; its retry is refused
            return first, second

    first, second = _run(go())
    assert not isinstance(first, rvclient.RentvineUnavailableError)
    assert isinstance(second, rvclient.RentvineUnavailableError)
    assert len(server.sent) == 5


def test_accounts_are_separate():
    rvclient._breakers["other"] = rvclient._Breaker(open_until=rvclient._now() + 600)
    server = _Server()

    async def go():
        async with server.client() as c:
            return await _read(c)

    assert _run(go()) is None


def test_the_tool_says_nothing_was_sent_and_when_to_try_again():
    from helixis.agents.rentvine_tools import _sanitize

    msg = _sanitize(rvclient.RentvineUnavailableError(300.0), "update the work order")
    assert "Nothing was sent" in msg
    assert "nothing was changed in Rentvine" in msg
    assert "5 minute" in msg
    assert "reconnect" not in msg.lower()


def test_the_connection_test_names_an_outage_not_a_bad_key():
    from helixis.api.routes.rentvine import _TEST_ADVICE

    advice = _TEST_ADVICE[transport.Failure.UNAVAILABLE]
    assert "Nothing is wrong with what you entered" in advice
    assert "key" not in advice.lower()
