"""Phase 4 guards: the client, the mapper, and the sync loop.

Fixtures are built from field names SOURCED to the captured spec (with ``allOf``
resolved), not invented — a mapper tested against a fixture somebody made up
tests the fixture. The drift guard below reads the real `entities.py` INSERTs so
the Rentvine column sets cannot diverge from the Buildium ones (gotcha 28).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from helixis.rentvine import client as rvclient
from helixis.rentvine import mapper, sync, transport
from helixis.rentvine._row_envelopes import ROW_ENVELOPE
from helixis.settings import RentvineSettings, Settings

from ._pg_bind_contract import reject_unbindable, reject_untyped_mirror_bind


def wire(operation: str, *records: dict[str, Any]) -> list[dict[str, Any]]:
    """Records as the SERVER sends them — wrapped per the operation's envelope.

    ⚠ The fixtures below are flat because a MAPPER takes the record. The wire
    does not: five of the seven collections the sync reads wrap each row, and a
    flat fixture at the HTTP boundary states a shape Rentvine does not produce.
    Every sync test here previously used one, so the fixtures and the client
    agreed with each other and neither agreed with the spec — the inverted-fake
    shape (gotcha 123), which is how the wrapper bug passed a full suite.

    ⚠ This derives from ``ROW_ENVELOPE``, the same map the client reads, so it
    CANNOT catch a wrong map — by construction it would agree with one. That
    job belongs to ``test_rentvine_row_envelopes.py``, which pins the map
    against literals measured from the spec by hand. This helper's only job is
    to keep the sync tests honest about the shape, given the map.
    """
    key = ROW_ENVELOPE[operation]
    return [r if key is None else {key: r} for r in records]


REPO = Path(__file__).resolve().parents[2]


def _settings() -> RentvineSettings:
    return RentvineSettings(enabled=True, account_code="acme", api_key="k", api_secret="s")


def _client(handler) -> rvclient.RentvineClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), auth=("k", "s"))
    return rvclient.RentvineClient(_settings(), client=http)


@pytest.fixture(autouse=True)
def _no_real_sleeping(monkeypatch):
    """Keep the real ATTEMPT COUNT and drop the real waits.

    ⚠ Two tests here answer a retryable status, and the day the client started
    ACTING on ``retryable`` they went from 0.01s to 7.5s each — 48x on this
    file, paid on every CI run, for sleeping. Patching the schedule rather than
    ``asyncio.sleep`` keeps the production attempt count intact: the bound is
    ``len(RETRY_BACKOFF_S) + 1``, so a same-length tuple of zeroes exercises
    exactly what ships. See ``test_rentvine_retry.py`` for the retry's own
    guards; these two are what prove it reaches the SYNC loop.
    """
    monkeypatch.setattr(rvclient, "RETRY_BACKOFF_S", (0.0,) * len(rvclient.RETRY_BACKOFF_S))
    # A 429 with no Retry-After backs off on its own, slower schedule.
    monkeypatch.setattr(
        rvclient, "RATE_LIMIT_BACKOFF_S", (0.0,) * len(rvclient.RATE_LIMIT_BACKOFF_S)
    )


# ── the client ───────────────────────────────────────────────────────────────


def test_a_client_cannot_be_built_without_an_account() -> None:
    """No constructor reads global settings — see the class docstring."""
    with pytest.raises(ValueError, match="account_code"):
        rvclient.RentvineClient(RentvineSettings(api_key="k", api_secret="s"))
    with pytest.raises(ValueError, match="api_key"):
        rvclient.RentvineClient(RentvineSettings(account_code="acme"))


def test_the_client_has_exactly_one_write_method() -> None:
    """⚠ **RE-POINTED IN PHASE 6, NOT DELETED, AND THE PROPERTY IS UNCHANGED.**

    This asserted ``post`` was ABSENT, and the class docstring's claim was that
    a later edit asking it to write would have to ADD A METHOD rather than pass
    an argument — "a diff a reviewer sees". Phase 6 is that diff, so the
    guarantee was not weakened, it was COLLECTED: the assertion moves from
    "there is no write" to "there is exactly one, it is POST, and nothing else
    on this class can write".

    ``put``/``patch``/``delete`` stay forbidden — Rentvine has no PUT and no
    PATCH at all (T1: 78 POST, 18 DELETE, zero of either), and its 18 DELETEs
    include properties, leases, units and contacts with NO documented soft-delete
    semantics. A destructive verb on this client would be one import away from a
    tool that erases a landlord's records.
    """
    public = {n for n in dir(rvclient.RentvineClient) if not n.startswith("_")}
    assert not (public & {"put", "patch", "delete", "request"}), public
    assert "post" in public, "the one write method went missing"
    src = Path(rvclient.__file__).read_text()
    for verb in ("client.put(", "client.patch(", "client.delete("):
        assert verb not in src
    # Exactly ONE transport-level write call, and it hardcodes the verb.
    assert src.count("self._client.post(") == 1, "a second write path appeared"


@pytest.mark.asyncio
async def test_an_undeclared_query_parameter_is_refused_before_it_is_sent() -> None:
    """Rentvine IGNORES an unknown filter and returns the WHOLE collection with
    a 200 — gotcha 24's shape. Refusing locally is the only way to notice."""
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json=[])

    c = _client(handler)
    with pytest.raises(transport.UnknownQueryParameterError):
        await c.get("/properties", query={"notAReal Filter": 1})
    assert called is False, "the request must not leave the process"
    await c.aclose()


@pytest.mark.asyncio
async def test_a_403_is_not_reported_as_a_credential_problem() -> None:
    """gotcha 102: telling a customer to reconnect a working connection sends
    them to redo something that is not broken."""
    c = _client(lambda r: httpx.Response(403, text="nope"))
    with pytest.raises(rvclient.RentvineAPIError) as exc:
        await c.get("/properties")
    assert exc.value.failure is transport.Failure.PERMISSION
    assert exc.value.retryable is False
    await c.aclose()


@pytest.mark.asyncio
async def test_an_undocumented_status_is_retryable_including_429() -> None:
    """⚠ The classification AND its consumer, because for a long time there was
    no consumer.

    ``retryable`` was computed here, carried on the exception, and read by
    nobody — so this test asserted a value that changed nothing, and a 429
    mid-sync marked a whole mirror table failed exactly as a dead credential
    would. Asserting the attempt count beside the flag is what makes the two
    sides agree (gotcha 28); the flag alone passed throughout.
    """
    attempts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(str(request.url))
        return httpx.Response(429, text="slow down")

    c = _client(handler)
    with pytest.raises(rvclient.RentvineAPIError) as exc:
        await c.get("/properties")
    assert exc.value.failure is transport.Failure.UNDOCUMENTED
    assert exc.value.retryable is True
    # ⚠ RE-POINTED (rule 19), not relaxed: a 429 with no Retry-After now backs
    # off on RATE_LIMIT_BACKOFF_S, the slower schedule terms rule 1 calls for,
    # so its bound is that schedule's length. Still more than one attempt.
    assert len(attempts) == len(rvclient.RATE_LIMIT_BACKOFF_S) + 1, "the flag has no consumer"
    await c.aclose()


# ── pagination: the header contract, and the fallback ────────────────────────


