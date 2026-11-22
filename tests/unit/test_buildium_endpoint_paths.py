"""Path-string regression tests for the Buildium endpoint helpers.

These pin the exact ``(method, path)`` pair each endpoint handler
sends to ``BuildiumClient.request`` / ``list_all``. Existing
endpoint tests mock the response shape; the four URL bugs PR-A
fixes were all *string* bugs that lived above the response handling,
so we need assertions specifically on the URL.

Per ``helixis-audit-findings-2026-04-29.md``, these four URLs were
shipped wrong in PR #41:

  1. ``/administration/accountinfo`` → ``/administration/account``
  2. ``/leases/{id}/moveout``        → ``/leases/{id}/moveouts``
                                       (list, plural) plus a
                                       ``/moveouts/{tenantId}`` single-tenant
                                       variant.
  3. GET ``/files/{id}/downloadrequest`` → POST (it mints a URL).
  4. ``/bills/payments``             → ``/bills/{billId}/payments``
                                       (the bare path is POST-only,
                                       creates multi-bill payments).

If any of these regress, this file should fail loudly with the path
string in the assertion message.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from helixis.buildium.api import BuildiumApi
from helixis.buildium.endpoints import (
    Bills,
    Communications,
    Files,
    GeneralLedger,
    Leases,
    Listings,
    MeterReadings,
    Requests,
)


@pytest.mark.asyncio
async def test_account_info_uses_administration_account_path() -> None:
    """Regression: BuildiumApi.account_info hits /administration/account.

    Pre-fix value was ``/administration/accountinfo`` which 404s on
    every Buildium tier — broke ``/api/v1/health/ready``.
    """
    client = MagicMock()
    client.request = AsyncMock(return_value={"AccountId": 42})
    api = BuildiumApi(client)

    await api.account_info()

    client.request.assert_awaited_once()
    method, path = client.request.await_args.args[:2]
    assert method == "GET", f"account_info should GET, got {method}"
    assert path == "/administration/account", (
        f"account_info URL regressed: {path!r}; spec says /administration/account"
    )


@pytest.mark.asyncio
async def test_list_move_outs_uses_plural_moveouts_path() -> None:
    """Regression: list_move_outs hits /leases/{id}/moveouts (plural).

    Pre-fix path was singular ``/moveout`` which 404s; spec route is
    plural list.
    """
    client = MagicMock()
    client.list_all = AsyncMock(return_value=[])
    leases = Leases(client)

    await leases.list_move_outs(5678)

    client.list_all.assert_awaited_once()
    path = client.list_all.await_args.args[0]
    assert path == "/leases/5678/moveouts", (
        f"list_move_outs URL regressed: {path!r}; spec says /leases/{{leaseId}}/moveouts"
    )


@pytest.mark.asyncio
async def test_get_move_out_for_tenant_targets_per_tenant_path() -> None:
    """The new per-tenant variant maps to /moveouts/{tenantId}."""
    client = MagicMock()
    client.request = AsyncMock(return_value={"Id": 1})
    leases = Leases(client)

    await leases.get_move_out_for_tenant(5678, 9)

    client.request.assert_awaited_once()
    method, path = client.request.await_args.args[:2]
    assert method == "GET"
    assert path == "/leases/5678/moveouts/9", (
        f"get_move_out_for_tenant URL regressed: {path!r}; spec says "
        "/leases/{leaseId}/moveouts/{tenantId}"
    )


@pytest.mark.asyncio
async def test_files_download_url_uses_post() -> None:
    """Regression: download_url POSTs to /files/{id}/downloadrequest.

    The endpoint is a server-side mutation (mints a presigned URL).
    GET 405s on every Buildium tier.
    """
    client = MagicMock()
    client.request = AsyncMock(return_value={"DownloadUrl": "https://example/x"})
    files = Files(client)

    await files.download_url(123)

    client.request.assert_awaited_once()
    method, path = client.request.await_args.args[:2]
    assert method == "POST", f"download_url should POST, got {method}"
    assert path == "/files/123/downloadrequest", f"download_url URL regressed: {path!r}"


@pytest.mark.asyncio
async def test_list_payments_uses_per_bill_path() -> None:
    """Regression: list_payments hits /bills/{billId}/payments.

    Pre-fix path was ``/bills/payments`` which is POST-only on
    Buildium's spec (it creates a multi-bill payment, not a list).
    Calling GET silently 404s.
    """
    client = MagicMock()
    client.list_all = AsyncMock(return_value=[])
    bills = Bills(client)

    await bills.list_payments(42)

    client.list_all.assert_awaited_once()
    path = client.list_all.await_args.args[0]
    assert path == "/bills/42/payments", (
        f"list_payments URL regressed: {path!r}; spec says /bills/{{billId}}/payments"
    )


@pytest.mark.asyncio
async def test_get_payment_targets_per_payment_path() -> None:
    """The new single-payment helper maps to /bills/{id}/payments/{paymentId}."""
    client = MagicMock()
    client.request = AsyncMock(return_value={"Id": 1})
    bills = Bills(client)

    await bills.get_payment(42, 7)

    client.request.assert_awaited_once()
    method, path = client.request.await_args.args[:2]
    assert method == "GET"
    assert path == "/bills/42/payments/7", (
        f"get_payment URL regressed: {path!r}; spec says /bills/{{billId}}/payments/{{paymentId}}"
    )


# ── Request task subtypes (read-only) ───────────────────────────────────


@pytest.mark.asyncio
async def test_request_list_paths_map_to_each_subtype() -> None:
    """Each list_* helper hits its /tasks/<subtype> collection."""
    client = MagicMock()
    client.list_all = AsyncMock(return_value=[])
    requests = Requests(client)

    cases = [
        (requests.list_resident, "/tasks/residentrequests"),
        (requests.list_rental_owner, "/tasks/rentalownerrequests"),
        (requests.list_contact, "/tasks/contactrequests"),
        (requests.list_to_do, "/tasks/todorequests"),
    ]
    for lister, expected in cases:
        client.list_all.reset_mock()
        await lister()
        path = client.list_all.await_args.args[0]
        assert path == expected, f"{lister.__name__} URL regressed: {path!r}"


@pytest.mark.asyncio
async def test_request_get_paths_map_to_each_subtype() -> None:
    """Each get_* helper hits /tasks/<subtype>/{id}."""
    client = MagicMock()
    client.request = AsyncMock(return_value={"Id": 1})
    requests = Requests(client)

    cases = [
        (requests.get_resident, "/tasks/residentrequests/9"),
        (requests.get_rental_owner, "/tasks/rentalownerrequests/9"),
        (requests.get_contact, "/tasks/contactrequests/9"),
        (requests.get_to_do, "/tasks/todorequests/9"),
    ]
    for getter, expected in cases:
        client.request.reset_mock()
        await getter(9)
        method, path = client.request.await_args.args[:2]
        assert method == "GET"
        assert path == expected, f"{getter.__name__} URL regressed: {path!r}"


# ── General Ledger (read-only) ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_gl_list_accounts_path() -> None:
    """list_accounts hits /glaccounts (the chart of accounts)."""
    client = MagicMock()
    client.list_all = AsyncMock(return_value=[])
    gl = GeneralLedger(client)

    await gl.list_accounts()

    path = client.list_all.await_args.args[0]
    assert path == "/glaccounts", f"list_accounts URL regressed: {path!r}"


@pytest.mark.asyncio
async def test_gl_get_account_path() -> None:
    """get_account hits /glaccounts/{glAccountId}."""
    client = MagicMock()
    client.request = AsyncMock(return_value={"Id": 1})
    gl = GeneralLedger(client)

    await gl.get_account(77)

    method, path = client.request.await_args.args[:2]
    assert method == "GET"
    assert path == "/glaccounts/77", f"get_account URL regressed: {path!r}"


@pytest.mark.asyncio
async def test_gl_account_balances_path() -> None:
    """list_account_balances hits /glaccounts/balances."""
    client = MagicMock()
    client.list_all = AsyncMock(return_value=[])
    gl = GeneralLedger(client)

    await gl.list_account_balances(asofdate="2026-06-29")

    path = client.list_all.await_args.args[0]
    assert path == "/glaccounts/balances", (
        f"list_account_balances URL regressed: {path!r}; spec says /glaccounts/balances"
    )


@pytest.mark.asyncio
async def test_gl_transactions_path() -> None:
    """list_transactions hits /generalledger/transactions."""
    client = MagicMock()
    client.list_all = AsyncMock(return_value=[])
    gl = GeneralLedger(client)

    await gl.list_transactions(startdate="2026-06-01", enddate="2026-06-29")

    path = client.list_all.await_args.args[0]
    assert path == "/generalledger/transactions", (
        f"list_transactions URL regressed: {path!r}; spec says /generalledger/transactions"
    )


@pytest.mark.asyncio
async def test_gl_get_transaction_path() -> None:
    """get_transaction hits /generalledger/transactions/{transactionId}."""
    client = MagicMock()
    client.request = AsyncMock(return_value={"Id": 1})
    gl = GeneralLedger(client)

    await gl.get_transaction(555)

    method, path = client.request.await_args.args[:2]
    assert method == "GET"
    assert path == "/generalledger/transactions/555", f"get_transaction URL regressed: {path!r}"


# ── Communications (read-only) ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_communications_list_paths() -> None:
    """Each list_* helper hits its /communications/<kind> collection."""
    client = MagicMock()
    client.list_all = AsyncMock(return_value=[])
    comms = Communications(client)

    cases = [
        (comms.list_announcements, "/communications/announcements"),
        (comms.list_emails, "/communications/emails"),
        (comms.list_phone_logs, "/communications/phonelogs"),
        (comms.list_templates, "/communications/templates"),
    ]
    for lister, expected in cases:
        client.list_all.reset_mock()
        await lister()
        path = client.list_all.await_args.args[0]
        assert path == expected, f"{lister.__name__} URL regressed: {path!r}"


@pytest.mark.asyncio
async def test_communications_get_paths() -> None:
    """Each get_* helper hits /communications/<kind>/{id}."""
    client = MagicMock()
    client.request = AsyncMock(return_value={"Id": 1})
    comms = Communications(client)

    cases = [
        (comms.get_announcement, "/communications/announcements/3"),
        (comms.get_email, "/communications/emails/3"),
        (comms.get_phone_log, "/communications/phonelogs/3"),
        (comms.get_template, "/communications/templates/3"),
    ]
    for getter, expected in cases:
        client.request.reset_mock()
        await getter(3)
        method, path = client.request.await_args.args[:2]
        assert method == "GET"
        assert path == expected, f"{getter.__name__} URL regressed: {path!r}"


# ── Listings (read-only) ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_listings_list_path() -> None:
    """list hits /rentals/units/listings (NOT a top-level /listings)."""
    client = MagicMock()
    client.list_all = AsyncMock(return_value=[])
    listings = Listings(client)

    await listings.list()

    path = client.list_all.await_args.args[0]
    assert path == "/rentals/units/listings", f"listings.list URL regressed: {path!r}"


@pytest.mark.asyncio
async def test_listings_get_for_unit_path() -> None:
    """get_for_unit hits /rentals/units/{unitId}/listing."""
    client = MagicMock()
    client.request = AsyncMock(return_value={"Id": 1})
    listings = Listings(client)

    await listings.get_for_unit(77)

    method, path = client.request.await_args.args[:2]
    assert method == "GET"
    assert path == "/rentals/units/77/listing", (
        f"get_for_unit URL regressed: {path!r}; spec says /rentals/units/{{unitId}}/listing"
    )


# ── Meter readings (read-only) ──────────────────────────────────────────
#
# ⚠ The `Appliances` wrapper that shared this block was REMOVED on 2026-09-22
# with the rest of the appliance surface (see buildium/api.py) — its paths
# stop answering on Buildium's 2026-10-19 retirement date, so do not restore
# these tests without first grounding the REPLACEMENT routes (gotcha 79).


@pytest.mark.asyncio
async def test_meter_readings_paths() -> None:
    """Meter readings are property-scoped under /rentals/{propertyId}."""
    client = MagicMock()
    client.request = AsyncMock(return_value=[])
    meters = MeterReadings(client)

    cases = [
        (lambda: meters.list(42), "/rentals/42/meterreadings"),
        (lambda: meters.get_summary(42), "/rentals/42/meterreadings/summary"),
    ]
    for call, expected in cases:
        client.request.reset_mock()
        await call()
        method, path = client.request.await_args.args[:2]
        assert method == "GET"
        assert path == expected, f"meter-readings URL regressed: {path!r}"


@pytest.mark.asyncio
async def test_work_order_add_note_rides_the_wo_put() -> None:
    """Write-probe 2026-07-05 rounds 1+2: NEITHER /workorders/{id}/notes NOR
    /tasks/{id}/notes exists — Buildium has no note endpoint at all. add_note
    must read the WO and PUT it back with the note appended to WorkDetails,
    carrying the PUT-required fields (EntryAllowed, VendorId) and line items."""
    from helixis.buildium.endpoints.work_orders import WorkOrders

    client = MagicMock()
    wo_raw = {
        "Id": 3584,
        "WorkDetails": "Fix the door",
        "EntryAllowed": "Yes",
        "VendorId": 70239,
        "VendorNotes": "gate code 1234",
        "LineItems": [{"GlAccountId": 9, "Quantity": 1.0, "Memo": "labor", "UnitPrice": 80.0}],
    }
    client.request = AsyncMock(side_effect=[wo_raw, {"ok": True}])
    work_orders = WorkOrders(client)

    await work_orders.add_note(3584, note="probe note")

    # First call reads the WO raw; second PUTs the merged body back.
    get_call = client.request.await_args_list[0]
    assert get_call.args[:2] == ("GET", "/workorders/3584")
    put_call = client.request.await_args_list[1]
    method, path = put_call.args[:2]
    assert method == "PUT"
    assert path == "/workorders/3584", f"WO-note route regressed: {path!r}"
    body = put_call.kwargs["body"]
    # ⚠ Was "[Helixis note ". This string is written into the CUSTOMER's
    # permanent Buildium record — a work order's WorkDetails, which a property
    # manager reads in Buildium and which gotcha 20 says cannot be deleted.
    # The product is Occupella; the other note writer (notes/policy.py) already
    # stamped "[Occupella]", and this test was pinning the one that did not.
    assert body["WorkDetails"].startswith("Fix the door\n\n[Occupella note ")
    assert body["WorkDetails"].endswith("] probe note")
    # PUT-required + carried-over fields survive the read-merge-write.
    assert body["EntryAllowed"] == "Yes"
    assert body["VendorId"] == 70239
    assert body["VendorNotes"] == "gate code 1234"
    assert body["LineItems"] == [
        {"GlAccountId": 9, "Quantity": 1.0, "Memo": "labor", "UnitPrice": 80.0}
    ]


@pytest.mark.asyncio
async def test_work_order_add_note_422s_without_vendor() -> None:
    """The WO PUT requires VendorId — no vendor means the note cannot be
    added; surface a clean API error instead of a raw Buildium 422."""
    from helixis.buildium.endpoints.work_orders import WorkOrders
    from helixis.buildium.errors import BuildiumApiError

    client = MagicMock()
    client.request = AsyncMock(return_value={"Id": 1, "VendorId": None})
    with pytest.raises(BuildiumApiError):
        await WorkOrders(client).add_note(1, note="n")


# ── To-do task writes (probe round 2, 2026-07-05) ───────────────────────


@pytest.mark.asyncio
async def test_task_update_reads_then_puts_full_merged_body() -> None:
    """Buildium's only to-do update route is a FULL-REPLACE PUT (no PATCH —
    the old PATCH 404'd live). update() must GET /tasks/todorequests/{id},
    map the response onto the PUT shape, merge the caller's fields, and PUT."""
    from helixis.buildium.endpoints.tasks import Tasks

    client = MagicMock()
    todo_raw = {
        "Id": 21856,
        "Title": "Probe task",
        "Description": "created by probe",
        "Category": {"Id": 4, "SubCategory": {"Id": 11}},
        "Property": {"Id": 10282, "Type": "Rental"},
        "UnitId": 55,
        "AssignedToUserId": 70237,
        "TaskStatus": "New",
        "Priority": "Normal",
        "DueDate": "2026-07-10",
    }
    client.request = AsyncMock(side_effect=[todo_raw, {"ok": True}])
    tasks = Tasks(client)

    await tasks.update(21856, fields={"TaskStatus": "Completed"})

    get_call = client.request.await_args_list[0]
    assert get_call.args[:2] == ("GET", "/tasks/todorequests/21856")
    put_call = client.request.await_args_list[1]
    method, path = put_call.args[:2]
    assert method == "PUT", f"to-do update must PUT (PATCH 404s), got {method}"
    assert path == "/tasks/todorequests/21856", f"to-do update route regressed: {path!r}"
    body = put_call.kwargs["body"]
    # The caller's field wins; everything else carries over so the
    # full-replace PUT can't null fields (GET→PUT key mapping included).
    assert body["TaskStatus"] == "Completed"
    assert body["Title"] == "Probe task"
    # Round 3: Message is the update COMMENT (a task-history entry), NOT the
    # description — a plain update must not send one, or every status flip
    # writes a duplicate-description history line.
    assert "Message" not in body
    assert body["CategoryId"] == 4 and body["SubCategoryId"] == 11
    assert body["PropertyId"] == 10282
    assert body["UnitId"] == 55
    assert body["AssignedToUserId"] == 70237
    assert body["Priority"] == "Normal"
    assert body["DueDate"] == "2026-07-10"


