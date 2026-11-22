"""Rentvine objects -> mirror rows. ADR 0010 Decision (a), Phase 4.

Pure functions, no I/O: a Rentvine JSON object in, a dict of mirror columns
out. That makes every mapping decision unit-testable against a fixture without
a database or a network, which matters because **none of this has been run
against a Rentvine server** and the fixtures are the only thing standing
between a field-name guess and a confidently wrong mirror.

THE SHAPE, from ADR 0010 Decision (a) revision 3.1
    The PMS's record id goes in the identity column the table ALREADY has
    (``buildium_id``), and ``pms`` says which system issued it. There is no
    ``external_id``: the mirror holds 65 ``bigint`` relationship columns whose
    both sides must stay ``bigint``, and a text identity silently breaks every
    join (probed — the Rentvine join returns 0 rows with no error).

EVERY FIELD NAME BELOW IS SOURCED TO THE CAPTURED SPEC
    ``docs/rentvine/vendor/docs.rentvine.com/openapi.json``, with ``allOf``
    resolved — resolving it matters, because ``property`` is a composition and
    reads as zero fields otherwise. Sourced is not observed: ``@unverified``
    marks every place where the spec is silent, ambiguous, or says something a
    live call could contradict.

⚠ WHAT IS NOT HERE
    Money. Rentvine keys transactions to a polymorphic ``ledger``, not to a
    property (ADR 0010 Decision (b)), so ledger-shaped rows need
    ``ledger_id``/``ledger_type`` columns this phase does not add. Mapping them
    on the property-grained assumption is the one thing that ADR forbids
    outright, so this module maps ENTITIES only.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any

#: The discriminator value for every row this module produces.
PMS = "rentvine"

# ---------------------------------------------------------------------------
# Enum labels, transcribed from the spec's own `description` text.
# ---------------------------------------------------------------------------

#: T1 ``primary_lease_statuses``: ``1 Pending / 2 Active / 3 Closed``.
LEASE_STATUS: dict[int, str] = {1: "Pending", 2: "Active", 3: "Closed"}

#: T1 ``priorities``: ``1 Low / 2 Medium / 3 High``.
WORK_ORDER_PRIORITY: dict[int, str] = {1: "Low", 2: "Medium", 3: "High"}

#: T1 ``primary_statuses``: ``1 Pending / 2 Open / 3 Closed / 4 On Hold``.
#:
#: ⚠ Mapped onto the vocabulary ``buildium/maintenance_status.py`` already owns,
#: because gotcha 50 is the standing record of two spellings of "open" drifting
#: apart and giving one question two answers. That module's CLOSED set is
#: ``("Completed", "Closed", "Cancelled")`` and open is its complement, so
#: Rentvine's ``3 Closed`` must land on the exact string ``"Closed"`` — anything
#: else reads as OPEN and a closed work order shows up as outstanding.
#:
#: ⚠ ``On Hold`` deliberately maps to itself rather than to a closed value. It
#: is not in the known set, and `is_open` treats an unrecognised status as OPEN,
#: which over-reports a visible number instead of quietly dropping work.
WORK_ORDER_STATUS: dict[int, str] = {
    1: "New",
    2: "InProgress",
    3: "Closed",
    4: "On Hold",
}

#: T1 ``property_types``, 1-10.
PROPERTY_TYPE: dict[int, str] = {
    1: "Single Family Home",
    2: "Apartment",
    3: "Condo",
    4: "Townhouse",
    5: "Duplex",
    6: "Multiplex",
    7: "Loft",
    8: "MobileHome",
    9: "Commercial",
    10: "Garage",
}


def _int(value: Any) -> int | None:
    """A Rentvine id as an ``int``, or ``None``.

    ⚠ This is where ADR 0010's load-bearing assumption is enforced rather than
    assumed. The design stores Rentvine ids in ``bigint`` columns because every
    entity id in the spec is an integer or a numeric string — but the spec is
    internally inconsistent about id types (``unit.unitID`` is declared
    ``string`` while ``lease.unitID`` is ``integer``), so this coerces and
    returns ``None`` on anything that is not a whole number.

    ⚠ It does NOT raise, and that is deliberate: a non-numeric id would mean the
    ADR's decision is wrong, which is a design problem to surface from a probe,
    not a crash to inflict on a sync. ``sync.py`` counts these and refuses to
    write a row whose identity is missing.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        text = str(value).strip()
    except Exception:  # noqa: BLE001 - defensive on arbitrary JSON
        return None
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _str(value: Any) -> str | None:
    """A trimmed string, or ``None`` for absent/blank."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _flag(value: Any) -> bool | None:
    """A Rentvine yes/no as a ``bool``, or ``None`` when it is not one.

    ⚠ ``bool()`` IS WRONG HERE, and it was the shipped coercion for
    ``is_active``. Measured 2026-10-02 on the sandbox: ``isActive`` arrives
    as the STRING ``"1"`` on every property and vendor, and the spec's own
    examples send ``"0"`` for no. ``bool("0")`` is ``True``, so every inactive
    property and vendor would have mirrored as active. Unknown stays ``None``
    rather than guessing either way (rule 14).
    """
    if value is True or value is False:
        return value
    text = str(value).strip().lower() if value is not None else ""
    if text in {"1", "true"}:
        return True
    if text in {"0", "false"}:
        return False
    return None


def _date(value: Any) -> date | None:
    """A Rentvine date string as a ``datetime.date``, or ``None``.

    ⚠ **A STRING HERE ABORTS THE WHOLE SYNC, and that was the shipped state.**
    ``lease.startDate`` is ``{"type": "string", "format": "date"}`` in the
    captured spec, ``helixis_leases.lease_from_date`` is ``timestamptz``, and
    ``sync._upsert`` binds it with no cast — so Postgres types the parameter
    from the target column and asyncpg encodes it with ``timestamptz_encode``,
    which takes a ``datetime.date`` or ``None`` and nothing else
    (``tests/unit/_pg_bind_contract.py`` READ that out of the installed 0.31.0).
    ``_sync_leases`` catches only ``RentvineAPIError``, so the ``DataError``
    propagated out of ``sync_company`` — and ``_sync_units`` runs on the NEXT
    line, so leases AND units stayed permanently empty with no ``SyncResult``
    ever returned to say which part of the mirror was stale.

    ⚠ **It returns ``None`` rather than raising.** A lease whose date will not
    parse still has a tenant, a unit and a rent worth mirroring, and one
    unreadable field must not cost the row — let alone the two tables after it.

    ⚠ **AND THAT NULL IS SILENT, which is a residual rather than a fix.** A
    NULL ``lease_to_date`` drops the lease out of the 90-day renewal window, so
    nobody is reminded about a renewal that is genuinely due. Nothing here logs
    the value it could not read: this module is pure by design (see the header
    — that is what makes every mapping decision testable with no database and
    no network), it runs once per row in a loop, and the sync layer above it
    cannot see WHICH field failed. Closing it properly means returning the
    unparsed values alongside the row so ``sync.py`` can report them, which is
    a signature change across seven mappers — worth doing deliberately, not as
    a side effect of fixing the abort. Recorded rather than papered over; the
    first live sandbox sync is what says whether any real date needs it.

    ⚠ **A full ISO timestamp is accepted even though the spec says ``date``.**
    Costing nothing and unverified against a live server, it is the difference
    between a working sync and a dead one if Rentvine sends
    ``2026-03-18T00:00:00Z`` on any endpoint. Taking the calendar part is
    correct rather than lossy: a lease term is a CALENDAR date, not an instant
    (gotcha 97), and the column already holds Buildium's dates the same way.

    ⚠ **``text[:10]`` IS the timestamp handling — a ``datetime.fromisoformat``
    arm ahead of it was DELETED as redundant, not dropped by accident.** It
    could not change the answer for any input: measured across
    ``2026-03-18T00:00:00Z``, ``…T14:32:00+00:00``, ``20260318`` and even
    ``…T25:00:00`` (which the datetime parser REFUSES), truncation alone
    returns the right date every time. Dead code wearing a safety check's
    costume, kept alive by a mutation that could not fail (gotcha 58).
    """
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
    """A Rentvine timestamp as an aware ``datetime``, or ``None``.

    MEASURED 2026-10-03: ``dateTimeModified`` arrives as ``"YYYY-MM-DD
    HH:MM:SS"`` with no zone on all 8 sandbox work orders. Like :func:`_date`,
    a value that will not parse is ``None``, never a raise (gotcha 148).
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = _str(value)
        if text is None:
            return None
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    # @unverified the zone of a zoneless Rentvine timestamp: read as UTC. A
    # few hours either way cannot move a 7-day stall. Settled on the sandbox by
    # editing a work order in Rentvine's UI and comparing the stamp it reads.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _money(value: Any) -> Decimal | None:
    """A Rentvine money field as a ``Decimal``, or ``None``.

    The mirror's money columns are ``numeric(12, 2)`` and the spec is
    inconsistent about what it sends into them: ``unit.rent`` is declared
    ``{"type": "string", "format": "double"}`` while ``lease.rentAmount`` is
    ``{"type": "number"}``. A ``Decimal`` is what a ``numeric`` column wants
    from either.

    ⚠ **This one is DEFENSIVE, not measured, and the difference is worth
    stating.** The date break above is proven — the driver's rule for
    ``timestamptz`` was read out of asyncpg's own encoder. Whether asyncpg
    *also* refuses a ``str`` for ``numeric`` could not be settled from this
    container (the codec is compiled and ships no readable source), so this
    is not a claim that money was broken. It is that a ``numeric`` column
    takes a number whatever the driver tolerates, the conversion costs
    nothing, and the spec already declares one of the two as a string.

    ⚠ ``float`` is deliberately NOT the intermediate. ``Decimal(str(value))``
    keeps ``"1800.00"`` exact; going through ``float`` first reintroduces the
    binary-fraction error that a ``numeric`` column exists to avoid.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    text = _str(value)
    if text is None:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _first_contact_value(value: Any) -> str | None:
    """One email or phone out of a contact's field.

    The spec names ``contact.emails`` and ``contact.phones`` PLURAL and types
    them ``["string", null]``, so a list, a delimited string and a plain string
    are all handled: a list takes its first entry, a delimited string the part
    before the first delimiter, and a plain string passes through.

    MEASURED 2026-10-03 (all 53 sandbox contacts): the list routes send neither
    plural key. They send ``email`` and ``phone`` instead, as a plain string or
    null, and none of the 30 populated values was delimited. :func:`_contact`
    reads that key first and keeps the plural as the spec-shaped fallback.
    """
    if isinstance(value, list):
        for item in value:
            got = _str(item if not isinstance(item, dict) else item.get("value"))
            if got:
                return got
        return None
    text = _str(value)
    if text is None:
        return None
    for delimiter in (",", ";", "|"):
        if delimiter in text:
            return _str(text.split(delimiter)[0])
    return text


def _contact(obj: dict[str, Any], key: str) -> str | None:
    """A contact's ``email`` or ``phone``: the key the server sends, then the
    spec's plural (``emails`` / ``phones``).

    ⚠ The mappers read ONLY the plural until 2026-10-03, which no live row
    carries, so 0 of 53 sandbox contacts had an email or a phone stored while
    Rentvine sent one for 30 (gotcha 186). Contact lookup said "no email on
    file", and an inbound text could never match its tenant.
    """
    return _first_contact_value(obj.get(key)) or _first_contact_value(obj.get(f"{key}s"))


def _address_line(obj: dict[str, Any]) -> str | None:
    """Street line, preferring the composed field over the parts.

    Rentvine carries BOTH ``address`` and a ``streetNumber``/``streetName``
    pair. The composed one wins when present; the parts are the fallback rather
    than an unused duplicate.
    """
    composed = _str(obj.get("address"))
    if composed:
        return composed
    number, street = _str(obj.get("streetNumber")), _str(obj.get("streetName"))
    joined = " ".join(p for p in (number, street) if p)
    return joined or None


# ---------------------------------------------------------------------------
# Entity mappers. Each returns the mirror's own column names.
# ---------------------------------------------------------------------------


def map_property(obj: dict[str, Any]) -> dict[str, Any]:
    """T1 ``property`` -> ``helixis_properties``."""
    return {
        "buildium_id": _int(obj.get("propertyID")),
        "pms": PMS,
        "name": _str(obj.get("name")),
        # ⚠ @unverified — the mirror's `rental_sub_type` is Buildium's
        # sub-type string. Rentvine's nearest equivalent is `propertyTypeID`,
        # an enum this module transcribes from the spec's own description text.
        # The VALUES therefore differ from Buildium's vocabulary; nothing reads
        # this column for classification today, so it carries Rentvine's label
        # rather than a forced translation into Buildium's. Settled by a Phase 8
        # probe: read one property and compare `propertyTypeID` against this table.
        "rental_sub_type": PROPERTY_TYPE.get(_int(obj.get("propertyTypeID")) or -1),
        "address_line1": _address_line(obj),
        "city": _str(obj.get("city")),
        # ⚠ @unverified — `stateID`, despite the ID suffix, carries a 2-letter
        # code in the spec's own examples ('FL', 'TX'). It is one of exactly
        # nine id-named fields in the whole spec whose example is not numeric.
        # Mapped as a string rather than coerced to an int, which would silently
        # null every address. Settled by a Phase 8 probe: read one property and
        # look at what `stateID` actually carries.
        "state": _str(obj.get("stateID")),
        "postal_code": _str(obj.get("postalCode")),
        "is_active": _flag(obj.get("isActive")),
        "raw": obj,
    }


def map_unit(obj: dict[str, Any]) -> dict[str, Any]:
    """T1 ``unit`` -> ``helixis_units``."""
    return {
        "buildium_id": _int(obj.get("unitID")),
        "pms": PMS,
        "property_id": _int(obj.get("propertyID")),
        # ⚠ The unit's label is `name`, NOT `unitNumber`. Rentvine has no
        # `unitNumber` field at all; reaching for the obvious name would have
        # nulled every unit label.
        "unit_number": _str(obj.get("name")),
        # ⚠ `unit.rent` is declared `{"type": "string", "format": "double"}` and
        # `market_rent` is `numeric(12, 2)` (migration 0038) — see `_money`.
        "market_rent": _money(obj.get("rent")),
        # ⚠ @unverified — Rentvine declares no occupancy flag. `unit.leaseID`
        # is present, so "has a lease attached" is the only available reading,
        # and it is an INFERENCE rather than a field: a unit with a closed lease
        # still carries the id. Recorded as such rather than presented as
        # occupancy the server reported. A probe on a known-vacant unit settles
        # whether leaseID clears — `rentvine_probe.py` probe J, which compares
        # the vacant group against an OCCUPIED control, because "vacant units
        # carry a leaseID" and "every unit carries one" are the same
        # observation until something tells them apart.
        "is_occupied": _int(obj.get("leaseID")) is not None,
        "raw": obj,
    }


def map_unit_export(obj: dict[str, Any]) -> dict[str, Any]:
    """A ``GET /properties/units/export`` row -> ``helixis_units``.

    The row is the ``unit`` record with its ``property`` sibling beside it
    (``client.iter_all(..., companion="property")``). ``propertyID`` is on the
    sibling, and occupancy is the export's own ``isVacant`` flag
    (:func:`export_unit_occupied`), never ``map_unit``'s ``leaseID`` inference.
    """
    sibling = obj.get("property")
    unit = {k: v for k, v in obj.items() if k != "property"}
    pid = sibling.get("propertyID") if isinstance(sibling, dict) else None
    out = map_unit({**unit, "propertyID": pid})
    out["is_occupied"] = export_unit_occupied(obj)
    out["raw"] = obj
    return out


def export_unit_occupied(obj: dict[str, Any]) -> bool | None:
    """An export unit's occupancy from its ``isVacant`` flag, or ``None``
    (UNKNOWN, never vacant) when the flag is absent or not a yes/no.

    MEASURED 2026-10-03 on the sandbox: every one of the 100 units the export
    filled carried ``isVacant`` as the string ``"1"``, each with no Active lease
    in the mirror. Until then this returned ``None`` for all of them, and every
    unit past the 20th on a property read "occupancy unknown" (gotcha 187).
    ⚠ ``map_unit``'s ``leaseID`` reading is NOT used here: the key is on the
    export row but null on all 100, which proves nothing about an occupied one.
    """
    # @unverified that "0" means occupied: no occupied unit came through the
    # export on the sandbox, so only the vacant side has been seen. Settled
    # once a unit past the 20th on a sandbox property has an Active lease.
    vacant = _flag(obj.get("isVacant"))
    return None if vacant is None else not vacant


def map_lease(obj: dict[str, Any]) -> dict[str, Any]:
    """T1 ``lease`` -> ``helixis_leases``."""
    return {
        "buildium_id": _int(obj.get("leaseID")),
        "pms": PMS,
        "property_id": _int(obj.get("propertyID")),
        "unit_id": _int(obj.get("unitID")),
        # `primaryLeaseStatusID` is the coarse 3-value enum; `leaseStatusID` is
        # a finer per-account one the spec does not enumerate, so the coarse one
        # is what can be mapped honestly.
        "status": LEASE_STATUS.get(_int(obj.get("primaryLeaseStatusID")) or -1),
        "lease_from_date": _date(obj.get("startDate")),
        "lease_to_date": _date(obj.get("endDate")),
        "rent": _money(obj.get("rentAmount")),
        # ⚠ @unverified — Rentvine carries NO deposit on the lease. `unit.deposit`
        # exists, but that is the unit's asking deposit, not what this tenant
        # actually paid, and writing one as the other would be a confident wrong
        # number about money (gotchas 26/41/51). Left NULL, which
        # `expiring_leases` says is not on file, never "no deposit".
        # The spec documents no per-lease deposit (2026-10-03 read of
        # docs/rentvine/vendor/.../openapi.json). Settled by `rentvine_probe.py`
        # probe I, which reports the key UNION across a page of leases rather
        # than one row: a server that omits nulls shows the field on the row
        # that has a value and hides it on the row that does not, so one clean
        # row proves nothing.
        "security_deposit": None,
        "raw": obj,
    }


def map_tenant(obj: dict[str, Any]) -> dict[str, Any]:
    """T1 ``contact`` (via ``GET /tenants``) -> ``helixis_tenants``.

    ⚠ Rentvine has ONE person model. Tenants, owners and vendors are all
    ``contact`` rows distinguished by ``contactTypeID``, where Buildium has
    three separate entities. So the three mappers below differ only in which
    mirror table they target — that is Rentvine's shape, not duplication.
    """
    return {
        "buildium_id": _int(obj.get("contactID")),
        "pms": PMS,
        "first_name": _str(obj.get("firstName")),
        "last_name": _str(obj.get("lastName")),
        "email": _contact(obj, "email"),
        "phone": _contact(obj, "phone"),
        "raw": obj,
    }


def map_owner(obj: dict[str, Any]) -> dict[str, Any]:
    """T1 ``contact`` (via ``GET /owners``) -> ``helixis_owners``."""
    return {
        "buildium_id": _int(obj.get("contactID")),
        "pms": PMS,
        "first_name": _str(obj.get("firstName")),
        "last_name": _str(obj.get("lastName")),
        "company_name": _str(obj.get("name")),
        "email": _contact(obj, "email"),
        "phone": _contact(obj, "phone"),
        # Rentvine models ownership through PORTFOLIOS, not a property list, so
        # `GET /owners` cannot fill this. It is written EMPTY on first insert
        # and never overwritten by this upsert (sync._PRESERVED_ON_UPDATE); the
        # real list is joined after the fact by `sync._link_owner_properties`
        # from each property's `portfolioID` and `GET /portfolios`' contacts.
        "property_ids": [],
        "raw": obj,
    }


def map_vendor(obj: dict[str, Any]) -> dict[str, Any]:
    """T1 ``contact`` (via ``GET /vendors``) -> ``helixis_vendors``.

    ⚠⚠ @unverified, and MORE SO than the others. ``GET /vendors``'s 200 response
    resolves to no row schema at all in the captured spec — only two enum
    ``$ref``s — so unlike tenants and owners there is no documented field list
    to map from. The fields below are the ``contact`` shape on the assumption
    that a vendor is a contact like the other two, which the ``vendorTypeID``
    field on ``contact`` supports but does not prove. **A single live
    ``GET /vendors`` settles it, and until it runs this mapper is the least
    trustworthy in the module.**
    """
    first, last = _str(obj.get("firstName")), _str(obj.get("lastName"))
    company = _str(obj.get("name"))
    return {
        "buildium_id": _int(obj.get("contactID")),
        "pms": PMS,
        "name": company or " ".join(p for p in (first, last) if p) or None,
        "is_company": bool(company and not (first or last)),
        "first_name": first,
        "last_name": last,
        "company_name": company,
        "is_active": _flag(obj.get("isActive")),
        "category_id": _int(obj.get("vendorTypeID")),
        # ⚠ The spec gives no name for `vendorTypeID`; `GET /maintenance/vendor-trades`
        # is the lookup that would resolve it. Not fetched here — a second call
        # per vendor is the N+1 shape gotchas 25/32/50 all record, and the id
        # alone is honest.
        "category_name": None,
        "phone": _contact(obj, "phone"),
        "email": _contact(obj, "email"),
        "raw": obj,
    }


def map_work_order(obj: dict[str, Any]) -> dict[str, Any]:
    """T1 ``work_order`` -> ``helixis_work_orders``."""
    return {
        "buildium_id": _int(obj.get("workOrderID")),
        "pms": PMS,
        # ⚠ Buildium's work orders are backed by a to-do task and the mirror
        # carries its id (gotcha 20 — status is DERIVED from that task). Rentvine
        # has no such concept, so this stays NULL. Nothing may infer "no task"
        # means anything about a Rentvine row.
        "task_id": None,
        "property_id": _int(obj.get("propertyID")),
        "unit_id": _int(obj.get("unitID")),
        # ⚠ Rentvine has no `title`. `description` is the only human-readable
        # summary, so it fills the mirror's title rather than leaving every
        # work order unlabelled.
        "title": _str(obj.get("description")),
        "status": WORK_ORDER_STATUS.get(_int(obj.get("primaryWorkOrderStatusID")) or -1),
        "priority": WORK_ORDER_PRIORITY.get(_int(obj.get("priorityID")) or -1),
        "category_id": _int(obj.get("vendorTradeID")),
        "assigned_to_user_id": _int(obj.get("assignedToUserID")),
        # ⚠ What "stalled" reads (migration 0105). The mirror's own updated_at
        # moves on every pass, so before this no Rentvine work order could
        # ever read as stalled (gotcha 190).
        "pms_updated_at": _instant(obj.get("dateTimeModified")),
        "raw": obj,
    }


#: The status a Rentvine lease-tenant link is written with: Buildium's own word
#: for a current link, so every reader of ``helixis_lease_tenants`` sees one
#: vocabulary. ⚠ Only ACTIVE links are written at all — no reader filters on
#: this column (cockpit, operations, renewals, mirror_tools, mirror_context and
#: the reminder producer all treat every link as a current tenant), so an
#: inactive tenant written here would be read as living there (2026-10-01).
LEASE_TENANT_STATUS = "Current"


def _active(value: Any) -> bool:
    """Rentvine's ``isActive``: an int on the export (measured 2026-10-01), a
    ``"1"``/``"0"`` string on ``leaseTenant``, a boolean in the spec. Only an
    explicit yes is active; anything else, absent included, is not."""
    return value is True or value == 1 or value == "1"


def map_lease_tenant_links(lease: dict[str, Any]) -> list[dict[str, Any]]:
    """``GET /leases/export`` ``lease.tenants`` -> ``helixis_lease_tenants`` rows.

    One row per ACTIVE tenant with an integer ``contactID``. ``contactID`` is
    the id :func:`map_tenant` already stores, so a link joins its tenant row
    directly. ``move_in_date`` is not on the export; the sync fills it from
    :func:`lease_tenant_move_ins` for leases it read this pass.
    """
    lease_id = _int(lease.get("leaseID"))
    if lease_id is None:
        return []
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    for tenant in lease.get("tenants") or []:
        if not isinstance(tenant, dict) or not _active(tenant.get("isActive")):
            continue
        contact_id = _int(tenant.get("contactID"))
        if contact_id is None or contact_id in seen:
            continue
        seen.add(contact_id)
        out.append(
            {
                "lease_buildium_id": lease_id,
                "tenant_buildium_id": contact_id,
                "status": LEASE_TENANT_STATUS,
                "move_in_date": None,
            }
        )
    return out


def lease_move_out(obj: dict[str, Any]) -> date | None:
    """A lease-export row's move-out date: the actual ``moveOutDate`` when it
    is set, else the ``expectedMoveOutDate`` from the tenant's notice, else
    ``None``. Both are declared on the spec's ``lease_export``; this is what
    opens a turnover (``sync._start_turnovers``), the way Buildium's
    ``Lease.MoveOut.Created`` webhook does."""
    return _date(obj.get("moveOutDate")) or _date(obj.get("expectedMoveOutDate"))


def lease_tenant_move_ins(tenants: Any) -> dict[int, date]:
    """``GET /leases/{leaseID}?includes=tenants`` -> ``{contactID: moveInDate}``.

    Each item is ``{leaseTenant, contact}``; the date is converted here, never
    passed through as the spec's string (gotcha 148)."""
    out: dict[int, date] = {}
    for item in tenants or []:
        link = item.get("leaseTenant") if isinstance(item, dict) else None
        if not isinstance(link, dict):
            continue
        contact_id = _int(link.get("contactID"))
        moved_in = _date(link.get("moveInDate"))
        if contact_id is not None and moved_in is not None:
            out[contact_id] = moved_in
    return out


