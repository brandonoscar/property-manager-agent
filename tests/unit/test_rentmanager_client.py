"""Rent Manager's HTTP surface: what can be sent, what each answer means,
and how the session token behaves.

Every behavioural test drives the real ``PmsHttpClient`` that
``rentmanager.client.build_client`` builds, over a mock transport, so what is
asserted is what would have left the process.
"""

from __future__ import annotations

import ast
import dataclasses
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from helixis.pms import auth as pms_auth
from helixis.pms import http, rate
from helixis.pms.errors import Failure, PmsApiError, RefusedBeforeSendingError
from helixis.pms.http import PmsHttpClient, operation_key
from helixis.pms.paging import read_all
from helixis.rentmanager import client as rm
from helixis.rentmanager.credentials import RentManagerCredentials
from helixis.rentmanager.resources import RESOURCES
from tests.unit._rentmanager_fakes import (
    COMPANY,
    CORP,
    LOCATION,
    PARTNER,
    PASSWORD,
    TOKEN,
    USERNAME,
    FakeRentManager,
    json_response,
    portfolio,
    serve,
)

PACKAGE = Path(rm.__file__).resolve().parent
ACCOUNT = f"rentmanager:{CORP}:{LOCATION}"


def _creds(
    *, partner: str | None = None, corp: str = CORP, version: str = ""
) -> RentManagerCredentials:
    return RentManagerCredentials(
        company_id=COMPANY,
        corp_id=corp,
        location_id=LOCATION,
        username=USERNAME,
        password=PASSWORD,
        partner_token=partner,
        version=version,
    )


@pytest.fixture
def waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    seen: list[float] = []

    async def _record(seconds: float) -> None:
        seen.append(seconds)

    monkeypatch.setattr(http, "_sleep", _record)
    return seen


Connect = Callable[..., PmsHttpClient]


@pytest.fixture
def connect(monkeypatch: pytest.MonkeyPatch) -> Connect:
    """``connect(server, **creds)``: the connector's own client, routed to a
    fake Rent Manager (``_rentmanager_fakes.serve``)."""

    def _connect(server: FakeRentManager, **creds: Any) -> PmsHttpClient:
        serve(monkeypatch, server)
        return rm.build_client(_creds(**creds))

    return _connect


async def _get(connect: Connect, server: FakeRentManager, path: str, **creds: Any) -> Any:
    async with connect(server, **creds) as client:
        return await client.get(path, query=RESOURCES["properties"].query())


# ── what can be sent ──────────────────────────────────────────────────


def test_the_index_holds_the_reads_the_location_the_login_and_the_two_writes_only() -> None:
    """Design R4's writes and nothing else: one create, one update, and the
    single-record reads they plan from. Every other POST is the login."""
    expected = {operation_key("GET", r.path) for r in RESOURCES.values()}
    expected |= {"GET /Locations/CurrentLocation", "POST /Authentication/AuthorizeUser"}
    expected |= {
        "GET /ServiceManagerIssues/{ServiceManagerIssueID}",
        "GET /Properties/{PropertyID}",
        "GET /Units/{UnitID}",
        "GET /ServiceManagerCategories",
        "POST /ServiceManagerIssues",
        "POST /ServiceManagerIssues/{ServiceManagerIssueID}",
    }
    assert set(rm.OPERATIONS) == expected
    verbs = {key.split(" ", 1)[0] for key in rm.OPERATIONS}
    assert verbs == {"GET", "POST"}
    assert sorted(k for k in rm.OPERATIONS if k.startswith("POST")) == [
        "POST /Authentication/AuthorizeUser",
        "POST /ServiceManagerIssues",
        "POST /ServiceManagerIssues/{ServiceManagerIssueID}",
    ]
    assert not any("ChangeLocation" in key for key in rm.OPERATIONS), "location is never changed"
    assert not any("Note" in key for key in rm.OPERATIONS), "no note write while RM-15 is open"


