"""The four Rentvine creates: work order, file, lease charge, lease payment.

Founder decision 2026-10-03 (``docs/decisions.md``). Each is driven through its
real ``_impl`` with the confirm gate answered, against a fake client that
records what is SENT and serves what is READ BACK, because the two failure
modes that matter are on those two sides:

* a body that mails somebody, picks a status or a tenant the server never
  named, or charges an account that is not income;
* a 2xx taken as the write (CLAUDE.md rule 6): every success here is decided
  by a read-back, and a read-back that does not match is a review, never a
  success.

All records are synthetic (ids 990 000 xxx).
"""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helixis.agents import rentvine_tools as rt
from helixis.agents import run_pms
from helixis.agents import tools as agent_tools
from helixis.memory.context import memory_context
from helixis.rentvine import work_orders as wo
from helixis.rentvine import writes
from helixis.rentvine.client import RentvineAPIError
from helixis.rentvine.transport import Failure

PROPERTY, UNIT, VENDOR, LEASE, NEW_WO, TXN = (
    990000101,
    990000301,
    990000501,
    990000701,
    990000901,
    990000951,
)
STATUS_ROWS = [
    {"workOrderStatusID": "71", "name": "Received", "primaryWorkOrderStatusID": "1"},
    {"workOrderStatusID": "72", "name": "Scheduled", "primaryWorkOrderStatusID": "2"},
    {"workOrderStatusID": "73", "name": "Done", "primaryWorkOrderStatusID": "3"},
]
ACCOUNTS = [
    {"accountID": "4100", "name": "Late Fee", "accountTypeID": "7", "isActive": "1"},
    {"accountID": "4200", "name": "Parking Fee", "accountTypeID": "8", "isActive": "1"},
    {"accountID": "4300", "name": "Old Fee", "accountTypeID": "7", "isActive": "0"},
    {"accountID": "6100", "name": "Repairs", "accountTypeID": "9", "isActive": "1"},
]


def _tenant(ident: str, name: str, *, active: str = "1", primary: str = "0") -> dict[str, Any]:
    return {
        "leaseTenant": {"leaseTenantID": ident, "isActive": active, "isPrimary": primary},
        "contact": {"name": name},
    }


def _lease_body(tenants: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "lease": {"leaseID": str(LEASE)},
        "unit": {"name": "4B"},
        "property": {"name": "Redbud Court"},
        "tenants": [_tenant("801", "Ana Ortiz", primary="1")] if tenants is None else tenants,
    }


class _Client:
    """Records every POST; serves reads, including the read-back after one."""

    def __init__(
        self,
        *,
        unit: dict[str, Any] | None = None,
        statuses: list[dict[str, Any]] | None = None,
        lease: dict[str, Any] | None = None,
        post_response: Any = None,
        read_back: dict[str, Any] | None = None,
        post_error: RentvineAPIError | None = None,
    ) -> None:
        self.unit = {"name": "4B", "leaseID": str(LEASE)} if unit is None else unit
        self.statuses = STATUS_ROWS if statuses is None else statuses
        self.lease = _lease_body() if lease is None else lease
        self.post_response = post_response
        self.read_back = read_back
        self.post_error = post_error
        self.posts: list[dict[str, Any]] = []
        self.closed = False

    async def get_rows(self, template: str, **_kw: Any) -> list[dict[str, Any]]:
        assert template == wo.STATUSES_PATH
        return list(self.statuses)

    async def get_object(self, template: str, **kw: Any) -> dict[str, Any]:
        if template == "/properties/{propertyID}":
            return {"propertyID": str(PROPERTY), "name": "Redbud Court"}
        if template == writes.UNIT_PATH:
            return dict(self.unit)
        if template == "/vendors/{contactID}":
            return {"contactID": str(VENDOR), "name": "Ace Plumbing"}
        if template == wo.GET_PATH:
            assert self.posts, "the work order was re-read before it was created"
            return dict(self.read_back or {})
        raise AssertionError(template)

    async def get(self, template: str, **kw: Any) -> Any:
        assert template == writes.LEASE_PATH
        if kw["query"]["includes"] == "transactions":
            assert self.posts, "the lease was re-read before anything was posted"
            return self.read_back or {}
        return self.lease

    async def iter_all(self, template: str, **_kw: Any) -> Any:
        assert template == writes.ACCOUNTS_PATH
        return SimpleNamespace(rows=list(ACCOUNTS), stop_was_documented=True)

    async def post(self, template: str, **kw: Any) -> Any:
        if self.post_error is not None:
            raise self.post_error
        self.posts.append({"template": template, **kw})
        return self.post_response

    async def aclose(self) -> None:
        self.closed = True


