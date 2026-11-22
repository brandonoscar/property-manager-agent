"""Propertyware records -> mirror rows. Pure: no I/O, no clock, no logging.

Every field name below comes from Propertyware's official spec
(``docs/propertyware/vendor/app.propertyware.com/openapi.json``). Documented
is not observed: nobody has read a live row yet, so every place the spec is
silent, ambiguous, or could be contradicted by a live call carries an
``@unverified PW-n`` comment naming the design's work-list item that settles
it (``scripts/propertyware_unverified.py`` lists them).

THE RULES EVERY MAPPER FOLLOWS
    * Ids go in ``buildium_id`` (the shared column, ADR 0010), and each mirror
      table takes ids from ONE Propertyware sequence: buildings, units,
      leases, contacts, owners, vendors, work orders, bills and GL accounts
      are separate sequences that can share numbers (design, "spine
      mapping"), and two sequences in one table would overwrite each other
      under ``UNIQUE (company_id, buildium_id)``.
    * **Unknown is None** (rule 14). A value Propertyware did not send, or
      that does not parse, is ``None``, never 0, "" or False.
    * **A value this pass could not determine is LEFT OUT of the row**, so
      the mirror keeps what it had (``pms.mirror``: an upsert writes only the
      columns a row carries). That is different from ``None``, which says
      "known to be unknown" and is written as NULL.
    * **Status words are the customer's own text**, never translated, except a
      lease's: its readers filter ``status = 'Active'``, so an active lease is
      written with that word (:func:`lease_status`) and the customer's stays
      in ``raw``.
    * Rows carry only mirror columns, plus ``buildium_id`` and ``raw``. The
      writer drops any other key without a sound, so a test holds every row
      to the writer's own column lists.

WHAT ``raw`` HOLDS
    The record as Propertyware sent it, less :data:`RAW_WITHHELD`: bank
    account and routing numbers (GL accounts), tax ids (vendors) and dates of
    birth (contacts). ``raw`` is readable by the Data Analyst, so it reaches a
    model provider (terms rule 18: send the fewest fields a task needs), and
    no reader needs them.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

#: Keys removed from ``raw`` wherever they appear (see the module docstring).
RAW_WITHHELD: frozenset[str] = frozenset(
    {"bankAccountNumber", "bankRoutingNumber", "taxId", "birthDate"}
)

#: The status a lease-tenant link is written with: the word Rentvine's links
#: use, so every reader of ``helixis_lease_tenants`` sees one vocabulary. The
#: spec describes ``Lease.contacts`` as "the current tenants on the lease".
#: Only an active lease is linked (:func:`lease_tenant_links`).
LEASE_TENANT_STATUS = "Current"

#: The word every lease reader filters on (``status = 'Active'``), written for
#: a lease whose ``active`` flag is true (:func:`lease_status`).
ACTIVE_LEASE_STATUS = "Active"

#: Buildium's five account types, the words every GL reader groups by.
GL_TYPES: tuple[str, ...] = ("Asset", "Liability", "Equity", "Income", "Expense")

#: Propertyware's own account types, each under the one of the five it belongs
#: to. The names and numbers are the spec's (``SaveAccountDTO.accountType``:
#: "1:Bank, 2:Credit Card, 5:Current Asset, 12:Non-Current Asset, 6:Current
#: Liability, 13:Non-Current Liability, 7:Equity, 8:Income, 9:Expense,
#: 10:Non-Operating Income, 11:Non-Operating Expense"). ⚠ The read side's
#: ``AccountDTO.accountType`` is a bare string, so which of these it carries
#: (the names, the numbers, or Buildium's five words) is the probe's to
#: settle; all three are read. Any other word is never forced into a type.
_GL_TYPE_BY_WORD: dict[str, str] = {
    **{word.lower(): word for word in GL_TYPES},
    "bank": "Asset",
    "credit card": "Liability",
    "current asset": "Asset",
    "non-current asset": "Asset",
    "current liability": "Liability",
    "non-current liability": "Liability",
    "non-operating income": "Income",
    "non-operating expense": "Expense",
    "1": "Asset",
    "2": "Liability",
    "5": "Asset",
    "12": "Asset",
    "6": "Liability",
    "13": "Liability",
    "7": "Equity",
    "8": "Income",
    "9": "Expense",
    "10": "Income",
    "11": "Expense",
}

#: The spec's bank account type, by name and by number. Only this says an
#: account IS a bank account; no type says one is not.
_BANK_TYPE_WORDS = frozenset({"bank", "1"})

#: ADR 0010 Decision (b)'s ledger type for a PORTFOLIO (migration 0104): money
#: that belongs to no single property. The spend report
#: (``mirror_tools._spending_report_impl``) states such lines' total on its
#: own, rather than leaving them out of its by-property totals in silence.
PORTFOLIO_LEDGER = 2

_CENT = Decimal("0.01")

#: The mirror's aging buckets by days since the charge posted, cut as
#: Rentvine's are: 0-30, 31-60, 61-90, over 90.
BUCKETS: tuple[tuple[str, int | None], ...] = (
    ("balance_0_30", 30),
    ("balance_31_60", 60),
    ("balance_61_90", 90),
    ("balance_over_90", None),
)

_MONEY_TEXT = re.compile(r"^-?\d+(?:\.\d+)?$")


# ── value coercion ──────────────────────────────────────────────────────


def _int(value: Any) -> int | None:
    """An id as an ``int``, or ``None``. Never raises: a non-numeric id is a
    row the writer refuses (and counts), not a crash."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    try:
        return int(text) if text else None
    except ValueError:
        return None