def test_only_a_read_that_narrows_to_what_changed_can_carry_a_filter() -> None:
    """An ignored filter answers about the whole account (gotcha 24), so a
    filter goes only where an incremental pass narrows a collection to what
    changed, behind the canary that proves Rent Manager applies one. Never on
    a lookup, a record read by its id, the login, the location or a write."""
    filtered = {operation_key("GET", r.path) for r in RESOURCES.values() if r.incremental}
    assert filtered, "no collection reads by what changed; the scan is broken"
    assert operation_key("GET", RESOURCES["properties"].path) in filtered, "the canary's"
    for key, params in rm.OPERATIONS.items():
        assert "filter" not in params, key
        assert ("filters" in params) is (key in filtered), key


def test_the_config_is_rent_managers() -> None:
    config = rm.client_config(_creds())
    assert config.pms == "rentmanager"
    assert config.account == f"{CORP}:{LOCATION}"
    assert config.base_url == f"https://{CORP}.api.rentmanager.com"
    assert (config.requests_per_sec, config.concurrency) == (1.0, 2)
    assert config.write_methods == frozenset({"POST"}), "the two work-order writes, POST only"
    assert "saveOptions" in config.forbidden_keys and "SaveOptions" in config.forbidden_keys
    assert config.status_meanings[403] == (Failure.PERMISSION, False)
    assert config.status_meanings[404] == (Failure.NOT_FOUND, False)
    assert config.outage_hold is rm.maintenance_hold
    assert config.rate_headers == rate.RateHeaders(
        "X-RateLimit", "X-RateRemaining", "X-RateTimeLeft"
    )
    assert config.hold_headers == ("retry-after",)
    assert config.bare_429_hold_s == 60.0


def test_the_paging_is_rent_managers() -> None:
    paging = rm.PAGING
    assert (paging.page_param, paging.size_param) == ("pageNumber", "pageSize")
    assert paging.page_size == 1000 and paging.total_header == "X-Total-Results"
    assert paging.empty_statuses == frozenset({404, 204})


@pytest.mark.parametrize("bad", ["evil.example/x", "a.b", "acme corp", "", "-acme"])
def test_a_stored_corp_id_never_becomes_a_host_unless_it_is_one_label(bad: str) -> None:
    with pytest.raises(ValueError):
        rm.base_url(bad)


def test_a_partner_token_rides_in_the_config_but_never_in_its_repr() -> None:
    """The shared client opens its own HTTP client with the config's headers,
    and the config is a dataclass whose repr prints every field: the token's
    own repr is masked."""
    config = rm.client_config(_creds(partner=PARTNER))
    assert config.extra_headers[rm.PARTNER_HEADER] == PARTNER
    assert PARTNER not in repr(config) and PARTNER not in str(config)
    assert rm.PARTNER_HEADER not in rm.client_config(_creds()).extra_headers


async def test_nothing_can_be_written_or_deleted(connect: Connect) -> None:
    server = FakeRentManager()
    async with connect(server) as client:
        for verb in ("POST", "PUT", "PATCH", "DELETE"):
            with pytest.raises(RefusedBeforeSendingError):
                await client.send_once(verb, "/Properties", json_body=[{"Name": "x"}])
    assert server.requests == []


def _docstring_ids(tree: ast.AST) -> set[int]:
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                out.add(id(body[0].value))
    return out


def _forbidden_keys_constants(tree: ast.AST) -> set[int]:
    """The string constants inside the ``FORBIDDEN_KEYS = ...`` declaration."""
    allowed: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign | ast.Assign):
            targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
            if any(isinstance(t, ast.Name) and t.id == "FORBIDDEN_KEYS" for t in targets):
                allowed |= {id(c) for c in ast.walk(node) if isinstance(c, ast.Constant)}
    return allowed