def _patches(stack: ExitStack, client: _Client, *, role: str = "admin") -> None:
    from helixis.agents import buildium_tools as bt

    settings = MagicMock()
    settings.rentvine.enabled = True
    stack.enter_context(memory_context(company_id="company-1", user_id="u1", session_id="s1"))
    stack.enter_context(patch.object(rt, "get_settings", return_value=settings))
    stack.enter_context(
        patch.object(rt, "load_rentvine_settings_for_company", new=AsyncMock(return_value=object()))
    )
    stack.enter_context(patch.object(rt, "RentvineClient", return_value=client))
    stack.enter_context(patch.object(bt, "_caller_role", new=AsyncMock(return_value=role)))


async def _drive(
    client: _Client, call: Any, *, approve: bool | None = True, role: str = "admin"
) -> tuple[str, dict[str, Any] | None]:
    """Run ``call()``; answer the confirm gate with ``approve`` (``None``: the
    tool must refuse before any gate). Returns ``(reply, confirm payload)``."""
    await agent_tools.drain_pending_confirmations()
    with ExitStack() as stack:
        _patches(stack, client, role=role)
        task = asyncio.create_task(call())
        payload: dict[str, Any] | None = None
        for _ in range(200):
            await asyncio.sleep(0)
            if task.done():
                break
            drained = await agent_tools.drain_pending_confirmations()
            if drained:
                payload = drained[0]
                break
        if payload is not None:
            assert approve is not None, "a refusal path reached the confirm gate"
            await agent_tools.resolve_confirmation(payload["confirm_id"], approve)
        reply = await asyncio.wait_for(task, timeout=2.0)
    if approve is not None:
        assert payload is not None, "the write never reached its confirm gate"
    return reply, payload


# ── create a work order ──────────────────────────────────────────────────────


def _created(description: str = "Kitchen sink leaking") -> dict[str, Any]:
    return {"workOrderID": str(NEW_WO), "propertyID": str(PROPERTY), "description": description}


@pytest.mark.asyncio
async def test_a_work_order_is_created_open_unshared_and_mailing_nobody() -> None:
    client = _Client(post_response={"workOrder": _created()}, read_back=_created())

    async def call() -> str:
        return await rt._create_work_order_impl(
            PROPERTY, "Kitchen sink leaking", unit_id=UNIT, priority="high", vendor_id=VENDOR
        )

    reply, card = await _drive(client, call)
    (sent,) = client.posts
    assert sent["template"] == writes.CREATE_WORK_ORDER_PATH
    body = sent["json_body"]
    assert body["workOrderStatusID"] == "72"  # the account's status whose primary is Open
    assert body["priorityID"] == "3" and body["description"] == "Kitchen sink leaking"
    assert (body["propertyID"], body["unitID"], body["vendorContactID"]) == (
        str(PROPERTY),
        str(UNIT),
        str(VENDOR),
    )
    assert body["isVacant"] is False  # the unit has a lease
    for flag in ("sendEmail", "sendVendorNotification", "sendReview", "isOwnerApproved"):
        assert body[flag] is False, flag
    assert body["isSharedWithTenant"] == "0" and body["isSharedWithOwner"] is False
    # The card names the SERVER's records, and what is not done.
    assert card is not None
    items = "\n".join(card["items"])
    assert "Unit 4B at Redbud Court" in items and "Ace Plumbing (not notified)" in items
    assert "nobody is emailed" in items
    assert card["request_body"] == body
    assert f"(work order {NEW_WO})" in reply and client.closed