def _str(value: Any) -> str | None:
    """A trimmed string, or ``None`` for absent or blank."""
    if value is None or isinstance(value, (dict, list)):
        return None
    text = str(value).strip()
    return text or None


def _bool(value: Any) -> bool | None:
    """A yes/no, or ``None`` when it is not one. The spec types these as JSON
    booleans; "true"/"false" and "1"/"0" text is read too. ``bool()`` is not
    used: ``bool("false")`` is True."""
    if value is True or value is False:
        return value
    text = str(value).strip().lower() if value is not None else ""
    if text in {"true", "1"}:
        return True
    if text in {"false", "0"}:
        return False
    return None


def _date(value: Any) -> date | None:
    """A ``YYYY-MM-DD`` date (the spec's format) as a ``date``, or ``None``.
    A timestamp is cut to its calendar date. Never passed through as text:
    the mirror's date columns refuse a string (gotcha 148)."""
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
    """An audit timestamp as an aware ``datetime``, or ``None``.

    The spec documents ``lastModifiedDateTime`` as UTC, in
    ``YYYY-MM-dd'T'HH:mm:ssXXX``. A value with no zone is read as UTC, as the
    spec says it is.
    """
    # @unverified PW-1 — that audit timestamps arrive in the documented
    # format, with their zone; settled by the probe's first read of a list.
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = _str(value)
        if text is None:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _money(value: Any) -> Decimal | None:
    """A money field (a JSON double) as an exact ``Decimal``, or ``None``.

    Through ``str``, never through ``float`` arithmetic, so 1450.55 stays
    1450.55. NaN and infinity (which ``json.loads`` accepts) are ``None``.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        number = value
    elif isinstance(value, (int, float)):
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


def _first(obj: Mapping[str, Any], keys: Iterable[str]) -> str | None:
    """The first of ``keys`` that carries a value."""
    for key in keys:
        found = _str(obj.get(key))
        if found:
            return found
    return None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _dicts(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def withheld(value: Any) -> Any:
    """``value`` with every :data:`RAW_WITHHELD` key removed, at any depth."""
    if isinstance(value, dict):
        return {k: withheld(v) for k, v in value.items() if k not in RAW_WITHHELD}
    if isinstance(value, list):
        return [withheld(v) for v in value]
    return value


#: A contact's phone, most useful first: a mobile can take a text.
_CONTACT_PHONES = ("mobilePhone", "homePhone", "workPhone", "otherPhone")


def record_id(obj: Mapping[str, Any]) -> int | None:
    """A record's own ``id``, or ``None`` when it has no usable one."""
    return _int(obj.get("id"))


# ── units: the lease → unit rule ──────────────────────────────────────────

#: Outcomes of :func:`resolve_unit`.
UNIT = "unit"
NO_UNIT = "no_unit"
SINGLE_FAMILY = "single_family"
NOT_FOUND = "not_found"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class UnitIndex:
    """Each unit's building, from this pass's units read, and whether that
    read was complete. Only a complete read can say a unit does NOT exist."""

    building_of: Mapping[int, int | None]
    complete: bool

    @classmethod
    def from_rows(cls, rows: Iterable[Mapping[str, Any]], *, complete: bool) -> UnitIndex:
        index: dict[int, int | None] = {}
        for row in rows:
            unit_id = _int(row.get("id"))
            if unit_id is not None:
                index[unit_id] = _int(row.get("buildingID"))
        return cls(building_of=index, complete=complete)