#: Rentvine ``transactionTypeID`` (the spec's ``transaction_types`` enum) for
#: the money a LEASE ledger carries -> ``(Buildium transaction_type, sign)``.
#:
#: The sign is Buildium's: a ledger row is signed by its EFFECT ON THE BALANCE
#: (``buildium/money.PAYMENTS_ARE_NEGATIVE``), so a charge is positive and a
#: payment negative, and every reader (``payments_received_sql``, the analyst,
#: the detectors) negates ``Payment`` rows on that understanding. A payment
#: RETURN is a ``Payment`` row signed positive, which is what money.py says a
#: reversal looks like, so "collected this month" nets it out.
#:
#: The direction comes from the TYPE and the magnitude from ``amount``, so the
#: row is right whether Rentvine sends unsigned magnitudes or already-signed
#: amounts (the spec declares only ``"type": "string"``): a reversal has its
#: own type here, so a sign inside one type carries nothing the type does not.
#: Measured live 2026-10-02: a $100 Lease Payment (type 2) posted to the
#: sandbox arrived as ``"amount": "100.00"``, unsigned, and was stored as
#: -100.00; the type is what carries the direction.
LEASE_TRANSACTION_TYPE: dict[int, tuple[str, int]] = {
    1: ("Charge", 1),  # Lease Charge
    2: ("Payment", -1),  # Lease Payment
    3: ("Credit", -1),  # Lease Credit
    4: ("Payment", 1),  # Lease Payment Return: money back out
}

