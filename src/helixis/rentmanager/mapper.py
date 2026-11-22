"""Rent Manager records -> mirror rows. Pure: a record in, mirror columns out.

No I/O, so every mapping decision is testable against a synthetic fixture,
which matters because none of this has been run against Rent Manager. Each
row carries only the shared writer's columns for its table, plus
``buildium_id`` (the shared id column, ADR 0010) and ``raw``:
``pms.mirror`` ignores any other key without a sound, so a misspelled column
would drop data silently (``test_rentmanager_mapper`` holds every row to the
writer's columns).

UNKNOWN IS NEVER ZERO (rule 14)
    A value Rent Manager did not send, or that does not parse, is ``None``:
    never 0, "" or False. Rent is the clearest case: it is not on Rent
    Manager's lease (a recurring charge, RM-11), so every lease is written
    with rent unknown, and the readers already say "rent not read yet".

THE CUSTOMER'S OWN WORDS
    Work-order status and priority names are the customer's (each account
    names its own), stored as they come, never translated into Buildium's
    vocabulary (design: spine mapping). A lease carries no status at all in
    Rent Manager; the mirror's Active/Past/Future is derived from its move
    dates (:func:`lease_status`), the one meaning every lease reader filters on.

Field names are the help scrape's (T1c) unless a comment says they are a guess.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from helixis.rentmanager.resources import (
    BILLS,
    CHANGE_FIELD,
    GL_ACCOUNTS,
    LEASES,
    OWNERS,
    PROPERTIES,
    TENANTS,
    UNITS,
    VENDORS,
    WORK_ORDER_PRIORITIES,
    WORK_ORDER_STATUSES,
    WORK_ORDERS,
)

# ---------------------------------------------------------------------------
# Coercions. Each returns None for absent, blank or unreadable: never a guess.
# ---------------------------------------------------------------------------


def _int(value: Any) -> int | None:
    """An id as an ``int``, or ``None``. A ``bool`` is not an id."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text or not (text.isdigit() or (text[0] == "-" and text[1:].isdigit())):
        return None
    return int(text)


def record_id(obj: dict[str, Any], key: str) -> int | None:
    """The record's id under ``key``, or ``None``."""
    return _int(obj.get(key))


def _str(value: Any) -> str | None:
    """A trimmed string, or ``None`` for absent or blank."""
    if value is None or isinstance(value, bool | dict | list):
        return None
    text = str(value).strip()
    return text or None


def _flag(value: Any) -> bool | None:
    """A yes/no, or ``None`` when it is not one (``bool("false")`` is True)."""
    if value is True or value is False:
        return value
    text = str(value).strip().lower() if value is not None else ""
    if text in {"true", "1"}:
        return True
    if text in {"false", "0"}:
        return False
    return None


def _date(value: Any) -> date | None:
    """A calendar date from Rent Manager's ``2026-04-29T15:57:12-04:00``, or
    ``None``. The calendar part is the date: a lease term or a due date is a
    day, not an instant (gotcha 97). Never the string: the mirror's
    ``timestamptz`` and ``date`` columns refuse one and abort the write
    (gotcha 148)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _str(value)
    if text is None:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _instant(value: Any) -> datetime | None:
    """An aware ``datetime``, or ``None``."""
    if value is None or isinstance(value, bool):
        return None
    text = _str(value)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    # @unverified RM-20 a timestamp without a zone is read as UTC; the help
    # scrape's samples carry an offset. Settled by one read of each resource
    # compared against Rent Manager's screen.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def changed_at(obj: dict[str, Any]) -> datetime | None:
    """When Rent Manager last changed the record (``CHANGE_FIELD``), or
    ``None`` when it did not say."""
    return _instant(obj.get(CHANGE_FIELD))


def _money(value: Any) -> Decimal | None:
    """A ``Decimal`` for a ``numeric`` column, or ``None``. Through ``str``,
    never ``float``, so ``1450.1`` stays exact."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    text = _str(value)
    if text is None:
        return None
    try:
        amount = Decimal(text)
    except InvalidOperation:
        return None
    return amount if amount.is_finite() else None


def _one(value: Any) -> dict[str, Any] | None:
    """An embedded object: a record, or a list holding exactly one."""
    if isinstance(value, list) and len(value) == 1:
        value = value[0]
    return value if isinstance(value, dict) else None


