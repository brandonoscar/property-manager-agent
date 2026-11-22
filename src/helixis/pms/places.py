"""What the person named, as one record id, from this company's synced mirror.

A write that takes a record id asks the model for a number no read tool shows
it: ``property_snapshot`` names a property and its units, the lease tools name
the tenant and the unit, and a ``#id`` appears only where a record has no name.
So the model had to invent one, and the card showed whatever record the guess
landed on. These turn the words the person used into exactly one id, or into
the question to ask instead: the join is code's (rule 13), and an id is never
invented.

One way to match, for every kind (Rent Manager's ``writes.find_place``,
generalised):

- ``#123`` is a record id, the form every question below prints, so the model
  can come back with the one the person picked. A unit is the exception: "#12"
  is how people write unit 12, so a unit's number is tried first;
- then the name, case and spacing ignored; then, failing that, the records
  whose name contains what was asked. A unit is matched whole only: "1" must
  never mean "11";
- exactly one record answers. Several are listed with their ``#id``, and no
  match says what is synced, each ending with what was NOT done, so the model
  asks rather than picks.

Every query is scoped to the company AND the system (rule 11), so a row another
system left in the mirror can never be picked.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from helixis.db.tenant_links import current_link_sql

#: How many records a question names before it says how many more.
LISTED = 8

PROPERTIES_SQL = """
    SELECT buildium_id, name FROM helixis_properties
     WHERE company_id = $1::uuid AND pms = $2 AND buildium_id IS NOT NULL
"""
UNITS_SQL = """
    SELECT buildium_id, unit_number FROM helixis_units
     WHERE company_id = $1::uuid AND pms = $2 AND property_id = $3
       AND buildium_id IS NOT NULL
"""
VENDORS_SQL = """
    SELECT buildium_id, name FROM helixis_vendors
     WHERE company_id = $1::uuid AND pms = $2 AND buildium_id IS NOT NULL
"""
#: Every lease with its place and its tenants' names, the current ones first,
#: so a question lists them in the order a person would pick from.
LEASES_SQL = f"""
    SELECT l.buildium_id, l.status, l.lease_to_date::date AS lease_to_date,
           p.name AS property_name, u.unit_number,
           ARRAY(
               SELECT NULLIF(TRIM(CONCAT(t.first_name, ' ', t.last_name)), '')
                 FROM helixis_lease_tenants lt
                 JOIN helixis_tenants t
                   ON t.buildium_id = lt.tenant_buildium_id
                  AND t.company_id = lt.company_id
                  AND t.pms = lt.pms
                WHERE lt.company_id = l.company_id
                  AND lt.pms = l.pms
                  AND lt.lease_buildium_id = l.buildium_id
                  -- On an Active lease, only the people living there now: a
                  -- co-tenant who moved out keeps a link to the lease the
                  -- others continue (gotcha 207). Any other lease is all
                  -- former or all incoming, so every name on it stands.
                  AND ({current_link_sql("lt")} OR l.status IS DISTINCT FROM 'Active')
                ORDER BY t.last_name NULLS LAST, t.first_name NULLS LAST
           ) AS tenant_names
      FROM helixis_leases l
      LEFT JOIN helixis_properties p
        ON p.buildium_id = l.property_id AND p.company_id = l.company_id AND p.pms = l.pms
      LEFT JOIN helixis_units u
        ON u.buildium_id = l.unit_id AND u.company_id = l.company_id AND u.pms = l.pms
     WHERE l.company_id = $1::uuid AND l.pms = $2 AND l.buildium_id IS NOT NULL
     ORDER BY (l.status = 'Active') DESC, l.lease_to_date DESC NULLS LAST, l.buildium_id DESC
"""
WORK_ORDER_SQL = """
    SELECT buildium_id FROM helixis_work_orders
     WHERE company_id = $1::uuid AND pms = $2 AND buildium_id = $3
"""

_ID = re.compile(r"#\s*(\d+)")
#: What a person puts before a unit's number: "unit 4B", "Apt. 4B", "#4B".
_UNIT_WORDS = re.compile(r"^(?:(?:unit|apt|apartment|suite|ste)\b\.?\s*)?#?\s*", re.IGNORECASE)
#: The same words anywhere in "Garden Row, Unit #4B", so a place compares as
#: "garden row 4b" on both sides.
_PLACE_NOISE = re.compile(r"\b(?:unit|apt|apartment|suite|ste)\b\.?|[#,]", re.IGNORECASE)


@dataclass(frozen=True)
class Match:
    """One record: its id in the system, and the mirror's words for it."""

    id: int
    label: str