#: The other types a lease ledger can carry. Stored with Rentvine's own name and
#: NO amount: their effect on the balance is not something the spec states, and
#: a guessed sign is a wrong number, where NULL is an honest unknown (rule 14).
#: No reader sums them; they ride along so the history is complete.
LEASE_TRANSACTION_UNSIGNED: dict[int, str] = {
    19: "Lease Deposit Release Credit",
    20: "Lease Payout",
    21: "Lease Payout Return",
}


def lease_transactions(items: Any, lease_id: int) -> list[dict[str, Any]] | None:
    """``GET /leases/{leaseID}?includes=transactions`` -> ``helixis_lease_transactions``
    rows for that lease, or ``None`` when the include did not come back as a
    list (unknown, so the caller must not replace what it has).

    The spec says "the lease's non-voided transactions, newest first" and types
    each item only as ``object``. An item is read as a ``transaction`` record
    whether bare or wrapped ``{transaction}`` (the shape the same response
    uses for ``chargesForPayment``). Which of the two is NOT VERIFIED (the
    marker below).

    - A voided row, one with no ``transactionID``, and a type that is not lease
      money (a bill charge on a lease ledger is not rent) are left out.
    - ``transaction_date`` is ``datePosted`` at midnight UTC: a ``timestamptz``
      column, and readers compare it against the start of the month.
    - ``total_amount`` is signed by :data:`LEASE_TRANSACTION_TYPE`, or NULL when
      the type's direction or the amount is unknown.
    """
    # ⚠ @unverified — whether an item arrives bare or wrapped {transaction}.
    # Phase 8 reports the count of each (runbook "Chart of accounts,
    # applicants and rent payments").
    if not isinstance(items, list):
        return None
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        wrapped = item.get("transaction")
        txn: dict[str, Any] = wrapped if isinstance(wrapped, dict) else item
        txn_id = _int(txn.get("transactionID"))
        kind = _int(txn.get("transactionTypeID"))
        if txn_id is None or _int(txn.get("isVoided")) == 1:
            continue
        signed = LEASE_TRANSACTION_TYPE.get(kind) if kind is not None else None
        if signed is not None:
            label: str = signed[0]
            amount = _money(txn.get("amount"))
            total = None if amount is None else abs(amount) * signed[1]
        elif kind in LEASE_TRANSACTION_UNSIGNED:
            label, total = LEASE_TRANSACTION_UNSIGNED[kind], None
        else:
            continue
        posted = _date(txn.get("datePosted"))
        out.append(
            {
                "buildium_id": txn_id,
                "lease_id": lease_id,
                "transaction_date": (
                    None if posted is None else datetime.combine(posted, time(), tzinfo=UTC)
                ),
                "transaction_type": label,
                "total_amount": total,
                "raw": txn,
            }
        )
    return out