@pytest.mark.asyncio
async def test_a_unit_with_no_lease_is_sent_as_vacant() -> None:
    client = _Client(
        unit={"name": "4B", "leaseID": None}, post_response=_created(), read_back=_created()
    )

    async def call() -> str:
        return await rt._create_work_order_impl(PROPERTY, "Kitchen sink leaking", unit_id=UNIT)

    await _drive(client, call)
    assert client.posts[0]["json_body"]["isVacant"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "client,why",
    [
        (_Client(statuses=[{"workOrderStatusID": "1", "primaryWorkOrderStatusID": "3"}]), "Open"),
        (_Client(unit={"name": "4B"}), "vacant"),
    ],
    ids=["no-open-status", "unit-cannot-say"],
)
async def test_what_the_account_cannot_say_is_never_guessed(client: _Client, why: str) -> None:
    async def call() -> str:
        return await rt._create_work_order_impl(PROPERTY, "Leak", unit_id=UNIT)

    reply, _ = await _drive(client, call, approve=None)
    assert why in reply and "Nothing was created" in reply
    assert client.posts == []


@pytest.mark.asyncio
async def test_a_work_order_the_re_read_cannot_find_is_a_review() -> None:
    """A 2xx is not the write (rule 6): the re-GET must show it."""
    client = _Client(post_response=_created(), read_back=_created("Something else"))

    async def call() -> str:
        return await rt._create_work_order_impl(PROPERTY, "Kitchen sink leaking")

    reply, _ = await _drive(client, call)
    assert "couldn't confirm it in Rentvine" in reply


@pytest.mark.asyncio
async def test_a_denied_work_order_sends_nothing() -> None:
    client = _Client(post_response=_created(), read_back=_created())

    async def call() -> str:
        return await rt._create_work_order_impl(PROPERTY, "Kitchen sink leaking")

    reply, _ = await _drive(client, call, approve=False)
    assert client.posts == [] and "Nothing was changed in Rentvine" in reply


@pytest.mark.asyncio
async def test_a_rejected_create_says_nothing_was_changed() -> None:
    client = _Client(post_error=RentvineAPIError(400, Failure.BAD_REQUEST, False, "bad"))

    async def call() -> str:
        return await rt._create_work_order_impl(PROPERTY, "Kitchen sink leaking")

    reply, _ = await _drive(client, call)
    assert "Nothing was changed in Rentvine" in reply


# ── upload a file ────────────────────────────────────────────────────────────


def _doc(name: str = "Lease Addendum.pdf") -> Any:
    return SimpleNamespace(
        id="doc-1", name=name, storage_path="co/doc-1.pdf", content_type="application/pdf"
    )


async def _upload(
    client: _Client, *, docs: list[Any], attach_to: str = "lease", approve: bool | None = True
) -> tuple[str, Any]:
    from helixis.db.repositories import docs_links

    async def call() -> str:
        return await rt._upload_file_impl("addendum", attach_to, LEASE)

    with (
        patch.object(docs_links, "list_documents", new=AsyncMock(return_value=docs)),
        patch("helixis.db.engine.get_pool", return_value=object()),
        # The library lookup needs a pool; the claim store must not get this one.
        patch("helixis.agents.idempotency.store_from_pool", return_value=None),
        patch("helixis.storage.download_object", new=AsyncMock(return_value=b"%PDF-1.4")),
    ):
        return await _drive(client, call, approve=approve)


@pytest.mark.asyncio
async def test_a_library_file_is_uploaded_and_attached_to_the_record() -> None:
    attached = {
        "file": {"fileID": "77"},
        "attachment": {"objectTypeID": "4", "objectID": str(LEASE)},
    }
    client = _Client(post_response=attached)
    reply, card = await _upload(client, docs=[_doc()])
    (sent,) = client.posts
    assert sent["template"] == writes.FILES_PATH
    assert sent["query"] == {"objectTypeID": 4, "objectID": LEASE, "includes": "attachment"}
    name, content, kind = sent["files"]["file"]
    assert (name, content, kind) == ("Lease Addendum.pdf", b"%PDF-1.4", "application/pdf")
    assert "json_body" not in sent
    assert reply.startswith('Uploaded "Lease Addendum.pdf" to lease')


