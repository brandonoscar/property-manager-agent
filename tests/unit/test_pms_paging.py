"""Reading a whole collection, and knowing whether it is whole.

``complete`` is the point: rows that arrived are written either way, but only
a read the vendor confirmed whole may be stamped fresh, pruned from, or
advance a watermark (gotchas 26/55/149). Each test drives the real paging
loop over the real ``PmsHttpClient`` and a mock transport.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from helixis.pms.auth import HeaderAuth
from helixis.pms.errors import PmsApiError
from helixis.pms.http import ClientConfig, PmsHttpClient
from helixis.pms.paging import (
    Collection,
    OffsetPaging,
    PageNumberPaging,
    has_next_link,
    read_all,
)

RM_OPS = {"GET /Tenants": frozenset({"pageNumber", "pageSize", "filters"})}
PW_OPS = {"GET /leases": frozenset({"limit", "offset", "lastModifiedDateTimeStart"})}


def _client(handler: Any, ops: dict[str, frozenset[str]]) -> PmsHttpClient:
    config = ClientConfig(
        pms="rentmanager",
        account="acme",
        base_url="https://acme.api.example.com",
        operations=ops,
        requests_per_sec=100.0,
        concurrency=1,
    )
    return PmsHttpClient(
        config,
        HeaderAuth({"X-Key": "k"}),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def _rows(start: int, count: int) -> list[dict[str, int]]:
    return [{"TenantID": i} for i in range(start, start + count)]


class _PagedServer:
    """Serves ``total`` rows a page at a time, like Rent Manager."""

    def __init__(self, total: int, *, link: bool = True, header: bool = True, status: int = 200):
        self.total = total
        self.link = link
        self.header = header
        self.status = status
        self.seen: list[dict[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        self.seen.append(params)
        page, size = int(params["pageNumber"]), int(params["pageSize"])
        start = (page - 1) * size
        rows = _rows(start, max(0, min(size, self.total - start)))
        headers = {"X-Total-Results": str(self.total)} if self.header else {}
        if self.link and start + size < self.total:
            headers["Link"] = (
                f'<https://acme.api.example.com/Tenants?pageNumber={page + 1}>; rel="next"'
            )
        return httpx.Response(self.status, content=json.dumps(rows).encode(), headers=headers)


async def test_page_number_paging_reads_to_the_named_total() -> None:
    server = _PagedServer(2500)
    got = await read_all(
        _client(server, RM_OPS), "/Tenants", strategy=PageNumberPaging(page_size=1000)
    )
    assert got == Collection(rows=_rows(0, 2500), complete=True, total=2500)
    assert [s["pageNumber"] for s in server.seen] == ["1", "2", "3"]
    assert {s["pageSize"] for s in server.seen} == {"1000"}


async def test_a_206_page_is_a_page() -> None:
    got = await read_all(
        _client(_PagedServer(30, status=206), RM_OPS),
        "/Tenants",
        strategy=PageNumberPaging(page_size=10),
    )
    assert got.complete and len(got.rows) == 30


async def test_without_a_total_the_read_is_never_complete() -> None:
    got = await read_all(
        _client(_PagedServer(15, header=False), RM_OPS),
        "/Tenants",
        strategy=PageNumberPaging(page_size=10),
    )
    assert len(got.rows) == 15
    assert got.complete is False and got.total is None


async def test_a_read_that_ends_short_of_the_total_is_incomplete() -> None:
    def _short(request: httpx.Request) -> httpx.Response:
        # Claims 50, sends one page of 10, then nothing.
        first = request.url.params["pageNumber"] == "1"
        rows = _rows(0, 10) if first else []
        return httpx.Response(
            200, content=json.dumps(rows).encode(), headers={"X-Total-Results": "50"}
        )

    got = await read_all(
        _client(_short, RM_OPS), "/Tenants", strategy=PageNumberPaging(page_size=10)
    )
    assert got.complete is False and got.total == 50 and len(got.rows) == 10


async def test_a_vendor_that_ignores_the_page_parameter_is_caught_not_counted() -> None:
    def _same_page(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=json.dumps(_rows(0, 10)).encode(), headers={"X-Total-Results": "50"}
        )

    got = await read_all(
        _client(_same_page, RM_OPS), "/Tenants", strategy=PageNumberPaging(page_size=10)
    )
    assert got.complete is False
    assert len(got.rows) == 10, "the repeated page was counted again"


async def test_a_next_link_left_over_means_incomplete() -> None:
    def _link_forever(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=json.dumps(_rows(0, 5)).encode(),
            headers={"X-Total-Results": "5", "Link": '<x?pageNumber=2>; rel="next"'},
        )

    got = await read_all(
        _client(_link_forever, RM_OPS),
        "/Tenants",
        strategy=PageNumberPaging(page_size=5),
        max_pages=3,
    )
    assert got.complete is False


async def test_a_next_link_on_the_last_page_means_incomplete_even_at_the_total() -> None:
    """The total is reached, then an empty page still names a next one: the
    vendor did not say the read ended, so it is not complete."""

    def _linked(request: httpx.Request) -> httpx.Response:
        first = request.url.params["pageNumber"] == "1"
        rows = _rows(0, 5) if first else []
        return httpx.Response(
            200,
            content=json.dumps(rows).encode(),
            headers={"X-Total-Results": "5", "Link": '<x?pageNumber=9>; rel="next"'},
        )

    got = await read_all(
        _client(_linked, RM_OPS), "/Tenants", strategy=PageNumberPaging(page_size=5)
    )
    assert len(got.rows) == 5 and got.total == 5
    assert got.complete is False


@pytest.mark.parametrize("status", [204, 404])
async def test_a_status_the_vendor_uses_for_no_rows_is_an_empty_complete_list(
    status: int,
) -> None:
    def _empty(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status)

    got = await read_all(
        _client(_empty, RM_OPS),
        "/Tenants",
        strategy=PageNumberPaging(page_size=10, empty_statuses=frozenset({204, 404})),
    )
    assert got == Collection(rows=[], complete=True, total=0)


async def test_a_404_on_a_list_is_an_error_unless_the_connector_names_it_empty() -> None:
    def _missing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"{}")

    with pytest.raises(PmsApiError):
        await read_all(_client(_missing, PW_OPS), "/leases", strategy=OffsetPaging(limit=10))


async def test_a_404_after_the_first_page_is_an_error_even_when_named_empty() -> None:
    calls: list[int] = []

    def _then_404(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(
                200, content=json.dumps(_rows(0, 10)).encode(), headers={"X-Total-Results": "20"}
            )
        return httpx.Response(404, content=b"{}")

    with pytest.raises(PmsApiError):
        await read_all(
            _client(_then_404, RM_OPS),
            "/Tenants",
            strategy=PageNumberPaging(page_size=10, empty_statuses=frozenset({404})),
        )


class _OffsetServer:
    """Serves Propertyware-style pages; ``totals`` is the header per call."""

    def __init__(self, rows: int, totals: list[int] | None = None):
        self.rows = rows
        self.totals = totals
        self.seen: list[dict[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        self.seen.append(params)
        offset, limit = int(params["offset"]), int(params["limit"])
        total = self.totals.pop(0) if self.totals else self.rows
        rows = [{"id": i} for i in range(offset, min(offset + limit, self.rows))]
        return httpx.Response(
            200, content=json.dumps(rows).encode(), headers={"X-Total-Count": str(total)}
        )


async def test_offset_paging_reads_to_the_named_total() -> None:
    server = _OffsetServer(1200)
    got = await read_all(_client(server, PW_OPS), "/leases", strategy=OffsetPaging(limit=500))
    assert got.complete and len(got.rows) == 1200 and got.total == 1200
    assert [s["offset"] for s in server.seen] == ["0", "500", "1000"]
    assert {s["limit"] for s in server.seen} == {"500"}


async def test_a_total_that_moves_once_is_read_again() -> None:
    # The first pass sees 20, then 21, then 20 again: rows moved under it.
    server = _OffsetServer(20, totals=[20, 21, 20])
    got = await read_all(_client(server, PW_OPS), "/leases", strategy=OffsetPaging(limit=10))
    assert got.complete and len(got.rows) == 20
    starts = [s["offset"] for s in server.seen].count("0")
    assert starts == 2, "the read was not repeated after the total moved"


async def test_a_total_that_moves_twice_is_incomplete() -> None:
    server = _OffsetServer(20, totals=[20, 21, 20, 22])
    got = await read_all(_client(server, PW_OPS), "/leases", strategy=OffsetPaging(limit=10))
    assert got.complete is False and len(got.rows) == 20


async def test_the_page_ceiling_makes_the_read_incomplete() -> None:
    server = _PagedServer(100)
    got = await read_all(
        _client(server, RM_OPS),
        "/Tenants",
        strategy=PageNumberPaging(page_size=10),
        max_pages=3,
    )
    assert got.complete is False and len(got.rows) == 30


async def test_rows_in_an_envelope_are_read_by_key() -> None:
    def _enveloped(request: httpx.Request) -> httpx.Response:
        body = {"records": [{"id": 1}, {"id": 2}]}
        return httpx.Response(
            200, content=json.dumps(body).encode(), headers={"X-Total-Count": "2"}
        )

    got = await read_all(
        _client(_enveloped, PW_OPS), "/leases", strategy=OffsetPaging(limit=10, rows_key="records")
    )
    assert got.complete and got.rows == [{"id": 1}, {"id": 2}]


DL_OPS = {"GET /properties": frozenset({"page_number", "page_size"})}


def _body_totals(total: Any, count: int) -> Any:
    """A DoorLoop-style list: ``{"total": total, "data": rows}``, paged by
    ``page_number``/``page_size``, the total stated in the body only."""

    def serve(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        page, size = int(params["page_number"]), int(params["page_size"])
        start = (page - 1) * size
        rows = _rows(start, max(0, min(size, count - start)))
        body = {"total": total, "data": rows}
        return httpx.Response(
            200, content=json.dumps(body).encode(), headers={"X-Total-Results": "1"}
        )

    return serve


def _doorloop_paging(size: int) -> PageNumberPaging:
    return PageNumberPaging(
        page_param="page_number",
        size_param="page_size",
        page_size=size,
        rows_key="data",
        total_key="total",
    )


async def test_a_total_in_the_body_is_read_by_key_and_the_header_is_not() -> None:
    got = await read_all(
        _client(_body_totals(25, 25), DL_OPS), "/properties", strategy=_doorloop_paging(10)
    )
    assert got.complete is True and got.total == 25 and len(got.rows) == 25
    assert [r["TenantID"] for r in got.rows] == list(range(25))


@pytest.mark.parametrize("total", ["25", True, -1, 2.5, None, [25]])
async def test_a_body_total_that_is_not_a_whole_number_never_completes(total: Any) -> None:
    got = await read_all(
        _client(_body_totals(total, 25), DL_OPS), "/properties", strategy=_doorloop_paging(10)
    )
    assert got.complete is False and got.total is None


async def test_a_whole_float_total_is_a_total() -> None:
    got = await read_all(
        _client(_body_totals(25.0, 25), DL_OPS), "/properties", strategy=_doorloop_paging(10)
    )
    assert got.complete is True and got.total == 25


@pytest.mark.parametrize("body", [b'{"message": "hi"}', b"[1, 2]", b'[{"id": 1}, "x"]'])
async def test_a_body_that_is_not_a_list_of_records_is_an_error(body: bytes) -> None:
    def _odd(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"X-Total-Count": "2"})

    with pytest.raises(PmsApiError):
        await read_all(_client(_odd, PW_OPS), "/leases", strategy=OffsetPaging(limit=10))


@pytest.mark.parametrize(
    ("link", "expected"),
    [
        ('<https://x/a?page=2>; rel="next"', True),
        ("<https://x/a?page=2>; rel=next", True),
        ('<https://x/a?page=1>; rel="prev", <https://x/a?page=3>; rel="next"', True),
        ('<https://x/a?page=1>; rel="prev"', False),
        ("", False),
    ],
)
def test_the_next_link_is_read_from_the_link_header(link: str, expected: bool) -> None:
    assert has_next_link({"Link": link}) is expected
