"""What the four Rentvine creates send, and what reads them back.

Founder decision 2026-10-03 (``docs/decisions.md``): create a work order, upload
a file, post a lease charge and record a lease payment, beside the one status
write (``work_orders.py``). The agent tools that gate them live in
``agents/rentvine_tools.py``; the bodies, paths and the assumptions under them
live HERE, because ``scripts/rentvine_unverified.py`` scans this package for the
Phase 8 work list and an assumption recorded anywhere else is one nobody
settles.

What is measured and what is not:

* Work-order create: the body ``scripts/rentvine_seed_sandbox.py`` sent to the
  sandbox (2026-09-24), which the live validator accepted.
* Lease payment: the body ``scripts/rentvine_test_records.py`` posted
  (2026-10-02): a $100 payment that came back as a type-2 transaction.
* Lease charge and file upload: the captured spec only (``@unverified`` below).

⚠ **NEITHER MONEY WRITE MOVES MONEY** (terms rule 15). A payment here RECORDS a
payment the manager already received (a check, a money order); nothing in the
body charges a card or pulls an ACH debit, and the spec offers no field that
would. A charge adds a line to what a tenant owes.

⚠ **AND NEITHER CAN BE UNDONE FROM HERE.** The documented surface has no void
for a lease charge or a lease payment (``transaction.isVoided`` is a state no
operation sets), so each is as permanent as Buildium's payment, and the confirm
card says so.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from helixis.rentvine import mapper

CREATE_WORK_ORDER_PATH = "/maintenance/work-orders"
LEASE_PATH = "/leases/{leaseID}"
UNIT_PATH = "/properties/{propertyID}/units/{unitID}"
ACCOUNTS_PATH = "/accounting/accounts"
LEASE_CHARGE_PATH = "/accounting/leases/{leaseID}/charges"
LEASE_PAYMENT_PATH = "/accounting/leases/{leaseID}/payments"
FILES_PATH = "/files"

#: Rentvine's fixed PRIMARY work-order statuses (spec T1 ``primary_statuses``):
#: 1 Pending, 2 Open, 3 Closed, 4 On Hold. Each account names its own statuses
#: on top, so a new work order takes the account's status whose primary is Open.
PRIMARY_OPEN = 2

#: Work-order priority, as the create declares it (``priorityID``).
PRIORITY_ID: dict[str, str] = {"low": "1", "medium": "2", "high": "3"}

#: What a file can be attached to, from the spec's Object Types table.
ATTACH_OBJECT_TYPE: dict[str, int] = {"property": 6, "unit": 7, "lease": 4, "work order": 16}


def positive_amount(value: Any) -> Decimal | None:
    """``value`` as dollars and cents, or ``None`` when it is not a positive
    amount. Money goes to Rentvine as a decimal STRING (``"1000.00"``)."""
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None
    return amount if amount.is_finite() and amount > 0 else None


def open_status_id(rows: list[dict[str, Any]]) -> str | None:
    """The account's status for a NEW work order: the first whose primary
    status is Open, or ``None`` when the account has none (the caller refuses
    rather than guessing one)."""
    for row in rows:
        if mapper._int(row.get("primaryWorkOrderStatusID")) == PRIMARY_OPEN:
            ident = row.get("workOrderStatusID")
            if ident not in (None, ""):
                return str(ident)
    return None


def unit_is_vacant(unit: dict[str, Any]) -> bool | None:
    """Whether the unit has no current lease, or ``None`` when the record
    cannot say.

    The unit view carries no ``isVacant`` (the create requires one), so its
    ``leaseID`` is read as the current lease. Measured 2026-10-03 on the
    per-property unit list: it agreed with the mirror's Active leases on 57 of
    57 units (8 with one, 49 without).
    """
    # @unverified the single-unit GET carries the same leaseID meaning as the
    # list, and a unit whose lease CLOSED clears it. The sandbox settles both:
    # create a work order on a unit whose lease ended and re-read isVacant.
    if "leaseID" not in unit:
        return None
    return mapper._int(unit.get("leaseID")) in (None, 0)


def work_order_body(
    *,
    property_id: int,
    unit_id: int | None,
    description: str,
    status_id: str,
    priority_id: str,
    is_vacant: bool,
    vendor_id: int | None,
) -> dict[str, Any]:
    """The create body, in the shape the sandbox seed's live creates used.

    ⚠ Every flag that mails somebody is FALSE (``sendEmail``,
    ``sendVendorNotification``, ``sendReview``): a message to a vendor, tenant
    or owner is its own confirmed action (terms rule 14), never a side effect of
    creating a record. Shared with neither portal and not owner-approved, since
    Occupella knows neither.
    """
    body: dict[str, Any] = {
        "propertyID": str(property_id),
        "workOrderStatusID": status_id,
        "priorityID": priority_id,
        "isOwnerApproved": False,
        "isVacant": is_vacant,
        "description": description,
        "isSharedWithTenant": "0",
        "isSharedWithOwner": False,
        "sendEmail": False,
        "sendVendorNotification": False,
        "sendReview": False,
    }
    if unit_id is not None:
        body["unitID"] = str(unit_id)
    if vendor_id is not None:
        body["vendorContactID"] = str(vendor_id)
    return body


def created_work_order_id(response: Any) -> int | None:
    """The new work order's id from the create's response.

    The spec says the create "returns the full work order detail (same shape as
    GET)", i.e. ``{"workOrder": {...}}``; the seed read the id from either
    shape, and so does this.
    """
    # @unverified which envelope the create returns. The founder's first
    # sandbox create settles it: the tool's read-back re-GETs this id.
    if not isinstance(response, dict):
        return None
    record = response.get("workOrder")
    record = record if isinstance(record, dict) else response
    return mapper._int(record.get("workOrderID"))


def income_accounts(rows: list[dict[str, Any]]) -> dict[int, str]:
    """``{accountID: name}`` for every ACTIVE income account: what a lease
    charge may post against (the spec: "an income-type account")."""
    out: dict[int, str] = {}
    for row in rows:
        kind = mapper.ACCOUNT_TYPE.get(mapper._int(row.get("accountTypeID")) or -1)
        ident = mapper._int(row.get("accountID"))
        if kind and kind[0] == "Income" and ident is not None and _truthy(row.get("isActive")):
            out[ident] = str(row.get("name") or f"account {ident}").strip()
    return out


def resolve_account(accounts: dict[int, str], wanted: str) -> int | None:
    """The income account ``wanted`` names (case-insensitive), or its id."""
    needle = str(wanted).strip().lower()
    for ident, name in accounts.items():
        if name.lower() == needle or str(ident) == needle:
            return ident
    return None


def charge_body(*, date: str, amount: Decimal, account_id: int, description: str) -> dict[str, Any]:
    """The lease-charge body: the spec's four required fields.

    The spec also says Rentvine may add a management-fee and a sales-tax charge
    on its own when the account is set up for them, which the confirm card
    states.
    """
    # @unverified this body has never been posted live. The founder's $1
    # sandbox charge settles it (the read-back finds it on the lease).
    return {
        "datePosted": date,
        "amount": f"{amount:.2f}",
        "chargeAccountID": str(account_id),
        "description": description,
    }


def active_lease_tenants(lease_body: Any) -> list[tuple[str, str]]:
    """``[(leaseTenantID, name)]`` for the ACTIVE tenants on a
    ``getLease?includes=tenants`` body, primary first."""
    items = lease_body.get("tenants") if isinstance(lease_body, dict) else None
    out: list[tuple[str, str, bool]] = []
    for item in items or []:
        link = item.get("leaseTenant") if isinstance(item, dict) else None
        if not isinstance(link, dict) or not _truthy(link.get("isActive")):
            continue
        ident = link.get("leaseTenantID")
        if ident in (None, ""):
            continue
        contact = _dict(item.get("contact"))
        name = str(
            contact.get("name")
            or " ".join(p for p in (contact.get("firstName"), contact.get("lastName")) if p)
            or f"tenant {ident}"
        ).strip()
        out.append((str(ident), name, _truthy(link.get("isPrimary"))))
    out.sort(key=lambda t: not t[2])
    return [(ident, name) for ident, name, _ in out]


def payment_body(
    *,
    lease_id: int,
    lease_tenant_id: str,
    amount: Decimal,
    date: str,
    reference: str | None,
    description: str,
) -> dict[str, Any]:
    """The lease-payment body, in the shape ``rentvine_test_records.py`` posted
    live: applied to the oldest open charges, into the portfolio's default trust
    account, and NO receipt email (a message to a tenant is its own confirmed
    action, terms rule 14)."""
    return {
        "leaseID": str(lease_id),
        "leaseTenantID": lease_tenant_id,
        "amount": f"{amount:.2f}",
        "datePosted": date,
        "applyAutomatically": True,
        "hasPrepayment": False,
        "bankAccountID": None,
        "emailReceipt": False,
        "description": description,
        "reference": reference,
    }


def created_transaction(response: Any) -> dict[str, Any] | None:
    """The ``transaction`` a charge or payment create returned."""
    txn = response.get("transaction") if isinstance(response, dict) else None
    return txn if isinstance(txn, dict) else None


def lease_has_transaction(lease_body: Any, transaction_id: int, amount: Decimal) -> bool:
    """Whether a ``getLease?includes=transactions`` re-read carries the posted
    transaction at the posted amount: the read-back for both money writes
    (CLAUDE.md rule 6: a 2xx is not the write)."""
    items = lease_body.get("transactions") if isinstance(lease_body, dict) else None
    for item in items or []:
        if not isinstance(item, dict):
            continue
        txn = _dict(item.get("transaction")) or item
        if mapper._int(txn.get("transactionID")) != transaction_id:
            continue
        got = positive_amount(str(txn.get("amount") or "").lstrip("-"))
        return got == amount and not _truthy(txn.get("isVoided"))
    return False


def lease_label(lease_body: Any) -> str | None:
    """'Unit 4B at Redbud (Aaron Tillman)' from a ``getLease?includes=tenants``
    body: the SERVER's description of the lease, for the confirm card."""
    if not isinstance(lease_body, dict):
        return None
    lease = _dict(lease_body.get("lease"))
    names = ", ".join(name for _, name in active_lease_tenants(lease_body))
    unit = _dict(lease_body.get("unit"))
    prop = _dict(lease_body.get("property"))
    place = " at ".join(
        p for p in (f"Unit {unit['name']}" if unit.get("name") else None, prop.get("name")) if p
    )
    ident = lease.get("leaseID")
    head = place or (f"Lease {ident}" if ident else "")
    if not head:
        return None
    return f"{head} ({names})" if names else head