def test_the_package_never_builds_save_options_or_a_delete() -> None:
    """AST, not a substring scan (rule 17): the docstrings explain the rule.
    ``saveOptions`` may appear only inside ``FORBIDDEN_KEYS``; no string in the
    package says DELETE, and nothing calls a ``delete``."""
    files = sorted(PACKAGE.glob("*.py"))
    assert len(files) >= 6, "the scan is not reading the package"
    offenders: list[str] = []
    declared = False
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        allowed = _forbidden_keys_constants(tree)
        declared = declared or bool(allowed)
        docs = _docstring_ids(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) in docs:
                    continue
                if "saveoptions" in node.value.lower() and id(node) not in allowed:
                    offenders.append(f"{path.name}:{node.lineno} {node.value!r}")
                if "DELETE" in node.value.upper().split():
                    offenders.append(f"{path.name}:{node.lineno} {node.value!r}")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr.lower() == "delete":
                    offenders.append(f"{path.name}:{node.lineno} .delete()")
    assert declared, "FORBIDDEN_KEYS was not found: the scan proves nothing"
    assert offenders == []


# ── the session token ─────────────────────────────────────────────────


async def test_the_login_sends_the_documented_fields_and_the_token_rides_every_read(
    connect: Connect,
) -> None:
    server = FakeRentManager()
    await _get(connect, server, "/Properties")
    await _get(connect, server, "/Properties")
    login, *reads = server.requests
    assert login.method == "POST" and login.url.path == "/Authentication/AuthorizeUser"
    assert json.loads(login.content) == {
        "Username": USERNAME,
        "Password": PASSWORD,
        "LocationID": LOCATION,
    }
    assert rm.TOKEN_HEADER not in login.headers
    assert len(reads) == 2, "one login for the account, cached for the next read"
    for read in reads:
        assert read.headers[rm.TOKEN_HEADER] == TOKEN


async def test_a_token_behind_a_bom_as_a_quoted_string_is_read(connect: Connect) -> None:
    """RM-1 and RM-16 together: the fake answers ``\\ufeff"token"``."""
    server = FakeRentManager(token="tok-with-bom")
    await _get(connect, server, "/Properties")
    assert server.requests[-1].headers[rm.TOKEN_HEADER] == "tok-with-bom"


@pytest.mark.parametrize("body", [b"{}", b"[]", b'""', b"12"])
async def test_a_login_that_answers_no_token_sends_nothing_else(
    body: bytes, connect: Connect
) -> None:
    server = FakeRentManager(
        overrides={"/Authentication/AuthorizeUser": lambda _r: httpx.Response(200, content=body)}
    )
    with pytest.raises(PmsApiError):
        await _get(connect, server, "/Properties")
    assert [r.url.path for r in server.requests] == ["/Authentication/AuthorizeUser"]


async def test_an_expired_token_logs_in_once_and_replays_once(connect: Connect) -> None:
    answers = iter([json_response(401, {}), json_response(200, [], {"X-Total-Results": "0"})])
    server = FakeRentManager(overrides={"/Properties": lambda _r: next(answers)})
    await _get(connect, server, "/Properties")
    paths = [r.url.path for r in server.requests]
    assert paths == [
        "/Authentication/AuthorizeUser",
        "/Properties",
        "/Authentication/AuthorizeUser",
        "/Properties",
    ]


async def test_a_second_401_marks_the_credentials_broken_and_stops_sending(
    connect: Connect,
) -> None:
    server = FakeRentManager(overrides={"/Properties": lambda _r: json_response(401, {})})
    async with connect(server) as client:
        with pytest.raises(PmsApiError) as caught:
            await client.get("/Properties", query=RESOURCES["properties"].query())
        assert caught.value.failure is Failure.CREDENTIALS
        sent = len(server.requests)
        with pytest.raises(PmsApiError) as again:
            await client.get("/Units", query=RESOURCES["units"].query())
    assert again.value.sent is False and "Reconnect Rent Manager" in again.value.detail
    assert len(server.requests) == sent, "a broken credential kept being sent"


async def test_the_partner_token_rides_every_request_when_stored(connect: Connect) -> None:
    server = FakeRentManager()
    await _get(connect, server, "/Properties", partner=PARTNER)
    assert len(server.requests) == 2
    assert all(r.headers[rm.PARTNER_HEADER] == PARTNER for r in server.requests)


async def test_no_partner_header_without_a_partner_token(connect: Connect) -> None:
    server = FakeRentManager()
    await _get(connect, server, "/Properties")
    assert all(rm.PARTNER_HEADER not in r.headers for r in server.requests)