@dataclass(frozen=True)
class _Row:
    id: int
    name: str
    label: str


@dataclass(frozen=True)
class _Lease:
    id: int
    tenants: tuple[str, ...]
    place: str
    status: str | None
    ends: Any

    @property
    def label(self) -> str:
        who = ", ".join(self.tenants)
        if who and self.place:
            return f"{who} — {self.place}"
        return who or self.place or f"Lease #{self.id}"

    @property
    def listed(self) -> str:
        ends = f", ends {self.ends}" if self.ends else ""
        return f"{self.label}, {self.status or 'status unknown'}{ends} (#{self.id})"


def fold(text: str | None) -> str:
    """Case and spacing ignored."""
    return " ".join((text or "").split()).casefold()


def record_id(asked: str) -> int | None:
    """The id in ``#123``: the only form read as an id."""
    found = _ID.fullmatch(asked.strip())
    return int(found.group(1)) if found else None


def _place_key(text: str) -> str:
    return fold(_PLACE_NOISE.sub(" ", text))


def _some(labels: Sequence[str], sep: str = ", ") -> str:
    shown = list(labels[:LISTED])
    more = len(labels) - len(shown)
    return sep.join(shown) + (f" and {more} more" if more > 0 else "")


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def nothing_synced(plural: str, system: str, nothing: str) -> str:
    """The mirror holds none of this kind for the company: say so, and the one
    thing that fills it (rule 16)."""
    return (
        f"Occupella hasn't synced any {plural} from {system} for this workspace yet, so I "
        f"can't tell which one you mean. {nothing} Run Sync now on the Connectors page "
        f"(Settings → Connectors → {system}), or make the change in {system}."
    )


def _named(rows: Sequence[_Row], asked: str) -> list[_Row]:
    """``#id``; else the name exactly; else the names that contain it."""
    wanted = record_id(asked)
    if wanted is not None:
        by_id = [r for r in rows if r.id == wanted]
        if by_id:
            return by_id
    folded = fold(asked)
    if not folded:
        return []
    exact = [r for r in rows if fold(r.name) == folded]
    return exact or [r for r in rows if folded in fold(r.name)]


def _one(
    rows: Sequence[_Row],
    found: Sequence[_Row],
    asked: str,
    *,
    kind: str,
    plural: str,
    system: str,
    nothing: str,
) -> Match | str:
    if len(found) == 1:
        return Match(found[0].id, found[0].label)
    if found:
        listed = sorted((f"{r.label} (#{r.id})" for r in found), key=str.casefold)
        return f'Several {plural} match "{asked}": {_some(listed)}. Which one? {nothing}'
    names = sorted({r.label for r in rows}, key=str.casefold)
    return (
        f'No {kind} called "{asked}" is synced from {system}. The {plural} synced: '
        f"{_some(names)}. {nothing}"
    )


async def property_named(
    conn: Any, *, company_id: str, pms: str, system: str, asked: str, nothing: str
) -> Match | str:
    """The one property ``asked`` names."""
    text = " ".join((asked or "").split())
    if not text:
        return f"Which property? {nothing}"
    rows = []
    for r in await conn.fetch(PROPERTIES_SQL, company_id, pms):
        ident = _as_int(r["buildium_id"])
        if ident is not None:
            name = str(r["name"] or "").strip()
            rows.append(_Row(ident, name, name or f"Property #{ident}"))
    if not rows:
        return nothing_synced("properties", system, nothing)
    return _one(
        rows,
        _named(rows, text),
        text,
        kind="property",
        plural="properties",
        system=system,
        nothing=nothing,
    )


async def unit_named(
    conn: Any,
    *,
    company_id: str,
    pms: str,
    system: str,
    place: Match,
    asked: str,
    nothing: str,
) -> Match | str:
    """The one unit ``asked`` names at ``place``, matched whole."""
    text = " ".join((asked or "").split())
    number = _UNIT_WORDS.sub("", text).strip()
    rows = []
    for r in await conn.fetch(UNITS_SQL, company_id, pms, place.id):
        ident = _as_int(r["buildium_id"])
        if ident is not None:
            name = str(r["unit_number"] or "").strip()
            rows.append(_Row(ident, name, f"Unit {name or f'#{ident}'} at {place.label}"))
    found = [r for r in rows if number and fold(r.name) == fold(number)]
    if not found and (wanted := record_id(text)) is not None:
        found = [r for r in rows if r.id == wanted]
    if len(found) == 1:
        return Match(found[0].id, found[0].label)
    if found:
        listed = sorted((f"{r.label} (#{r.id})" for r in found), key=str.casefold)
        return (
            f'Several units at {place.label} match "{text}": {_some(listed)}. Which one? {nothing}'
        )
    if not rows:
        return f"No units at {place.label} are synced from {system}. {nothing}"
    names = sorted({r.name or f"#{r.id}" for r in rows}, key=str.casefold)
    return (
        f'{place.label} has no unit "{number or text}" synced from {system}. Its units: '
        f"{_some(names)}. {nothing}"
    )


