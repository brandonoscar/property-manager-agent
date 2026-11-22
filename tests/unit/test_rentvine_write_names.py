"""The Rentvine writes take the person's words, never an id the model can't see.

No read tool prints a Rentvine property, unit, lease or vendor id: they print
names, and an id only where a record has none. The four writes asked for those
ids, so the model had to invent one, and the upload's card then showed a bare
number nobody could check. ``pms.places`` turns the words into exactly one id
from the company's mirror, or into the question to ask.

Pinned here:
- the matcher's rules (exact before fragment, a unit matched whole, ``#id``
  as the way back from a question, a lease never matched by a place fragment);
- every query scoped to the company and the system (rule 11);
- the gate order: the dark flag, the connection and the role floor answer
  before the mirror is read;
- each write end to end by name, through its real core and its card;
- the schema the model sees asks for no id (the status update's work-order id
  is printed by ``open_work_orders``, and stays);
- each tool hands its words straight to the core the eval harness mocks, so a
  golden case bypasses the lookup as it bypasses the gates (rule 15).

All records are synthetic.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agents.tool_context import ToolContext

from helixis.agents import rentvine_tools as rt
from helixis.evals import agent_flow
from helixis.pms import places

from .test_rentvine_creates import (
    LEASE,
    PROPERTY,
    TXN,
    UNIT,
    VENDOR,
    _Client,
    _created,
    _doc,
    _drive,
    _txn_back,
)

COMPANY = "company-1"
OTHER_PROPERTY, OTHER_UNIT, OLD_LEASE, OTHER_LEASE = 990000102, 990000302, 990000702, 990000703


def _lease_row(
    ident: int, tenants: list[str], prop: str, unit: str | None, status: str
) -> dict[str, Any]:
    ends = date(2026, 8, 31) if status == "Active" else date(2025, 8, 31)
    return {
        "buildium_id": ident,
        "status": status,
        "lease_to_date": ends,
        "property_name": prop,
        "unit_number": unit,
        "tenant_names": tenants,
    }


class _Conn:
    """Serves each query from fixed rows and records what it was asked."""

    def __init__(self) -> None:
        self.properties = [
            {"buildium_id": PROPERTY, "name": "Redbud Court"},
            {"buildium_id": OTHER_PROPERTY, "name": "Redbud Court East"},
            {"buildium_id": 990000103, "name": "Maple House"},
        ]
        self.units = {
            PROPERTY: [
                {"buildium_id": UNIT, "unit_number": "4B"},
                {"buildium_id": OTHER_UNIT, "unit_number": "1"},
                {"buildium_id": 990000303, "unit_number": "11"},
                {"buildium_id": 12, "unit_number": "3A"},
                {"buildium_id": 990000305, "unit_number": "12"},
            ],
            OTHER_PROPERTY: [],
        }
        self.vendors = [
            {"buildium_id": VENDOR, "name": "Ace Plumbing"},
            {"buildium_id": 990000502, "name": "Ace Electric"},
        ]
        self.leases = [
            _lease_row(LEASE, ["Ana Ortiz"], "Redbud Court", "4B", "Active"),
            _lease_row(OLD_LEASE, ["Ana Ortiz"], "Redbud Court", "4B", "Closed"),
            _lease_row(OTHER_LEASE, ["Ben Dana"], "Maple House", None, "Active"),
        ]
        self.work_orders = {990000901}
        self.asked: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.asked.append((sql, args))
        if sql == places.PROPERTIES_SQL:
            return self.properties
        if sql == places.UNITS_SQL:
            return self.units.get(args[2], [])
        if sql == places.VENDORS_SQL:
            return self.vendors
        if sql == places.LEASES_SQL:
            return self.leases
        if sql == places.WORK_ORDER_SQL:
            return [{"buildium_id": args[2]}] if args[2] in self.work_orders else []
        raise AssertionError(sql)


_WHERE = {"company_id": COMPANY, "pms": "rentvine", "system": "Rentvine", "nothing": "NOTHING."}


# ── the matcher ─────────────────────────────────────────────────────────────


def test_every_query_is_scoped_to_the_company_and_the_system() -> None:
    for sql in (
        places.PROPERTIES_SQL,
        places.UNITS_SQL,
        places.VENDORS_SQL,
        places.LEASES_SQL,
        places.WORK_ORDER_SQL,
    ):
        assert "company_id = $1::uuid" in sql and "pms = $2" in sql, sql
    # The lease's joins too: a tenant or a place another system left behind
    # must never name one of this system's leases.
    for join in ("p.pms = l.pms", "u.pms = l.pms", "lt.pms = l.pms", "t.pms = lt.pms"):
        assert join in places.LEASES_SQL, join


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "asked,expected",
    [
        ("redbud  COURT", PROPERTY),  # exact beats "Redbud Court East" containing it
        ("Maple", 990000103),  # a fragment, when nothing is exact
        (f"#{OTHER_PROPERTY}", OTHER_PROPERTY),  # the way back from a question
    ],
)
async def test_a_property_is_found_by_name_fragment_or_number(asked: str, expected: int) -> None:
    conn = _Conn()
    found = await places.property_named(conn, **_WHERE, asked=asked)
    assert isinstance(found, places.Match) and found.id == expected
    assert all(args[:2] == (COMPANY, "rentvine") for _sql, args in conn.asked)


@pytest.mark.asyncio
async def test_several_properties_are_listed_with_their_numbers_and_none_is_picked() -> None:
    found = await places.property_named(_Conn(), **_WHERE, asked="Redbud")
    assert isinstance(found, str)
    assert (
        f"Redbud Court (#{PROPERTY})" in found and f"Redbud Court East (#{OTHER_PROPERTY})" in found
    )
    assert found.endswith("Which one? NOTHING.")


@pytest.mark.asyncio
async def test_no_match_names_what_is_synced_and_an_empty_mirror_says_how_to_fill_it() -> None:
    conn = _Conn()
    found = await places.property_named(conn, **_WHERE, asked="Oak Hill")
    assert isinstance(found, str) and "Maple House" in found and found.endswith("NOTHING.")
    conn.properties = []
    empty = await places.property_named(conn, **_WHERE, asked="Oak Hill")
    assert isinstance(empty, str) and "Run Sync now" in empty and "NOTHING." in empty


@pytest.mark.asyncio
@pytest.mark.parametrize("asked", ["4B", "unit 4b", "#4B", "Apt. 4B", "Unit #4B"])
async def test_a_unit_is_found_however_a_person_writes_its_number(asked: str) -> None:
    place = places.Match(PROPERTY, "Redbud Court")
    found = await places.unit_named(_Conn(), **_WHERE, place=place, asked=asked)
    assert found == places.Match(UNIT, "Unit 4B at Redbud Court")


@pytest.mark.asyncio
async def test_a_unit_is_matched_whole_and_its_number_before_any_id() -> None:
    place = places.Match(PROPERTY, "Redbud Court")
    one = await places.unit_named(_Conn(), **_WHERE, place=place, asked="1")
    assert isinstance(one, places.Match) and one.id == OTHER_UNIT  # never "11"
    # "#12" is how people write unit 12, though a unit with id 12 is there too.
    twelve = await places.unit_named(_Conn(), **_WHERE, place=place, asked="#12")
    assert isinstance(twelve, places.Match) and twelve.id == 990000305
    # An id the questions print still works when no unit has that number.
    by_id = await places.unit_named(_Conn(), **_WHERE, place=place, asked=f"#{UNIT}")
    assert isinstance(by_id, places.Match) and by_id.id == UNIT
    missing = await places.unit_named(_Conn(), **_WHERE, place=place, asked="9")
    assert isinstance(missing, str) and '"9"' in missing and "4B" in missing


@pytest.mark.asyncio
async def test_a_lease_is_found_by_number_or_by_place() -> None:
    by_id = await places.lease_named(_Conn(), **_WHERE, asked=f"#{OLD_LEASE}")
    assert isinstance(by_id, places.Match) and by_id.id == OLD_LEASE
    single = await places.lease_named(_Conn(), **_WHERE, asked="maple house")
    assert single == places.Match(OTHER_LEASE, "Ben Dana — Maple House")
    by_fragment = await places.lease_named(_Conn(), **_WHERE, asked="Dana")
    assert isinstance(by_fragment, places.Match) and by_fragment.id == OTHER_LEASE


@pytest.mark.asyncio
@pytest.mark.parametrize("asked", ["Ana Ortiz", "Redbud Court 4B", "Redbud Court, Unit #4B"])
async def test_a_tenants_two_leases_are_listed_current_first_never_picked(asked: str) -> None:
    found = await places.lease_named(_Conn(), **_WHERE, asked=asked)
    assert isinstance(found, str)
    current = f"Ana Ortiz — Redbud Court 4B, Active, ends 2026-08-31 (#{LEASE})"
    old = f"Ana Ortiz — Redbud Court 4B, Closed, ends 2025-08-31 (#{OLD_LEASE})"
    assert current in found and old in found and found.index(current) < found.index(old)
    assert found.startswith("2 leases match") and found.endswith("Which one? NOTHING.")


@pytest.mark.asyncio
async def test_a_place_fragment_never_names_a_lease() -> None:
    """ "Redbud Court 4" is not 4B: a lease is matched by its place whole."""
    found = await places.lease_named(_Conn(), **_WHERE, asked="Redbud Court 4")
    assert isinstance(found, str) and found.startswith("No lease matches")


@pytest.mark.asyncio
async def test_a_vendor_and_a_work_order() -> None:
    vendor = await places.vendor_named(_Conn(), **_WHERE, asked="ace plumbing")
    assert vendor == places.Match(VENDOR, "Ace Plumbing")
    several = await places.vendor_named(_Conn(), **_WHERE, asked="Ace")
    assert isinstance(several, str) and f"(#{VENDOR})" in several
    for asked in ("#990000901", "990000901"):
        found = await places.work_order_numbered(_Conn(), **_WHERE, asked=asked)
        assert found == places.Match(990000901, "work order #990000901")
    missing = await places.work_order_numbered(_Conn(), **_WHERE, asked="#5")
    assert isinstance(missing, str) and "No work order #5" in missing
    words = await places.work_order_numbered(_Conn(), **_WHERE, asked="the sink one")
    assert isinstance(words, str) and "#number" in words


# ── through the real writes ─────────────────────────────────────────────────


class _Pool:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn
        self.acquired = 0

    @asynccontextmanager
    async def acquire(self) -> Any:
        self.acquired += 1
        yield self.conn


def _mirror(stack_pool: _Pool) -> Any:
    return patch.object(rt, "_mirror_pool", return_value=stack_pool)


@pytest.mark.asyncio
async def test_a_work_order_is_created_by_names() -> None:
    client = _Client(post_response={"workOrder": _created()}, read_back=_created())
    pool = _Pool(_Conn())

    async def call() -> str:
        return await rt._create_work_order_impl(
            "Redbud Court", "Kitchen sink leaking", "unit 4B", "high", "Ace Plumbing"
        )

    with _mirror(pool):
        reply, card = await _drive(client, call)
    body = client.posts[0]["json_body"]
    assert (body["propertyID"], body["unitID"], body["vendorContactID"]) == (
        str(PROPERTY),
        str(UNIT),
        str(VENDOR),
    )
    assert card is not None and "Unit 4B at Redbud Court" in "\n".join(card["items"])
    assert client.closed


@pytest.mark.asyncio
async def test_an_ambiguous_name_asks_and_sends_nothing() -> None:
    client = _Client()
    pool = _Pool(_Conn())

    async def call() -> str:
        return await rt._create_work_order_impl("Redbud", "Leak", "4B")

    with _mirror(pool):
        reply, card = await _drive(client, call, approve=None)
    assert f"(#{PROPERTY})" in reply and "Nothing was created." in reply
    assert card is None and client.posts == [] and client.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["dark", "not connected", "member"])
async def test_the_gates_answer_before_the_mirror_is_read(gate: str) -> None:
    """Nobody is asked "which lease?" about a write that cannot happen."""
    from helixis.agents import buildium_tools as bt
    from helixis.memory.context import memory_context

    client = _Client()
    pool = _Pool(_Conn())
    settings = MagicMock()
    settings.rentvine.enabled = gate != "dark"
    creds = None if gate == "not connected" else object()
    role = "member" if gate == "member" else "admin"
    with (
        _mirror(pool),
        memory_context(company_id=COMPANY, user_id="u1", session_id="s1"),
        patch.object(rt, "get_settings", return_value=settings),
        patch.object(rt, "load_rentvine_settings_for_company", new=AsyncMock(return_value=creds)),
        patch.object(rt, "RentvineClient", return_value=client),
        patch.object(bt, "_caller_role", new=AsyncMock(return_value=role)),
    ):
        reply = await rt._post_charge_impl("Ana Ortiz", 12.5, "late fee", "2026-10-03", "Late")
    expected = {
        "dark": rt._DARK,
        "not connected": rt._NOT_CONNECTED,
    }.get(gate)
    assert reply == expected if expected else "Manager or Admin" in reply
    assert pool.acquired == 0 and client.posts == []


@pytest.mark.asyncio
async def test_a_charge_is_posted_to_the_lease_the_person_picked() -> None:
    client = _Client(post_response={"transaction": {"transactionID": TXN}}, read_back=_txn_back())
    pool = _Pool(_Conn())

    async def asks() -> str:
        return await rt._post_charge_impl("Ana Ortiz", 12.5, "late fee", "2026-10-03", "Late")

    async def picks() -> str:
        return await rt._post_charge_impl(f"#{LEASE}", 12.5, "late fee", "2026-10-03", "Late")

    with _mirror(pool):
        question, _ = await _drive(client, asks, approve=None)
        assert f"(#{LEASE})" in question and "Nothing was posted." in question
        assert client.posts == []
        reply, card = await _drive(client, picks)
    (sent,) = client.posts
    assert sent["path_params"] == {"leaseID": LEASE}
    # The card names Rentvine's own record, read by the id the words resolved to.
    assert card is not None and "Lease: Unit 4B at Redbud Court (Ana Ortiz)" in card["items"]
    assert reply.startswith("Posted a $12.50 Late Fee charge")


@pytest.mark.asyncio
async def test_a_payment_is_recorded_on_the_lease_a_place_names() -> None:
    client = _Client(
        post_response={"transaction": {"transactionID": TXN}}, read_back=_txn_back("1250.00")
    )
    conn = _Conn()
    conn.leases = conn.leases[:1]  # only the current lease at 4B
    pool = _Pool(conn)

    async def call() -> str:
        return await rt._record_payment_impl("Redbud Court 4B", "1250", "2026-10-03")

    with _mirror(pool):
        reply, _ = await _drive(client, call)
    assert client.posts[0]["path_params"] == {"leaseID": LEASE}
    assert reply.startswith("Recorded a $1,250.00 payment from Ana Ortiz")


async def _upload_named(
    client: _Client, attach_to: str, record: str, unit: str | None, approve: bool | None
) -> tuple[str, Any]:
    from helixis.db.repositories import docs_links

    async def call() -> str:
        return await rt._upload_file_impl("addendum", attach_to, record, None, unit)

    with (
        _mirror(_Pool(_Conn())),
        patch.object(docs_links, "list_documents", new=AsyncMock(return_value=[_doc()])),
        patch("helixis.db.engine.get_pool", return_value=object()),
        patch("helixis.agents.idempotency.store_from_pool", return_value=None),
        patch("helixis.storage.download_object", new=AsyncMock(return_value=b"%PDF-1.4")),
    ):
        return await _drive(client, call, approve=approve)


@pytest.mark.asyncio
async def test_an_uploads_card_names_the_record_not_a_bare_number() -> None:
    attached = {
        "file": {"fileID": "77"},
        "attachment": {"objectTypeID": "7", "objectID": str(UNIT)},
    }
    client = _Client(post_response=attached)
    reply, card = await _upload_named(client, "unit", "Redbud Court", "4B", approve=True)
    (sent,) = client.posts
    assert sent["query"]["objectID"] == UNIT
    assert card is not None and "Attach to: Unit 4B at Redbud Court" in card["items"]
    assert str(UNIT) not in "\n".join(card["items"])


@pytest.mark.asyncio
async def test_an_upload_to_a_unit_needs_the_unit_and_closes_the_client() -> None:
    client = _Client()
    reply, card = await _upload_named(client, "unit", "Redbud Court", None, approve=None)
    assert reply.startswith("Which unit at Redbud Court?") and card is None
    assert client.posts == [] and client.closed


# ── what the model sees ─────────────────────────────────────────────────────

_NAMED = {
    "rentvine_create_work_order": "_create_work_order_impl",
    "rentvine_upload_file": "_upload_file_impl",
    "rentvine_post_charge": "_post_charge_impl",
    "rentvine_record_payment": "_record_payment_impl",
}


@pytest.mark.parametrize("name", sorted(_NAMED))
def test_the_model_is_never_asked_for_an_id_it_cannot_see(name: str) -> None:
    tool = getattr(rt, name)
    fields = set(tool.params_json_schema["properties"])
    assert not {f for f in fields if f.endswith("_id")}, fields
    assert "#number" in tool.description


def test_the_status_update_keeps_the_id_open_work_orders_prints() -> None:
    from helixis.agents import mirror_tools

    assert "work_order_id" in rt.rentvine_update_work_order_status.params_json_schema["properties"]
    assert (
        "The id it prints is what a status update needs"
        in mirror_tools.open_work_orders.description
    )


#: What the model sends each tool, and what its core must receive, in order.
_CALLS: dict[str, tuple[dict[str, Any], tuple[Any, ...]]] = {
    "rentvine_create_work_order": (
        {
            "property_name": "Redbud Court",
            "description": "Leak",
            "unit_name": "4B",
            "priority": "High",
            "vendor_name": "Ace Plumbing",
        },
        ("Redbud Court", "Leak", "4B", "High", "Ace Plumbing"),
    ),
    "rentvine_upload_file": (
        {
            "document_name": "addendum",
            "attach_to": "unit",
            "record": "Redbud Court",
            "unit_name": "4B",
            "title": "Signed addendum",
        },
        ("addendum", "unit", "Redbud Court", "Signed addendum", "4B"),
    ),
    "rentvine_post_charge": (
        {
            "lease": "Ana Ortiz",
            "amount": 12.5,
            "charge_account": "Late Fee",
            "date": "2026-10-03",
            "description": "October late fee",
        },
        ("Ana Ortiz", 12.5, "Late Fee", "2026-10-03", "October late fee"),
    ),
    "rentvine_record_payment": (
        {
            "lease": f"#{LEASE}",
            "amount": 1250,
            "date": "2026-10-03",
            "tenant_name": "Ana",
            "reference": "Check 1042",
            "description": "October rent",
        },
        (f"#{LEASE}", 1250, "2026-10-03", "Ana", "Check 1042", "October rent"),
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(_NAMED))
async def test_each_tool_hands_the_words_to_the_core_the_harness_mocks(name: str) -> None:
    """The golden-case mock replaces the core, so the lookup is bypassed with
    the gates: a case measures the model, never the mirror (rule 15). Called
    through the SDK, so a swapped argument shows."""
    target, _factory = agent_flow._TOOL_MOCK_TARGETS[name]
    assert target == f"{rt.__name__}.{_NAMED[name]}"
    sent, expected = _CALLS[name]
    arguments = json.dumps(sent)
    ctx = ToolContext(context=None, tool_name=name, tool_call_id="c1", tool_arguments=arguments)
    core = AsyncMock(return_value="done")
    with patch.object(rt, _NAMED[name], new=core):
        out = await getattr(rt, name).on_invoke_tool(ctx, arguments)
    assert out == "done"
    core.assert_awaited_once_with(*expected)