def _many(value: Any) -> list[dict[str, Any]] | None:
    """An embedded list of records, or ``None`` when it did not come back or
    came back in a shape not understood. An empty list is Rent Manager saying
    there are none; absent or unreadable is unknown. ⚠ Never filtered down:
    a list of bare ids read as "no records" would turn an occupied unit
    vacant and an owner into one who owns nothing."""
    if isinstance(value, dict):
        return [value]
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        return None
    return list(value)


def _primary(items: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    """The one marked ``IsPrimary``, else the only one, else ``None``."""
    if not items:
        return None
    marked = [i for i in items if _flag(i.get("IsPrimary")) is True]
    if len(marked) == 1:
        return marked[0]
    return items[0] if len(items) == 1 else None


def _phone(value: Any) -> str | None:
    """A phone from a ``PhoneNumbers`` embed: the primary, else the first."""
    numbers = _many(value) or []
    ordered = sorted(numbers, key=lambda n: _flag(n.get("IsPrimary")) is not True)
    for number in ordered:
        found = _str(number.get("PhoneNumber"))
        if found:
            return found
    return None


def _single(values: Iterable[int | None]) -> int | None:
    """The one distinct id, or ``None`` when there is none or more than one:
    two answers that disagree are unknown, never the first of them."""
    found = {v for v in values if v is not None}
    return found.pop() if len(found) == 1 else None


# ---------------------------------------------------------------------------
# Entities.
# ---------------------------------------------------------------------------


def map_property(obj: dict[str, Any]) -> dict[str, Any]:
    """A ``Properties`` record -> ``helixis_properties``."""
    # @unverified RM-20 the address fields inside the PrimaryAddress embed
    # (the help scrape does not document AddressModel; Street/City/State/
    # PostalCode are the names its other models use). Settled by one property
    # read with the embed, against Rent Manager's screen.
    address = _one(obj.get("PrimaryAddress")) or {}
    street = _str(address.get("Street"))
    return {
        "buildium_id": _int(obj.get(PROPERTIES.key)),
        "name": _str(obj.get("Name")),
        # Rent Manager's own word for the kind of property, as it comes.
        "rental_sub_type": _str(obj.get("PropertyType")),
        "address_line1": street.splitlines()[0].strip() if street else None,
        "city": _str(address.get("City")),
        "state": _str(address.get("State")),
        "postal_code": _str(address.get("PostalCode")),
        "is_active": _flag(obj.get("IsActive")),
        "raw": obj,
    }


def map_unit(obj: dict[str, Any]) -> dict[str, Any]:
    """A ``Units`` record -> ``helixis_units``."""
    # @unverified RM-18 occupancy from the CurrentOccupants embed (a list of
    # tenants: empty is vacant, absent is unknown) and market rent from the
    # CurrentMarketRent embed's Amount. Settled by one unit read with those
    # embeds, against Rent Manager's screen.
    occupants = _many(obj.get("CurrentOccupants"))
    market = _one(obj.get("CurrentMarketRent")) or {}
    return {
        "buildium_id": _int(obj.get(UNITS.key)),
        "property_id": _int(obj.get("PropertyID")),
        "unit_number": _str(obj.get("Name")),
        "market_rent": _money(market.get("Amount")),
        "is_occupied": None if occupants is None else bool(occupants),
        "raw": obj,
    }


def map_tenant(obj: dict[str, Any]) -> dict[str, Any]:
    """A ``Tenants`` record (the account that holds the ledger) ->
    ``helixis_tenants``. Email and phone are the primary contact's; other
    contacts are not rows here, because their ids are another sequence."""
    first, last = _str(obj.get("FirstName")), _str(obj.get("LastName"))
    if first is None and last is None:
        # A company account carries only its Name.
        last = _str(obj.get("Name"))
    # @unverified RM-20 the contact's IsPrimary and Email, and the nested
    # PhoneNumbers embed's PhoneNumber. Settled by one tenant read with the
    # Contacts embed, against Rent Manager's screen.
    contact = _primary(_many(obj.get("Contacts"))) or {}
    return {
        "buildium_id": tenant_id(obj),
        "first_name": first,
        "last_name": last,
        "email": _str(contact.get("Email")),
        "phone": _phone(contact.get("PhoneNumbers")),
        "raw": obj,
    }


def map_owner(obj: dict[str, Any]) -> dict[str, Any]:
    """An ``Owners`` record -> ``helixis_owners``.

    ``property_ids`` comes from the ``Ownerships`` embed. An empty list is
    Rent Manager saying the owner owns nothing; an embed that did not come
    back is unknown (``None``), never "owns nothing".
    """
    contact = _one(obj.get("Contact")) or {}
    # @unverified RM-20 an ownership's PropertyID (the help scrape does not
    # document OwnerContractModel), and whether past ownerships are listed
    # too. Settled by one owner read with the embed, against the screen.
    ownerships = _many(obj.get("Ownerships"))
    owned = (
        None
        if ownerships is None
        else sorted({p for o in ownerships if (p := _int(o.get("PropertyID"))) is not None})
    )
    return {
        "buildium_id": _int(obj.get(OWNERS.key)),
        "first_name": _str(contact.get("FirstName")),
        "last_name": _str(contact.get("LastName")),
        # The account's name, as Rentvine's owners carry theirs.
        "company_name": _str(obj.get("Name")),
        "email": _str(contact.get("Email")),
        "phone": _phone(obj.get("PhoneNumbers")),
        "property_ids": owned,
        "raw": obj,
    }


def map_vendor(obj: dict[str, Any]) -> dict[str, Any]:
    """A ``Vendors`` record -> ``helixis_vendors``. Rent Manager's vendor has
    no company/person flag and its category is a word with no id, so those
    are unknown rather than guessed."""
    contact = _one(obj.get("Contact")) or {}
    return {
        "buildium_id": _int(obj.get(VENDORS.key)),
        "name": _str(obj.get("Name")),
        "is_company": None,
        "first_name": _str(contact.get("FirstName")),
        "last_name": _str(contact.get("LastName")),
        "company_name": None,
        "is_active": _flag(obj.get("IsActive")),
        "category_id": None,
        "category_name": _str(obj.get("Category")),
        "phone": _phone(obj.get("PhoneNumbers")),
        "email": _str(contact.get("Email")),
        "raw": obj,
    }


#: Buildium's five account types, the words every GL reader groups by.
GL_TYPES: tuple[str, ...] = ("Asset", "Liability", "Equity", "Income", "Expense")


def map_gl_account(obj: dict[str, Any]) -> dict[str, Any]:
    """A ``GLAccounts`` record -> ``helixis_gl_accounts``.

    ``sub_type`` keeps Rent Manager's own account-type word. ``type`` is that
    word only when it IS one of :data:`GL_TYPES`; any other word is unknown
    there (``None``), never forced into one of the five.
    """
    # @unverified RM-25 GLAccountType's values (the help scrape names the enum,
    # not its values) and that Reference is the account number. Settled by one
    # read of the chart of accounts, against Rent Manager's screen.
    word = _str(obj.get("GLAccountType"))
    kind = next((t for t in GL_TYPES if word is not None and word.lower() == t.lower()), None)
    return {
        "buildium_id": _int(obj.get(GL_ACCOUNTS.key)),
        "account_number": _str(obj.get("Reference")),
        "name": _str(obj.get("Name")),
        "type": kind,
        "sub_type": word,
        "is_active": _flag(obj.get("IsActive")),
        "is_bank_account": None,
        "parent_gl_account_id": _int(obj.get("ParentGLAccountID")),
        "raw": obj,
    }


def bill_is_reversed(obj: dict[str, Any]) -> bool:
    """A bill Rent Manager has reversed is not money anybody owes."""
    # @unverified RM-24 a set ReversalDate means the bill was reversed.
    # Settled by one reversed bill read against Rent Manager's screen.
    return _str(obj.get("ReversalDate")) is not None


def map_bill(obj: dict[str, Any]) -> dict[str, Any]:
    """A ``Bills`` record -> ``helixis_bills`` (the caller leaves out a
    reversed one: :func:`bill_is_reversed`).

    Read-only money. Its lines are not mirrored: the help scrape names the
    ``BillDetails`` embed but not its fields, and spend attributed to a guessed
    field is a wrong number, so ``helixis_bill_lines`` stays unfilled and its
    readers decline. ``work_order_id`` is unknown, never "none".
    """
    # @unverified RM-24 IsFullyAllocated means paid, and the Vendor embed
    # names the payee (empty for a bill to an owner or a tenant). Settled by a
    # paid, an unpaid and a reversed bill against Rent Manager's screen.
    vendor = _one(obj.get("Vendor")) or {}
    return {
        "buildium_id": _int(obj.get(BILLS.key)),
        "vendor_id": _int(vendor.get("VendorID")),
        "bill_date": _date(obj.get("TransactionDate")),
        "due_date": _date(obj.get("DueDate")),
        "total_amount": _money(obj.get("Amount")),
        "is_paid": _flag(obj.get("IsFullyAllocated")),
        "reference_number": _str(obj.get("Reference")),
        "work_order_id": None,
        "raw": obj,
    }


def lookup_names(rows: Iterable[dict[str, Any]], key: str) -> dict[int, str]:
    """A ServiceManager lookup (statuses, priorities) as ``{id: name}``."""
    out: dict[int, str] = {}
    for row in rows:
        rid, name = _int(row.get(key)), _str(row.get("Name"))
        if rid is not None and name is not None:
            out[rid] = name
    return out


def status_names(rows: Iterable[dict[str, Any]]) -> dict[int, str]:
    """``ServiceManagerStatuses`` as ``{StatusID: the customer's name}``."""
    return lookup_names(rows, WORK_ORDER_STATUSES.key)


def priority_names(rows: Iterable[dict[str, Any]]) -> dict[int, str]:
    """``ServiceManagerPriorities`` as ``{PriorityID: the customer's name}``."""
    return lookup_names(rows, WORK_ORDER_PRIORITIES.key)


def work_order_status_known(obj: dict[str, Any], statuses: Mapping[int, str]) -> bool:
    """Whether this work order's status resolves to a word. Most "open work"
    readers drop a work order with no status, so one that does not resolve
    keeps the word it had and its table is reported incomplete (sync)."""
    status_id = _int(obj.get("StatusID"))
    return status_id is not None and status_id in statuses


def map_work_order(
    obj: dict[str, Any], statuses: Mapping[int, str], priorities: Mapping[int, str]
) -> dict[str, Any]:
    """A ``ServiceManagerIssues`` record -> ``helixis_work_orders``.

    Status and priority are the customer's own words from the two lookups
    (RM-14). The issue has no scalar property or unit: they come from its
    linked properties and units, and a link that names more than one is
    unknown, never the first of them.
    """
    # @unverified RM-14 StatusID and PriorityID resolve through the
    # ServiceManagerStatuses and ServiceManagerPriorities lookups. Settled by
    # reading each lookup whole, as the design's RM-14 row says.
    status_id, priority_id = _int(obj.get("StatusID")), _int(obj.get("PriorityID"))
    # @unverified RM-20 the LinkedUnits and LinkedProperties embeds carry
    # UnitID and PropertyID. Settled by one work order read with both embeds,
    # against Rent Manager's screen.
    units = _many(obj.get("LinkedUnits")) or []
    properties = _many(obj.get("LinkedProperties")) or []
    unit_id = _single(_int(u.get("UnitID")) for u in units)
    property_id = _single(
        [_int(p.get("PropertyID")) for p in properties] + [_int(u.get("PropertyID")) for u in units]
    )
    return {
        "buildium_id": _int(obj.get(WORK_ORDERS.key)),
        # Buildium's task link; Rent Manager has no such record.
        "task_id": None,
        "property_id": property_id,
        "unit_id": unit_id,
        "title": _str(obj.get("Title")),
        "status": statuses.get(status_id) if status_id is not None else None,
        "priority": priorities.get(priority_id) if priority_id is not None else None,
        "category_id": _int(obj.get("CategoryID")),
        "assigned_to_user_id": _int(obj.get("AssignedToUserID")),
        # What "stalled" reads (migration 0105): when the issue last changed.
        "pms_updated_at": changed_at(obj),
        "raw": obj,
    }


# ---------------------------------------------------------------------------
# Leases, and what hangs off them.
# ---------------------------------------------------------------------------

#: The words the mirror's lease readers filter on (``status = 'Active'``).
LEASE_ACTIVE, LEASE_PAST, LEASE_FUTURE = "Active", "Past", "Future"

#: What a lease-tenant link is written with: the word every link reader
#: reads as "lives there now". Only active leases get a link (Rentvine's rule).
LEASE_TENANT_STATUS = "Current"


def lease_id(obj: dict[str, Any]) -> int | None:
    return _int(obj.get(LEASES.key))


def lease_tenant_id(obj: dict[str, Any]) -> int | None:
    return _int(obj.get("TenantID"))


def lease_move_in(obj: dict[str, Any]) -> date | None:
    return _date(obj.get("MoveInDate"))


def lease_status(obj: dict[str, Any], today: date) -> str | None:
    """Active, Past or Future from the lease's move dates, or ``None``.

    Rent Manager's lease carries no status field, and the lease readers
    filter on one, so ``None`` for every lease would read as "no active
    leases" (rule 14). Moved out before today is Past; moving out today or
    later, or never, after a move-in on or before today is Active; a move-in
    after today is Future; no readable move-in is unknown.
    """
    # @unverified RM-22 a lease's status is its move dates: the lease model
    # has none. Settled by one current, one past and one future lease,
    # compared against Rent Manager's screen.
    move_out = _date(obj.get("MoveOutDate"))
    if move_out is not None and move_out < today:
        return LEASE_PAST
    move_in = lease_move_in(obj)
    if move_in is None:
        return None
    return LEASE_FUTURE if move_in > today else LEASE_ACTIVE


def lease_unit_id(obj: dict[str, Any]) -> int | None:
    """The lease's unit: the scalar ``UnitID`` the help scrape lists and the
    ``Unit`` embed the design names. Two that disagree are unknown."""
    # @unverified RM-18 the lease's unit; the help scrape lists a scalar
    # UnitID, the research notes say only the Unit embed carries it. Settled by
    # one lease read with the embed, against Rent Manager's screen.
    unit = _one(obj.get("Unit")) or {}
    return _single((_int(obj.get("UnitID")), _int(unit.get("UnitID"))))


def map_lease(obj: dict[str, Any], today: date) -> dict[str, Any]:
    """A ``Leases`` record -> ``helixis_leases``, without ``security_deposit``
    (the deposit is the tenant account's: :func:`current_leases`)."""
    # @unverified RM-11 rent is a recurring charge, not on the lease, so it is
    # unknown here, never 0. Settled by one tenant's recurring charges against
    # Rent Manager's screen.
    return {
        "buildium_id": lease_id(obj),
        "property_id": _int(obj.get("PropertyID")),
        "unit_id": lease_unit_id(obj),
        "status": lease_status(obj, today),
        "lease_from_date": _date(obj.get("StartDate")),
        "lease_to_date": _date(obj.get("EndDate")),
        "rent": None,
        "raw": obj,
    }


def lease_move_out(obj: dict[str, Any]) -> date | None:
    """The move-out a turnover opens on: the actual ``MoveOutDate``, else the
    ``ExpectedMoveOutDate`` from a notice, else ``None``. A move-out is not
    the lease's end date."""
    # @unverified RM-18 MoveOutDate and ExpectedMoveOutDate are the move-out
    # and the notice's expected date. Settled by one lease with a notice,
    # against Rent Manager's screen.
    return _date(obj.get("MoveOutDate")) or _date(obj.get("ExpectedMoveOutDate"))


def lease_tenant_link(obj: dict[str, Any], today: date) -> dict[str, Any] | None:
    """The ``helixis_lease_tenants`` row for an ACTIVE lease, or ``None``.

    One tenant account per lease. Only active leases are linked: every link
    reader reads a link as a tenant who lives there now.
    """
    lid, tid = lease_id(obj), lease_tenant_id(obj)
    if lid is None or tid is None or lease_status(obj, today) != LEASE_ACTIVE:
        return None
    return {
        "lease_buildium_id": lid,
        "tenant_buildium_id": tid,
        "status": LEASE_TENANT_STATUS,
        "move_in_date": lease_move_in(obj),
    }


def current_leases(leases: Iterable[dict[str, Any]], today: date) -> dict[int, int]:
    """``{TenantID: the lease that account is on now}``.

    A tenant account's deposit and balance are held per ACCOUNT, so each goes
    on one lease only: the account's only lease, or the one it moved into most
    recently (on or before today). A tenant whose leases cannot be ordered (a
    move-in missing, or two on the same day) has none here: unknown, never the
    first of them.
    """
    by_tenant: dict[int, dict[int, date | None]] = {}
    for obj in leases:
        lid, tid = lease_id(obj), lease_tenant_id(obj)
        if lid is not None and tid is not None:
            by_tenant.setdefault(tid, {})[lid] = lease_move_in(obj)
    out: dict[int, int] = {}
    for tid, moves in by_tenant.items():
        if len(moves) == 1:
            out[tid] = next(iter(moves))
            continue
        if any(m is None for m in moves.values()):
            continue
        started = {lid: m for lid, m in moves.items() if m is not None and m <= today}
        if not started:
            continue
        latest = max(started.values())
        at_latest = [lid for lid, m in started.items() if m == latest]
        if len(at_latest) == 1:
            out[tid] = at_latest[0]
    return out


def tenant_id(obj: dict[str, Any]) -> int | None:
    return _int(obj.get(TENANTS.key))


def tenant_deposit(obj: dict[str, Any]) -> Decimal | None:
    """The deposit Rent Manager holds for this tenant account, or ``None``."""
    # @unverified RM-23 SecurityDepositHeld (with its embed) is the deposit
    # held now. Settled by one tenant with a deposit, against the screen.
    return _money(obj.get("SecurityDepositHeld"))


def tenant_balance(obj: dict[str, Any]) -> Decimal | None:
    """What this tenant account owes now (the ``Balance`` embed), or ``None``
    when it did not come through: unknown, never $0."""
    # @unverified RM-23 Balance (with its embed) is what the account owes now,
    # a credit as a negative. Settled by one tenant with a balance, against
    # Rent Manager's ledger screen.
    return _money(obj.get("Balance"))


#: Aging buckets by days since the charge posted: Buildium's four, in order.
_BUCKETS: tuple[tuple[str, int | None], ...] = (
    ("balance_0_30", 30),
    ("balance_31_60", 60),
    ("balance_61_90", 90),
    ("balance_over_90", None),
)


def aging(obj: dict[str, Any], total: Decimal, today: date) -> dict[str, Decimal] | None:
    """The four aging buckets from the account's open receivables, or
    ``None`` when they cannot be stated.

    ⚠ Only when they add up: every receivable's amount, amount paid and date
    must read, and their open parts must sum to ``total`` to the cent.
    Anything else leaves all four unknown, which readers show as no aging,
    never as "current" (rule 14).
    """
    # @unverified RM-23 an open receivable's Amount, AmountAllocated and
    # TransactionDate (the help scrape does not document OpenReceivableModel;
    # these are ChargeModel's names). Settled by one tenant with a balance,
    # against Rent Manager's ledger screen.
    receivables = obj.get("OpenReceivables")
    if not isinstance(receivables, list):
        return None
    out = {name: Decimal(0) for name, _limit in _BUCKETS}
    seen = Decimal(0)
    for item in receivables:
        if not isinstance(item, dict):
            return None
        amount, paid = _money(item.get("Amount")), _money(item.get("AmountAllocated"))
        posted = _date(item.get("TransactionDate"))
        if amount is None or paid is None or posted is None:
            return None
        owed = amount - paid
        if owed <= 0:
            continue
        seen += owed
        age = (today - posted).days
        for name, limit in _BUCKETS:
            if limit is None or age <= limit:
                out[name] += owed
                break
    if seen.quantize(Decimal("0.01")) != total.quantize(Decimal("0.01")):
        return None
    return out


def balance_row(
    tenant: dict[str, Any], lease: dict[str, Any], total: Decimal, today: date
) -> dict[str, Any]:
    """A ``helixis_lease_balances`` row: the tenant account's balance on its
    current lease (``lease`` is that lease's mapped row)."""
    buckets = aging(tenant, total, today)
    return {
        "lease_id": lease["buildium_id"],
        "property_id": lease["property_id"],
        "unit_id": lease["unit_id"],
        "total_balance": total,
        **{name: (buckets[name] if buckets else None) for name, _limit in _BUCKETS},
        "raw": {
            TENANTS.key: tenant.get(TENANTS.key),
            "Balance": tenant.get("Balance"),
            "OpenReceivables": tenant.get("OpenReceivables"),
        },
    }


__all__ = [
    "GL_TYPES",
    "LEASE_ACTIVE",
    "LEASE_FUTURE",
    "LEASE_PAST",
    "LEASE_TENANT_STATUS",
    "aging",
    "balance_row",
    "bill_is_reversed",
    "changed_at",
    "current_leases",
    "lease_id",
    "lease_move_in",
    "lease_move_out",
    "lease_status",
    "lease_tenant_id",
    "lease_tenant_link",
    "lease_unit_id",
    "lookup_names",
    "map_bill",
    "map_gl_account",
    "map_lease",
    "map_owner",
    "map_property",
    "map_tenant",
    "map_unit",
    "map_vendor",
    "map_work_order",
    "priority_names",
    "record_id",
    "status_names",
    "tenant_balance",
    "tenant_deposit",
    "tenant_id",
    "work_order_status_known",
]