@pytest.mark.asyncio
async def test_an_upload_attached_elsewhere_is_a_review() -> None:
    client = _Client(
        post_response={
            "file": {"fileID": "77"},
            "attachment": {"objectTypeID": "7", "objectID": "1"},
        }
    )
    reply, _ = await _upload(client, docs=[_doc()])
    assert "couldn't confirm it in Rentvine" in reply


@pytest.mark.asyncio
async def test_an_unknown_target_or_document_uploads_nothing() -> None:
    client = _Client()
    reply, _ = await _upload(client, docs=[_doc()], attach_to="garage", approve=None)
    assert "property, unit, lease or work order" in reply
    reply, _ = await _upload(client, docs=[], approve=None)
    assert "library is empty" in reply
    assert client.posts == []


# ── post a lease charge ──────────────────────────────────────────────────────


def _txn_back(amount: str = "12.50", *, voided: str = "0") -> dict[str, Any]:
    return {
        "transactions": [
            {"transaction": {"transactionID": str(TXN), "amount": amount, "isVoided": voided}}
        ]
    }


def _charge(client: _Client, **kw: Any) -> Any:
    args = {
        "amount": 12.5,
        "charge_account": "late fee",
        "date": "2026-10-03",
        "description": "October late fee",
        **kw,
    }

    async def call() -> str:
        return await rt._post_charge_impl(LEASE, **args)

    return call


@pytest.mark.asyncio
async def test_a_charge_posts_to_the_named_income_account_and_is_read_back() -> None:
    client = _Client(post_response={"transaction": {"transactionID": TXN}}, read_back=_txn_back())
    reply, card = await _drive(client, _charge(client))
    (sent,) = client.posts
    assert sent["template"] == writes.LEASE_CHARGE_PATH
    assert sent["path_params"] == {"leaseID": LEASE}
    assert sent["json_body"] == {
        "datePosted": "2026-10-03",
        "amount": "12.50",
        "chargeAccountID": "4100",
        "description": "October late fee",
    }
    assert card is not None
    items = "\n".join(card["items"])
    assert "Unit 4B at Redbud Court (Ana Ortiz)" in items and "Account: Late Fee" in items
    assert "management-fee or sales-tax" in items and "can't be undone" in items
    assert (
        reply
        == "Posted a $12.50 Late Fee charge to Unit 4B at Redbud Court (Ana Ortiz) in Rentvine."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ["Repairs", "Old Fee", "Rent"])
async def test_only_an_active_income_account_can_be_charged(account: str) -> None:
    """An expense account, an inactive one, or a name the account lacks."""
    client = _Client()
    reply, _ = await _drive(client, _charge(client, charge_account=account), approve=None)
    assert "Late Fee, Parking Fee" in reply and client.posts == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kw,why",
    [
        ({"amount": 0}, "positive"),
        ({"amount": "x"}, "positive"),
        ({"date": "10/03/2026"}, "YYYY-MM-DD"),
    ],
)
async def test_a_bad_amount_or_date_posts_nothing(kw: dict[str, Any], why: str) -> None:
    client = _Client()
    reply, _ = await _drive(client, _charge(client, **kw), approve=None)
    assert why in reply and client.posts == []


@pytest.mark.asyncio
async def test_only_a_manager_may_post_a_charge() -> None:
    client = _Client()
    reply, _ = await _drive(client, _charge(client), approve=None, role="member")
    assert "Manager or Admin" in reply and client.posts == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "back",
    [{"transactions": []}, _txn_back("99.00"), _txn_back(voided="1")],
    ids=["absent", "wrong-amount", "voided"],
)
async def test_a_charge_the_lease_does_not_show_is_a_review(back: dict[str, Any]) -> None:
    client = _Client(post_response={"transaction": {"transactionID": TXN}}, read_back=back)
    reply, _ = await _drive(client, _charge(client))
    assert "couldn't confirm it in Rentvine" in reply


# ── record a lease payment ───────────────────────────────────────────────────