@pytest.mark.asyncio
async def test_paging_follows_the_servers_own_page_count() -> None:
    """40 of the 44 paginating operations return `pagination-total-pages`."""
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        seen.append(page)
        return httpx.Response(
            200,
            json=wire("GET /properties", {"propertyID": page}),
            headers={"pagination-total-pages": "3", "pagination-total-items": "3"},
        )

    c = _client(handler)
    got = await c.iter_all("/properties", page_size=1)
    assert seen == [1, 2, 3], "must stop at the server's stated page count"
    assert len(got.rows) == 3
    # The server named the page count, so the end is a FACT — this is the half
    # of the pair that must be claimed complete.
    assert got.stop_was_documented is True
    await c.aclose()


@pytest.mark.asyncio
async def test_without_the_header_it_falls_back_to_the_short_page_heuristic() -> None:
    """The six export-style operations declare no pagination headers."""

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        units = [{"unitID": 1}, {"unitID": 2}] if page == 1 else [{"unitID": 3}]
        rows = wire("GET /properties/units/export", *units)
        return httpx.Response(200, json=rows)  # no headers at all

    c = _client(handler)
    got = await c.iter_all("/properties/units/export", page_size=2)
    assert len(got.rows) == 3
    # ⚠ THE OTHER HALF OF THE PAIR, and the one the defect turned on: the rows
    # are real and the END is a guess from a short page. Claiming this count
    # as a total is the fabricated-completeness class (gotchas 26/55/57).
    assert got.stop_was_documented is False
    await c.aclose()


@pytest.mark.asyncio
async def test_a_reconciled_row_count_is_documented_even_without_a_page_count() -> None:
    """⚠ THE THIRD STATE, which a two-way reading of the headers misses.

    ``pagination-total-items`` WITHOUT ``pagination-total-pages`` is a real
    combination, and it is a server-confirmed end rather than a guess: the
    stop came from a short page, but :meth:`iter_all` reconciles the collected
    count against the server's own total and RAISES on a mismatch, so
    returning at all means the server named the number and we matched it.
    Arguably stronger than following a page count.

    Written because the mutation that drops that arm survived every other
    assertion in this file — no test supplied the distinguishing input, which
    is gotcha 58's question answered "the test", not "the code is redundant".
    Under-claiming here is not harmless: it withholds a true completeness
    signal on the collections the reconcile makes safest.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=wire("GET /properties", {"propertyID": 4610}),
            headers={"pagination-total-items": "1"},  # no total-pages
        )

    c = _client(handler)
    got = await c.iter_all("/properties", page_size=5)
    assert len(got.rows) == 1
    # The page itself could only guess...
    assert rvclient.Page(got.rows, False, total_items=1).stop_condition_was_documented is False
    # ...and the COLLECTION still knows the server confirmed the count.
    assert got.stop_was_documented is True
    await c.aclose()


@pytest.mark.asyncio
async def test_a_short_collection_is_refused_rather_than_reported_as_complete() -> None:
    """The server said 10 items; we could only page 1. Returning it silently is
    how a customer's portfolio goes quietly missing (gotchas 26/55)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=wire("GET /properties", {"propertyID": 1}),
            headers={"pagination-total-pages": "1", "pagination-total-items": "10"},
        )

    c = _client(handler)
    with pytest.raises(rvclient.RentvineAPIError, match="pagination-total-items"):
        await c.iter_all("/properties", page_size=5)
    await c.aclose()


@pytest.mark.asyncio
async def test_the_page_ceiling_raises_instead_of_truncating() -> None:
    c = _client(lambda r: httpx.Response(200, json=wire("GET /properties", {"propertyID": 1})))
    with pytest.raises(rvclient.RentvineAPIError, match="ceiling"):
        await c.iter_all("/properties", page_size=1, max_pages=3)
    await c.aclose()


def test_a_page_says_whether_the_stop_was_documented_or_guessed() -> None:
    assert rvclient.Page([], False, total_pages=2).stop_condition_was_documented is True
    assert rvclient.Page([], False).stop_condition_was_documented is False


def test_iter_all_reads_the_pages_own_property_rather_than_re_deriving_it() -> None:
    """⚠ A SOURCE GUARD, because the tidy-up it stops is behaviourally INVISIBLE.

    ``got.stop_condition_was_documented`` and ``got.total_pages is not None``
    return the same value for every input, so no behavioural test can tell them
    apart (gotcha 58 — check whether the mutant differs at all). What separates
    them is that inlining puts the rule in two places and leaves the property
    with no production reader again, which is the whole defect this fix is
    about: ``grep`` found only its definition and two assertions in this file.
    Gotcha 147 added the same guard for ``exc.retryable``, and for the same
    reason — a second spelling drifts, and this one drifts silently.
    """
    import ast

    source = Path(rvclient.__file__).read_text(encoding="utf-8")
    fn = next(
        n
        for n in ast.walk(ast.parse(source))
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "iter_all"
    )
    reads = {
        n.attr
        for n in ast.walk(fn)
        if isinstance(n, ast.Attribute) and n.attr == "stop_condition_was_documented"
    }
    assert reads, (
        "iter_all must read Page.stop_condition_was_documented rather than "
        "re-deriving `total_pages is not None` — two spellings of one rule is "
        "the drift gotcha 25 exists to stop, and inlining it returns that "
        "property to having no production reader at all."
    )


def test_an_ambiguous_envelope_is_refused_rather_than_guessed() -> None:
    assert rvclient._rows_from([{"a": 1}]) == [{"a": 1}]
    assert rvclient._rows_from({"results": [{"a": 1}]}) == [{"a": 1}]
    assert rvclient._rows_from({"nothing": 1}) == []
    with pytest.raises(rvclient.RentvineAPIError, match="list-valued"):
        rvclient._rows_from({"results": [{"a": 1}], "errors": [{"b": 2}]})


def test_junk_pagination_headers_do_not_become_zero() -> None:
    """A junk total read as 0 turns 'we could not tell' into 'you have none'."""
    assert transport.total_pages_from({"pagination-total-pages": "seven"}) is None
    assert transport.total_pages_from({}) is None
    assert transport.total_pages_from({"pagination-total-pages": "-3"}) is None
    assert transport.total_pages_from({"Pagination-Total-Pages": "4"}) == 4


# ── the mapper ───────────────────────────────────────────────────────────────

# Field names are the spec's, with allOf resolved.
PROPERTY = {
    "propertyID": 4610,
    "name": "Redbud",
    "address": "500 Woodlane St",
    "city": "Denver",
    "stateID": "CO",
    "postalCode": "80202",
    "isActive": True,
    "propertyTypeID": 1,
}
UNIT = {"unitID": 77, "propertyID": 4610, "name": "4B", "rent": 1800, "leaseID": 991}
LEASE = {
    "leaseID": 991,
    "propertyID": 4610,
    "unitID": 77,
    "startDate": "2026-01-01",
    "endDate": "2027-03-18",
    "rentAmount": 1800,
    "primaryLeaseStatusID": 2,
}
CONTACT = {
    "contactID": 229933,
    "firstName": "Aaron",
    "lastName": "Tillman",
    "emails": "aaron@example.com",
    "phones": "555-0100",
    "isActive": True,
}
WORK_ORDER = {
    "workOrderID": 11316,
    "propertyID": 4610,
    "unitID": 77,
    "description": "Water heater leaking",
    "priorityID": 3,
    "primaryWorkOrderStatusID": 2,
    "assignedToUserID": 7,
    "vendorTradeID": 3,
}