async def vendor_named(
    conn: Any, *, company_id: str, pms: str, system: str, asked: str, nothing: str
) -> Match | str:
    """The one vendor ``asked`` names."""
    text = " ".join((asked or "").split())
    if not text:
        return f"Which vendor? {nothing}"
    rows = []
    for r in await conn.fetch(VENDORS_SQL, company_id, pms):
        ident = _as_int(r["buildium_id"])
        if ident is not None:
            name = str(r["name"] or "").strip()
            rows.append(_Row(ident, name, name or f"Vendor #{ident}"))
    if not rows:
        return nothing_synced("vendors", system, nothing)
    return _one(
        rows,
        _named(rows, text),
        text,
        kind="vendor",
        plural="vendors",
        system=system,
        nothing=nothing,
    )


def _leases_named(leases: Sequence[_Lease], text: str) -> list[_Lease]:
    """``#id``; else a tenant's full name; else the place ("Garden Row 4B");
    else the tenants whose name contains it. A place is never matched by a
    fragment: "Garden Row 4" must not mean 4B."""
    wanted = record_id(text)
    if wanted is not None:
        by_id = [lease for lease in leases if lease.id == wanted]
        if by_id:
            return by_id
    folded = fold(text)
    if not folded:
        return []
    by_tenant = [lease for lease in leases if any(fold(t) == folded for t in lease.tenants)]
    if by_tenant:
        return by_tenant
    key = _place_key(text)
    by_place = [lease for lease in leases if lease.place and _place_key(lease.place) == key]
    if by_place:
        return by_place
    return [lease for lease in leases if any(folded in fold(t) for t in lease.tenants)]


async def lease_named(
    conn: Any, *, company_id: str, pms: str, system: str, asked: str, nothing: str
) -> Match | str:
    """The one lease ``asked`` names: a tenant, a place, or ``#id``."""
    text = " ".join((asked or "").split())
    if not text:
        return f"Which lease? Name the tenant, or the property and unit. {nothing}"
    leases = []
    for r in await conn.fetch(LEASES_SQL, company_id, pms):
        ident = _as_int(r["buildium_id"])
        if ident is None:
            continue
        prop = str(r["property_name"] or "").strip()
        unit = str(r["unit_number"] or "").strip()
        place = f"{prop} {unit}".strip() if prop else (f"Unit {unit}" if unit else "")
        tenants = tuple(str(t).strip() for t in (r["tenant_names"] or ()) if t and str(t).strip())
        leases.append(_Lease(ident, tenants, place, r["status"], r["lease_to_date"]))
    if not leases:
        return nothing_synced("leases", system, nothing)
    found = _leases_named(leases, text)
    if len(found) == 1:
        return Match(found[0].id, found[0].label)
    if found:
        return (
            f'{len(found)} leases match "{text}": {_some([f.listed for f in found], "; ")}. '
            f"Which one? {nothing}"
        )
    return (
        f'No lease matches "{text}" in what is synced from {system}. Try the tenant\'s full '
        f'name, or the property and unit as in "Garden Row 4B". {nothing}'
    )


async def work_order_numbered(
    conn: Any, *, company_id: str, pms: str, system: str, asked: str, nothing: str
) -> Match | str:
    """The work order ``asked`` numbers: ``#123`` or ``123``, the id
    ``open_work_orders`` prints."""
    text = " ".join((asked or "").split())
    ident = record_id(text) if text.startswith("#") else _as_int(text)
    if ident is None:
        return f"Give the work order's #number, as the open work orders list shows it. {nothing}"
    if await conn.fetch(WORK_ORDER_SQL, company_id, pms, ident):
        return Match(ident, f"work order #{ident}")
    return f"No work order #{ident} is synced from {system}. {nothing}"


__all__ = [
    "LEASES_SQL",
    "LISTED",
    "PROPERTIES_SQL",
    "UNITS_SQL",
    "VENDORS_SQL",
    "WORK_ORDER_SQL",
    "Match",
    "fold",
    "lease_named",
    "nothing_synced",
    "property_named",
    "record_id",
    "unit_named",
    "vendor_named",
    "work_order_numbered",
]
