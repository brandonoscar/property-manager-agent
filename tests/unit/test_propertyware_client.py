"""The Propertyware client: what it may call, how, and what it refuses.

Each behavioural test drives the real ``PmsHttpClient`` the connector builds,
over an ``httpx.MockTransport``, so what is asserted is what would have left
the process. The walks over the operations index read the index the client is
actually GIVEN (``client.config(...).operations``), not a list typed here.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from helixis.pms import http as pms_http
from helixis.pms.errors import Failure, PmsApiError, RefusedBeforeSendingError, classify_status
from helixis.pms.paging import read_all
from helixis.propertyware import _paths, client, sync
from helixis.propertyware.credentials import PropertywareCredentials
from tests.unit._propertyware_fake import (
    API_PREFIX,
    CLIENT_ID,
    CLIENT_SECRET,
    COMPANY,
    SYSTEM_ID,
    FakePropertyware,
    fixture_rows,
)

REPO = Path(__file__).resolve().parents[2]
PACKAGE = REPO / "src" / "helixis" / "propertyware"

CREDS = PropertywareCredentials(
    company_id=COMPANY, system_id=SYSTEM_ID, client_id=CLIENT_ID, client_secret=CLIENT_SECRET
)


@pytest.fixture
def waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    seen: list[float] = []

    async def _record(seconds: float) -> None:
        seen.append(seconds)

    monkeypatch.setattr(pms_http, "_sleep", _record)
    return seen


def _spec_paths() -> set[str]:
    return {key.split(" ", 1)[1] for key in _paths.QUERY_PARAMS}


# ── the configuration ─────────────────────────────────────────────────────


def test_the_client_configuration_is_the_designs() -> None:
    cfg = client.config(SYSTEM_ID)
    assert cfg.pms == "propertyware"
    assert cfg.account == SYSTEM_ID
    assert cfg.base_url == "https://api.propertyware.com/pw/api/rest/v1"
    # Design W6: one in flight, at most two a second per company.
    assert cfg.requests_per_sec == 2.0
    assert cfg.concurrency == 1
    # No write verb at all in this slice, and nothing to strip from a body.
    assert cfg.write_methods == frozenset()
    assert cfg.forbidden_keys == frozenset()
    # Propertyware's own status meanings are the shared defaults.
    assert dict(cfg.status_meanings) == {}


def test_the_operations_index_is_the_reads_this_slice_makes() -> None:
    ops = client.config(SYSTEM_ID).operations
    assert set(ops) == {f"GET {path}" for path in client.READS}
    for key, params in ops.items():
        assert params == _paths.QUERY_PARAMS[key], key


def test_no_payment_type_endpoint_is_in_the_index_the_client_is_given() -> None:
    """Design W3 / terms rule 11: no payment-type call, reads included."""
    ops = client.config(SYSTEM_ID).operations
    offenders = [key for key in ops if client.is_payment_path(key.split(" ", 1)[1])]
    assert offenders == []


def test_the_payment_matcher_finds_every_family_the_terms_name() -> None:
    """Both sides: each family named in the terms exists in the spec, and the
    matcher flags all of it and nothing else."""
    families = {
        "bill payments": "/bills/payment",
        "vendor checks": "/bills/vendorchecks",
        "owner draws": "/accounting/ownerdraws",
        "owner contributions": "/accounting/ownercontributions",
        "bank deposits": "/accounting/bankdeposits",
        "lease payments": "/leases/payments",
        "refunds": "/leases/refunds",
        "auto e-payments": "/leases/{leaseId}/autoepayments",
    }
    paths = _spec_paths()
    for family, prefix in families.items():
        matching = [p for p in paths if p.startswith(prefix)]
        assert matching, f"{family}: the spec has no such path any more"
        assert all(client.is_payment_path(p) for p in matching), family
    flagged = {p for p in paths if client.is_payment_path(p)}
    assert all(any(p.startswith(prefix) for prefix in families.values()) for p in flagged)
    # W3 reads delinquency from charges: they are not a payment endpoint.
    assert not client.is_payment_path("/leases/charges")


def test_no_operation_the_client_may_call_is_beta_marked() -> None:
    ops = client.config(SYSTEM_ID).operations
    assert [key for key in ops if key in _paths.BETA] == []


def test_every_read_declares_a_total_so_it_can_be_complete() -> None:
    ops = client.config(SYSTEM_ID).operations
    assert [key for key in ops if key not in _paths.TOTAL_COUNT] == []


@pytest.mark.parametrize(
    ("added", "why"),
    [
        ("/leases/payments", "a payment-type read"),
        ("/Buildings", "a path not in the spec (wrong case)"),
        ("/leases/autocharges", "a list with no X-Total-Count"),
    ],
)
def test_an_unusable_read_stops_the_client_being_built(
    monkeypatch: pytest.MonkeyPatch, added: str, why: str
) -> None:
    monkeypatch.setattr(client, "READS", (*client.READS, added))
    with pytest.raises(client.UnusableReadError):
        client.config(SYSTEM_ID)


def test_a_beta_marked_read_stops_the_client_being_built(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_paths, "BETA", _paths.BETA | {"GET /buildings"})
    with pytest.raises(client.UnusableReadError, match="beta"):
        client.config(SYSTEM_ID)


def test_paging_is_limit_500_by_offset_with_x_total_count() -> None:
    assert client.PAGING.limit == 500
    assert (client.PAGING.limit_param, client.PAGING.offset_param) == ("limit", "offset")
    assert client.PAGING.total_header == "X-Total-Count"
    # A 404 on a list is an ERROR: no status means "no rows".
    assert client.PAGING.empty_statuses == frozenset()


@pytest.mark.parametrize(
    ("status", "failure", "retryable"),
    [
        (401, Failure.CREDENTIALS, False),
        (403, Failure.PERMISSION, False),
        (404, Failure.NOT_FOUND, False),
        (400, Failure.BAD_REQUEST, False),
        (429, Failure.UNDOCUMENTED, True),
        (503, Failure.UNDOCUMENTED, True),
    ],
)
def test_status_meanings(status: int, failure: Failure, retryable: bool) -> None:
    meanings = client.config(SYSTEM_ID).status_meanings
    assert classify_status(status, meanings) == (failure, retryable)


# ── what a request carries ────────────────────────────────────────────────


async def test_a_read_sends_the_three_headers_and_no_credential_in_the_url() -> None:
    server = FakePropertyware({"/buildings": fixture_rows("/buildings")})
    pw = client.build_client(CREDS, http=server.http())
    got = await read_all(
        pw, "/buildings", strategy=client.PAGING, query=sync.query_for("/buildings")
    )
    assert got.complete and len(got.rows) == len(fixture_rows("/buildings"))
    (request,) = server.requests
    assert request.headers["x-propertyware-client-id"] == CLIENT_ID
    assert request.headers["x-propertyware-client-secret"] == CLIENT_SECRET
    assert request.headers["x-propertyware-system-id"] == SYSTEM_ID
    assert CLIENT_ID not in str(request.url) and CLIENT_SECRET not in str(request.url)
    assert request.url.path == f"{API_PREFIX}/buildings"
    assert request.url.params["limit"] == "500"
    assert request.url.params["offset"] == "0"
    assert request.url.params["orderby"] == "id asc"


async def test_pages_are_read_by_offset_until_the_total() -> None:
    rows = [{"id": 990100000 + i} for i in range(1, 1201)]
    server = FakePropertyware({"/buildings": rows})
    pw = client.build_client(CREDS, http=server.http())
    got = await read_all(pw, "/buildings", strategy=client.PAGING)
    assert got.complete and len(got.rows) == 1200
    assert [r.url.params["offset"] for r in server.requests] == ["0", "500", "1000"]
    assert {r.url.params["limit"] for r in server.requests} == {"500"}


async def test_a_404_on_a_list_is_an_error_never_no_rows() -> None:
    server = FakePropertyware({"/buildings": []}, fail={"/buildings": 404})
    pw = client.build_client(CREDS, http=server.http())
    with pytest.raises(PmsApiError) as raised:
        await read_all(pw, "/buildings", strategy=client.PAGING)
    assert raised.value.status == 404 and raised.value.failure is Failure.NOT_FOUND
    assert len(server.requests) == 1, "a 404 is not retried"


@pytest.mark.parametrize(
    ("status", "failure"), [(401, Failure.CREDENTIALS), (403, Failure.PERMISSION)]
)
async def test_an_auth_refusal_is_final(status: int, failure: Failure) -> None:
    """Fixed headers: nothing to refresh, so a 401 is never replayed."""
    server = FakePropertyware({"/buildings": []}, fail={"/buildings": status})
    pw = client.build_client(CREDS, http=server.http())
    with pytest.raises(PmsApiError) as raised:
        await read_all(pw, "/buildings", strategy=client.PAGING)
    assert raised.value.failure is failure
    assert len(server.requests) == 1


async def test_a_429_backs_off_then_retries_the_read(waits: list[float]) -> None:
    served: list[int] = []

    def answer(request: httpx.Request) -> httpx.Response:
        served.append(1)
        if len(served) == 1:
            return httpx.Response(429, json={"errorCode": "RATE"})
        return httpx.Response(200, json=[{"id": 990100001}], headers={"X-Total-Count": "1"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(answer))
    pw = client.build_client(CREDS, http=http)
    got = await read_all(pw, "/buildings", strategy=client.PAGING)
    assert got.complete and len(served) == 2
    # The shared toolkit's 429 schedule: a slower first wait than a 5xx gets.
    assert waits and waits[0] >= 10.0


async def test_an_error_body_echoing_a_credential_is_scrubbed() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, content=json.dumps({"userMessage": f"bad {CLIENT_SECRET}"}))

    pw = client.build_client(CREDS, http=httpx.AsyncClient(transport=httpx.MockTransport(answer)))
    with pytest.raises(PmsApiError) as raised:
        await read_all(pw, "/buildings", strategy=client.PAGING)
    assert CLIENT_SECRET not in str(raised.value)


# ── what is refused before sending ────────────────────────────────────────


@pytest.mark.parametrize(
    ("template", "query"),
    [
        ("/Buildings", None),  # wrong case: a 404 at Propertyware
        ("/leases/payments", None),  # a payment-type read
        ("/leases/autocharges", None),  # not a read this slice makes
        ("/buildings", {"Limit": 500}),  # a wrong-case paging name
        ("/buildings", {"buildingId": 1}),  # a filter the list does not declare
    ],
)
async def test_an_undocumented_call_is_never_sent(template: str, query: Any) -> None:
    server = FakePropertyware()
    pw = client.build_client(CREDS, http=server.http())
    with pytest.raises(RefusedBeforeSendingError):
        await pw.get(template, query=query)
    assert server.requests == []


@pytest.mark.parametrize("verb", ["POST", "PUT", "PATCH", "DELETE"])
async def test_no_write_can_be_sent(verb: str) -> None:
    server = FakePropertyware()
    pw = client.build_client(CREDS, http=server.http())
    with pytest.raises(RefusedBeforeSendingError):
        await pw.send_once(verb, "/workorders", json_body={"description": "x"})
    assert server.requests == []


@pytest.mark.parametrize("bad", ["990\r\nX-Evil: 1", "99\n0", "", "  ", "\x00"])
def test_a_system_id_that_is_not_header_safe_never_becomes_a_header(bad: str) -> None:
    creds = PropertywareCredentials(
        company_id=COMPANY, system_id=bad, client_id=CLIENT_ID, client_secret=CLIENT_SECRET
    )
    with pytest.raises(ValueError):
        client.auth(creds)


# ── no literal path outside the generated index (an AST guard) ────────────


def _literal_paths(source: str) -> set[str]:
    """String constants that are a bare API path, docstrings excluded."""
    tree = ast.parse(source)
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    found: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            and node.value.startswith("/")
            and len(node.value) > 1
            and all(c.isalnum() or c in "/{}_-" for c in node.value)
        ):
            found.add(node.value)
    return found


def test_every_literal_path_in_the_package_is_in_the_generated_index() -> None:
    found: dict[str, set[str]] = {}
    for path in sorted(PACKAGE.glob("*.py")):
        if path.name == "_paths.py":
            continue
        found[path.name] = _literal_paths(path.read_text(encoding="utf-8"))
    every = set().union(*found.values())
    assert set(client.READS) <= every, "the walk found the reads it must find"
    assert sorted(every - _spec_paths()) == []


def test_the_path_walk_can_fail() -> None:
    source = '"""/docstring/only"""\nX = "/Buildings"\n'
    assert _literal_paths(source) == {"/Buildings"}