#: Every mapper, by the mirror table it targets. ``sync.py`` iterates this, so
#: adding an entity means adding one entry rather than editing a loop.
#: Rentvine ``primaryApplicationStatusID`` -> Buildium's applicant ``Status``
#: vocabulary (migration 0040's enum), which every applicant reader speaks.
#: The ids are the spec's ``primary_application_statuses`` enum; the custom
#: sub-status an account adds rides along in ``raw``.
APPLICATION_STATUS: dict[int, str] = {
    1: "Draft",  # Pending: started, not submitted
    2: "New",  # Submitted
    3: "Undecided",  # Screening
    4: "Undecided",  # Processing
    5: "Deferred",  # On Hold
    6: "Approved",
    7: "Rejected",  # Declined
    8: "Cancelled",  # Withdrawn
}


def map_applicant(obj: dict[str, Any]) -> dict[str, Any]:
    """T1 ``application`` (via ``GET /screening/applications``, with its
    ``status``, ``property`` and ``lease`` carried beside it) ->
    ``helixis_applicants``.

    ⚠ ONE ROW PER APPLICATION, NOT PER PERSON. Buildium's applicant is a
    person with an email and a phone; Rentvine's application lists its
    applicants as ONE comma-separated names string and carries no contact
    for them. So the first named applicant fills ``first_name`` /
    ``last_name``, every name stays in ``raw["applicants"]``, and ``email`` /
    ``phone`` are ``None``: unknown, never invented. A lead can therefore not
    be matched to a Rentvine applicant by contact (CRM auto-move is
    Buildium-webhook-only today anyway).

    An application with a created lease reads ``AddedToLease`` whatever its
    status says, as Buildium's does.

    NOT VERIFIED on a live row (the marker below).
    """
    # ⚠ @unverified — whether `applicants` is the names string the spec
    # declares, and the status ids. Runbook "Chart of accounts, applicants and
    # rent payments" settles it against Rentvine's applications screen.
    raw_status, raw_prop, raw_lease = obj.get("status"), obj.get("property"), obj.get("lease")
    status: dict[str, Any] = raw_status if isinstance(raw_status, dict) else {}
    prop: dict[str, Any] = raw_prop if isinstance(raw_prop, dict) else {}
    lease: dict[str, Any] = raw_lease if isinstance(raw_lease, dict) else {}
    names = [n.strip() for n in str(obj.get("applicants") or "").split(",") if n.strip()]
    first, _, last = names[0].partition(" ") if names else ("", "", "")
    primary = _int(status.get("primaryApplicationStatusID"))
    label = APPLICATION_STATUS.get(primary) if primary is not None else None
    if _int(lease.get("leaseID")) is not None:
        label = "AddedToLease"
    return {
        "buildium_id": _int(obj.get("applicationID")),
        "pms": PMS,
        "first_name": _str(first),
        "last_name": _str(last),
        "email": None,
        "phone": None,
        "status": label,
        "applicant_group_id": None,
        "property_id": _int(prop.get("propertyID")),
        "unit_id": _int(obj.get("unitID")),
        "tenant_id": None,
        "raw": obj,
    }


