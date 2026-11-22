"""DoorLoop records -> mirror rows. Pure: no I/O, no clock, no logging.

Every field name below comes from DoorLoop's reference
(``docs/doorloop/vendor/api.doorloop.com/openapi.json``). Documented is not
observed: nobody has read a live row yet, so every place the spec is silent,
ambiguous, or could be contradicted by a live call carries an
``@unverified DL-n`` comment naming the design's work-list item that settles
it (``scripts/doorloop_unverified.py`` lists them).

IDS: DOORLOOP'S ARE TEXT, THE MIRROR'S ARE NUMBERS
    A DoorLoop id is a MongoDB ObjectId, 24 hex characters (the spec's
    ``mongoId``). Every mirror table keys its rows by a ``bigint``
    (``buildium_id``, ADR 0010), and so does every column that names another
    record (``property_id``, ``lease_buildium_id``...). So each id is written
    as :func:`mirror_id`: the first eight bytes of the SHA-256 of the
    lowercased id, cut to 53 bits.

    * **53 bits, not 63.** The web app reads ids as JavaScript numbers, which
      are exact only up to 2**53 - 1. A larger id would turn into a different
      one in a link.
    * **Fixed forever.** Changing the function re-keys every DoorLoop row:
      the old rows would never be pruned and every link would break.
    * **One way.** The DoorLoop id stays in ``raw["id"]``, which is what
      anything that calls DoorLoop back reads.
    * **A collision is refused, never written.** Two different ids with one
      mirror id in the same read would overwrite each other under
      ``UNIQUE (company_id, buildium_id)``; :func:`collisions` names them and
      the sync fails that table instead.

THE RULES EVERY MAPPER FOLLOWS (as Propertyware's)
    * **Unknown is None** (rule 14). A value DoorLoop did not send, or that
      does not parse, is ``None``, never 0, "" or False.
    * **A value this pass could not determine is LEFT OUT of the row**, so the
      mirror keeps what it had (``pms.mirror`` writes only the columns a row
      carries). That is different from ``None``: known to be unknown.
    * **A word a reader filters on is the readers' word, never DoorLoop's.**
      A lease's status (``status = 'Active'``, :data:`ACTIVE_LEASE_STATUS`),
      a lease tenant link's (:data:`LINK_STATUS`), a work order's or task's
      status (:data:`TASK_STATUS`: "open" is anything not Completed, Closed or
      Cancelled), a task's type and priority, an applicant's status
      (:data:`APPLICANT_STATUS`) and an account's type (:data:`GL_TYPE`). A
      DoorLoop word with no reader's word is ``None``, and DoorLoop's own
      stays in ``raw``.
    * Rows carry only mirror columns, plus ``buildium_id`` and ``raw``.

WHAT ``raw`` HOLDS
    The record as DoorLoop sent it, less :data:`RAW_WITHHELD`: dates of birth,
    tax information, e-payment details, and the people a record lists who are
    not its subject (emergency contacts, dependants). ``raw`` is readable by
    the Data Analyst, so it reaches a model provider, and no reader needs them.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any

#: Keys removed from ``raw`` wherever they appear (see the module docstring).
RAW_WITHHELD: frozenset[str] = frozenset(
    {"dateOfBirth", "federalTaxInfo", "ePayInfo", "emergencyContacts", "dependants"}
)

#: A DoorLoop id (the spec's ``mongoId``: ``/^[a-f\\d]{24}$/i``).
OBJECT_ID = re.compile(r"[0-9a-fA-F]{24}")

#: The largest id the web app reads exactly (JavaScript's safe integer).
MAX_MIRROR_ID = (1 << 53) - 1

#: The word every lease reader filters on (``status = 'Active'``), written for
#: a lease DoorLoop marks ``ACTIVE``.
ACTIVE_LEASE_STATUS = "Active"

#: DoorLoop's lease status that maps to :data:`ACTIVE_LEASE_STATUS`.
DOORLOOP_ACTIVE = "ACTIVE"

#: The link word every "who lives here" reader keeps
#: (``db.tenant_links.CURRENT_LINK_STATUS``; ``test_tenant_links`` holds each
#: connector to it).
LEASE_TENANT_STATUS = "Current"

#: A lease tenant's ``status`` -> the link word the readers speak
#: (``db.tenant_links``): Buildium's three.
LINK_STATUS: dict[str, str] = {
    "CURRENT": LEASE_TENANT_STATUS,
    "FUTURE": "Future",
    "PAST": "MovedOut",
}

#: The task ``type`` that is a work order.
WORK_ORDER = "WORK_ORDER"

#: The tenant ``type`` of a tenant on a lease (the other is a prospect).
LEASE_TENANT = "LEASE_TENANT"

#: The tenant ``type`` of a prospect: a lead or an applicant, never a tenant.
PROSPECT_TENANT = "PROSPECT_TENANT"

#: A task's ``status`` -> the readers' maintenance word
#: (``buildium.maintenance_status``: open is anything not Completed, Closed or
#: Cancelled, so a finished job must land on one of those exactly, or it is
#: counted as outstanding). RECEIVED is a request taken in and not started.
TASK_STATUS: dict[str, str] = {
    "NOT_STARTED": "New",
    "RECEIVED": "New",
    "IN_PROGRESS": "InProgress",
    "COMPLETED": "Completed",
    "ARCHIVED": "Closed",
}

#: A task's ``priority`` -> the readers' word, in Rentvine's case.
TASK_PRIORITY: dict[str, str] = {"LOW": "Low", "MEDIUM": "Medium", "HIGH": "High"}

#: A task's ``type`` (other than a work order) -> Buildium's ``TaskType``
#: (migration 0041: ContactRequest | ResidentRequest | Todo | RentalOwnerRequest).
TASK_TYPE: dict[str, str] = {
    "INTERNAL_TASK": "Todo",
    "TENANT_REQUEST": "ResidentRequest",
    "OWNER_REQUEST": "RentalOwnerRequest",
}

#: A prospect's ``prospectInfo.status`` -> Buildium's applicant status (the
#: words every applicant reader speaks; Rentvine's ``APPLICATION_STATUS`` maps
#: to the same). Only a prospect who has reached the application is an
#: applicant: a lead still being contacted or shown round is not, and nor is
#: a lost one, because DoorLoop does not say whether they had applied.
APPLICANT_STATUS: dict[str, str] = {
    "APPLICATION_SENT": "Draft",
    "APPLICATION_SUBMITTED": "New",
    "LEASE_OFFER_MADE": "Approved",
    "LEASE_OFFER_ACCEPTED": "Approved",
    "LEASE_SENT_FOR_SIGNATURE": "Approved",
    "CLOSED_MOVED_IN": "AddedToLease",
}

#: An account's ``type`` prefix -> Buildium's five types, what every GL reader
#: groups by (Rentvine's ``ACCOUNT_TYPE`` does the same).
GL_TYPE: dict[str, str] = {
    "ASSET_": "Asset",
    "LIABILITY_": "Liability",
    "EQUITY_": "Equity",
    "REVENUE_": "Income",
    "EXPENSE_": "Expense",
}

#: The account ``type`` that is a bank account.
BANK_ACCOUNT_TYPE = "ASSET_BANK"

#: A contact's phone, most useful first: a mobile can take a text. A fax is
#: never a phone to call.
PHONE_ORDER: tuple[str, ...] = ("Mobile", "Home", "Work", "Other")

_CENT = Decimal("0.01")

#: The mirror's aging buckets by days since the charge posted, cut as
#: Rentvine's and Propertyware's are: 0-30, 31-60, 61-90, over 90.
BUCKETS: tuple[tuple[str, int | None], ...] = (
    ("balance_0_30", 30),
    ("balance_31_60", 60),
    ("balance_61_90", 90),
    ("balance_over_90", None),
)

#: A task timestamp above this is in milliseconds (in seconds it would be
#: after the year 2286).
_MILLISECONDS_ABOVE = 10_000_000_000

_MONEY_TEXT = re.compile(r"^-?\d+(?:\.\d+)?$")


# ── ids ─────────────────────────────────────────────────────────────────


def mirror_id(value: Any) -> int | None:
    """The mirror's number for a DoorLoop id, or ``None`` when ``value`` is
    not one. See the module docstring: 53 bits, fixed forever, one way."""
    # @unverified DL-6 — that every record and every reference to one carries
    # a 24-hex ObjectId (the spec's mongoId); settled by the probe's reads.
    if not isinstance(value, str) or not OBJECT_ID.fullmatch(value.strip()):
        return None
    digest = hashlib.sha256(value.strip().lower().encode("ascii")).digest()
    number = int.from_bytes(digest[:8], "big") & MAX_MIRROR_ID
    return number or None


def record_id(obj: Mapping[str, Any]) -> int | None:
    """A record's own ``id`` as a mirror id, or ``None``."""
    return mirror_id(obj.get("id"))