async def test_a_credential_echoed_in_an_error_is_masked(connect: Connect) -> None:
    echo = f"bad request for {USERNAME}/{PASSWORD} token {TOKEN} partner {PARTNER}"
    server = FakeRentManager(overrides={"/Properties": lambda _r: json_response(400, echo)})
    with pytest.raises(PmsApiError) as caught:
        await _get(connect, server, "/Properties", partner=PARTNER)
    for secret in (USERNAME, PASSWORD, TOKEN, PARTNER):
        assert secret not in caught.value.detail
        assert secret not in str(caught.value)


async def test_the_session_token_is_kept_per_saved_credential_set(connect: Connect) -> None:
    """The key carries the version of the saved row (``credentials.version_of``)."""
    server = FakeRentManager()
    await _get(connect, server, "/Properties", version="v1")
    assert pms_auth._tokens == {f"rentmanager:{COMPANY}:{LOCATION}:v1": TOKEN}
    assert rm.token_cache_key(COMPANY, LOCATION, "v1") in pms_auth._tokens


async def test_a_broken_mark_on_old_credentials_never_blocks_new_ones(connect: Connect) -> None:
    """The worker cannot hear the web process save new details. A second 401
    there marks the OLD set broken; the new set has its own key, so the next
    tick logs in with it instead of refusing until a restart."""
    pms_auth._broken.add(rm.token_cache_key(COMPANY, LOCATION, "old"))
    server = FakeRentManager()
    async with connect(server, version="old") as client:
        with pytest.raises(PmsApiError) as caught:
            await client.get("/Properties", query=RESOURCES["properties"].query())
    assert caught.value.sent is False and server.requests == []
    async with connect(server, version="new") as client:
        assert await rm.current_location_id(client) == LOCATION
    assert [r.url.path for r in server.requests] == [
        "/Authentication/AuthorizeUser",
        "/Locations/CurrentLocation",
    ]


# ── one page's count (the connection test) ────────────────────────────


async def test_the_count_is_rent_managers_total_from_one_page_of_one_row(
    connect: Connect,
) -> None:
    data = portfolio()
    data["properties"] = [{**data["properties"][0], "PropertyID": 880000100 + i} for i in range(7)]
    server = FakeRentManager(data)
    async with connect(server) as client:
        assert await rm.count_of(client, RESOURCES["properties"]) == 7
    (read,) = server.to("/Properties")
    assert dict(read.url.params) == {
        **RESOURCES["properties"].query(),
        "pageNumber": "1",
        "pageSize": "1",
    }


@pytest.mark.parametrize(
    "answer",
    [
        lambda _r: json_response(200, [{"PropertyID": 1}]),
        lambda _r: json_response(200, [], {"X-Total-Results": "-1"}),
        lambda _r: httpx.Response(404),
        lambda _r: httpx.Response(204),
    ],
    ids=["no header", "a negative total", "404", "204"],
)
async def test_a_count_that_was_not_stated_is_none_never_zero(
    answer: Callable[[httpx.Request], httpx.Response], connect: Connect
) -> None:
    server = FakeRentManager(overrides={"/Properties": answer})
    async with connect(server) as client:
        assert await rm.count_of(client, RESOURCES["properties"]) is None


async def test_a_count_rent_manager_refuses_is_an_error_not_unknown(connect: Connect) -> None:
    server = FakeRentManager(overrides={"/Properties": lambda _r: json_response(403, {})})
    async with connect(server) as client:
        with pytest.raises(PmsApiError) as caught:
            await rm.count_of(client, RESOURCES["properties"])
    assert caught.value.failure is Failure.PERMISSION


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"x-total-results": "12"}, 12),
        ({"X-Total-Results": " 0 "}, 0),
        ({"X-TOTAL-RESULTS": "3"}, 3),
        ({"X-Total-Results": "many"}, None),
        ({"X-Total-Results": "-2"}, None),
        ({"X-Total-Results": "\u00b2"}, None),
        ({"X-Total-Count": "5"}, None),
        ({}, None),
    ],
)
def test_the_total_header_is_a_whole_number_or_unknown(
    headers: dict[str, str], expected: int | None
) -> None:
    assert rm.total_results(headers) == expected