#: Rentvine ``accountTypeID`` -> (Buildium's ``type``, a ``sub_type`` label).
#: From the spec's own ``accountTypeID`` filter description on
#: ``GET /accounting/accounts`` (the ``account`` schema declares only
#: "integer"). Buildium's five types are what every GL reader groups by.
ACCOUNT_TYPE: dict[int, tuple[str, str]] = {
    1: ("Asset", "AccountsReceivable"),
    2: ("Liability", "AccountsPayable"),
    3: ("Asset", "Bank"),
    4: ("Asset", "Asset"),
    5: ("Liability", "Liability"),
    6: ("Equity", "Equity"),
    7: ("Income", "Income"),
    8: ("Income", "OtherIncome"),
    9: ("Expense", "Expense"),
    10: ("Expense", "OtherExpense"),
}

#: ``accountTypeID`` 3, the one type that is a bank account.
BANK_ACCOUNT_TYPE = 3


def map_gl_account(obj: dict[str, Any]) -> dict[str, Any]:
    """T1 ``account`` (via ``GET /accounting/accounts``) -> ``helixis_gl_accounts``.

    ``type`` / ``sub_type`` come from :data:`ACCOUNT_TYPE`; an id it does not
    know leaves both ``None`` and ``is_bank_account`` ``None`` (unknown, never
    "not a bank account").

    NOT VERIFIED on a live row (the marker below).
    """
    # ⚠ @unverified — that the type ids mean what ACCOUNT_TYPE says (the spec's
    # examples send `"7"` and the schema an int; both are coerced). Runbook
    # "Chart of accounts, applicants and rent payments" settles it against
    # Rentvine's own chart-of-accounts screen.
    type_id = _int(obj.get("accountTypeID"))
    kind = ACCOUNT_TYPE.get(type_id) if type_id is not None else None
    return {
        "buildium_id": _int(obj.get("accountID")),
        "pms": PMS,
        "account_number": _str(obj.get("number")),
        "name": _str(obj.get("name")),
        "type": kind[0] if kind else None,
        "sub_type": kind[1] if kind else None,
        "is_active": _flag(obj.get("isActive")),
        "is_bank_account": (type_id == BANK_ACCOUNT_TYPE) if kind else None,
        "parent_gl_account_id": _int(obj.get("parentAccountID")),
        "raw": obj,
    }