def file_attachment(response: Any) -> tuple[int | None, dict[str, Any] | None]:
    """``(fileID, attachment)`` from an upload's response.

    The spec documents no GET for a file or an attachment, so the response is
    the only read-back there is.
    """
    # @unverified the multipart field name (``file``), and that
    # includes=attachment returns the attachment beside the file. Never
    # uploaded live; the founder's first sandbox upload settles both.
    if not isinstance(response, dict):
        return None, None
    record = _dict(response.get("file"))
    attachment = response.get("attachment")
    return mapper._int(record.get("fileID")), attachment if isinstance(attachment, dict) else None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true"}


__all__ = [
    "ACCOUNTS_PATH",
    "ATTACH_OBJECT_TYPE",
    "CREATE_WORK_ORDER_PATH",
    "FILES_PATH",
    "LEASE_CHARGE_PATH",
    "LEASE_PATH",
    "LEASE_PAYMENT_PATH",
    "PRIMARY_OPEN",
    "PRIORITY_ID",
    "UNIT_PATH",
    "active_lease_tenants",
    "charge_body",
    "created_transaction",
    "created_work_order_id",
    "file_attachment",
    "income_accounts",
    "lease_has_transaction",
    "lease_label",
    "open_status_id",
    "payment_body",
    "positive_amount",
    "resolve_account",
    "unit_is_vacant",
    "work_order_body",
]