def resolve_unit(
    unit_id: int | None, building_id: int | None, units: UnitIndex | None
) -> tuple[str, int | None]:
    """The design's lease → unit rule, as ``(outcome, unit id or None)``.

    1. A unit with id ``unit_id`` exists **and** its building is
       ``building_id``: that unit (:data:`UNIT`). The building is checked
       because the unit and building id sequences can share numbers.
    2. ``unit_id`` equals ``building_id``: a single-family home whose lease
       names its building (W5, :data:`SINGLE_FAMILY`). The design's synthetic
       negative-id unit waits for a live row (PW-8), so there is no unit.
    3. Otherwise no unit Occupella can name (:data:`NOT_FOUND`), or the record
       names none at all (:data:`NO_UNIT`). Never guessed.

    :data:`UNKNOWN` when the units read failed or was incomplete and found no
    match: a unit missing from a partial read may still exist, so the caller
    leaves the stored ``unit_id`` as it was instead of blanking it.
    """
    if unit_id is None:
        return NO_UNIT, None
    if units is not None and building_id is not None:
        if units.building_of.get(unit_id) == building_id:
            return UNIT, unit_id
    if units is None or not units.complete:
        return UNKNOWN, None
    # @unverified PW-8 — that a single-family lease's unitID is its building
    # id (the write-side SaveLease.unitID reads "Unit/Building ID"); settled
    # by one single-family lease on a live account.
    if unit_id == building_id:
        return SINGLE_FAMILY, None
    return NOT_FOUND, None


# ── entity mappers: each returns mirror columns, plus buildium_id and raw ──


def map_building(obj: Mapping[str, Any]) -> dict[str, Any]:
    """``BuildingDTO`` -> ``helixis_properties``.

    ``propertyType`` is a read-side LABEL ("Du/Fourplex", "Section
    8/Affordable"), kept as written. ``baseYear*`` is disabled upstream and
    not read.
    """
    # @unverified PW-1 — the building fields and the address object's keys
    # are the spec's; settled by the probe's first buildings read.
    address = _dict(obj.get("address"))
    return {
        "buildium_id": _int(obj.get("id")),
        "name": _str(obj.get("name")),
        "rental_sub_type": _str(obj.get("propertyType")),
        "address_line1": _str(address.get("address")),
        "city": _str(address.get("city")),
        "state": _str(address.get("stateRegion")),
        "postal_code": _str(address.get("postalCode")),
        "is_active": _bool(obj.get("active")),
        "raw": withheld(dict(obj)),
    }


#: The ``targetRentUnits`` value that makes ``targetRent`` a monthly rent.
MONTHLY_RENT_UNITS = "DOLLAR_PER_MONTH"


def unit_occupied(obj: Mapping[str, Any]) -> bool | None:
    """A unit's occupancy from its ``status`` ("Property status
    (occupied/vacant)"), or ``None`` for any other word. Never vacant by
    default: an unknown occupancy must not read as an empty unit."""
    # @unverified PW-17 — whether occupancy is the unit's status or its
    # leaseID; settled by 2-3 units against Propertyware's own screen.
    status = (_str(obj.get("status")) or "").lower()
    if status == "occupied":
        return True
    if status == "vacant":
        return False
    return None


def map_unit(obj: Mapping[str, Any]) -> dict[str, Any]:
    """``UnitDTO`` -> ``helixis_units``.

    ``market_rent`` is ``targetRent`` only when ``targetRentUnits`` says it is
    dollars per month. A per-square-foot, per-week or per-night figure is not
    a monthly rent, so it is unknown rather than shown as one.
    """
    # @unverified PW-1 — that targetRentUnits accompanies targetRent, and that
    # a unit's label is its name; settled by the probe's first units read.
    monthly = _str(obj.get("targetRentUnits")) == MONTHLY_RENT_UNITS
    return {
        "buildium_id": _int(obj.get("id")),
        "property_id": _int(obj.get("buildingID")),
        "unit_number": _first(obj, ("name", "abbreviation")),
        "market_rent": _money(obj.get("targetRent")) if monthly else None,
        "is_occupied": unit_occupied(obj),
        "raw": withheld(dict(obj)),
    }


def map_tenant(obj: Mapping[str, Any]) -> dict[str, Any] | None:
    """A ``ContactDTO`` from ``GET /contacts?type=TENANT`` -> ``helixis_tenants``,
    or ``None`` when the row says it is another type of contact.

    The read asks for tenants. A row typed otherwise means the filter was not
    applied to it (gotcha 24's ignored filter), and an owner or a vendor
    must never land in the tenants table. A row with no type is kept: it came
    back for a tenant query.
    """
    # @unverified PW-1 — that type=TENANT is honoured and every row carries
    # its type; settled by the probe's first contacts read.
    kind = _str(obj.get("type"))
    if kind is not None and kind.upper() != "TENANT":
        return None
    return {
        "buildium_id": _int(obj.get("id")),
        "first_name": _str(obj.get("firstName")),
        "last_name": _str(obj.get("lastName")),
        "email": _first(obj, ("email", "altEmail")),
        "phone": _first(obj, _CONTACT_PHONES),
        "raw": withheld(dict(obj)),
    }