#: A ``GET /leases/export`` lease, built from the spec's ``lease_export`` schema
#: (16 fields, NO ``rentAmount``) plus the ``contact_simplified`` tenants it
#: carries. Synthetic: no value here came from a live account.
EXPORT_LEASE = {
    "leaseID": 991,
    "propertyID": 4610,
    "unitID": 77,
    "startDate": "2026-01-01",
    "endDate": "2027-03-18",
    "primaryLeaseStatusID": 2,
    "dateTimeModified": "2026-09-30 04:00:00",
    "tenants": [
        {"contactID": 229933, "name": "Aaron Tillman", "isActive": 1},
        {"contactID": 229934, "name": "Former Roommate", "isActive": 0},
    ],
}


#: The ``balances`` object the export sends BESIDE each lease (spec schema
#: ``balances``). Zero by default: these leases owe nothing. Synthetic.
EXPORT_BALANCES = {
    "unpaidTotalAmount": 0,
    "unpaidRentAmount": 0,
    "pastDueTotalAmount": 0,
    "pastDueRentAmount": 0,
}


def export_row(lease: dict[str, Any], balances: Any = EXPORT_BALANCES) -> dict[str, Any]:
    """One export row as the server sends it: the lease, its balances beside."""
    return {"lease": lease, "balances": balances}


def export_leases(n: int, **over: Any) -> list[dict[str, Any]]:
    """``n`` distinct export leases, ids 1..n, each with one active tenant."""
    return [
        {
            **EXPORT_LEASE,
            "leaseID": i,
            "tenants": [{"contactID": 50_000 + i, "name": f"T{i}", "isActive": 1}],
            **over,
        }
        for i in range(1, n + 1)
    ]