def property_portfolio_id(obj: dict[str, Any]) -> int | None:
    """The portfolio that owns a property (``property-2.portfolioID``).

    Measured 2026-10-01: present, with a value, on every one of the sandbox's
    stored property rows (13 of 13, 4 distinct portfolios). The spec types it
    as a string ("88"), so it is coerced like every other id.
    """
    return _int(obj.get("portfolioID"))


def property_point(obj: dict[str, Any]) -> tuple[float, float] | None:
    """``(lat, lng)`` from a property's own ``latitude``/``longitude``, or
    ``None`` when either is missing, not a finite number, out of range, or the
    ``0, 0`` placeholder.

    MEASURED 2026-10-03: decimal strings on all 13 sandbox properties, every one
    inside the US. Nothing stored them, so no Rentvine property had a point and
    local-law, flood and "near" questions had nothing to work from (gotcha 188).
    """
    try:
        lat, lng = float(str(obj.get("latitude"))), float(str(obj.get("longitude")))
    except ValueError:
        return None
    # The range test also refuses NaN and infinity: every comparison with
    # them is false.
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0) or (lat == 0.0 and lng == 0.0):
        return None
    return lat, lng


def portfolio_owner_contact_ids(obj: dict[str, Any]) -> tuple[int | None, set[int]]:
    """``(portfolioID, {owner contactIDs})`` from one ``GET /portfolios`` row.

    The row SHAPE was measured live on 2026-10-02 (the founder's
    ``rentvine_owner_links_check.py``, PASS): each row arrives wrapped as
    ``{"portfolio": …}`` as the generated envelope expects, ``contacts`` is a
    list carrying ``contactID``, and every property's ``portfolioID`` joined
    (13 properties, 4 portfolios, 5 owners on the sandbox that day). The
    by-hand comparison against Rentvine's owner screen is the founder's.

    Every owner on the portfolio counts, whatever ``percentOwned`` says: a 10%
    owner is still an owner a manager may need to reach. An unusable contact
    id is dropped, never coerced to something else.
    """
    pid = _int(obj.get("portfolioID"))
    contacts = obj.get("contacts")
    ids: set[int] = set()
    if isinstance(contacts, list):
        for c in contacts:
            cid = _int(c.get("contactID")) if isinstance(c, dict) else None
            if cid is not None:
                ids.add(cid)
    return pid, ids


#: The siblings the client attaches to a payables row; they belong to the LINE.
_PAYABLES_SIBLINGS = ("transaction", "ledger", "property", "unit")