def collisions(objs: Iterable[Mapping[str, Any]]) -> list[str]:
    """DoorLoop ids in ``objs`` whose mirror id another, different id has too.
    Empty is the only answer a read may be written with."""
    seen: dict[int, str] = {}
    clashing: set[str] = set()
    for obj in objs:
        source = obj.get("id")
        number = mirror_id(source)
        if number is None or not isinstance(source, str):
            continue
        key = source.strip().lower()
        other = seen.setdefault(number, key)
        if other != key:
            clashing.update({other, key})
    return sorted(clashing)


# ── value coercion ──────────────────────────────────────────────────────


def _str(value: Any) -> str | None:
    """A trimmed string, or ``None`` for absent or blank."""
    if value is None or isinstance(value, (dict, list, bool)):
        return None
    text = str(value).strip()
    return text or None


def _bool(value: Any) -> bool | None:
    """A JSON boolean, or ``None``. ``bool()`` is not used: ``bool("false")``
    is True."""
    return value if value is True or value is False else None


def _date(value: Any) -> date | None:
    """A ``YYYY-MM-DD`` date (the spec's format) as a ``date``, or ``None``.
    Never passed through as text: the mirror's date columns refuse a string
    (gotcha 148)."""
    text = _str(value)
    if text is None:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _midnight(day: date | None) -> datetime | None:
    """A posting date as midnight UTC: ``transaction_date`` is a
    ``timestamptz``, and readers compare it against the start of a month."""
    return None if day is None else datetime.combine(day, time(), tzinfo=UTC)