@pytest.mark.asyncio
async def test_task_add_note_sends_note_as_update_message() -> None:
    """No note endpoint exists on tasks — Buildium's note mechanism is the
    PUT's Message field (the update comment), which lands as a task-history
    entry. add_note sends the note verbatim as Message via read-merge-write;
    the task's Description is never touched (round-3 live proof)."""
    from helixis.buildium.endpoints.tasks import Tasks

    client = MagicMock()
    todo_raw = {
        "Id": 21856,
        "Title": "Probe task",
        "Description": "created by probe",
        "AssignedToUserId": 70237,
        "TaskStatus": "New",
        "Priority": "Normal",
    }
    client.request = AsyncMock(side_effect=[todo_raw, {"ok": True}])
    tasks = Tasks(client)

    await tasks.add_note(21856, note="call the tenant back")

    put_call = client.request.await_args_list[1]
    method, path = put_call.args[:2]
    assert (method, path) == ("PUT", "/tasks/todorequests/21856")
    body = put_call.kwargs["body"]
    # The note IS the update message — verbatim, not merged into the
    # description (history entries carry their own timestamp + author).
    assert body["Message"] == "call the tenant back"
    # Required PUT fields survive so the full replace can't blank the task.
    assert body["Title"] == "Probe task"
    assert body["TaskStatus"] == "New"
    assert body["Priority"] == "Normal"
    assert body["AssignedToUserId"] == 70237