def map_vendor(obj: Mapping[str, Any]) -> dict[str, Any]:
    """``VendorDTO`` -> ``helixis_vendors``.

    A vendor has a ``name`` and a ``companyName`` and no person's name, so
    ``first_name``/``last_name`` are unknown, and so is ``is_company`` (no
    field says). ``type`` ("The type of vendor") is free text, kept as the
    category's name; there is no category id.
    """
    # @unverified PW-1 — the vendor fields; settled by the probe's first
    # vendors read.
    company = _str(obj.get("companyName"))
    return {
        "buildium_id": _int(obj.get("id")),
        "name": _str(obj.get("name")) or company,
        "is_company": None,
        "first_name": None,
        "last_name": None,
        "company_name": company,
        "is_active": _bool(obj.get("active")),
        "category_id": None,
        "category_name": _str(obj.get("type")),
        "phone": _first(obj, ("phone", "otherPhone")),
        "email": _str(obj.get("email")),
        "raw": withheld(dict(obj)),
    }


def gl_type(word: str | None) -> str | None:
    """Buildium's account type for Propertyware's ``accountType`` word: one of
    :data:`GL_TYPES` for a word the spec names (or one of the five), in any
    case, else ``None``. A word the spec does not name is never forced into
    a type."""
    if word is None:
        return None
    return _GL_TYPE_BY_WORD.get(word.strip().lower())


def is_bank_type(word: str | None) -> bool | None:
    """``True`` for the spec's bank account type, else ``None``: unknown,
    never "not a bank account" (rule 14)."""
    if word is not None and word.strip().lower() in _BANK_TYPE_WORDS:
        return True
    return None


def map_gl_account(obj: Mapping[str, Any]) -> dict[str, Any]:
    """``AccountDTO`` -> ``helixis_gl_accounts``.

    ``sub_type`` is Propertyware's own ``accountType`` word. ``type`` is
    Buildium's word for it, for the types the spec names (:func:`gl_type`);
    any other word leaves it unknown. An account is a bank account when its
    type is the spec's Bank; otherwise that is unknown, never "no".
    ``securityDepositAccount`` stays in ``raw`` for the deposit workflow
    (design); the bank account and routing numbers do not (:data:`RAW_WITHHELD`).
    """
    # @unverified PW-1 — the account fields and the accountType words (which
    # ones name Buildium's five types); settled by the probe's first GL
    # accounts read.
    word = _str(obj.get("accountType"))
    return {
        "buildium_id": _int(obj.get("id")),
        "account_number": _str(obj.get("accountNumber")),
        "name": _str(obj.get("name")),
        "type": gl_type(word),
        "sub_type": word,
        "is_active": _bool(obj.get("active")),
        "is_bank_account": is_bank_type(word),
        "parent_gl_account_id": _int(obj.get("parentGLAccountId")),
        "raw": withheld(dict(obj)),
    }


def map_work_order(obj: Mapping[str, Any], units: UnitIndex | None) -> tuple[dict[str, Any], str]:
    """``WorkOrderDTO`` -> ``helixis_work_orders``, and the unit outcome.

    ``status`` is free text and ``priority`` an open enum (HIGH, MEDIUM, LOW),
    both kept as written. There is no title, so the description stands in
    (Rentvine's choice too), and ``category`` is text with no id. The unit is
    resolved by the lease rule (:func:`resolve_unit`), and left as it was
    when this pass cannot tell.
    """
    # @unverified PW-10 — the work-order status strings; settled by reading
    # the distinct statuses of the work-order list.
    building = _int(obj.get("buildingID"))
    outcome, unit = resolve_unit(_int(obj.get("unitID")), building, units)
    row: dict[str, Any] = {
        "buildium_id": _int(obj.get("id")),
        "task_id": None,
        "property_id": building,
        "title": _str(obj.get("description")),
        "status": _str(obj.get("status")),
        "priority": _str(obj.get("priority")),
        "category_id": None,
        "assigned_to_user_id": None,
        "pms_updated_at": _instant(obj.get("lastModifiedDateTime")),
        "raw": withheld(dict(obj)),
    }
    if outcome != UNKNOWN:
        row["unit_id"] = unit
    return row, outcome