def _instant(value: Any) -> datetime | None:
    """A task timestamp as an aware ``datetime``, or ``None``.

    The spec types ``createdAt``/``updatedAt`` as numbers; a number is read
    as epoch milliseconds, or seconds when it is too small to be those. ISO
    text is read too, and a value with no zone is UTC.
    """
    # @unverified DL-10 — that a task's createdAt/updatedAt are epoch
    # milliseconds; settled by the probe's first tasks read beside the task's
    # dates in DoorLoop.
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > _MILLISECONDS_ABOVE else value
        try:
            return datetime.fromtimestamp(seconds, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    text = _str(value)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _money(value: Any) -> Decimal | None:
    """A money field (a JSON number) as an exact ``Decimal``, or ``None``.
    Through ``str``, never ``float`` arithmetic, so 1450.55 stays 1450.55.
    NaN and infinity (which ``json.loads`` accepts) are ``None``."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = Decimal(str(value))
    else:
        text = _str(value)
        if text is None or not _MONEY_TEXT.match(text):
            return None
        try:
            number = Decimal(text)
        except InvalidOperation:
            return None
    return number if number.is_finite() else None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _dicts(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _items(value: Any) -> list[Any]:
    """A list as it came, or none: a field that should be a list and is not
    (a number, a string) names nothing, and never crashes the table."""
    return value if isinstance(value, list) else []


def withheld(value: Any) -> Any:
    """``value`` with every :data:`RAW_WITHHELD` key removed, at any depth."""
    if isinstance(value, dict):
        return {k: withheld(v) for k, v in value.items() if k not in RAW_WITHHELD}
    if isinstance(value, list):
        return [withheld(v) for v in value]
    return value


def primary_email(obj: Mapping[str, Any]) -> str | None:
    """The ``Primary`` email, else the first one with an address."""
    emails = _dicts(obj.get("emails"))
    for email in emails:
        if _str(email.get("type")) == "Primary" and _str(email.get("address")):
            return _str(email.get("address"))
    for email in emails:
        if _str(email.get("address")):
            return _str(email.get("address"))
    return None


def primary_phone(obj: Mapping[str, Any]) -> str | None:
    """The first phone by :data:`PHONE_ORDER`'s types; never a fax."""
    phones = _dicts(obj.get("phones"))
    for kind in PHONE_ORDER:
        for phone in phones:
            if _str(phone.get("type")) == kind and _str(phone.get("number")):
                return _str(phone.get("number"))
    return None


def _person(obj: Mapping[str, Any]) -> dict[str, Any]:
    """The name and contact columns a person record (tenant, owner, vendor)
    fills. ``company_name`` is the record's company, else its ``name`` when it
    gives no person's name."""
    first, last = _str(obj.get("firstName")), _str(obj.get("lastName"))
    company = _str(obj.get("companyName")) or _str(obj.get("company"))
    if company is None and not (first or last):
        company = _str(obj.get("name"))
    return {
        "first_name": first,
        "last_name": last,
        "company_name": company,
        "email": primary_email(obj),
        "phone": primary_phone(obj),
    }


# ── entity mappers: each returns mirror columns, plus buildium_id and raw ──


def map_property(obj: Mapping[str, Any]) -> dict[str, Any]:
    """``property`` -> ``helixis_properties``.

    ``rental_sub_type`` is DoorLoop's ``type`` word (``RESIDENTIAL_CONDO``),
    kept as written.
    """
    address = _dict(obj.get("address"))
    # @unverified DL-7 — the address schema has no state field; a "state" key
    # is read if a live row carries one, else the state is unknown; settled by
    # the probe's first properties read.
    return {
        "buildium_id": record_id(obj),
        "name": _str(obj.get("name")),
        "rental_sub_type": _str(obj.get("type")),
        "address_line1": _str(address.get("street1")),
        "city": _str(address.get("city")),
        "state": _str(address.get("state")),
        "postal_code": _str(address.get("zip")),
        "is_active": _bool(obj.get("active")),
        "raw": withheld(dict(obj)),
    }


@dataclass(frozen=True)
class Occupancy:
    """Which units an ACTIVE lease lists, from this pass's leases read, and
    whether that read was complete. Only a complete read can say a unit is
    vacant."""

    occupied: frozenset[int]
    complete: bool

    @classmethod
    def from_leases(cls, leases: Iterable[Mapping[str, Any]], *, complete: bool) -> Occupancy:
        units: set[int] = set()
        for lease in leases:
            if _str(lease.get("status")) != DOORLOOP_ACTIVE:
                continue
            for unit in _items(lease.get("units")):
                number = mirror_id(unit)
                if number is not None:
                    units.add(number)
        return cls(occupied=frozenset(units), complete=complete)

    def of(self, unit_id: int | None) -> bool | None:
        """Occupied, vacant, or ``None`` when this pass cannot say."""
        if unit_id is None:
            return None
        if unit_id in self.occupied:
            return True
        return False if self.complete else None


def map_unit(obj: Mapping[str, Any], occupancy: Occupancy | None) -> dict[str, Any]:
    """``unit`` -> ``helixis_units``.

    DoorLoop's unit has no occupancy field, so a unit is occupied when an
    ACTIVE lease lists it (:class:`Occupancy`). When the leases read failed or
    was incomplete and lists no such lease, ``is_occupied`` is LEFT OUT, so
    the stored value stands: an unknown occupancy must never read as vacant.
    """
    # @unverified DL-12 — that a unit is occupied exactly when an ACTIVE lease
    # lists it; settled against DoorLoop's rent roll ("occupied") for 2-3 units.
    unit_id = record_id(obj)
    row: dict[str, Any] = {
        "buildium_id": unit_id,
        "property_id": mirror_id(obj.get("property")),
        "unit_number": _str(obj.get("name")),
        "market_rent": _money(obj.get("marketRent")),
        "raw": withheld(dict(obj)),
    }
    occupied = occupancy.of(unit_id) if occupancy is not None else None
    if occupied is not None:
        row["is_occupied"] = occupied
    return row


def map_tenant(obj: Mapping[str, Any]) -> dict[str, Any] | None:
    """``tenant`` -> ``helixis_tenants``, or ``None`` for a prospect.

    A prospect has applied or enquired and lives nowhere: in the tenants table
    it would read as somebody's tenant (one who has applied is an applicant,
    :func:`map_applicant`). A row with no type is kept: it came back from the
    tenants list.
    """
    kind = _str(obj.get("type"))
    if kind is not None and kind != LEASE_TENANT:
        return None
    person = _person(obj)
    return {
        "buildium_id": record_id(obj),
        "first_name": person["first_name"],
        "last_name": person["last_name"],
        "email": person["email"],
        "phone": person["phone"],
        "raw": withheld(dict(obj)),
    }


def owners_by_id(properties: Iterable[Mapping[str, Any]]) -> dict[int, list[int]]:
    """``{owner mirror id: [property mirror ids]}`` from the properties read,
    each property's ``owners[].owner``."""
    out: dict[int, set[int]] = {}
    for prop in properties:
        prop_id = record_id(prop)
        if prop_id is None:
            continue
        for share in _dicts(prop.get("owners")):
            owner = mirror_id(share.get("owner"))
            if owner is not None:
                out.setdefault(owner, set()).add(prop_id)
    return {owner: sorted(props) for owner, props in out.items()}


def map_owner(obj: Mapping[str, Any], owned: Mapping[int, list[int]] | None) -> dict[str, Any]:
    """``owner`` -> ``helixis_owners``.

    ``property_ids`` comes from the properties read's ``owners[]``, written
    only when that read was COMPLETE (``owned`` is not ``None``): a partial
    list would name an owner's properties as if it were all of them, so
    otherwise it is left as it was. An owner on no property owns none.
    """
    owner_id = record_id(obj)
    row: dict[str, Any] = {"buildium_id": owner_id, **_person(obj), "raw": withheld(dict(obj))}
    if owned is not None and owner_id is not None:
        row["property_ids"] = list(owned.get(owner_id, []))
    return row


def map_vendor(obj: Mapping[str, Any]) -> dict[str, Any]:
    """``vendor`` -> ``helixis_vendors``.

    No field says whether a vendor is a company, and none gives a category, so
    those are unknown.
    """
    person = _person(obj)
    return {
        "buildium_id": record_id(obj),
        "name": _str(obj.get("name")) or person["company_name"],
        "is_company": None,
        "first_name": person["first_name"],
        "last_name": person["last_name"],
        "company_name": person["company_name"],
        "is_active": _bool(obj.get("active")),
        "category_id": None,
        "category_name": None,
        "phone": person["phone"],
        "email": person["email"],
        "raw": withheld(dict(obj)),
    }


def lease_unit(obj: Mapping[str, Any]) -> int | None:
    """A lease's unit when it lists exactly one, else ``None``: a lease over
    several units names no single one (the mirror row has one unit column),
    and they stay in ``raw``."""
    units = [mirror_id(u) for u in _items(obj.get("units"))]
    return units[0] if len(units) == 1 else None


def lease_status(obj: Mapping[str, Any]) -> str | None:
    """:data:`ACTIVE_LEASE_STATUS` for an ``ACTIVE`` lease, DoorLoop's own word
    for any other, ``None`` when there is none."""
    word = _str(obj.get("status"))
    return ACTIVE_LEASE_STATUS if word == DOORLOOP_ACTIVE else word


def map_lease(obj: Mapping[str, Any]) -> dict[str, Any]:
    """``Lease`` -> ``helixis_leases``.

    * ``rent`` is ``totalRecurringRent`` and ``security_deposit`` is
      ``totalDepositsHeld``, both read-only totals DoorLoop keeps.
    * The term is ``start``/``end`` (``YYYY-MM-DD``).
    * ``unit_id`` by :func:`lease_unit`.
    """
    # @unverified DL-8 — that totalRecurringRent is the monthly rent and
    # totalDepositsHeld the deposit held; settled against DoorLoop's screen for
    # 2-3 leases.
    return {
        "buildium_id": record_id(obj),
        "property_id": mirror_id(obj.get("property")),
        "unit_id": lease_unit(obj),
        "status": lease_status(obj),
        "lease_from_date": _date(obj.get("start")),
        "lease_to_date": _date(obj.get("end")),
        "rent": _money(obj.get("totalRecurringRent")),
        "security_deposit": _money(obj.get("totalDepositsHeld")),
        "raw": withheld(dict(obj)),
    }


def lease_tenant_link(obj: Mapping[str, Any]) -> dict[str, Any] | None:
    """A ``leaseTenant`` -> one ``helixis_lease_tenants`` row, or ``None`` when
    it names no lease.

    The link takes the readers' word for its ``status`` (:data:`LINK_STATUS`),
    so a co-tenant who moved out of a lease the others continue reads
    ``MovedOut`` and drops off every "who lives here" list
    (``db.tenant_links``). A status DoorLoop does not document is unknown:
    ``None``, which those readers count as current, as they do for every
    system whose links carry none. A tenant with no usable id still yields a
    row, so the writer counts it and does not prune from a read that lost
    someone.
    """
    # @unverified DL-9 — that a leaseTenant's tenant is the tenant record the
    # tenants list carries (same id), and that its status matches DoorLoop's
    # own screen; settled by joining the probe's leases/tenants and tenants
    # reads.
    lease_id = mirror_id(obj.get("lease"))
    if lease_id is None:
        return None
    tenant = obj.get("tenant")
    tenant_id = record_id(tenant) if isinstance(tenant, dict) else mirror_id(tenant)
    return {
        "lease_buildium_id": lease_id,
        "tenant_buildium_id": tenant_id,
        "status": LINK_STATUS.get(_str(obj.get("status")) or ""),
        "move_in_date": _date(obj.get("moveInAt")),
    }


def map_work_order(obj: Mapping[str, Any]) -> dict[str, Any] | None:
    """A ``task`` of type ``WORK_ORDER`` -> ``helixis_work_orders``, or ``None``
    for any other task type (an internal task or a request is not a work
    order).

    ``status`` and ``priority`` take the readers' words (:data:`TASK_STATUS`,
    :data:`TASK_PRIORITY`): DoorLoop's ``COMPLETED`` kept as written would be
    counted as an open work order by every reader. The title is the task's
    ``subject``. The vendor and the bill (``workOrder.assignedToVendor``,
    ``.vendorBill``) stay in ``raw``; the bill names its work order
    (:func:`bill_work_orders`).
    """
    if _str(obj.get("type")) != WORK_ORDER:
        return None
    return {
        "buildium_id": record_id(obj),
        "task_id": None,
        "property_id": mirror_id(obj.get("property")),
        "unit_id": mirror_id(obj.get("unit")),
        "title": _str(obj.get("subject")),
        "status": TASK_STATUS.get(_str(obj.get("status")) or ""),
        "priority": TASK_PRIORITY.get(_str(obj.get("priority")) or ""),
        "category_id": None,
        "assigned_to_user_id": None,
        "pms_updated_at": _instant(obj.get("updatedAt")),
        "raw": withheld(dict(obj)),
    }


def map_task(obj: Mapping[str, Any]) -> dict[str, Any] | None:
    """A ``task`` other than a work order -> ``helixis_tasks``, or ``None`` for
    a work order (its own table) or a type Occupella does not know.

    Type, status and priority take the readers' words (:data:`TASK_TYPE`,
    :data:`TASK_STATUS`, :data:`TASK_PRIORITY`). The assignees are DoorLoop
    users, which Occupella does not sync, so ``assigned_to_user_id`` is
    unknown and they stay in ``raw``. DoorLoop has no task category.
    """
    # @unverified DL-14 — that RECEIVED is a request not yet started and
    # ARCHIVED a finished one; settled against DoorLoop's task screen.
    kind = TASK_TYPE.get(_str(obj.get("type")) or "")
    if kind is None:
        return None
    return {
        "buildium_id": record_id(obj),
        "task_type": kind,
        "title": _str(obj.get("subject")),
        "status": TASK_STATUS.get(_str(obj.get("status")) or ""),
        "priority": TASK_PRIORITY.get(_str(obj.get("priority")) or ""),
        "due_date": _date(obj.get("dueDate")),
        "property_id": mirror_id(obj.get("property")),
        "unit_id": mirror_id(obj.get("unit")),
        "assigned_to_user_id": None,
        "category_name": None,
        "raw": withheld(dict(obj)),
    }


def map_applicant(obj: Mapping[str, Any]) -> dict[str, Any] | None:
    """A prospect (a ``tenant`` of type ``PROSPECT_TENANT``) who has reached
    the application -> ``helixis_applicants``, else ``None``.

    ``status`` is :data:`APPLICANT_STATUS`'s word; a prospect before the
    application, or lost, is not an applicant. The place applied for is the
    one property (and unit) the prospect is interested in, or unknown when
    there are several. DoorLoop has no applicant group, and the tenant record
    a prospect becomes is not linked, so both are unknown.
    """
    # @unverified DL-13 — that a prospect's status says how far they got (an
    # offer made means approved); settled against DoorLoop's prospects screen.
    if _str(obj.get("type")) != PROSPECT_TENANT:
        return None
    info = _dict(obj.get("prospectInfo"))
    status = APPLICANT_STATUS.get(_str(info.get("status")) or "")
    if status is None:
        return None
    interests = _dicts(info.get("interests"))
    places = {(mirror_id(i.get("property")), mirror_id(i.get("unit"))) for i in interests}
    property_id, unit_id = places.pop() if len(places) == 1 else (None, None)
    person = _person(obj)
    return {
        "buildium_id": record_id(obj),
        "first_name": person["first_name"],
        "last_name": person["last_name"],
        "email": person["email"],
        "phone": person["phone"],
        "status": status,
        "applicant_group_id": None,
        "property_id": property_id,
        "unit_id": unit_id,
        "tenant_id": None,
        "raw": withheld(dict(obj)),
    }


def gl_type(word: str | None) -> str | None:
    """Buildium's type for an account ``type`` word (:data:`GL_TYPE`), or
    ``None`` for a word that names none of them."""
    if word is None:
        return None
    return next((kind for prefix, kind in GL_TYPE.items() if word.startswith(prefix)), None)


def map_gl_account(obj: Mapping[str, Any]) -> dict[str, Any]:
    """``account`` -> ``helixis_gl_accounts``.

    ``type`` is Buildium's word (:func:`gl_type`) and ``sub_type`` DoorLoop's
    own (``EXPENSE_COGS``). An account of a known type is a bank account or
    not; of an unknown type, that is unknown. DoorLoop's chart of accounts has
    no numbers and no parent accounts.
    """
    word = _str(obj.get("type"))
    kind = gl_type(word)
    return {
        "buildium_id": record_id(obj),
        "account_number": None,
        "name": _str(obj.get("name")),
        "type": kind,
        "sub_type": word,
        "is_active": _bool(obj.get("active")),
        "is_bank_account": (word == BANK_ACCOUNT_TYPE) if kind is not None else None,
        "parent_gl_account_id": None,
        "raw": withheld(dict(obj)),
    }


def bill_work_orders(tasks: Iterable[Mapping[str, Any]]) -> dict[int, int]:
    """``{bill mirror id: work order mirror id}`` from the tasks read: each
    work order names the vendor bill it was billed on (``workOrder.vendorBill``,
    documented as set once ``billed`` is true)."""
    out: dict[int, int] = {}
    for task in tasks:
        if _str(task.get("type")) != WORK_ORDER:
            continue
        bill = mirror_id(_dict(task.get("workOrder")).get("vendorBill"))
        order = record_id(task)
        if bill is not None and order is not None:
            out[bill] = order
    return out


def bill_paid(obj: Mapping[str, Any]) -> bool | None:
    """Paid when nothing is left on it (``totalBalance`` is 0), unpaid when
    something is, unknown when the balance did not come through."""
    balance = _money(obj.get("totalBalance"))
    if balance is None or balance < 0:
        return None
    return balance == 0


#: Whether a NULL ``work_order_id`` on this system's bills means "no work
#: order", which lets the no-work-order rule read them
#: (``detectors.mirror_state_rules.bill_link_systems``). Not yet: the link
#: comes from the tasks read, so a pass whose tasks read stopped short writes
#: a NEW bill with the column NULL, and that rule would accuse it (rule 14).
BILL_WORK_ORDER_LINK = False


def map_bill(obj: Mapping[str, Any], work_orders: Mapping[int, int] | None) -> dict[str, Any]:
    """A vendor bill (``vendorTransaction``) -> ``helixis_bills``.

    ``total_amount`` is ``totalAmount``; ``is_paid`` is :func:`bill_paid`.
    ``work_order_id`` comes from the tasks read (:func:`bill_work_orders`),
    and only from a complete one (``work_orders`` is ``None`` otherwise): a
    partial read would call a bill's work order "none", so the column is then
    left as it was. A vendor credit is not a bill (it is not money owed), so
    it is never read.
    """
    # @unverified DL-15 — that totalBalance is what is left to pay on the
    # bill; settled against DoorLoop's bills screen for 2-3 bills.
    bill_id = record_id(obj)
    row: dict[str, Any] = {
        "buildium_id": bill_id,
        "vendor_id": mirror_id(obj.get("vendor")),
        "bill_date": _date(obj.get("date")),
        "due_date": _date(obj.get("dueDate")),
        "total_amount": _money(obj.get("totalAmount")),
        "is_paid": bill_paid(obj),
        "reference_number": _str(obj.get("reference")),
        "raw": withheld(dict(obj)),
    }
    if work_orders is not None and bill_id is not None:
        row["work_order_id"] = work_orders.get(bill_id)
    return row


#: ADR 0010 Decision (b)'s ledger type for money charged to the manager, not a
#: property (migration 0104's ``1 Manager``): the spend report states it on
#: its own instead of leaving it out of every property silently.
MANAGER_LEDGER = 1


def bill_lines(obj: Mapping[str, Any]) -> list[dict[str, Any]]:
    """A bill's ``lines[]`` -> ``helixis_bill_lines`` rows.

    A line is linked to a property or to nothing (the spec: "At the moment,
    only property is supported"). A line linked to nothing is the company's
    own spend: it carries :data:`MANAGER_LEDGER`, as a Rentvine manager-ledger
    line does, so no property's total silently includes or loses it. DoorLoop
    lines name no unit.
    """
    # @unverified DL-15 — that a line linked to nothing is the company's own
    # spend; settled against a bill in DoorLoop's screen.
    bill_id = record_id(obj)
    out: list[dict[str, Any]] = []
    for index, line in enumerate(_dicts(obj.get("lines"))):
        linked = _str(line.get("linkedToResourceType"))
        property_id = mirror_id(line.get("linkedToResourceId")) if linked == "property" else None
        company_wide = linked is None and line.get("linkedToResourceId") is None
        out.append(
            {
                "bill_id": bill_id,
                "line_index": index,
                "property_id": property_id,
                "unit_id": None,
                "gl_account_id": mirror_id(line.get("account")),
                "amount": _money(line.get("amount")),
                "memo": _str(line.get("memo")),
                "ledger_id": None,
                "ledger_type": MANAGER_LEDGER if company_wide else None,
            }
        )
    return out


def lease_move_outs(links: Iterable[Mapping[str, Any]]) -> dict[int, date]:
    """``{lease mirror id: the day it turns over}`` from the lease tenants read:
    the last ``moveOutAt`` of a lease every one of whose tenants has one. A
    lease with any tenant still staying (no move-out date) does not turn over.
    """
    # @unverified DL-16 — that moveOutAt is the day a tenant leaves (or is due
    # to); settled against a move-out in DoorLoop's screen.
    dates: dict[int, list[date | None]] = {}
    for link in links:
        lease_id = mirror_id(link.get("lease"))
        if lease_id is not None:
            dates.setdefault(lease_id, []).append(_date(link.get("moveOutAt")))
    return {
        lease_id: max(d for d in days if d is not None)
        for lease_id, days in dates.items()
        if days and all(d is not None for d in days)
    }


# ── the lease ledger ──────────────────────────────────────────────────────

#: Each ledger read -> ``(Buildium transaction_type, sign)``. The sign is the
#: row's effect on the balance (``buildium/money.PAYMENTS_ARE_NEGATIVE``): a
#: charge raises it, a payment or a credit lowers it, and a reversed payment
#: puts the money back (a positive ``Payment`` row, as Rentvine writes one).
#: The magnitude comes from the amount; the direction only from the read.
LEDGER_KINDS: dict[str, tuple[str, int]] = {
    "charges": ("Charge", 1),
    "credits": ("Credit", -1),
    "payments": ("Payment", -1),
    "reversed_payments": ("Payment", 1),
}


def transaction_amount(kind: str, obj: Mapping[str, Any]) -> Decimal | None:
    """The amount a ledger row moved: a payment's ``amountReceived``, a charge's
    or credit's lines' ``amount`` summed. ``None`` when any part is unreadable
    or a charge has no lines."""
    # @unverified DL-11 — that a charge's or credit's amount is the sum of its
    # lines' amount, and a reversed payment's is amountReceived; settled by
    # the probe's first ledger reads against DoorLoop's lease ledger.
    if kind in ("payments", "reversed_payments"):
        return _money(obj.get("amountReceived"))
    lines = _dicts(obj.get("lines"))
    if not lines:
        return None
    total = Decimal(0)
    for line in lines:
        amount = _money(line.get("amount"))
        if amount is None:
            return None
        total += amount
    return total


def ledger_row(kind: str, obj: Mapping[str, Any]) -> dict[str, Any] | None:
    """One charge, credit, payment or reversed payment -> a
    ``helixis_lease_transactions`` row, or ``None`` when it names no lease.

    ``total_amount`` is signed by :data:`LEDGER_KINDS` from the magnitude of
    :func:`transaction_amount`, or ``None`` when that is unknown (rule 14).
    """
    # @unverified DL-11 — that each ledger row carries its own "id" (the
    # leaseTransaction schema lists none); settled by the probe's first
    # charges read.
    label, sign = LEDGER_KINDS[kind]
    lease_id = mirror_id(obj.get("lease"))
    if lease_id is None:
        return None
    amount = transaction_amount(kind, obj)
    return {
        "buildium_id": record_id(obj),
        "lease_id": lease_id,
        "transaction_date": _midnight(_date(obj.get("date"))),
        "transaction_type": label,
        "total_amount": None if amount is None else abs(amount) * sign,
        "raw": withheld(dict(obj)),
    }


# ── rent balances ─────────────────────────────────────────────────────────


class _Unknown:
    """The balance of a lease that came without a readable ``totalBalancedue``."""

    def __repr__(self) -> str:
        return "BALANCE_UNKNOWN"


#: What :func:`lease_balance` returns for a lease whose balance did not come
#: through. Distinct from ``None`` (read, and owes nothing).
BALANCE_UNKNOWN: Any = _Unknown()


def open_charges_by_lease(
    charges: Iterable[Mapping[str, Any]],
) -> dict[int, list[tuple[date | None, Decimal | None]]]:
    """``{lease: [(posted, open amount)]}`` from the charges read. A charge's
    open amount is its lines' ``balance`` ("Remaining unpaid amount on this
    line") summed, or ``None`` when any line's is unreadable."""
    out: dict[int, list[tuple[date | None, Decimal | None]]] = {}
    for charge in charges:
        lease_id = mirror_id(charge.get("lease"))
        if lease_id is None:
            continue
        lines = _dicts(charge.get("lines"))
        balances = [_money(line.get("balance")) for line in lines]
        open_amount = (
            None
            if not lines or any(b is None for b in balances)
            else sum((b for b in balances if b is not None), Decimal(0))
        )
        out.setdefault(lease_id, []).append((_date(charge.get("date")), open_amount))
    return out


def lease_aging(
    charges: Iterable[tuple[date | None, Decimal | None]], total: Decimal, today: date
) -> dict[str, Decimal] | None:
    """The four aging buckets from one lease's open charges, or ``None`` when
    they cannot be stated.

    Each open amount goes in a bucket by days since the charge posted, in the
    company's calendar (``today``). ⚠ ONLY WHEN THEY ADD UP: the buckets are
    stated only if the open amounts sum to the lease's balance to the cent. A
    credit on account, an unread charge, or a payment not yet applied would
    otherwise put a wrong number in a bucket, and an unknown bucket must never
    read as $0 (rule 14).
    """
    buckets = {name: Decimal(0) for name, _ in BUCKETS}
    owed = Decimal(0)
    for posted, open_amount in charges:
        if open_amount is None:
            return None
        if open_amount <= 0:
            continue
        if posted is None:
            return None
        owed += open_amount
        age = (today - posted).days
        for name, limit in BUCKETS:
            if limit is None or age <= limit:
                buckets[name] += open_amount
                break
    if owed.quantize(_CENT) != total.quantize(_CENT):
        return None
    return buckets


def lease_balance(
    obj: Mapping[str, Any],
    charges: list[tuple[date | None, Decimal | None]] | None,
    today: date,
) -> Any:
    """One lease -> a ``helixis_lease_balances`` row, ``None`` when it owes
    nothing, or :data:`BALANCE_UNKNOWN`.

    The total is DoorLoop's own ``totalBalancedue``. Rows exist only for leases
    that owe, as on the Buildium path, whose readers count every row as a
    delinquent lease; a lease in credit owes nothing. ``charges`` is ``None``
    when the charges read failed or was incomplete: the total stands and every
    bucket is unknown.
    """
    # @unverified DL-8 — that totalBalancedue is what the lease owes now;
    # settled against DoorLoop's screen for 2-3 leases.
    lease_id = record_id(obj)
    total = _money(obj.get("totalBalancedue"))
    if lease_id is None or total is None:
        return BALANCE_UNKNOWN
    if total <= 0:
        return None
    aging = lease_aging(charges, total, today) if charges is not None else None
    return {
        "lease_id": lease_id,
        "property_id": mirror_id(obj.get("property")),
        "unit_id": lease_unit(obj),
        "total_balance": total,
        **{name: (aging[name] if aging is not None else None) for name, _ in BUCKETS},
        "raw": {"id": obj.get("id"), "totalBalancedue": obj.get("totalBalancedue")},
    }


__all__ = [
    "ACTIVE_LEASE_STATUS",
    "APPLICANT_STATUS",
    "BALANCE_UNKNOWN",
    "BANK_ACCOUNT_TYPE",
    "BILL_WORK_ORDER_LINK",
    "BUCKETS",
    "DOORLOOP_ACTIVE",
    "GL_TYPE",
    "LEASE_TENANT",
    "LEASE_TENANT_STATUS",
    "LEDGER_KINDS",
    "LINK_STATUS",
    "MANAGER_LEDGER",
    "MAX_MIRROR_ID",
    "OBJECT_ID",
    "PHONE_ORDER",
    "PROSPECT_TENANT",
    "RAW_WITHHELD",
    "TASK_PRIORITY",
    "TASK_STATUS",
    "TASK_TYPE",
    "WORK_ORDER",
    "Occupancy",
    "bill_lines",
    "bill_paid",
    "bill_work_orders",
    "collisions",
    "gl_type",
    "lease_aging",
    "lease_balance",
    "lease_move_outs",
    "lease_status",
    "lease_tenant_link",
    "lease_unit",
    "ledger_row",
    "map_applicant",
    "map_bill",
    "map_gl_account",
    "map_lease",
    "map_owner",
    "map_property",
    "map_task",
    "map_tenant",
    "map_unit",
    "map_vendor",
    "map_work_order",
    "mirror_id",
    "open_charges_by_lease",
    "owners_by_id",
    "primary_email",
    "primary_phone",
    "record_id",
    "transaction_amount",
    "withheld",
]
