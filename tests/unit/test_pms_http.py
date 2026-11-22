"""The shared PMS HTTP client: documented calls only, reads retry, writes
never do, every vendor wait is binding, and a failing vendor is left alone.

Each test drives the real ``PmsHttpClient`` over an ``httpx.MockTransport``,
so what is asserted is what would have left the process. Waits are recorded,
not slept.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import httpx
import pytest

from helixis.pms import auth as pms_auth
from helixis.pms import breaker, http, rate
from helixis.pms.auth import HeaderAuth, SessionTokenAuth, header_value, host_label
from helixis.pms.errors import Failure, PmsApiError, PmsUnavailableError, RefusedBeforeSendingError
from helixis.pms.http import ClientConfig, PmsHttpClient

OPS = {
    "GET /Properties": frozenset({"pageSize", "pageNumber", "filters"}),
    "GET /Properties/{PropertyID}": frozenset(),
    "POST /WorkOrders": frozenset(),
    "PATCH /WorkOrders/{WorkOrderID}": frozenset(),
    "POST /Authentication/AuthorizeUser": frozenset(),
}
SECRET = "k-secret-value"
KEY = "rentmanager:acme"


def _config(**over: Any) -> ClientConfig:
    base: dict[str, Any] = {
        "pms": "rentmanager",
        "account": "acme",
        "base_url": "https://acme.api.rentmanager.com",
        "operations": OPS,
        "requests_per_sec": 100.0,
        "concurrency": 2,
    }
    base.update(over)
    return ClientConfig(**base)


class _Server:
    """Answers each request with the next queued response; keeps every request."""

    def __init__(self, *responses: httpx.Response | Callable[[httpx.Request], httpx.Response]):
        self.queue = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.queue:
            raise AssertionError(f"unexpected request {request.method} {request.url}")
        nxt = self.queue.pop(0)
        return nxt(request) if callable(nxt) else nxt


def _client(server: _Server, auth: Any = None, **over: Any) -> PmsHttpClient:
    return PmsHttpClient(
        _config(**over),
        auth or HeaderAuth({"X-Key": SECRET}),
        company_id="co-1",
        client=httpx.AsyncClient(transport=httpx.MockTransport(server)),
    )


def _json(status: int, body: Any, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, content=json.dumps(body).encode(), headers=headers or {})


@pytest.fixture
def waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    seen: list[float] = []

    async def _record(seconds: float) -> None:
        seen.append(seconds)

    monkeypatch.setattr(http, "_sleep", _record)
    return seen


# ── documented calls only ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("template", "kwargs", "why"),
    [
        ("/Tenants", {}, "an operation the index does not document"),
        ("/Properties", {"query": {"propertyid": 1}}, "an undeclared filter"),
        ("/Properties/{PropertyID}", {"path_params": {"PropertyID": "1/../2"}}, "a path escape"),
        ("/Properties/{PropertyID}", {}, "a missing path value"),
    ],
)
async def test_a_call_the_index_does_not_document_is_never_sent(
    template: str, kwargs: dict[str, Any], why: str
) -> None:
    server = _Server()
    with pytest.raises(RefusedBeforeSendingError):
        await _client(server).get(template, **kwargs)
    assert server.requests == [], why


async def test_a_forbidden_key_is_refused_anywhere_in_the_body_or_the_query() -> None:
    server = _Server()
    client = _client(server, forbidden_keys=frozenset({"saveOptions"}))
    nested = {"WorkOrder": {"Lines": [{"saveOptions": {"Override": True}}]}}
    with pytest.raises(RefusedBeforeSendingError):
        await client.send_once("POST", "/WorkOrders", json_body=nested)
    with pytest.raises(RefusedBeforeSendingError):
        await client.get("/Properties", query={"saveOptions": "x"})
    assert server.requests == []


@pytest.mark.parametrize("spelling", ["SaveOptions", "SAVEOPTIONS", "saveoptions"])
async def test_a_forbidden_key_is_refused_in_any_letter_case(spelling: str) -> None:
    """A .NET API binds JSON names case-insensitively, so a spelling the
    connector did not list would still reach Rent Manager as the override."""
    server = _Server()
    client = _client(server, forbidden_keys=frozenset({"saveOptions"}))
    body = [{"Title": "Leak", spelling: {"AllowOverdraft": True}}]
    with pytest.raises(RefusedBeforeSendingError, match="may never carry"):
        await client.send_once("POST", "/WorkOrders", json_body=body)
    assert server.requests == []


async def test_only_the_forbidden_key_itself_is_refused() -> None:
    """The match is the whole key, folded: the word inside a value or a longer
    key name is ordinary data and goes out."""
    server = _Server(_json(201, {"WorkOrderID": 9}))
    client = _client(server, forbidden_keys=frozenset({"saveOptions"}))
    body = {"Title": "saveOptions", "saveOptionsNote": "about the screen"}
    resp = await client.send_once("POST", "/WorkOrders", json_body=body)
    assert resp.status == 201
    assert len(server.requests) == 1


def test_a_header_value_never_reaches_the_configs_repr() -> None:
    """A connector's extra headers can hold a secret (Rent Manager's partner
    token), and a config is printed wherever a client is logged or fails."""
    config = _config(extra_headers={"Accept": "application/json", "X-Partner": SECRET})
    assert SECRET not in repr(config) and SECRET not in str(config)
    assert config.extra_headers["X-Partner"] == SECRET


async def test_there_is_no_way_to_send_delete() -> None:
    server = _Server()
    with pytest.raises(RefusedBeforeSendingError):
        await _client(server).send_once("DELETE", "/WorkOrders", json_body={})
    with pytest.raises(ValueError):
        _config_with_delete = _config(write_methods=frozenset({"POST", "DELETE"}))
        PmsHttpClient(_config_with_delete, HeaderAuth({"X-Key": SECRET}))
    assert server.requests == []


def test_only_https_is_accepted() -> None:
    with pytest.raises(ValueError):
        PmsHttpClient(_config(base_url="http://acme.api.rentmanager.com"), HeaderAuth({"X": "y"}))


# ── reads ─────────────────────────────────────────────────────────────


async def test_a_read_returns_the_body_and_logs_one_line_per_attempt(
    caplog: pytest.LogCaptureFixture,
) -> None:
    server = _Server(_json(200, [{"PropertyID": 7}]))
    with caplog.at_level(logging.INFO, logger="helixis.pms_calls"):
        resp = await _client(server).get("/Properties/{PropertyID}", path_params={"PropertyID": 7})
    assert resp.body == [{"PropertyID": 7}]
    assert str(server.requests[0].url) == "https://acme.api.rentmanager.com/Properties/7"
    (line,) = [r.getMessage() for r in caplog.records]
    assert "pms=rentmanager" in line and "endpoint=/Properties/{PropertyID}" in line
    assert "status=200" in line and "outcome=ok" in line
    assert "/Properties/7" not in line, "the record id reached the audit line"


async def test_a_utf8_bom_is_stripped_before_parsing() -> None:
    server = _Server(httpx.Response(200, content=b"\xef\xbb\xbf" + b'"token-123"'))
    resp = await _client(server).get("/Properties")
    assert resp.body == "token-123"


async def test_an_empty_204_reads_as_no_body() -> None:
    server = _Server(httpx.Response(204))
    resp = await _client(server).get("/Properties")
    assert (resp.status, resp.body) == (204, None)


async def test_a_read_that_is_not_json_is_an_error_not_an_empty_list() -> None:
    server = _Server(httpx.Response(200, content=b"<html>maintenance</html>"))
    with pytest.raises(PmsApiError) as caught:
        await _client(server).get("/Properties")
    assert caught.value.failure is Failure.UNDOCUMENTED


async def test_a_5xx_read_is_retried_on_the_schedule(waits: list[float]) -> None:
    server = _Server(_json(503, {}), _json(502, {}), _json(200, []))
    resp = await _client(server).get("/Properties")
    assert resp.body == []
    assert len(server.requests) == 3
    assert waits[0] >= rate.RETRY_BACKOFF_S[0] and waits[1] >= rate.RETRY_BACKOFF_S[1]


async def test_retries_stop_after_the_schedule_with_the_original_error(waits: list[float]) -> None:
    server = _Server(*[_json(500, {"error": "boom"})] * (len(rate.RETRY_BACKOFF_S) + 1))
    with pytest.raises(PmsApiError) as caught:
        await _client(server).get("/Properties")
    assert caught.value.status == 500
    assert len(server.requests) == len(rate.RETRY_BACKOFF_S) + 1


@pytest.mark.parametrize(
    ("status", "failure"),
    [
        (400, Failure.BAD_REQUEST),
        (401, Failure.CREDENTIALS),
        (403, Failure.PERMISSION),
        (404, Failure.NOT_FOUND),
        (409, Failure.CONFLICT),
        (412, Failure.NEEDS_OVERRIDE),
    ],
)
async def test_a_status_with_a_meaning_is_not_retried(
    status: int, failure: Failure, waits: list[float]
) -> None:
    server = _Server(_json(status, {}))
    with pytest.raises(PmsApiError) as caught:
        await _client(server).get("/Properties")
    assert caught.value.failure is failure
    assert len(server.requests) == 1 and waits == []


async def test_a_connector_can_give_a_status_its_own_meaning() -> None:
    server = _Server(_json(422, {}))
    client = _client(server, status_meanings={422: (Failure.BAD_REQUEST, False)})
    with pytest.raises(PmsApiError) as caught:
        await client.get("/Properties")
    assert caught.value.failure is Failure.BAD_REQUEST and len(server.requests) == 1


async def test_a_transport_error_is_retried_like_an_outage(waits: list[float]) -> None:
    def _boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    server = _Server(_boom, _json(200, []))
    resp = await _client(server).get("/Properties")
    assert resp.body == [] and len(server.requests) == 2


# ── every vendor wait is binding ──────────────────────────────────────


async def test_a_429_with_a_short_retry_after_waits_at_least_that_long(waits: list[float]) -> None:
    server = _Server(_json(429, {}, {"Retry-After": "7"}), _json(200, []))
    await _client(server).get("/Properties")
    assert len(server.requests) == 2
    # The retry's own wait honours the ask. (A recorded sleep does not move
    # the clock, so the hold check after it may record the remainder too.)
    assert waits[0] >= 7.0


async def test_a_429_with_a_long_retry_after_stops_and_holds_the_account(
    waits: list[float],
) -> None:
    server = _Server(_json(429, {}, {"Retry-After": "120"}))
    client = _client(server)
    with pytest.raises(PmsApiError) as caught:
        await client.get("/Properties")
    assert caught.value.status == 429 and len(server.requests) == 1
    assert waits == [], "a wait longer than one request may sleep was slept"
    assert caught.value.sent is True, "the vendor's own 429, not a hold Occupella made"
    held = rate.held_for(KEY)
    assert held is not None and held > 100
    # Nothing else in this process sends to the account while it is held.
    with pytest.raises(PmsApiError) as again:
        await client.get("/Properties")
    assert again.value.sent is False and len(server.requests) == 1


async def test_a_429_naming_no_wait_backs_off_slowly(waits: list[float]) -> None:
    server = _Server(_json(429, {}), _json(200, []))
    await _client(server).get("/Properties")
    assert waits and waits[0] >= rate.RATE_LIMIT_BACKOFF_S[0]


async def test_a_retry_after_date_is_honoured(waits: list[float]) -> None:
    when = format_datetime(datetime.now(UTC) + timedelta(seconds=20), usegmt=True)
    server = _Server(_json(429, {}, {"Retry-After": when}), _json(200, []))
    await _client(server).get("/Properties")
    assert max(waits) >= 15


async def test_an_unreadable_retry_after_is_never_read_as_no_wait(waits: list[float]) -> None:
    server = _Server(_json(429, {}, {"Retry-After": "soon-ish"}))
    with pytest.raises(PmsApiError):
        await _client(server).get("/Properties")
    held = rate.held_for(KEY)
    assert held is not None and held > rate.MAX_RETRY_AFTER_S


async def test_a_vendor_hold_header_counts_when_named(waits: list[float]) -> None:
    server = _Server(_json(429, {}, {"X-RateTimeLeft": "90"}))
    client = _client(server, hold_headers=("retry-after", "x-ratetimeleft"))
    with pytest.raises(PmsApiError):
        await client.get("/Properties")
    held = rate.held_for(KEY)
    assert held is not None and held > 80


async def test_a_nearly_spent_quota_holds_until_the_reset(waits: list[float]) -> None:
    quota = {"X-RateLimit": "100", "X-RateRemaining": "5", "X-RateTimeLeft": "45"}
    server = _Server(_json(200, [], quota))
    client = _client(
        server, rate_headers=rate.RateHeaders("X-RateLimit", "X-RateRemaining", "X-RateTimeLeft")
    )
    await client.get("/Properties")
    held = rate.held_for(KEY)
    assert held is not None and 40 < held <= 45


async def test_a_connector_named_outage_holds_the_account() -> None:
    def _maintenance(status: int, text: str) -> float | None:
        return 300.0 if status == 503 and "database updates" in text else None

    server = _Server(
        *[_json(503, {"error": "Service unavailable while performing database updates"})] * 4
    )
    client = _client(server, outage_hold=_maintenance)
    with pytest.raises(PmsApiError):
        await client.get("/Properties")
    held = rate.held_for(KEY)
    assert held is not None and held > 250


def test_a_hold_only_ever_extends() -> None:
    rate.hold(KEY, 100)
    rate.hold(KEY, 5)
    held = rate.held_for(KEY)
    assert held is not None and held > 90, "a shorter hold cut a longer one"


@pytest.mark.parametrize("raw", ["-5", "nan", "inf", "soon"])
def test_a_retry_after_that_cannot_be_a_wait_is_read_as_unreadable(raw: str) -> None:
    assert rate.retry_after_seconds({"Retry-After": raw}) == rate.UNREADABLE_RETRY_AFTER_S


# ── the circuit breaker ───────────────────────────────────────────────


async def test_the_breaker_opens_after_repeated_outages_and_stops_sending(
    waits: list[float],
) -> None:
    server = _Server(*[_json(503, {})] * breaker.BREAKER_FAILURES)
    client = _client(server)
    with pytest.raises(PmsApiError):
        await client.get("/Properties")  # 4 sends
    with pytest.raises(PmsApiError):
        await client.get("/Properties")  # the 5th opens it
    sent = len(server.requests)
    with pytest.raises(PmsUnavailableError) as caught:
        await client.get("/Properties")
    assert caught.value.sent is False and "Rent Manager unavailable" in caught.value.detail
    with pytest.raises(PmsUnavailableError):
        await client.send_once("POST", "/WorkOrders", json_body={"x": 1})
    assert len(server.requests) == sent


async def test_after_the_window_one_read_probes_and_an_answer_closes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [1000.0]
    monkeypatch.setattr(breaker, "_now", lambda: clock[0])
    for _ in range(breaker.BREAKER_FAILURES):
        breaker.record(KEY, PmsApiError(503, Failure.UNDOCUMENTED, True, ""))
    clock[0] += breaker.BREAKER_OPEN_S + 1
    # Half-open: a write is still refused; a read is let through as the probe.
    server = _Server(_json(200, []))
    client = _client(server)
    with pytest.raises(PmsUnavailableError):
        await client.send_once("POST", "/WorkOrders", json_body={"x": 1})
    await client.get("/Properties")
    assert len(server.requests) == 1
    breaker.admit(KEY, "Rent Manager", probe=False)  # closed again


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    now = [1000.0]
    monkeypatch.setattr(breaker, "_now", lambda: now[0])
    return now


def _outage() -> PmsApiError:
    return PmsApiError(503, Failure.UNDOCUMENTED, True, "")


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 412])
def test_a_refusal_is_the_vendor_working_and_never_opens_the_breaker(status: int) -> None:
    for _ in range(breaker.BREAKER_FAILURES * 2):
        breaker.record(KEY, PmsApiError(status, Failure.BAD_REQUEST, False, ""))
    breaker.admit(KEY, "Rent Manager", probe=False)


@pytest.mark.parametrize("status", [0, 429, 500, 503])
def test_each_kind_of_outage_counts(status: int) -> None:
    for _ in range(breaker.BREAKER_FAILURES):
        breaker.record(KEY, PmsApiError(status, Failure.UNDOCUMENTED, True, ""))
    with pytest.raises(PmsUnavailableError):
        breaker.admit(KEY, "Rent Manager", probe=True)


def test_outages_spread_wider_than_the_window_do_not_open_it(clock: list[float]) -> None:
    for _ in range(breaker.BREAKER_FAILURES - 1):
        breaker.record(KEY, _outage())
    clock[0] += breaker.BREAKER_WINDOW_S + 1
    breaker.record(KEY, _outage())
    breaker.admit(KEY, "Rent Manager", probe=False)


def test_half_open_lets_exactly_one_probe_through(clock: list[float]) -> None:
    for _ in range(breaker.BREAKER_FAILURES):
        breaker.record(KEY, _outage())
    clock[0] += breaker.BREAKER_OPEN_S + 1
    breaker.admit(KEY, "Rent Manager", probe=True)
    with pytest.raises(PmsUnavailableError):
        breaker.admit(KEY, "Rent Manager", probe=True)
    # A probe that never reported back is abandoned, so half-open cannot wedge.
    clock[0] += breaker.BREAKER_PROBE_STALE_S + 1
    breaker.admit(KEY, "Rent Manager", probe=True)


def test_a_failed_probe_opens_it_for_a_full_period_again(clock: list[float]) -> None:
    for _ in range(breaker.BREAKER_FAILURES):
        breaker.record(KEY, _outage())
    clock[0] += breaker.BREAKER_OPEN_S + 1
    breaker.admit(KEY, "Rent Manager", probe=True)
    breaker.record(KEY, _outage())
    clock[0] += breaker.BREAKER_OPEN_S - 1
    with pytest.raises(PmsUnavailableError) as caught:
        breaker.admit(KEY, "Rent Manager", probe=True)
    assert caught.value.remaining is not None and caught.value.remaining > 0


# ── writes ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("status", [500, 502, 503, 504])
async def test_a_write_is_never_retried(status: int, waits: list[float]) -> None:
    server = _Server(_json(status, {}))
    with pytest.raises(PmsApiError) as caught:
        await _client(server).send_once("POST", "/WorkOrders", json_body={"x": 1})
    assert len(server.requests) == 1 and waits == []
    assert caught.value.sent is True, "a 5xx write may have landed"


async def test_a_write_that_timed_out_is_reported_as_possibly_sent() -> None:
    def _timeout(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    server = _Server(_timeout)
    with pytest.raises(PmsApiError) as caught:
        await _client(server).send_once("POST", "/WorkOrders", json_body={"x": 1})
    assert caught.value.sent is True and len(server.requests) == 1


async def test_a_write_into_a_hold_is_refused_without_sending() -> None:
    rate.hold(KEY, 5.0)
    server = _Server()
    with pytest.raises(PmsApiError) as caught:
        await _client(server).send_once("POST", "/WorkOrders", json_body={"x": 1})
    assert caught.value.sent is False and server.requests == []


async def test_a_2xx_write_with_an_unreadable_body_is_not_a_failure() -> None:
    server = _Server(httpx.Response(201, content=b"Created"))
    resp = await _client(server).send_once("POST", "/WorkOrders", json_body={"x": 1})
    assert (resp.status, resp.body) == (201, None)


async def test_a_patch_carries_exactly_the_body_given() -> None:
    server = _Server(_json(200, {}))
    await _client(server).send_once(
        "PATCH",
        "/WorkOrders/{WorkOrderID}",
        path_params={"WorkOrderID": 9},
        json_body={"description": "new"},
    )
    sent = server.requests[0]
    assert sent.method == "PATCH" and json.loads(sent.content) == {"description": "new"}


@pytest.mark.parametrize("verb", ["GET", "get", "HEAD", "OPTIONS"])
async def test_send_once_refuses_a_verb_that_is_not_a_write(verb: str) -> None:
    server = _Server()
    with pytest.raises(RefusedBeforeSendingError):
        await _client(server).send_once(verb, "/Properties")
    assert server.requests == []


# ── credentials never leave in an error ───────────────────────────────


async def test_an_echoed_secret_is_masked_in_the_error() -> None:
    server = _Server(_json(400, {"error": f"bad key {SECRET}"}))
    with pytest.raises(PmsApiError) as caught:
        await _client(server).get("/Properties")
    assert SECRET not in caught.value.detail and "***" in caught.value.detail


# ── guards on stored values ───────────────────────────────────────────


@pytest.mark.parametrize("bad", ["evil.example/x", "a.b", "acme corp", "", "-acme", "acme/"])
def test_a_stored_value_never_becomes_a_host_unless_it_is_one_label(bad: str) -> None:
    if bad == "acme/":
        assert host_label(bad, what="corp id") == "acme"
        return
    with pytest.raises(ValueError):
        host_label(bad, what="corp id")


def test_a_host_label_spares_real_codes() -> None:
    assert host_label("Acme-Properties", what="corp id") == "acme-properties"


@pytest.mark.parametrize("bad", ["abc\r\nX-Evil: 1", "ab\ncd", "", "a\x00b"])
def test_a_header_value_refuses_line_breaks_and_control_characters(bad: str) -> None:
    with pytest.raises(ValueError):
        header_value(bad, what="system id")


def test_surrounding_whitespace_is_trimmed_not_refused() -> None:
    assert header_value("  sys-1\n", what="system id") == "sys-1"


async def test_header_auth_sends_its_headers_on_every_request() -> None:
    server = _Server(_json(200, []), _json(200, []))
    client = _client(server, auth=HeaderAuth({"X-Client": "id-1", "X-Secret": "s-2"}))
    await client.get("/Properties")
    await client.get("/Properties")
    for request in server.requests:
        assert request.headers["X-Client"] == "id-1" and request.headers["X-Secret"] == "s-2"


# ── session tokens (Rent Manager) ─────────────────────────────────────


def _session(logins: list[int]) -> SessionTokenAuth:
    async def _login(client: PmsHttpClient) -> str:
        logins.append(1)
        resp = await client.login(
            "/Authentication/AuthorizeUser", json_body={"Username": "u", "Password": "pw-9"}
        )
        return str(resp.body)

    return SessionTokenAuth(
        cache_key="rentmanager:co-1:1",
        header="X-RM12Api-ApiToken",
        login=_login,
        static_secrets=("u", "pw-9"),
        name="Rent Manager",
    )


def _token(n: int) -> httpx.Response:
    return httpx.Response(200, content=json.dumps(f"tok-{n}").encode())


async def test_a_session_logs_in_once_and_sends_the_token() -> None:
    logins: list[int] = []
    server = _Server(_token(1), _json(200, []), _json(200, []))
    client = _client(server, auth=_session(logins))
    await client.get("/Properties")
    await client.get("/Properties")
    login, first, second = server.requests
    assert login.url.path == "/Authentication/AuthorizeUser"
    assert "X-RM12Api-ApiToken" not in login.headers
    assert first.headers["X-RM12Api-ApiToken"] == "tok-1" == second.headers["X-RM12Api-ApiToken"]
    assert logins == [1]


async def test_an_expired_token_logs_in_once_and_replays_once() -> None:
    logins: list[int] = []
    server = _Server(_token(1), _json(401, {}), _token(2), _json(200, ["ok"]))
    resp = await _client(server, auth=_session(logins)).get("/Properties")
    assert resp.body == ["ok"] and len(logins) == 2
    assert server.requests[-1].headers["X-RM12Api-ApiToken"] == "tok-2"


async def test_a_second_401_marks_the_credentials_broken_and_stops_sending() -> None:
    logins: list[int] = []
    server = _Server(_token(1), _json(401, {}), _token(2), _json(401, {}))
    client = _client(server, auth=_session(logins))
    with pytest.raises(PmsApiError) as caught:
        await client.get("/Properties")
    assert caught.value.failure is Failure.CREDENTIALS
    sent = len(server.requests)
    with pytest.raises(PmsApiError) as refused:
        await client.get("/Properties")
    assert refused.value.sent is False and "Reconnect" in refused.value.detail
    assert len(server.requests) == sent, "a broken credential kept being sent"
    pms_auth.forget("rentmanager:co-1:1")
    assert "rentmanager:co-1:1" not in pms_auth._broken


async def test_a_refused_login_marks_the_credentials_broken() -> None:
    logins: list[int] = []
    server = _Server(_json(401, {"error": "bad password pw-9"}))
    client = _client(server, auth=_session(logins))
    with pytest.raises(PmsApiError) as caught:
        await client.get("/Properties")
    assert "pw-9" not in caught.value.detail
    with pytest.raises(PmsApiError) as again:
        await client.get("/Properties")
    assert again.value.sent is False and len(server.requests) == 1


async def test_concurrent_reads_share_one_login() -> None:
    logins: list[int] = []

    def _answer(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("AuthorizeUser"):
            return _token(1)
        return _json(200, [])

    server = _Server(*[_answer] * 5)
    client = _client(server, auth=_session(logins), concurrency=4)
    await asyncio.gather(*(client.get("/Properties") for _ in range(4)))
    assert logins == [1]


async def test_a_write_refused_with_401_is_replayed_once_after_a_new_login() -> None:
    logins: list[int] = []
    server = _Server(_token(1), _json(401, {}), _token(2), _json(201, {"WorkOrderID": 5}))
    resp = await _client(server, auth=_session(logins)).send_once(
        "POST", "/WorkOrders", json_body={"x": 1}
    )
    assert resp.body == {"WorkOrderID": 5}
    writes = [r for r in server.requests if r.url.path == "/WorkOrders"]
    assert len(writes) == 2, "a 401 is a refusal before processing, so one replay"


async def test_a_write_refused_twice_with_401_stops_and_marks_the_credentials_broken() -> None:
    """One replay, never a loop: the second 401 is the answer."""
    server = _Server(_token(1), _json(401, {}), _token(2), _json(401, {}))
    client = _client(server, auth=_session([]))
    with pytest.raises(PmsApiError) as caught:
        await client.send_once("POST", "/WorkOrders", json_body={"x": 1})
    assert caught.value.failure is Failure.CREDENTIALS
    writes = [r for r in server.requests if r.url.path == "/WorkOrders"]
    assert len(writes) == 2
    with pytest.raises(PmsApiError) as again:
        await client.send_once("POST", "/WorkOrders", json_body={"x": 1})
    assert again.value.sent is False, "a broken credential was sent again"
    assert len(server.requests) == 4


async def test_a_refresh_after_another_request_already_did_logs_in_no_more() -> None:
    """Two requests fail with the same stale token: the second one's refresh
    finds the token already replaced and uses it."""
    logins: list[int] = []
    auth = _session(logins)
    server = _Server(_token(2))
    client = _client(server, auth=auth)
    pms_auth._tokens["rentmanager:co-1:1"] = "tok-2"
    await auth.refresh(client, {"X-RM12Api-ApiToken": "tok-1"})
    assert logins == [] and server.requests == []
    await auth.refresh(client, {"X-RM12Api-ApiToken": "tok-2"})
    assert logins == [1], "the token this request used was the current one, so log in"


@pytest.mark.parametrize(
    "answers",
    [
        # The first login fails: the write is never sent.
        (_json(503, {}),),
        # The write is refused with a 401 and the new login fails.
        (_token(1), _json(401, {}), _json(503, {})),
    ],
)
async def test_a_write_whose_login_fails_says_it_was_not_sent(
    answers: tuple[httpx.Response, ...],
) -> None:
    """The login's own status is not the write's. A guard that read a 503
    from the login as "the write may have landed" would block a safe retry
    and tell somebody to check for a change that never happened."""
    server = _Server(*answers)
    with pytest.raises(PmsApiError) as caught:
        await _client(server, auth=_session([])).send_once(
            "POST", "/WorkOrders", json_body={"x": 1}
        )
    assert caught.value.sent is False
    writes = [r for r in server.requests if r.url.path == "/WorkOrders"]
    assert len(writes) == len(answers) // 2


@pytest.mark.pms_rate_limit
async def test_the_real_limiter_is_shared_per_account() -> None:
    first = rate.bucket(KEY, 2.0)
    assert rate.bucket(KEY, 2.0) is first
    assert rate.bucket("rentmanager:other", 2.0) is not first