def lease_active(obj: Mapping[str, Any]) -> bool | None:
    """A lease's typed ``active`` flag ("Indicates if lease is Active"), or
    ``None`` when it is absent or not a yes/no. Unknown is never inactive."""
    return _bool(obj.get("active"))


def lease_status(obj: Mapping[str, Any]) -> str | None:
    """The status a lease is written with, in the word its readers filter on.

    Every lease reader asks ``status = 'Active'``, and each Propertyware
    customer names its own statuses (``GET /leases/statuses``), so the
    customer's word for an active lease is not the readers' word. The typed
    ``active`` flag decides, as Rentvine's coarse status does:

    * ``active`` true: :data:`ACTIVE_LEASE_STATUS`;
    * ``active`` false: the customer's own word, or ``None`` when that word
      says "Active" (in any case): a record that contradicts itself is unknown;
    * ``active`` absent or unreadable: ``None``, never inactive.

    The customer's own word always stays in ``raw``.
    """
    # @unverified PW-10 — that a lease's active flag agrees with its status's
    # defineActive in GET /leases/statuses; settled by reading the distinct
    # statuses of the lease list beside each lease's active flag.
    active = lease_active(obj)
    if active is True:
        return ACTIVE_LEASE_STATUS
    if active is False:
        word = _str(obj.get("status"))
        if word is not None and word.casefold() == ACTIVE_LEASE_STATUS.casefold():
            return None
        return word
    return None


def map_lease(obj: Mapping[str, Any], units: UnitIndex | None) -> tuple[dict[str, Any], str]:
    """``LeaseDTO`` -> ``helixis_leases``, and the unit outcome.

    * ``rent`` is ``baseRent``; the term is ``startDate``/``endDate``. A
      move-out is not the lease end: it opens a turnover (:func:`lease_move_out`).
    * ``status`` is the readers' word for an active lease, else the
      customer's own (:func:`lease_status`).
    * ``security_deposit`` is unknown: Propertyware has no deposit-held amount
      on the read side (api-notes §10). Unknown, never 0.
    * ``unit_id`` by :func:`resolve_unit`; left as it was when the units read
      cannot say.

    The whole record stays in ``raw``: the customer's status word, and its
    ``contacts[]`` with each tenant's ``primary`` flag and ``role`` (the link
    table has no ``raw``).
    """
    building = _int(obj.get("buildingID"))
    outcome, unit = resolve_unit(_int(obj.get("unitID")), building, units)
    row: dict[str, Any] = {
        "buildium_id": _int(obj.get("id")),
        "property_id": building,
        "status": lease_status(obj),
        "lease_from_date": _date(obj.get("startDate")),
        "lease_to_date": _date(obj.get("endDate")),
        "rent": _money(obj.get("baseRent")),
        "security_deposit": None,
        "raw": withheld(dict(obj)),
    }
    if outcome != UNKNOWN:
        row["unit_id"] = unit
    return row, outcome


def lease_tenant_links(obj: Mapping[str, Any]) -> list[dict[str, Any]]:
    """An ACTIVE lease's ``contacts[]`` -> ``helixis_lease_tenants`` rows.

    ⚠ Only a lease whose ``active`` flag is true is linked. No reader filters
    on a link's status (cockpit, operations, renewals, the mirror tools,
    ``mirror_context`` and the reminder producer all read a link as "lives
    there now"), so a past, pending or unknown lease's contacts would read as
    living there. Rentvine shipped exactly that and fixed it on 2026-10-01.
    A lease that turns inactive loses its links at the next complete read
    (the shared writer prunes every link a complete read no longer lists), or
    at the next window that reads it (a delta's links are rewritten per lease
    read, ``sync.mirror_rows``).

    One link per contact, keyed on the contact id :func:`map_tenant` stores.
    A contact without a usable id still yields a row (with no tenant id), so
    the writer counts it and does not prune from a read that lost someone.
    ``move_in_date`` is unknown: Propertyware's move-in date is the lease's,
    not each tenant's, and a co-tenant added later would read the original.
    """
    # @unverified PW-1 — that Lease.contacts[].id is the contact id a tenant
    # row carries; settled by joining the probe's leases and contacts reads.
    lease_id = _int(obj.get("id"))
    if lease_id is None or lease_active(obj) is not True:
        return []
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    for contact in _dicts(obj.get("contacts")):
        tenant_id = _int(contact.get("id"))
        if tenant_id is not None:
            if tenant_id in seen:
                continue
            seen.add(tenant_id)
        out.append(
            {
                "lease_buildium_id": lease_id,
                "tenant_buildium_id": tenant_id,
                "status": LEASE_TENANT_STATUS,
                "move_in_date": None,
            }
        )
    return out