def _payment(**kw: Any) -> Any:
    args = {"amount": "1250", "date": "2026-10-03", "reference": "Check 1042", **kw}

    async def call() -> str:
        return await rt._record_payment_impl(LEASE, **args)

    return call


@pytest.mark.asyncio
async def test_a_payment_is_recorded_from_the_tenant_and_mails_nobody() -> None:
    client = _Client(
        post_response={"transaction": {"transactionID": TXN}}, read_back=_txn_back("1250.00")
    )
    reply, card = await _drive(client, _payment())
    (sent,) = client.posts
    assert sent["template"] == writes.LEASE_PAYMENT_PATH
    body = sent["json_body"]
    assert body["leaseTenantID"] == "801" and body["amount"] == "1250.00"
    assert body["applyAutomatically"] is True and body["emailReceipt"] is False
    assert body["bankAccountID"] is None and body["reference"] == "Check 1042"
    assert card is not None
    items = "\n".join(card["items"])
    assert "From: Ana Ortiz" in items and "charges nobody" in items and "can't be undone" in items
    assert reply.startswith("Recorded a $1,250.00 payment from Ana Ortiz")


@pytest.mark.asyncio
async def test_two_tenants_and_no_name_asks_who_paid() -> None:
    two = _lease_body([_tenant("801", "Ana Ortiz"), _tenant("802", "Ben Ortiz")])
    client = _Client(lease=two)
    reply, _ = await _drive(client, _payment(), approve=None)
    assert "Ana Ortiz, Ben Ortiz" in reply and client.posts == []
    client = _Client(
        lease=two,
        post_response={"transaction": {"transactionID": TXN}},
        read_back=_txn_back("1250.00"),
    )
    await _drive(client, _payment(tenant_name="ben"))
    assert client.posts[0]["json_body"]["leaseTenantID"] == "802"


@pytest.mark.asyncio
async def test_no_active_tenant_records_nothing() -> None:
    client = _Client(lease=_lease_body([_tenant("801", "Ana Ortiz", active="0")]))
    reply, _ = await _drive(client, _payment(), approve=None)
    assert "no active tenant" in reply and client.posts == []


@pytest.mark.asyncio
async def test_only_a_manager_may_record_a_payment() -> None:
    client = _Client()
    reply, _ = await _drive(client, _payment(), approve=None, role="member")
    assert "Manager or Admin" in reply and client.posts == []


# ── who can see them ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("pms,shown", [("rentvine", True), ("buildium", False), (None, False)])
def test_the_creates_are_shown_on_rentvine_runs_only(pms: str | None, shown: bool) -> None:
    token = run_pms.bind_run_pms(pms)
    try:
        for tool in rt.rentvine_tools()[1:]:
            assert tool.is_enabled(None, None) is shown, tool.name
    finally:
        run_pms.reset_run_pms(token)


# Re-pointed (rule 19): this pinned the status write as shown on EVERY run,
# the visibility reviewed before Rent Manager and Propertyware runs existed.
# On those, with their writes off, it was the only status tool the model saw,
# and it answered "connect Rentvine". Every run it was shown on keeps it.
@pytest.mark.parametrize(
    "pms,shown",
    [
        ("rentvine", True),
        ("buildium", True),
        (None, True),
        ("appfolio", True),
        ("rentmanager", False),
        ("propertyware", False),
    ],
)
def test_the_status_write_is_hidden_only_on_the_pms_connectors_runs(
    pms: str | None, shown: bool
) -> None:
    status_tool = rt.rentvine_tools()[0]
    assert status_tool.name == "rentvine_update_work_order_status"
    token = run_pms.bind_run_pms(pms)
    try:
        assert status_tool.is_enabled(None, None) is shown
    finally:
        run_pms.reset_run_pms(token)


def test_money_is_parsed_to_the_cent_and_never_below_it() -> None:
    assert writes.positive_amount("1250") == Decimal("1250.00")
    assert writes.positive_amount(12.5) == Decimal("12.50")
    for bad in (0, -5, "abc", "NaN", None):
        assert writes.positive_amount(bad) is None


# ── how a failed send is filed (the real guard, an in-memory store) ─────────