# ── what an answer means ──────────────────────────────────────────────


async def _read(connect: Connect, server: FakeRentManager, name: str = "tenants") -> Any:
    resource = RESOURCES[name]
    async with connect(server) as client:
        return await read_all(client, resource.path, strategy=rm.PAGING, query=resource.query())


@pytest.mark.parametrize("status", [404, 204])
async def test_an_empty_collection_is_empty_and_whole(status: int, connect: Connect) -> None:
    server = FakeRentManager(overrides={"/Tenants": lambda _r: httpx.Response(status)})
    got = await _read(connect, server)
    assert (got.rows, got.complete, got.total) == ([], True, 0)


async def test_a_403_on_a_collection_is_blind_never_empty(connect: Connect) -> None:
    """403: the user can see NONE of it (Overview p.12). Stale, never zero."""
    server = FakeRentManager(overrides={"/Tenants": lambda _r: json_response(403, {})})
    with pytest.raises(PmsApiError) as caught:
        await _read(connect, server)
    assert caught.value.failure is Failure.PERMISSION


async def test_a_404_on_one_record_is_an_error_never_a_deletion(connect: Connect) -> None:
    server = FakeRentManager(
        overrides={"/Locations/CurrentLocation": lambda _r: httpx.Response(404)}
    )
    async with connect(server) as client:
        with pytest.raises(PmsApiError) as caught:
            await rm.current_location_id(client)
    assert caught.value.failure is Failure.NOT_FOUND


async def test_a_206_page_is_a_page_and_completeness_needs_the_total(connect: Connect) -> None:
    rows = [{"TenantID": 880000300 + i} for i in range(3)]
    server = FakeRentManager(
        overrides={"/Tenants": lambda _r: json_response(206, rows, {"X-Total-Results": "3"})}
    )
    got = await _read(connect, server)
    assert got.complete and len(got.rows) == 3
    server = FakeRentManager(overrides={"/Tenants": lambda _r: json_response(200, rows)})
    got = await _read(connect, server)
    assert got.complete is False, "no X-Total-Results: the vendor never named the end"


async def test_every_read_asks_for_its_fields_and_embeds_a_page_at_a_time(connect: Connect) -> None:
    server = FakeRentManager()
    await _read(connect, server, "tenants")
    (read,) = server.to("/Tenants")
    params = dict(read.url.params)
    assert params["fields"].split(",") == list(RESOURCES["tenants"].fields)
    assert params["embeds"].split(",") == list(RESOURCES["tenants"].embeds)
    assert (params["pageNumber"], params["pageSize"]) == ("1", "1000")


async def test_a_removed_resource_is_not_retried(waits: list[float], connect: Connect) -> None:
    """410: removed after a deprecation notice (Overview p.12). Retrying
    cannot bring it back, so the read fails once, and the table is stale."""
    server = FakeRentManager(overrides={"/Tenants": lambda _r: json_response(410, {})})
    with pytest.raises(PmsApiError) as caught:
        await _read(connect, server)
    assert caught.value.retryable is False
    assert len(server.to("/Tenants")) == 1 and waits == []


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"LocationID": 7, "Name": "x"}, 7),
        ([{"LocationID": 7}], 7),
        (7, 7),
        ("7", 7),
        ({"Name": "x"}, None),
        ([{"LocationID": 7}, {"LocationID": 8}], None),
        (True, None),
        (None, None),
        ("\u00b2", None),
    ],
)
def test_the_current_location_is_read_from_any_shape_or_not_at_all(
    body: Any, expected: int | None
) -> None:
    assert rm.location_id_of(body) == expected


# ── waits ─────────────────────────────────────────────────────────────


def test_the_maintenance_window_holds_five_minutes() -> None:
    text = "Service unavailable while performing database updates. Try again after a few minutes"
    assert rm.maintenance_hold(503, text) == 300.0
    assert rm.maintenance_hold(500, "Internal error") is None