def lease_move_out(obj: Mapping[str, Any]) -> date | None:
    """The date a lease's tenants leave: ``moveOutDate`` ("Day that the
    tenant(s) vacated the property") when set, else ``scheduleMoveOutDate``
    ("expected to vacate"), else ``None``. What opens a turnover."""
    return _date(obj.get("moveOutDate")) or _date(obj.get("scheduleMoveOutDate"))


def portfolio_buildings(
    buildings: Iterable[Mapping[str, Any]],
) -> dict[int, set[int]]:
    """``{portfolio id: {building ids}}`` from the buildings read."""
    out: dict[int, set[int]] = {}
    for building in buildings:
        building_id = _int(building.get("id"))
        portfolio_id = _int(building.get("portfolioID"))
        if building_id is not None and portfolio_id is not None:
            out.setdefault(portfolio_id, set()).add(building_id)
    return out


def owners_from_portfolios(
    portfolios: Iterable[Mapping[str, Any]],
    buildings_by_portfolio: Mapping[int, set[int]] | None,
) -> list[dict[str, Any]]:
    """``Portfolio.owners[]`` -> ``helixis_owners`` rows, one per owner.

    An owner on several portfolios is one row, and owns the buildings of all
    of them. ``property_ids`` is written only when ``buildings_by_portfolio``
    comes from a COMPLETE buildings read; otherwise it is left as it was (a
    partial list would name an owner's properties as if it were all of them).
    An owner on a portfolio with no id has unknowable properties, so theirs
    is left as it was too.

    ``company_name`` is the owner's ``name`` only when no person's name is
    given: for a person, ``name`` is just their full name.
    """
    # @unverified PW-15 — that Portfolio.owners[].id is a contact id, and the
    # owner's name fields; settled against /contacts?type=OWNER on a live
    # account.
    rows: dict[int, dict[str, Any]] = {}
    # A row with no usable id is still returned, so the writer counts it.
    no_id: list[dict[str, Any]] = []
    portfolios_of: dict[int, set[int | None]] = {}
    for portfolio in portfolios:
        portfolio_id = _int(portfolio.get("id"))
        for owner in _dicts(portfolio.get("owners")):
            owner_id = _int(owner.get("id"))
            first, last = _str(owner.get("firstName")), _str(owner.get("lastName"))
            row = {
                "buildium_id": owner_id,
                "first_name": first,
                "last_name": last,
                "company_name": None if (first or last) else _str(owner.get("name")),
                "email": _str(owner.get("email")),
                "phone": _first(owner, _CONTACT_PHONES),
                "raw": withheld(dict(owner)),
            }
            if owner_id is None:
                no_id.append(row)
                continue
            rows.setdefault(owner_id, row)
            portfolios_of.setdefault(owner_id, set()).add(portfolio_id)
    for owner_id, row in rows.items():
        theirs = portfolios_of.get(owner_id, set())
        if buildings_by_portfolio is not None and None not in theirs:
            row["property_ids"] = sorted(
                {b for p in theirs if p is not None for b in buildings_by_portfolio.get(p, set())}
            )
    return [*rows.values(), *no_id]


def is_vendor_credit(obj: Mapping[str, Any]) -> bool:
    """Whether a ``BillDTO`` is a vendor credit ("Indicates if the bill is a
    vendor credit"). Only an explicit yes counts."""
    # @unverified PW-1 — that a credit is flagged on the bill read; settled by
    # the probe's first bills read.
    return _bool(obj.get("credit")) is True


def map_bill(obj: Mapping[str, Any]) -> dict[str, Any] | None:
    """``BillDTO`` -> ``helixis_bills``, or ``None`` for a vendor credit.

    * A vendor credit is not money anybody owes, so it is left out (with its
      lines), the way Rentvine leaves out a voided bill: in the table it would
      add to "what do we owe".
    * ``total_amount`` is the bill's ``amount``.
    * ``is_paid`` comes from the splits (:func:`bill_paid`): ``amountPaid``
      and ``paid`` are fields of the bill read, not a payment endpoint (W3).
    """
    # @unverified PW-1 — that a bill's amount is its total; settled by the
    # probe's first bills read.
    if is_vendor_credit(obj):
        return None
    return {
        "buildium_id": _int(obj.get("id")),
        "vendor_id": _int(obj.get("vendorID")),
        "bill_date": _date(obj.get("billDate")),
        "due_date": _date(obj.get("dueDate")),
        "total_amount": _money(obj.get("amount")),
        "is_paid": bill_paid(_dicts(obj.get("billSplits"))),
        "reference_number": _str(obj.get("refNo")),
        "work_order_id": _int(obj.get("workOrderID")),
        "raw": withheld(dict(obj)),
    }