def group_payables(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """One record per bill from ``GET /accounting/payables/search``, and how
    many bills were left out because Rentvine voided them.

    Each payables row is ONE LINE of a bill: the bill record (the row
    envelope) with its ``transaction`` (the line: ``amount``, ``amountPaid``)
    and its ``ledger`` and ``property`` siblings beside it, which the client
    carries (``nullable_companions``). The bill record has no amount and no
    paid flag in the spec, so :func:`map_bill` computes both from ``lines``.

    A voided bill, or one whose every line is voided, is left out: it is not
    money anybody owes, and leaving it in would add it to "what do we owe".

    ⚠ NO PROPERTY ON THE BILL (ADR 0010 Decision (b)). A Rentvine line is
    charged to a LEDGER (manager, portfolio, property or unit) and only the
    last two belong to one property, so the property lives on the LINE: each
    line keeps its ledger, property and unit here (so in ``raw``) and
    :func:`bill_lines` turns them into ``helixis_bill_lines`` rows, attributed
    to a property only for a property or unit ledger.
    """
    by_bill: dict[int, dict[str, Any]] = {}
    voided: set[int] = set()
    for row in rows:
        bill_id = _int(row.get("billID"))
        txn = row.get("transaction")
        if bill_id is None or not isinstance(txn, dict):
            continue
        if _int(row.get("isVoided")) == 1:
            voided.add(bill_id)
            continue
        bill = {k: v for k, v in row.items() if k not in _PAYABLES_SIBLINGS}
        entry = by_bill.setdefault(bill_id, {**bill, "lines": []})
        if _int(txn.get("isVoided")) == 1:
            continue
        raw_ledger, raw_prop, raw_unit = row.get("ledger"), row.get("property"), row.get("unit")
        ledger: dict[str, Any] = raw_ledger if isinstance(raw_ledger, dict) else {}
        prop: dict[str, Any] = raw_prop if isinstance(raw_prop, dict) else {}
        unit: dict[str, Any] = raw_unit if isinstance(raw_unit, dict) else {}
        entry["lines"].append(
            {
                "transactionID": _int(txn.get("transactionID")),
                "amount": _str(txn.get("amount")),
                "amountPaid": _str(txn.get("amountPaid")),
                "description": _str(txn.get("description")),
                "chargeAccountID": _int(txn.get("chargeAccountID")),
                "ledgerID": _int(ledger.get("ledgerID")),
                "ledgerTypeID": _int(ledger.get("ledgerTypeID")),
                "propertyID": _int(prop.get("propertyID")),
                "unitID": _int(unit.get("unitID")),
            }
        )
    out: list[dict[str, Any]] = []
    for bill_id, record in sorted(by_bill.items()):
        if bill_id in voided or not record["lines"]:
            voided.add(bill_id)
            continue
        record["lines"].sort(key=lambda ln: ln["transactionID"] or 0)
        out.append(record)
    return out, len(voided)


#: Ledger types whose money belongs to ONE property (ADR 0010 Decision (b)).
#: 1 Manager and 2 Portfolio are mirrored and attributed to no property.
PROPERTY_LEDGER_TYPES = frozenset({3, 4})


def bill_lines(obj: dict[str, Any]) -> list[dict[str, Any]]:
    """One :func:`group_payables` record -> its ``helixis_bill_lines`` rows.

    ⚠ ``property_id`` / ``unit_id`` ONLY for a property- or unit-ledger line
    (:data:`PROPERTY_LEDGER_TYPES`). A portfolio- or manager-ledger line keeps
    them NULL and carries its ``ledger_type``, so a by-property rollup can say
    how much it leaves out instead of hiding it; splitting a portfolio amount
    across properties would be an allocation basis nobody chose. A line whose
    ledger type did not come through is attributed to nothing either.

    ``gl_account_id`` is the line's ``chargeAccountID``, the id
    ``helixis_gl_accounts.buildium_id`` holds for a Rentvine account.
    """
    bill_id = _int(obj.get("billID"))
    raw_lines = obj.get("lines")
    lines: list[Any] = raw_lines if isinstance(raw_lines, list) else []
    out: list[dict[str, Any]] = []
    if bill_id is None:
        return out
    for index, ln in enumerate(x for x in lines if isinstance(x, dict)):
        kind = _int(ln.get("ledgerTypeID"))
        local = kind in PROPERTY_LEDGER_TYPES
        out.append(
            {
                "bill_id": bill_id,
                "line_index": index,
                "property_id": _int(ln.get("propertyID")) if local else None,
                "unit_id": _int(ln.get("unitID")) if local else None,
                "gl_account_id": _int(ln.get("chargeAccountID")),
                "amount": _money(ln.get("amount")),
                "memo": _str(ln.get("description")),
                "ledger_id": _int(ln.get("ledgerID")),
                "ledger_type": kind if kind in {1, 2, 3, 4} else None,
            }
        )
    return out


def map_bill(obj: dict[str, Any]) -> dict[str, Any]:
    """One :func:`group_payables` record -> ``helixis_bills``.

    - ``total_amount`` is the sum of the unvoided lines' ``amount``, or
      ``None`` when any does not parse (rule 14: an unknown line makes the
      total unknown, never smaller).
    - ``is_paid`` is ``amountPaid`` reaching ``amount`` on every line (the
      spec's own ``isPaid`` rule), or ``None`` when any does not parse. A
      part-paid bill is unpaid and carries its FULL total, which is how the
      Buildium path treats one (``bill_amount``/``bill_is_paid``), so "what do
      we owe" means the same thing for both systems.
    - ``vendor_id`` is ``payeeContactID``, the contact id
      ``helixis_vendors.buildium_id`` holds for a Rentvine vendor.
    - ``work_order_id`` stays ``None``: the payables row's bill carries no
      ``workOrderID`` (the full ``bill`` schema does). Unknown, not "none".

    Two things only a live row settles are NOT VERIFIED (the marker below).
    """
    # ⚠ @unverified — that `amount`/`amountPaid` are the decimal strings the
    # spec declares, and whether a markup or discount CHILD bill (`parentBillID`
    # set) is money owed to the payee; both are mirrored as their own bills.
    # Runbook "Bills" settles it once the sandbox has a bill.
    raw_lines = obj.get("lines")
    lines: list[Any] = raw_lines if isinstance(raw_lines, list) else []
    amounts = [_money(ln.get("amount")) for ln in lines if isinstance(ln, dict)]
    paid = [_money(ln.get("amountPaid")) for ln in lines if isinstance(ln, dict)]
    known = bool(amounts) and None not in amounts
    total = sum((a for a in amounts if a is not None), Decimal(0)) if known else None
    is_paid = (
        all(p >= a for p, a in zip(paid, amounts, strict=True))  # type: ignore[operator]
        if known and None not in paid
        else None
    )
    return {
        "buildium_id": _int(obj.get("billID")),
        "pms": PMS,
        "vendor_id": _int(obj.get("payeeContactID")),
        "bill_date": _date(obj.get("billDate")),
        "due_date": _date(obj.get("dateDue")),
        "total_amount": total,
        "is_paid": is_paid,
        "reference_number": _str(obj.get("reference")),
        "work_order_id": None,
        "raw": obj,
    }


#: The value :func:`lease_balance_known` puts on a lease whose balance did not
#: come through readably. Distinct from ``None`` (read, and owes nothing).
BALANCE_UNKNOWN = object()

#: Aging buckets, by days since the charge posted: Buildium's four, in order.
_BUCKETS = (
    ("balance_0_30", 30),
    ("balance_31_60", 60),
    ("balance_61_90", 90),
    ("balance_over_90", None),
)


def _aging(charges: Any, total: Decimal, today: date) -> dict[str, Decimal] | None:
    """The four aging buckets from ``unpaidCharges``, or ``None`` when they
    cannot be stated.

    ⚠ ONLY WHEN THE CHARGES ADD UP. The spec declares ``unpaidCharges`` as one
    object while describing many; nobody has seen a live row. So the buckets
    are filled only when it is a list of charges whose unpaid parts
    (``amount - amountPaid``) sum to the lease's ``unpaidTotalAmount`` to the
    cent. Anything else leaves all four NULL: unknown, which every reader
    prints as no aging rather than as "current" (rule 14).
    """
    if not isinstance(charges, list):
        return None
    out = {name: Decimal(0) for name, _ in _BUCKETS}
    seen = Decimal(0)
    for charge in charges:
        if not isinstance(charge, dict):
            return None
        amount, paid = _money(charge.get("amount")), _money(charge.get("amountPaid"))
        posted = _date(charge.get("datePosted"))
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


def map_lease_balance(obj: dict[str, Any], today: date) -> dict[str, Any]:
    """One lease-export record (with ``balances`` and ``unpaidCharges``
    carried beside it) -> a ``helixis_lease_balances`` row's columns.

    ``total_balance`` is ``balances.unpaidTotalAmount``: every unpaid charge,
    due or not, which is what Buildium's ``TotalBalance`` is too (its 0-30
    bucket is the current month). ``None`` when the object is missing or the
    figure does not parse; :func:`lease_balance_known` turns that into
    :data:`BALANCE_UNKNOWN` for the sync.

    The ``balances`` object beside each export lease was measured live on
    2026-10-02 (the founder's ``rentvine_balances_check.py``, PASS: every
    sandbox lease carried one, and aging was known for every lease that owed).
    Whether ``unpaidTotalAmount`` nets a tenant's credit is still open (marker
    below): the spec's four figures are all "unpaid", so likely not, and a
    credit would read as 0, never negative.
    """
    # ⚠ @unverified — whether a tenant credit nets into unpaidTotalAmount. The
    # sandbox had no lease in credit. Runbook "Rent balances" settles it.
    raw_bal = obj.get("balances")
    bal: dict[str, Any] = raw_bal if isinstance(raw_bal, dict) else {}
    total = _money(bal.get("unpaidTotalAmount"))
    aging = _aging(obj.get("unpaidCharges"), total, today) if total is not None else None
    return {
        "lease_id": _int(obj.get("leaseID")),
        "pms": PMS,
        "property_id": _int(obj.get("propertyID")),
        "unit_id": _int(obj.get("unitID")),
        "total_balance": total,
        "balance_0_30": aging["balance_0_30"] if aging else None,
        "balance_31_60": aging["balance_31_60"] if aging else None,
        "balance_61_90": aging["balance_61_90"] if aging else None,
        "balance_over_90": aging["balance_over_90"] if aging else None,
        "raw": {
            "leaseID": obj.get("leaseID"),
            "balances": raw_bal,
            "unpaidCharges": obj.get("unpaidCharges"),
        },
    }


def lease_balance_known(obj: dict[str, Any], today: date) -> Any:
    """The row to write, ``None`` when the lease owes nothing, or
    :data:`BALANCE_UNKNOWN` when its balance did not come through.

    Rows exist only for leases that owe (``total_balance > 0``), as on the
    Buildium path, whose readers count every row as a delinquent lease.
    """
    row = map_lease_balance(obj, today)
    if row["lease_id"] is None or row["total_balance"] is None:
        return BALANCE_UNKNOWN
    return row if row["total_balance"] > 0 else None


MAPPERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "helixis_properties": map_property,
    "helixis_units": map_unit,
    "helixis_leases": map_lease,
    "helixis_tenants": map_tenant,
    "helixis_owners": map_owner,
    "helixis_vendors": map_vendor,
    "helixis_work_orders": map_work_order,
    "helixis_bills": map_bill,
    "helixis_gl_accounts": map_gl_account,
    "helixis_applicants": map_applicant,
}

__all__ = [
    "ACCOUNT_TYPE",
    "APPLICATION_STATUS",
    "BANK_ACCOUNT_TYPE",
    "LEASE_STATUS",
    "LEASE_TENANT_STATUS",
    "MAPPERS",
    "portfolio_owner_contact_ids",
    "property_portfolio_id",
    "PMS",
    "PROPERTY_TYPE",
    "WORK_ORDER_PRIORITY",
    "WORK_ORDER_STATUS",
    "lease_tenant_move_ins",
    "lease_move_out",
    "lease_transactions",
    "LEASE_TRANSACTION_TYPE",
    "LEASE_TRANSACTION_UNSIGNED",
    "BALANCE_UNKNOWN",
    "PROPERTY_LEDGER_TYPES",
    "bill_lines",
    "group_payables",
    "lease_balance_known",
    "map_lease_balance",
    "map_bill",
    "map_applicant",
    "map_gl_account",
    "map_lease",
    "map_lease_tenant_links",
    "map_owner",
    "map_property",
    "map_tenant",
    "map_unit",
    "map_vendor",
    "map_work_order",
]