async def test_a_maintenance_answer_holds_the_account_and_is_not_retried_hard(
    waits: list[float],
    connect: Connect,
) -> None:
    text = "Service unavailable while performing database updates"
    server = FakeRentManager(overrides={"/Tenants": lambda _r: json_response(503, text)})
    with pytest.raises(PmsApiError):
        await _read(connect, server)
    held = rate.held_for(ACCOUNT)
    assert held is not None and held > 250
    assert len(server.to("/Tenants")) == 1, "a maintenance window was retried into"


async def test_an_empty_list_carrying_the_quota_headers_holds_nothing(connect: Connect) -> None:
    """The reason ``X-RateTimeLeft`` is not a hold header: the shared client
    reads hold headers on EVERY error, and Rent Manager reportedly sends it on
    every response. A 404 empty list would otherwise hold the account until
    the hourly reset, and every read after it would fail."""
    quota = {"X-RateLimit": "1000", "X-RateRemaining": "900", "X-RateTimeLeft": "3000"}
    server = FakeRentManager(
        overrides={"/Tenants": lambda _r: httpx.Response(404, content=b"", headers=quota)}
    )
    got = await _read(connect, server)
    assert got.complete and got.rows == []
    assert rate.held_for(ACCOUNT) is None


async def test_naming_x_rate_time_left_as_a_hold_header_would_hold_an_empty_list() -> None:
    """The counterfactual, pinned: if the shared client ever reads hold headers
    on a 429 only, this fails and the header can be named."""
    quota = {"X-RateTimeLeft": "3000"}
    naive = dataclasses.replace(
        rm.client_config(_creds()), hold_headers=("retry-after", "x-ratetimeleft")
    )
    server = FakeRentManager(
        overrides={"/Tenants": lambda _r: httpx.Response(404, content=b"", headers=quota)}
    )
    async with httpx.AsyncClient(transport=server.transport()) as raw:
        client = PmsHttpClient(naive, rm.session_auth(_creds()), company_id=COMPANY, client=raw)
        await read_all(client, "/Tenants", strategy=rm.PAGING, query=RESOURCES["tenants"].query())
    held = rate.held_for(ACCOUNT)
    assert held is not None and held > 2900


async def test_a_429_that_says_the_quota_is_spent_holds_until_the_reset(
    waits: list[float], connect: Connect
) -> None:
    quota = {"X-RateLimit": "1000", "X-RateRemaining": "0", "X-RateTimeLeft": "900"}
    server = FakeRentManager(overrides={"/Tenants": lambda _r: json_response(429, {}, quota)})
    with pytest.raises(PmsApiError):
        await _read(connect, server)
    held = rate.held_for(ACCOUNT)
    assert held is not None and 850 < held <= 900


async def test_a_429_naming_no_wait_holds_a_minute(waits: list[float], connect: Connect) -> None:
    server = FakeRentManager(overrides={"/Tenants": lambda _r: json_response(429, {})})
    with pytest.raises(PmsApiError):
        await _read(connect, server)
    held = rate.held_for(ACCOUNT)
    assert held is not None and 55 < held <= 60


async def test_a_429_with_a_short_retry_after_waits_it_out(
    waits: list[float], connect: Connect
) -> None:
    answers = iter(
        [
            json_response(429, {}, {"Retry-After": "7"}),
            json_response(200, [], {"X-Total-Results": "0"}),
        ]
    )
    server = FakeRentManager(overrides={"/Tenants": lambda _r: next(answers)})
    got = await _read(connect, server)
    assert got.complete and waits and waits[0] >= 7.0


async def test_a_nearly_spent_quota_on_a_success_holds_until_the_reset(connect: Connect) -> None:
    quota = {"X-RateLimit": "100", "X-RateRemaining": "5", "X-RateTimeLeft": "45"}
    server = FakeRentManager(
        overrides={"/Tenants": lambda _r: json_response(200, [], {**quota, "X-Total-Results": "0"})}
    )
    await _read(connect, server)
    held = rate.held_for(ACCOUNT)
    assert held is not None and 40 < held <= 45