def _split_paid(split: Mapping[str, Any]) -> bool | None:
    flag = _bool(split.get("paid"))
    if flag is not None:
        return flag
    amount, paid = _money(split.get("amount")), _money(split.get("amountPaid"))
    if amount is None or paid is None:
        return None
    return paid >= amount


def bill_paid(splits: list[dict[str, Any]]) -> bool | None:
    """Paid only when every split is paid. One unpaid split makes the bill
    unpaid (a part-paid bill carries its full total, as Buildium's path
    does); a split that cannot be read, with none unpaid, makes it unknown.
    No splits at all is unknown."""
    if not splits:
        return None
    unknown = False
    for split in splits:
        paid = _split_paid(split)
        if paid is False:
            return False
        if paid is None:
            unknown = True
    return None if unknown else True


def bill_lines(obj: Mapping[str, Any], units: UnitIndex | None) -> list[dict[str, Any]]:
    """A bill's ``billSplits[]`` -> ``helixis_bill_lines`` rows.

    A split charged to a portfolio and no building belongs to no single
    property: its ``property_id`` is unknown, and it carries the portfolio
    ledger (:data:`PORTFOLIO_LEDGER`, with the portfolio's id) so the spend
    report says how much its property totals leave out. Every other line
    leaves the ledger columns NULL, as Buildium's lines do.

    The unit follows the lease rule. A line cannot be left as it was (lines
    are replaced per bill), so a unit this pass cannot confirm is unknown.
    """
    bill_id = _int(obj.get("id"))
    out: list[dict[str, Any]] = []
    for index, split in enumerate(_dicts(obj.get("billSplits"))):
        building = _int(split.get("buildingID"))
        portfolio = _int(split.get("portfolioID"))
        # @unverified PW-1 — that a split with a portfolio and no building is
        # charged to the portfolio as a whole; settled by the probe's first
        # bills read, against the bill in Propertyware's own screen.
        portfolio_wide = building is None and portfolio is not None
        _outcome, unit = resolve_unit(_int(split.get("unitID")), building, units)
        out.append(
            {
                "bill_id": bill_id,
                "line_index": index,
                "property_id": building,
                "unit_id": unit,
                "gl_account_id": _int(split.get("glAccountID")),
                "amount": _money(split.get("amount")),
                "memo": _str(split.get("comments")),
                "ledger_id": portfolio if portfolio_wide else None,
                "ledger_type": PORTFOLIO_LEDGER if portfolio_wide else None,
            }
        )
    return out


# ── rent balances ─────────────────────────────────────────────────────────


class _Unknown:
    """The balance of a lease that came without a readable ``arBalance``."""

    def __repr__(self) -> str:
        return "BALANCE_UNKNOWN"


#: What :func:`lease_balance` returns for a lease whose balance did not come
#: through. Distinct from ``None`` (read, and owes nothing).
BALANCE_UNKNOWN: Any = _Unknown()


def _hidden_recharge(
    charge: Mapping[str, Any], posted: date, by_id: Mapping[int, Mapping[str, Any]]
) -> bool | None:
    """Whether ``charge`` is a hidden recharge, or ``None`` when it cannot be
    told. The spec: "If parent charge date and recharge date are same
    consider it as a hidden recharge and do not include it in ledger balance
    calculation." A parent this pass did not read leaves it unknown."""
    # @unverified PW-9 PW-14 — the hidden-recharge rule, and that a charge
    # with no parent sends parentTxID null or 0 (real ids being positive);
    # settled against Propertyware's aging report for a lease with a recharge.
    parent_id = _int(charge.get("parentTxID"))
    if parent_id is None or parent_id <= 0:
        return False
    parent = by_id.get(parent_id)
    if parent is None:
        return None
    parent_date = _date(parent.get("date"))
    if parent_date is None:
        return None
    return parent_date == posted


