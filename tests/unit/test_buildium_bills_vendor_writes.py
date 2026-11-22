"""Bill create + vendor note writes — endpoint contract + tool guard rails.

Endpoint side pins the spec-grounded wire contract (2026-07-05, LeadSimple
SDK): POST /bills body shape (CreateBillRequest + BillLinePostMessage with
the AccountingEntity sub-object and the exact ``GlAccountId`` casing), and
the vendor-notes POST/GET paths with the shared ``{"Note": …}`` body.

Tool side pins the money-write guard rails: amount/date validation happens
BEFORE any confirm card, the approved execute sends exactly the spec body,
and optional fields (unit, work-order link, reference) appear only when
provided.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helixis.agents import buildium_tools as bt
from helixis.buildium.endpoints.bills import Bills
from helixis.buildium.endpoints.vendors import Vendors
from helixis.memory.context import memory_context

# ── endpoints ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bills_create_posts_to_bills() -> None:
    request = AsyncMock(return_value={"Id": 901})
    client = MagicMock()
    client.request = request

    body = {"Date": "2026-07-05", "DueDate": "2026-07-20", "VendorId": 1, "Lines": []}
    await Bills(client).create(body)

    method, path = request.await_args.args[:2]
    assert (method, path) == ("POST", "/bills")
    assert request.await_args.kwargs["body"] is body


@pytest.mark.asyncio
async def test_vendor_note_paths_and_body() -> None:
    request = AsyncMock(return_value={"Id": 55, "Note": "gate code 1234"})
    client = MagicMock()
    client.request = request
    client.list_all = AsyncMock(return_value=[])

    await Vendors(client).add_note(70239, note="gate code 1234")
    method, path = request.await_args.args[:2]
    assert (method, path) == ("POST", "/vendors/70239/notes")
    assert request.await_args.kwargs["body"] == {"Note": "gate code 1234"}

    # Note read-back is a COLLECTION → paginates via list_all, not a bare GET.
    await Vendors(client).list_notes(70239)
    assert client.list_all.await_args.args[0] == "/vendors/70239/notes"


# ── tool: pre-confirm validation ─────────────────────────────────


def _ctx() -> Any:
    return memory_context(company_id="co-1", user_id="u1", session_id="s1")


_BILL_ARGS: dict[str, Any] = dict(
    vendor_id=70239,
    amount=450.0,
    gl_account_id=88,
    property_id=10282,
    due_date="2026-07-20",
    bill_date="",
    memo="Garden Row repair",
    unit_id=None,
    work_order_id=None,
    reference_number="",
    vendor_label="Apex Plumbing",
    property_label="Garden Row",
)


@pytest.mark.asyncio
async def test_bill_rejects_non_positive_amount() -> None:
    with _ctx():
        out = await bt._create_bill_impl(**{**_BILL_ARGS, "amount": 0.0})
    assert "greater than zero" in out


@pytest.mark.asyncio
async def test_bill_rejects_bad_due_date() -> None:
    with _ctx():
        out = await bt._create_bill_impl(**{**_BILL_ARGS, "due_date": "next Tuesday"})
    assert "YYYY-MM-DD" in out


@pytest.mark.asyncio
async def test_vendor_note_requires_text() -> None:
    with _ctx():
        out = await bt._add_vendor_note_impl(vendor_id=1, note="   ", vendor_label=None)
    assert "note text" in out


# ── tool: approved execute sends the exact spec body ─────────────


def _client_api_for_bill() -> tuple[Any, Any, AsyncMock]:
    create = AsyncMock(return_value={"Id": 901})
    api_mock = MagicMock()
    api_mock.bills = MagicMock()
    api_mock.bills.create = create
    api_mock.bills.list = AsyncMock(return_value=[{"Id": 901}])
    client_mock = MagicMock()
    client_mock.__aenter__ = AsyncMock(return_value=client_mock)
    client_mock.__aexit__ = AsyncMock(return_value=False)
    return client_mock, api_mock, create


async def _fake_guard(**kwargs: Any) -> Any:
    result = await kwargs["execute"]()
    guard = MagicMock()
    guard.outcome = "executed"
    guard.result = result
    return guard


@pytest.mark.asyncio
async def test_approved_bill_sends_spec_body() -> None:
    client_mock, api_mock, create = _client_api_for_bill()
    with (
        _ctx(),
        patch.object(bt, "_open_api", AsyncMock(return_value=(client_mock, api_mock, None))),
        patch.object(bt, "run_guarded", new=_fake_guard),
    ):
        out = await bt._create_bill_impl(
            **{
                **_BILL_ARGS,
                "bill_date": "2026-07-05",
                "unit_id": 77,
                "work_order_id": 3586,
                "reference_number": "INV-2288",
            }
        )

    assert "bill #901" in out
    body = create.await_args.args[0]
    assert body == {
        "Date": "2026-07-05",
        "DueDate": "2026-07-20",
        "VendorId": 70239,
        "Lines": [
            {
                "AccountingEntity": {
                    "Id": 10282,
                    "AccountingEntityType": "Rental",
                    "UnitId": 77,
                },
                # Exact wire casing per the SDK attribute_map — NOT GLAccountId.
                "GlAccountId": 88,
                "Amount": 450.0,
                "Memo": "Garden Row repair",
            }
        ],
        "Memo": "Garden Row repair",
        "WorkOrderId": 3586,
        "ReferenceNumber": "INV-2288",
    }


@pytest.mark.asyncio
async def test_approved_bill_omits_optionals_and_defaults_bill_date() -> None:
    client_mock, api_mock, create = _client_api_for_bill()
    with (
        _ctx(),
        patch.object(bt, "_open_api", AsyncMock(return_value=(client_mock, api_mock, None))),
        patch.object(bt, "run_guarded", new=_fake_guard),
    ):
        await bt._create_bill_impl(**{**_BILL_ARGS, "memo": ""})

    body = create.await_args.args[0]
    assert "WorkOrderId" not in body
    assert "ReferenceNumber" not in body
    assert "Memo" not in body
    assert "UnitId" not in body["Lines"][0]["AccountingEntity"]
    # Defaulted bill date is a real YYYY-MM-DD string.
    assert bt._parse_iso_date(body["Date"]) == body["Date"]


@pytest.mark.asyncio
async def test_approved_vendor_note_posts_and_reports_id() -> None:
    add_note = AsyncMock(return_value={"Id": 55, "Note": "gate code 1234"})
    api_mock = MagicMock()
    api_mock.vendors = MagicMock()
    api_mock.vendors.add_note = add_note
    # The card derives the vendor name from the RECORD (_gate_label), so the
    # getter must be mocked. Deliberately NOT the name the tests pass as
    # vendor_label, so a card rendering the model's string is visibly wrong.
    api_mock.vendors.get = AsyncMock(return_value=MagicMock(company_name="Apex Plumbing"))
    client_mock = MagicMock()
    client_mock.__aenter__ = AsyncMock(return_value=client_mock)
    client_mock.__aexit__ = AsyncMock(return_value=False)

    with (
        _ctx(),
        patch.object(bt, "_open_api", AsyncMock(return_value=(client_mock, api_mock, None))),
        patch.object(bt, "run_guarded", new=_fake_guard),
    ):
        out = await bt._add_vendor_note_impl(
            vendor_id=70239, note="gate code 1234", vendor_label="Apex Plumbing"
        )

    assert "note #55" in out and "Apex Plumbing" in out
    add_note.assert_awaited_once_with(70239, note="gate code 1234")