def lease_route(
    request: httpx.Request,
    leases: list[dict[str, Any]],
    *,
    headers: bool = True,
    rent: Any = 1800,
) -> httpx.Response | None:
    """Answer the two lease routes the sync reads, or ``None`` for any other.

    The export pages by the request's own ``page``/``pageSize`` and, when
    ``headers`` is True, sends the pagination headers the live server was
    measured sending (``transport.MEASURED_PAGINATION_HEADERS``). ``getLease``
    answers ``{"lease": {...rentAmount}, "tenants": [...], "transactions": [...]}``."""
    path = request.url.path
    if path.endswith("/leases/export"):
        page = int(request.url.params.get("page", "1"))
        size = int(request.url.params.get("pageSize", "25"))
        chunk = leases[(page - 1) * size : page * size]
        pages = max(1, -(-len(leases) // size))
        head = (
            {"pagination-total-items": str(len(leases)), "pagination-total-pages": str(pages)}
            if headers
            else {}
        )
        return httpx.Response(200, json=[export_row(row) for row in chunk], headers=head)
    tail = path.rsplit("/leases/", 1)
    if len(tail) == 2 and tail[1].isdigit():
        lease_id = int(tail[1])
        return httpx.Response(
            200,
            json={
                "lease": {"leaseID": lease_id, "rentAmount": rent},
                "tenants": [
                    {
                        "leaseTenant": {
                            "leaseID": str(lease_id),
                            "contactID": str(50_000 + lease_id),
                            "isActive": "1",
                            "moveInDate": "2026-01-02",
                        },
                        "contact": {"contactID": 50_000 + lease_id},
                    }
                ],
                # The rent read also brings the lease's history (one payment).
                "transactions": [
                    {
                        "transactionID": 70_000 + lease_id,
                        "transactionTypeID": 2,
                        "amount": "1800.00",
                        "isVoided": 0,
                        "datePosted": "2026-10-01",
                    }
                ],
            },
        )
    return None


def test_every_mapper_puts_the_rentvine_id_in_the_identity_column() -> None:
    """ADR 0010 Decision (a): no external_id; the id goes where it already goes."""
    cases = [
        (mapper.map_property, PROPERTY, 4610),
        (mapper.map_unit, UNIT, 77),
        (mapper.map_lease, LEASE, 991),
        (mapper.map_tenant, CONTACT, 229933),
        (mapper.map_owner, CONTACT, 229933),
        (mapper.map_vendor, CONTACT, 229933),
        (mapper.map_work_order, WORK_ORDER, 11316),
    ]
    for fn, obj, expected in cases:
        out = fn(obj)
        assert out["buildium_id"] == expected, fn.__name__
        assert out["pms"] == "rentvine", fn.__name__
        assert "external_id" not in out, f"{fn.__name__} resurrected external_id"


def test_the_state_code_survives_being_called_an_id() -> None:
    """`stateID` carries 'CO', not a number. Coercing it would null every address."""
    assert mapper.map_property(PROPERTY)["state"] == "CO"


def test_the_unit_label_comes_from_name_not_unitNumber() -> None:  # noqa: N802
    """Rentvine has no `unitNumber`; reaching for it nulls every unit."""
    assert mapper.map_unit(UNIT)["unit_number"] == "4B"


def test_a_closed_work_order_maps_onto_the_mirrors_own_closed_vocabulary() -> None:
    """gotcha 50: two spellings of 'open' giving one question two answers."""
    from helixis.buildium.maintenance_status import is_open

    closed = mapper.map_work_order({**WORK_ORDER, "primaryWorkOrderStatusID": 3})
    assert is_open(closed["status"]) is False, closed["status"]
    assert is_open(mapper.map_work_order(WORK_ORDER)["status"]) is True


def test_an_unknown_work_order_status_reads_as_open_not_dropped() -> None:
    """The dangerous value is one the table does NOT contain.

    ⚠ This asserted `primaryWorkOrderStatusID: 4` and that is "On Hold" — a
    MAPPED status, so it never reached the fallback and the test was named for a
    property it did not exercise. The mutation battery is what found it: giving
    `.get()` a default of "Closed" left it green while every unrecognised status
    silently vanished from the open-maintenance rollups.
    """
    from helixis.buildium.maintenance_status import is_open

    unknown = 99
    assert unknown not in mapper.WORK_ORDER_STATUS, (
        "pick an id Rentvine does not document — if this one becomes real, this "
        "test stops testing the fallback rather than failing"
    )
    out = mapper.map_work_order({**WORK_ORDER, "primaryWorkOrderStatusID": unknown})
    assert out["status"] is None
    assert is_open(out["status"]) is True
    # and the fourth DOCUMENTED status is open too — a separate claim, since
    # "On Hold" is a thing Rentvine really sends.
    on_hold = mapper.map_work_order({**WORK_ORDER, "primaryWorkOrderStatusID": 4})
    assert on_hold["status"] == "On Hold"
    assert is_open(on_hold["status"]) is True


def test_a_lease_deposit_is_left_null_rather_than_borrowed_from_the_unit() -> None:
    """The unit's asking deposit is not what this tenant paid — writing one as
    the other is a confident wrong number about money."""
    assert mapper.map_lease(LEASE)["security_deposit"] is None


def test_contact_values_survive_all_three_plausible_shapes() -> None:
    """`emails` is named plural and typed string — nobody has seen a real one."""
    assert mapper._first_contact_value("a@x.com") == "a@x.com"
    assert mapper._first_contact_value("a@x.com,b@x.com") == "a@x.com"
    assert mapper._first_contact_value(["a@x.com", "b@x.com"]) == "a@x.com"
    assert mapper._first_contact_value([{"value": "a@x.com"}]) == "a@x.com"
    assert mapper._first_contact_value(None) is None
    assert mapper._first_contact_value("") is None


def test_a_non_numeric_id_yields_none_rather_than_a_fabricated_row() -> None:
    """If this ever fires on real data, ADR 0010 Decision (a) is wrong — which
    is a finding to surface, not a case to work around."""
    assert mapper.map_property({**PROPERTY, "propertyID": "not-a-number"})["buildium_id"] is None
    assert mapper.map_property({**PROPERTY, "propertyID": "4610"})["buildium_id"] == 4610
    assert mapper.map_property({**PROPERTY, "propertyID": None})["buildium_id"] is None
    assert mapper.map_property({**PROPERTY, "propertyID": True})["buildium_id"] is None


# ── the drift guard: both sides ──────────────────────────────────────────────


def _buildium_insert_columns(table: str) -> set[str]:
    src = (REPO / "src/helixis/db/repositories/entities.py").read_text()
    m = re.search(rf"INSERT INTO {table}\s*\((.*?)\)\s*VALUES", src, re.S)
    assert m, f"no INSERT found for {table} — this guard is pointed at nothing"
    return {c.strip() for c in m.group(1).replace("\n", " ").split(",") if c.strip()}


#: Columns only the Rentvine writer fills, because only Rentvine sends the fact.
#: Each needs its reason here, and every reader must fall back when a Buildium
#: row leaves it NULL (``test_a_rentvine_only_column_is_read_with_a_fallback``).
RENTVINE_ONLY: dict[str, set[str]] = {
    # Migration 0105: `dateTimeModified`. Buildium's /workorders exposes no
    # created or updated date at all (schemas.py, probe 2026-07-13).
    "helixis_work_orders": {"pms_updated_at"},
}


def test_rentvine_writes_the_same_columns_the_buildium_path_writes() -> None:
    """One mirror, two writers. If they drift, a Rentvine row is missing a column
    every mirror tool expects — and the tool reports the absence as a fact."""
    always = {"buildium_id", "account_id", "company_id", "raw"}
    for table, cols in sync.TABLE_COLUMNS.items():
        buildium = _buildium_insert_columns(table)
        # helixis_lease_balances has no buildium_id: it is keyed by lease_id,
        # which its column list names (sync._replace_balances writes it).
        rentvine = set(cols) | (always - {"buildium_id"} if "lease_id" in cols else always)
        missing = buildium - rentvine
        invented = rentvine - buildium - RENTVINE_ONLY.get(table, set())
        assert not missing, f"{table}: Rentvine never writes {sorted(missing)}"
        assert not invented, f"{table}: Rentvine writes {sorted(invented)}, Buildium does not"


def test_a_rentvine_only_column_is_read_with_a_fallback() -> None:
    """The other side of :data:`RENTVINE_ONLY`: a Buildium row leaves
    ``pms_updated_at`` NULL, so a reader that compared it bare would never count
    a Buildium work order as stalled. Both stall counts must fall back to the
    mirror's own clock, and neither may still read that clock alone."""
    from helixis.db.repositories import first_scan, morning_brief

    for name, sql in (
        ("first_scan", first_scan._SCAN_SQL),
        ("morning_brief", morning_brief._BRIEF_SQL),
    ):
        flat = " ".join(sql.split())
        assert "COALESCE(pms_updated_at, updated_at) < now()" in flat, name
        assert "AND updated_at < now()" not in flat, name
    assert set().union(*RENTVINE_ONLY.values()) == {"pms_updated_at"}, (
        "a new Rentvine-only column needs its own fallback check here"
    )


def test_every_source_has_a_mapper_and_a_column_list() -> None:
    """Adding an entity means adding an entry, not editing a loop — so the three
    registries must agree."""
    # Leases, units and bills are read outside the SOURCES loop (bills: one
    # payables row per bill LINE, grouped by mapper.group_payables).
    tables = {t for t, _ in sync.SOURCES} | {
        "helixis_leases",
        "helixis_units",
        "helixis_bills",
        "helixis_applicants",
    }
    assert tables == set(mapper.MAPPERS), tables ^ set(mapper.MAPPERS)
    # Balances have a column list (the drift guard holds it to Buildium's
    # INSERT) but no MAPPERS entry: they need the company's date for aging.
    assert tables | {"helixis_lease_balances"} == set(sync.TABLE_COLUMNS), tables ^ set(
        sync.TABLE_COLUMNS
    )


def test_every_source_path_is_a_documented_operation() -> None:
    """A path the spec does not document is gotcha 79's shape — one wrong
    segment, invisible because every mock answers whatever it is handed."""
    for _table, path in sync.SOURCES:
        transport.validate_query("GET", path, {})
    transport.validate_query("GET", sync.LEASES_EXPORT_PATH, {"page": 1, "pageSize": 25})
    transport.validate_query("GET", sync.LEASE_PATH, {"includes": "tenants"})
    transport.validate_query("GET", sync.UNITS_PATH, {})


def test_paged_sources_really_do_declare_pagination() -> None:
    """The bug this catches is real: `GET /leases` declares ZERO parameters, so
    paging it raises. Anything in SOURCES goes through `iter_all`."""
    from helixis.rentvine._query_index import QUERY_PARAMS

    for _table, path in sync.SOURCES:
        declared = QUERY_PARAMS[transport.operation_key("GET", path)]
        assert {"page", "pageSize"} <= set(declared), f"{path} cannot be paged"


def test_leases_come_from_the_export_because_list_leases_cannot_be_paged() -> None:
    """Re-pointed 2026-10-01. ``GET /leases`` still declares no paging — the
    reason the sync no longer reads it — and the export it reads instead does,
    with ``getLease`` declaring the ``includes`` the rent read sends."""
    from helixis.rentvine._query_index import QUERY_PARAMS

    assert "page" not in QUERY_PARAMS[transport.operation_key("GET", "/leases")]
    export = QUERY_PARAMS[transport.operation_key("GET", sync.LEASES_EXPORT_PATH)]
    assert {"page", "pageSize"} <= set(export)
    assert "includes" in QUERY_PARAMS[transport.operation_key("GET", sync.LEASE_PATH)]
    assert "/leases" not in {sync.LEASES_EXPORT_PATH, sync.LEASE_PATH}


def test_units_come_from_the_route_that_carries_the_property_link() -> None:
    """`unit_export` has no propertyID at all — a unit with no property breaks
    the very relationship columns ADR 0010 Decision (a) protects."""
    assert "{propertyID}" in sync.UNITS_PATH
    assert "export" not in sync.UNITS_PATH


def test_units_cannot_be_paged_either() -> None:
    """The reason `_sync_units` reads unpaged, pinned so a future edit that
    "tidies" it back to `iter_all` fails here rather than at runtime."""
    from helixis.rentvine._query_index import QUERY_PARAMS

    declared = QUERY_PARAMS[transport.operation_key("GET", sync.UNITS_PATH)]
    assert "page" not in declared


@pytest.mark.asyncio
async def test_the_two_readers_are_mutually_exclusive_by_construction() -> None:
    """Each refuses exactly the endpoints the other exists for.

    The dangerous direction is the unpaged reader pointed at a pageable
    collection: it returns the server's own default slice with a 200 and nothing
    says so, which reports part of a customer's data as all of it (gotchas
    26/55). The other direction fails loudly already — an undeclared `page` is
    refused before a request is built — and that is how the units bug surfaced.
    """
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(str(request.url))
        return httpx.Response(200, json=[], headers={"pagination-total-pages": "1"})

    c = _client(handler)
    # unpaged reader, pageable collection → refused, and never sent.
    with pytest.raises(rvclient.RentvineAPIError, match="use iter_all"):
        await c.get_rows("/properties")
    assert sent == []
    # paged reader, unpageable collection → refused before anything is built.
    # (`/leases` is the measured unpageable collection; the sync no longer
    # reads it, but the split it illustrates is the client's, not the sync's.)
    with pytest.raises(transport.UnknownQueryParameterError):
        await c.iter_all("/leases")
    assert sent == []
    # and each is accepted on its own side of the split.
    await c.get_rows("/leases")
    await c.iter_all("/properties")
    assert len(sent) == 2
    await c.aclose()


@pytest.mark.asyncio
async def test_the_split_covers_every_path_the_sync_actually_calls() -> None:
    """A per-call test cannot see a SIXTH source added on the wrong side."""
    c = _client(lambda r: httpx.Response(200, json=[], headers={"pagination-total-pages": "1"}))
    for _table, path in sync.SOURCES:
        with pytest.raises(rvclient.RentvineAPIError, match="use iter_all"):
            await c.get_rows(path)
    # The one unpaged collection the sync still reads is per-property units.
    with pytest.raises(transport.UnknownQueryParameterError):
        await c.iter_all(sync.UNITS_PATH, path_params={"propertyID": 1})
    # ...and the lease export is on the PAGED side: the unpaged reader refuses it.
    with pytest.raises(rvclient.RentvineAPIError, match="use iter_all"):
        await c.get_rows(sync.LEASES_EXPORT_PATH)
    await c.aclose()


@pytest.mark.asyncio
async def test_only_the_paged_collections_are_claimed_complete() -> None:
    """An unpaged read has no total to reconcile against, so the sync must not
    say the customer's units are all of them — the fabricated-total shape.

    Re-pointed 2026-10-01: leases come from the paged export now, so they ARE
    claimed when the server named the end — and so are their links, which ride
    on the same rows. Units still cannot be."""
    app = Settings()
    app = app.model_copy(update={"rentvine": app.rentvine.model_copy(update={"enabled": True})})

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/properties?" in url:
            return httpx.Response(
                200,
                json=wire("GET /properties", PROPERTY),
                headers={"pagination-total-pages": "1", "pagination-total-items": "1"},
            )
        if url.endswith(f"/properties/{PROPERTY['propertyID']}/units"):
            return httpx.Response(200, json=wire("GET /properties/{propertyID}/units", UNIT))
        answered = lease_route(request, export_leases(1))
        if answered is not None:
            return answered
        return httpx.Response(200, json=[], headers={"pagination-total-pages": "1"})

    result = await sync.sync_company(_fake_pool(), "c", settings=app, client=_client(handler))
    assert result.ok, result.failed
    # Both really did sync — this is not passing by not running.
    assert result.rows_by_table["helixis_leases"] == 1
    assert result.rows_by_table["helixis_lease_tenants"] == 1
    assert result.rows_by_table["helixis_units"] == 1
    # Re-pointed 2026-10-01: bills are a PAGED read (payables search) and this
    # handler names the end of every collection, so they are claimed too.
    assert result.confirmed_complete == {t for t, _ in sync.SOURCES} | {
        "helixis_leases",
        "helixis_lease_tenants",
        "helixis_bills",
        # Balances ride on the same complete export (sync._replace_balances).
        "helixis_lease_balances",
        # Applications are paged too (sync._sync_applicants).
        "helixis_applicants",
        # Bill lines are claimed with the bills they belong to.
        "helixis_bill_lines",
        # Every lease's history read, on the same complete export.
        "helixis_lease_transactions",
    }
    assert "helixis_units" not in result.confirmed_complete


@pytest.mark.asyncio
async def test_a_guessed_stop_syncs_its_rows_and_is_not_claimed_complete() -> None:
    """⚠ THE DISTINGUISHING INPUT THE TEST ABOVE CANNOT SUPPLY.

    That one's handler sends ``pagination-total-pages`` for every source, so it
    exercises only the leases/units exclusion and never the question
    ``confirmed_complete``'s own docstring asks — *confirmed a total for,
    versus guessed the end from a short page*. Before this, ``sync.py`` added
    the table on any fetch that did not raise, so the answer was "confirmed"
    either way and ``Page.stop_condition_was_documented`` had no production
    reader at all.

    Here ``/vendors`` answers with NO pagination headers — the state the module
    flags ``@unverified`` at ``MAX_PAGES`` (the spec declares the header on all
    five of these paths; nobody has watched one arrive). Both halves are
    asserted, because a fix that simply stopped claiming anything would pass a
    one-sided check (gotchas 38/51): the vendor row must still be MIRRORED, and
    only the completeness CLAIM withheld.
    """
    app = Settings()
    app = app.model_copy(update={"rentvine": app.rentvine.model_copy(update={"enabled": True})})

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/vendors?" in url:
            # A short page and not one header — the loop can only guess.
            return httpx.Response(200, json=wire("GET /vendors", CONTACT))
        if "/properties?" in url:
            return httpx.Response(
                200,
                json=wire("GET /properties", PROPERTY),
                headers={"pagination-total-pages": "1", "pagination-total-items": "1"},
            )
        if url.endswith(f"/properties/{PROPERTY['propertyID']}/units"):
            return httpx.Response(200, json=wire("GET /properties/{propertyID}/units", UNIT))
        if url.endswith("/leases"):
            return httpx.Response(200, json=wire("GET /leases", LEASE))
        return httpx.Response(200, json=[], headers={"pagination-total-pages": "1"})

    result = await sync.sync_company(_fake_pool(), "c", settings=app, client=_client(handler))
    assert result.ok, result.failed
    # A guessed stop is NOT a failure: the rows are real and they are mirrored.
    assert result.rows_by_table["helixis_vendors"] == 1
    assert "helixis_vendors" not in result.failed
    # ...and it is NOT a confirmed total.
    assert "helixis_vendors" not in result.confirmed_complete
    # The control: every other paged source still earns its claim, so this is
    # measuring the header and not some blanket withdrawal.
    assert "helixis_properties" in result.confirmed_complete


# ── the sync loop ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_sync_refuses_when_the_deployment_flag_is_off() -> None:
    app = Settings()
    assert app.rentvine.enabled is False
    with pytest.raises(sync.RentvineSyncDisabled, match="deployment"):
        await sync.sync_company(MagicMock(), "c", settings=app)


@pytest.mark.asyncio
async def test_a_company_with_no_credentials_is_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not a Rentvine customer is a state, not a failure."""
    app = Settings()
    app = app.model_copy(update={"rentvine": app.rentvine.model_copy(update={"enabled": True})})
    monkeypatch.setattr(sync, "load_rentvine_settings_for_company", AsyncMock(return_value=None))
    with pytest.raises(sync.RentvineSyncDisabled, match="no Rentvine connection"):
        await sync.sync_company(MagicMock(), "c", settings=app)


@pytest.mark.asyncio
async def test_one_failing_collection_does_not_abandon_the_others() -> None:
    """And the failure is RECORDED, so a stale table is never read as empty."""
    app = Settings()
    app = app.model_copy(update={"rentvine": app.rentvine.model_copy(update={"enabled": True})})

    def handler(request: httpx.Request) -> httpx.Response:
        if "/vendors" in str(request.url):
            return httpx.Response(403, text="role cannot read vendors")
        return httpx.Response(200, json=[], headers={"pagination-total-pages": "1"})

    pool = _fake_pool()
    result = await sync.sync_company(pool, "c", settings=app, client=_client(handler))
    assert "helixis_vendors" in result.failed
    assert result.ok is False
    # ⚠ Measured against TABLE_COLUMNS, not SOURCES. SOURCES is only the PAGED
    # third of the sync — leases and units are fetched separately because their
    # endpoints declare no pagination — so asserting against it would have said
    # nothing about whether those two stages ran, which is exactly the half a
    # 403 on one collection could plausibly take down with it. TABLE_COLUMNS
    # names every table the sync writes, so a stage added later and left unwired
    # fails here rather than passing by omission (gotcha 28).
    # Re-pointed 2026-10-01 to SYNC_STATE_STAGE: the lease-tenant links are
    # written too and have no TABLE_COLUMNS entry (their writer is their own).
    # Re-pointed 2026-10-02: turnovers are written too (sync.DERIVED_TABLES).
    assert set(result.rows_by_table) == (set(sync.SYNC_STATE_STAGE) | sync.DERIVED_TABLES) - {
        "helixis_vendors"
    }


@pytest.mark.asyncio
async def test_units_are_fetched_only_for_properties_that_reached_the_mirror() -> None:
    """A property whose id did not coerce is SKIPPED from the mirror, so its
    units must not be fetched either — a unit row whose `property_id` points at
    a property the mirror does not have is the broken relationship ADR 0010
    Decision (a) exists to prevent, arriving from the side nobody watches.

    ⚠ **The third property carries id `0`, and it is the ONLY input that
    separates this from the obvious alternative.** Re-deriving the list as
    ``[pid for pid in (_int(r["propertyID"]) for r in rows) if pid]`` agrees with
    the written ids on every other input — `_int` returns None exactly where
    `_write_rows` skips — so a mutation swapping the two survives without it
    (gotcha 58: check whether the mutant can change any output before writing a
    test for it). `0` is falsy and an int, so the truthiness filter drops a
    property the mirror DID store, leaving it permanently unitless.
    """
    app = Settings()
    app = app.model_copy(update={"rentvine": app.rentvine.model_copy(update={"enabled": True})})
    unusable = {**PROPERTY, "propertyID": "not-a-number"}
    zero = {**PROPERTY, "propertyID": 0}
    unit_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/properties?" in url:
            return httpx.Response(
                200,
                json=wire("GET /properties", PROPERTY, unusable, zero),
                headers={"pagination-total-pages": "1", "pagination-total-items": "3"},
            )
        if "/units" in url:
            unit_calls.append(url)
            return httpx.Response(200, json=wire("GET /properties/{propertyID}/units", UNIT))
        return httpx.Response(200, json=[], headers={"pagination-total-pages": "1"})

    result = await sync.sync_company(_fake_pool(), "c", settings=app, client=_client(handler))
    assert result.skipped_no_id["helixis_properties"] == 1
    assert result.rows_by_table["helixis_properties"] == 2
    fetched = [u.rsplit("/properties/", 1)[1].removesuffix("/units") for u in unit_calls]
    assert fetched == [str(PROPERTY["propertyID"]), "0"], unit_calls


@pytest.mark.asyncio
async def test_the_skip_warning_names_the_rows_keys_rather_than_a_guessed_cause(
    caplog, monkeypatch
) -> None:
    """The first live run's diagnosis, and the wording that sent it wrong.

    ⚠ This warning used to end "ADR 0010 Decision (a)'s numeric-id assumption
    needs re-examining, not working around" — a cause, asserted, on the one
    line an operator reads when a table comes back empty. The first Phase 8
    sync (2026-09-23) skipped every unit and every owner for a completely
    different reason (the row was a WRAPPER the envelope map called a record)
    and the ids involved coerced perfectly. Somebody following the sentence
    would have gone after the data model.

    The row's own KEYS separate the two without guessing, so they are printed.
    """
    app = Settings()
    app = app.model_copy(update={"rentvine": app.rentvine.model_copy(update={"enabled": True})})
    # ⚠ THE REAL DEFECT, not a stand-in for it: the map says this collection is
    # bare while the server wraps each row, so the wrapper reaches the mapper
    # intact. `wire()` is deliberately not used — it derives from the same map,
    # so it would emit a flat row and agree with the mistake (gotcha 123).
    monkeypatch.setitem(rvclient.ROW_ENVELOPE, "GET /properties", None)
    wrapper_row = {"property": dict(PROPERTY), "token": "abc"}

    def handler(request: httpx.Request) -> httpx.Response:
        if "/properties?" in str(request.url):
            return httpx.Response(
                200,
                json=[wrapper_row],
                headers={"pagination-total-pages": "1", "pagination-total-items": "1"},
            )
        return httpx.Response(200, json=[], headers={"pagination-total-pages": "1"})

    with caplog.at_level(logging.WARNING, logger="helixis.rentvine.sync"):
        result = await sync.sync_company(_fake_pool(), "c", settings=app, client=_client(handler))

    assert result.skipped_no_id["helixis_properties"] == 1
    assert result.rows_by_table["helixis_properties"] == 0
    skip_lines = [r for r in caplog.records if "no usable integer id" in r.getMessage()]
    assert len(skip_lines) == 1, [r.getMessage() for r in caplog.records]
    message = skip_lines[0].getMessage()
    # The keys are the whole diagnosis: a reader seeing `property` and `token`
    # knows to look at the envelope map, not at ADR 0010.
    assert "'property'" in message and "'token'" in message, message
    assert "_row_envelopes" in message, message
    # ⚠ EVERY row skipped is structurally different from some, because it is
    # the state that reads as a successfully-synced empty table.
    assert skip_lines[0].levelno == logging.ERROR, message
    assert "EVERY ROW" in message, message


@pytest.mark.asyncio
async def test_a_partial_skip_is_a_warning_and_does_not_claim_the_table_is_empty(
    caplog,
) -> None:
    """The other direction (gotchas 38/51).

    A run that escalated every skip to ERROR and said "EVERY ROW" would be
    wrong about the ordinary case — one bad row among good ones — and a level
    that is always ERROR carries no information at all.
    """
    app = Settings()
    app = app.model_copy(update={"rentvine": app.rentvine.model_copy(update={"enabled": True})})
    unusable = {**PROPERTY, "propertyID": "not-a-number"}

    def handler(request: httpx.Request) -> httpx.Response:
        if "/properties?" in str(request.url):
            return httpx.Response(
                200,
                json=wire("GET /properties", PROPERTY, unusable),
                headers={"pagination-total-pages": "1", "pagination-total-items": "2"},
            )
        return httpx.Response(200, json=[], headers={"pagination-total-pages": "1"})

    with caplog.at_level(logging.WARNING, logger="helixis.rentvine.sync"):
        await sync.sync_company(_fake_pool(), "c", settings=app, client=_client(handler))

    skip_lines = [r for r in caplog.records if "no usable integer id" in r.getMessage()]
    assert len(skip_lines) == 1
    assert skip_lines[0].levelno == logging.WARNING
    message = skip_lines[0].getMessage()
    assert "EVERY ROW" not in message, message
    # The skipped row's own field names — so the reader can see this one is the
    # id itself refusing to coerce, which IS the ADR 0010 case.
    assert "'propertyID'" in message, message


@pytest.mark.asyncio
async def test_units_are_not_reported_empty_when_the_property_fetch_failed() -> None:
    """gotcha 104's derived-stage trap, exactly.

    Units are fetched per property, so a failed PROPERTY fetch leaves the id
    list empty — and a stage that never ran then writes zero rows and looks
    identical to a company with no units. The distinction has to be recorded,
    because "you have no units" is a claim and "we never asked" is not.
    """
    app = Settings()
    app = app.model_copy(update={"rentvine": app.rentvine.model_copy(update={"enabled": True})})

    tried: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "/properties?" in str(request.url):
            tried.append(str(request.url))
            return httpx.Response(500, text="rentvine fell over")
        return httpx.Response(200, json=[], headers={"pagination-total-pages": "1"})

    result = await sync.sync_company(_fake_pool(), "c", settings=app, client=_client(handler))
    assert "helixis_properties" in result.failed
    assert "helixis_units" in result.failed, "a stage that never ran must not read as empty"
    assert "helixis_units" not in result.confirmed_complete
    assert result.ok is False
    # ⚠ And the sync gave the blip its chance first. This is the end-to-end
    # half: a 5xx has to cost attempts INSIDE the sync loop, not only in a
    # client exercised on its own, or the retry is a well-tested helper the
    # production caller never reaches (gotchas 62/66/69).
    assert len(tried) == len(rvclient.RETRY_BACKOFF_S) + 1, tried


@pytest.mark.asyncio
async def test_one_propertys_units_failing_is_recorded_not_swallowed() -> None:
    """A partial units table must never report as a complete one."""
    app = Settings()
    app = app.model_copy(update={"rentvine": app.rentvine.model_copy(update={"enabled": True})})
    second = {**PROPERTY, "propertyID": 4611}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/properties?" in url:
            return httpx.Response(
                200,
                json=wire("GET /properties", PROPERTY, second),
                headers={"pagination-total-pages": "1", "pagination-total-items": "2"},
            )
        if url.endswith(f"/properties/{second['propertyID']}/units"):
            return httpx.Response(403, text="role cannot read this property")
        if url.endswith(f"/properties/{PROPERTY['propertyID']}/units"):
            return httpx.Response(200, json=wire("GET /properties/{propertyID}/units", UNIT))
        return httpx.Response(200, json=[], headers={"pagination-total-pages": "1"})

    result = await sync.sync_company(_fake_pool(), "c", settings=app, client=_client(handler))
    assert "helixis_units" in result.failed
    assert str(second["propertyID"]) in result.failed["helixis_units"]
    assert "helixis_units" not in result.confirmed_complete
    assert result.ok is False


@pytest.mark.asyncio
async def test_a_failure_at_the_front_does_not_starve_the_properties_behind_it() -> None:
    """The DISTINGUISHING input: the failing property comes FIRST.

    ⚠ The sibling test above passes against both implementations, because its
    failing property is the LAST one — there is nothing behind it to abandon. So
    it pinned the property it names ("a partial table must not read as complete")
    and was blind to the one that cost 26 properties their units every night.
    The order is the whole test (gotchas 38/51: the distinguishing input is the
    one the two implementations disagree on).
    """
    app = Settings()
    app = app.model_copy(update={"rentvine": app.rentvine.model_copy(update={"enabled": True})})
    broken = {**PROPERTY, "propertyID": 4611}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/properties?" in url:
            return httpx.Response(
                200,
                # broken FIRST — the old loop returned here and never reached the
                # property whose units it could have read.
                json=wire("GET /properties", broken, PROPERTY),
                headers={"pagination-total-pages": "1", "pagination-total-items": "2"},
            )
        if url.endswith(f"/properties/{broken['propertyID']}/units"):
            return httpx.Response(403, text="role cannot read this property")
        if url.endswith(f"/properties/{PROPERTY['propertyID']}/units"):
            return httpx.Response(200, json=wire("GET /properties/{propertyID}/units", UNIT))
        return httpx.Response(200, json=[], headers={"pagination-total-pages": "1"})

    result = await sync.sync_company(_fake_pool(), "c", settings=app, client=_client(handler))
    # The reachable property's units ARE mirrored…
    assert result.rows_by_table["helixis_units"] == 1
    # …and the table is still reported STALE, naming the property to look at.
    assert str(broken["propertyID"]) in result.failed["helixis_units"]
    # The vendor's own status survives: "403" and "404" need opposite operator
    # actions (gotchas 102/153), and a summary that dropped the reason would
    # leave the app log as the only place carrying it (gotcha 157).
    assert "403" in result.failed["helixis_units"]
    assert result.ok is False


@pytest.mark.asyncio
async def test_the_walk_stops_when_nothing_has_answered_and_says_what_it_skipped() -> None:
    """Evidence, not tolerance: with no property answering, the route is the
    suspect — and the message must separate "failed" from "never tried"."""
    app = Settings()
    app = app.model_copy(update={"rentvine": app.rentvine.model_copy(update={"enabled": True})})
    props = [{**PROPERTY, "propertyID": 4610 + i} for i in range(8)]
    asked: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/properties?" in url:
            return httpx.Response(
                200,
                json=wire("GET /properties", *props),
                headers={"pagination-total-pages": "1", "pagination-total-items": str(len(props))},
            )
        match = re.search(r"/properties/(\d+)/units$", url)
        if match:
            asked.append(int(match.group(1)))
            return httpx.Response(404, text="no such route")
        return httpx.Response(200, json=[], headers={"pagination-total-pages": "1"})

    result = await sync.sync_company(_fake_pool(), "c", settings=app, client=_client(handler))
    assert len(asked) == sync.UNIT_FANOUT_EVIDENCE, asked
    detail = result.failed["helixis_units"]
    assert "never tried" in detail
    assert str(len(props) - sync.UNIT_FANOUT_EVIDENCE) in detail
    # Zero is REPORTED rather than omitted — an absent key reads as "no units
    # exist" to the reconciler, which is the opposite of what happened.
    assert result.rows_by_table["helixis_units"] == 0


@pytest.mark.asyncio
async def test_once_one_property_answers_the_walk_finishes_however_many_fail() -> None:
    """The evidence rule can only fire BEFORE anything has answered."""
    app = Settings()
    app = app.model_copy(update={"rentvine": app.rentvine.model_copy(update={"enabled": True})})
    props = [{**PROPERTY, "propertyID": 4610 + i} for i in range(8)]
    asked: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/properties?" in url:
            return httpx.Response(
                200,
                json=wire("GET /properties", *props),
                headers={"pagination-total-pages": "1", "pagination-total-items": str(len(props))},
            )
        match = re.search(r"/properties/(\d+)/units$", url)
        if match:
            pid = int(match.group(1))
            asked.append(pid)
            if pid == props[0]["propertyID"]:
                return httpx.Response(200, json=wire("GET /properties/{propertyID}/units", UNIT))
            return httpx.Response(404, text="no such property")
        return httpx.Response(200, json=[], headers={"pagination-total-pages": "1"})

    result = await sync.sync_company(_fake_pool(), "c", settings=app, client=_client(handler))
    assert len(asked) == len(props), "one answer means every later failure is that property's own"
    detail = result.failed["helixis_units"]
    assert "never tried" not in detail
    for prop in props[1:]:
        assert str(prop["propertyID"]) in detail, "every failing property must be named"


@pytest.mark.asyncio
async def test_a_row_with_no_usable_id_is_skipped_and_counted_never_written() -> None:
    app = Settings()
    app = app.model_copy(update={"rentvine": app.rentvine.model_copy(update={"enabled": True})})

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/properties?page=1&pageSize=100") or "/properties?" in str(
            request.url
        ):
            return httpx.Response(
                200,
                json=wire("GET /properties", PROPERTY, {**PROPERTY, "propertyID": "nope"}),
                headers={"pagination-total-pages": "1", "pagination-total-items": "2"},
            )
        return httpx.Response(200, json=[], headers={"pagination-total-pages": "1"})

    pool = _fake_pool()
    result = await sync.sync_company(pool, "c", settings=app, client=_client(handler))
    assert result.rows_by_table["helixis_properties"] == 1
    assert result.skipped_no_id["helixis_properties"] == 1


@pytest.mark.asyncio
async def test_the_upsert_uses_the_buildium_conflict_target_unchanged() -> None:
    """ADR 0010 Decision (a): no per-PMS conflict target is needed.

    ⚠ **RE-POINTED TO THE MIRROR UPSERTS, never relaxed to "at least one".**
    This walked EVERY statement the sync executed, which was a true reading of
    "the sync writes mirror rows and nothing else" and stopped being one the
    day it also stamped ``helixis_mirror_sync_state`` — a row that is
    deliberately keyed ``(company_id, table_name)`` and carries no
    ``buildium_id`` at all. Weakening it to ``any(...)`` would let a second
    mirror table land on the wrong conflict target in silence, which is the
    identity collision the ADR's own reasoning is built on.
    """
    app = Settings()
    app = app.model_copy(update={"rentvine": app.rentvine.model_copy(update={"enabled": True})})
    pool = _fake_pool()

    def handler(request: httpx.Request) -> httpx.Response:
        if "/properties?" in str(request.url):
            return httpx.Response(
                200,
                json=wire("GET /properties", PROPERTY),
                headers={"pagination-total-pages": "1", "pagination-total-items": "1"},
            )
        answered = lease_route(request, export_leases(1))
        if answered is not None:
            return answered
        return httpx.Response(200, json=[], headers={"pagination-total-pages": "1"})

    await sync.sync_company(pool, "c", settings=app, client=_client(handler))
    statements = [c.args[0] for c in pool._conn.execute.await_args_list]
    mirror = [s for s in statements if "INSERT INTO" in s and "helixis_mirror_sync_state" not in s]
    # The bills prune after a complete read must stay inside this company's
    # Rentvine rows (rule 11).
    for sql in (s for s in statements if s.startswith("DELETE FROM helixis_bills")):
        assert sql.startswith("DELETE FROM helixis_bills WHERE company_id = $1::uuid")
        assert "pms = 'rentvine'" in sql
    assert mirror, "no mirror row was written"
    links = [s for s in mirror if "helixis_lease_tenants" in s]
    assert links, "no lease-tenant link was written"
    for sql in mirror:
        if sql in links:
            # Its own natural key — the target Buildium's writer uses
            # (db/repositories/entities.py), and the table's primary key.
            assert "ON CONFLICT (company_id, lease_buildium_id, tenant_buildium_id)" in sql
        else:
            assert "ON CONFLICT (company_id, buildium_id)" in sql
        assert "pms = EXCLUDED.pms" in sql
    # Every delete is scoped to this company AND this PMS (rule 11; gotcha 112).
    for sql in (s for s in statements if "DELETE" in s):
        assert "company_id = $1::uuid" in sql and "pms = 'rentvine'" in sql


def _fake_pool() -> Any:
    """A pool that refuses what asyncpg refuses.

    ⚠ **THIS WAS A BARE ``AsyncMock`` AND IT HELD A REAL DEFECT IN PLACE.**
    ``map_lease`` put ``'2026-03-18'`` — a ``str`` — into
    ``helixis_leases.lease_from_date``, a ``timestamptz``, and ``_upsert`` binds
    it with no cast, so Postgres types the parameter from the target column and
    asyncpg's ``timestamptz_encode`` refuses it outright. ``_sync_leases``
    catches only ``RentvineAPIError``, so the ``DataError`` propagated out of
    ``sync_company`` and ``_sync_units`` — which runs on the next line — never
    executed. Measured with a control: properties (same code path, no temporal
    column) wrote fine; the sync raised; the units endpoint was never reached.

    Every test in this file was green throughout, because the fake accepted
    what production rejects. Gotcha 123's inverted-fake shape, second instance.
    """
    conn = MagicMock()

    def _enforce(sql: str, *args: Any) -> str:
        reject_unbindable(sql, args)
        reject_untyped_mirror_bind(sql, args)
        return "INSERT 0 1"

    # ⚠ A `side_effect` rather than a plain async function: tests here read
    # `conn.execute.await_args_list` to check the SQL, and a bare coroutine
    # carries none of that.
    conn.execute = AsyncMock(side_effect=_enforce)
    # The lease pass reads the stored rent markers first, and the
    # owner-to-property join reads the stored owners; an empty mirror by
    # default. A test that needs stored rows sets `pool._conn.fetch`.
    conn.fetch = AsyncMock(return_value=[])
    # The prune and the owner join run inside `conn.transaction()`, as
    # asyncpg's does.
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=tx)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx)
    pool = MagicMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=ctx)
    pool._conn = conn
    return pool


# ── the @unverified contract ─────────────────────────────────────────────────


def test_every_unverifiable_assumption_is_tagged() -> None:
    """Phase 8's work list is `grep -rn '@unverified' src/helixis/rentvine/`.

    An untagged guess is the failure Phase 4 exists to avoid, so this asserts
    the tags EXIST rather than counting them — a count would just get bumped.
    """
    pkg = REPO / "src/helixis/rentvine"
    tagged = {f.name for f in pkg.glob("*.py") if "@unverified" in f.read_text()}
    assert {"client.py", "mapper.py", "sync.py"} <= tagged, tagged