def lease_aging(
    charges: Iterable[Mapping[str, Any]],
    by_id: Mapping[int, Mapping[str, Any]],
    total: Decimal,
    today: date,
) -> dict[str, Decimal] | None:
    """The four aging buckets from one lease's open charges, or ``None`` when
    they cannot be stated.

    A charge is open when its ``amountDue`` is above zero. Hidden recharges
    are dropped first. Each open charge goes in a bucket by the days since its
    post ``date``, in the company's calendar (``today``).

    ⚠ ONLY WHEN THEY ADD UP. The buckets are stated only if the open charges
    sum to the lease's ``arBalance`` to the cent. A credit on account, an
    unread charge, or a misread recharge would otherwise put a wrong number in
    a bucket, and an unknown bucket must never read as $0 (rule 14). Any
    charge whose amount due or date does not parse makes them unknown too.
    """
    # @unverified PW-9 — that amountDue is what is still owed on a charge;
    # settled against Propertyware's aging report.
    buckets = {name: Decimal(0) for name, _ in BUCKETS}
    owed_total = Decimal(0)
    for charge in charges:
        due = _money(charge.get("amountDue"))
        if due is None:
            return None
        if due <= 0:
            continue
        posted = _date(charge.get("date"))
        if posted is None:
            return None
        hidden = _hidden_recharge(charge, posted, by_id)
        if hidden is None:
            return None
        if hidden:
            continue
        owed_total += due
        age = (today - posted).days
        for name, limit in BUCKETS:
            if limit is None or age <= limit:
                buckets[name] += due
                break
    if owed_total.quantize(_CENT) != total.quantize(_CENT):
        return None
    return buckets


def charges_by_lease(
    charges: Iterable[Mapping[str, Any]],
) -> tuple[dict[int, list[Mapping[str, Any]]], dict[int, Mapping[str, Any]]]:
    """``({lease id: its charges}, {charge id: charge})`` from the charges read.
    A charge with no lease belongs to no lease's aging."""
    by_lease: dict[int, list[Mapping[str, Any]]] = {}
    by_id: dict[int, Mapping[str, Any]] = {}
    for charge in charges:
        charge_id = _int(charge.get("id"))
        if charge_id is not None:
            by_id[charge_id] = charge
        lease_id = _int(charge.get("leaseID"))
        if lease_id is not None:
            by_lease.setdefault(lease_id, []).append(charge)
    return by_lease, by_id


def lease_balance(
    obj: Mapping[str, Any],
    charges: Iterable[Mapping[str, Any]] | None,
    by_id: Mapping[int, Mapping[str, Any]],
    units: UnitIndex | None,
    today: date,
) -> Any:
    """One lease -> a ``helixis_lease_balances`` row, ``None`` when it owes
    nothing, or :data:`BALANCE_UNKNOWN`.

    The total is Propertyware's own ``arBalance`` (read with
    ``includeOtherBalances=true``). Rows exist only for leases that owe, as on
    the Buildium path, whose readers count every row as a delinquent lease;
    a lease in credit owes nothing. ``charges`` is ``None`` when the charges
    read failed or was incomplete: the total stands and every bucket is
    unknown.
    """
    # @unverified PW-6 — that the list returns arBalance only with
    # includeOtherBalances=true; settled by one read with it and one without.
    # @unverified PW-7 — what arBalance means beside leaseBalance; settled by
    # 2-3 leases against Propertyware's screen.
    lease_id = _int(obj.get("id"))
    total = _money(obj.get("arBalance"))
    if lease_id is None or total is None:
        return BALANCE_UNKNOWN
    if total <= 0:
        return None
    aging = lease_aging(charges, by_id, total, today) if charges is not None else None
    building = _int(obj.get("buildingID"))
    _outcome, unit = resolve_unit(_int(obj.get("unitID")), building, units)
    return {
        "lease_id": lease_id,
        "property_id": building,
        "unit_id": unit,
        "total_balance": total,
        **{name: (aging[name] if aging is not None else None) for name, _ in BUCKETS},
        "raw": {"leaseID": obj.get("id"), "arBalance": obj.get("arBalance")},
    }


__all__ = [
    "ACTIVE_LEASE_STATUS",
    "BALANCE_UNKNOWN",
    "BUCKETS",
    "GL_TYPES",
    "LEASE_TENANT_STATUS",
    "MONTHLY_RENT_UNITS",
    "NOT_FOUND",
    "NO_UNIT",
    "PORTFOLIO_LEDGER",
    "RAW_WITHHELD",
    "SINGLE_FAMILY",
    "UNIT",
    "UNKNOWN",
    "UnitIndex",
    "bill_lines",
    "bill_paid",
    "charges_by_lease",
    "gl_type",
    "is_bank_type",
    "is_vendor_credit",
    "lease_active",
    "lease_aging",
    "lease_balance",
    "lease_move_out",
    "lease_status",
    "lease_tenant_links",
    "map_bill",
    "map_building",
    "map_gl_account",
    "map_lease",
    "map_tenant",
    "map_unit",
    "map_vendor",
    "map_work_order",
    "owners_from_portfolios",
    "portfolio_buildings",
    "record_id",
    "resolve_unit",
    "unit_occupied",
    "withheld",
]