def _creates(client: _Client) -> dict[str, Any]:
    """Each create, ready to call, with everything it reads in place."""

    async def work_order() -> str:
        return await rt._create_work_order_impl(PROPERTY, "Kitchen sink leaking")

    async def charge() -> str:
        return await _charge(client)()

    async def payment() -> str:
        return await _payment()()

    async def upload() -> str:
        from helixis.db.repositories import docs_links

        with (
            patch.object(docs_links, "list_documents", new=AsyncMock(return_value=[_doc()])),
            patch("helixis.db.engine.get_pool", return_value=object()),
            patch("helixis.storage.download_object", new=AsyncMock(return_value=b"%PDF-1.4")),
        ):
            return await rt._upload_file_impl("addendum", "lease", LEASE)

    return {"work order": work_order, "charge": charge, "payment": payment, "upload": upload}


@pytest.mark.asyncio
@pytest.mark.parametrize("which", ["work order", "charge", "payment", "upload"])
@pytest.mark.parametrize(
    "error,status",
    [
        (RentvineAPIError(400, Failure.BAD_REQUEST, False, "bad"), "failed_retryable"),
        (RentvineAPIError(500, Failure.UNDOCUMENTED, True, "server"), "abandoned_unknown"),
    ],
    ids=["rejected", "may-have-landed"],
)
async def test_a_rejected_create_is_retryable_and_an_ambiguous_one_never_is(
    monkeypatch: pytest.MonkeyPatch, which: str, error: RentvineAPIError, status: str
) -> None:
    """A 4xx provably wrote nothing, so the same request may be tried again.
    A 5xx may have written, so it is held for review: a create sent twice is
    a second work order or a second charge (gotcha 21's shape)."""
    from helixis.agents import idempotency as idem

    from .test_idempotency_site_dedup import FakeStore

    async def approve(**_kw: object) -> bool:
        return True

    store = FakeStore()
    monkeypatch.setattr(idem, "store_from_pool", lambda: store)
    monkeypatch.setattr(idem, "confirm_or_abort", approve)
    client = _Client(post_error=error)
    with ExitStack() as stack:
        _patches(stack, client)
        reply = await _creates(client)[which]()
    (row,) = store._rows.values()
    assert row.status == status, (which, reply)
    if status == "failed_retryable":
        assert "Nothing was changed in Rentvine" in reply


@pytest.mark.asyncio
async def test_an_upload_attached_to_another_record_of_the_same_kind_is_a_review() -> None:
    client = _Client(
        post_response={
            "file": {"fileID": "77"},
            "attachment": {"objectTypeID": "4", "objectID": "1"},
        }
    )
    reply, _ = await _upload(client, docs=[_doc()])
    assert "couldn't confirm it in Rentvine" in reply


# ── the client's one write method, for real ─────────────────────────────────


@pytest.mark.asyncio
async def test_the_upload_sends_multipart_with_its_declared_query() -> None:
    import httpx

    from .test_rentvine_sync import _client

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"file": {"fileID": "77"}})

    client = _client(handler)
    query = {"objectTypeID": 4, "objectID": LEASE, "includes": "attachment"}
    await client.post(
        writes.FILES_PATH, files={"file": ("a.pdf", b"%PDF", "application/pdf")}, query=query
    )
    (request,) = seen
    assert request.method == "POST" and request.url.path.endswith("/files")
    assert dict(request.url.params) == {
        "objectTypeID": "4",
        "objectID": str(LEASE),
        "includes": "attachment",
    }
    assert request.headers["content-type"].startswith("multipart/form-data")
    assert b"%PDF" in request.content
    # A filter the spec does not declare never leaves the process.
    from helixis.rentvine import transport

    with pytest.raises(transport.UnknownQueryParameterError):
        await client.post(
            writes.FILES_PATH, files={"file": ("a", b"x", "text/plain")}, query={"notDeclared": 1}
        )
    assert len(seen) == 1
    # Exactly one body.
    with pytest.raises(ValueError):
        await client.post(writes.FILES_PATH, query=query)
    with pytest.raises(ValueError):
        await client.post(writes.FILES_PATH, json_body={}, files={"file": ("a", b"x", "t")})
    await client.aclose()
