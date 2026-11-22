"""What the two Propertyware work-order writes send, and what reads them back.

Design W4: create a work order (``POST /workorders``) and edit one
(``PATCH /workorders/{workOrderID}``), both under ``WORK ORDERS - Write``.
Nothing else is written. The agent tools that gate them are in
``agents/propertyware_tools.py``; the bodies, the cards, the read-backs and
every assumption about Propertyware live HERE, because
``scripts/propertyware_unverified.py`` scans this package for the work list.

Pure: no I/O. Every function takes what was read and returns what to send,
show or conclude.

⚠ **NEITHER WRITE CAN SET A STATUS OR A VENDOR.** The spec's create and patch
bodies (``SaveWorkOrderDTO``, ``PatchWorkOrderDTO``) have no field for either.
A request for one is answered with :data:`CANT_STATUS_OR_VENDOR` and nothing is
sent, and both cards say so.

⚠ **A PATCH IS JSON MERGE PATCH: A FIELD SENT AS ``null`` IS CLEARED.** So the
edit body carries only the fields that change (:func:`patch_body`): never a
``None``, never an unchanged value, never a field outside :data:`EDITABLE`.

⚠ **BOTH PORTAL FLAGS ARE SENT FALSE ON EVERY CREATE** (terms rule 13). An
omitted flag is the vendor's default (Rentvine's gotcha 191), and the card can
only promise what the body says.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from helixis.pms.http import operation_key
from helixis.propertyware import _paths, mapper

#: The writes, exactly as the spec writes them (case and all).
CREATE_PATH = "/workorders"
UPDATE_PATH = "/workorders/{workOrderID}"

#: The single-record reads the writes need. The spec spells the work order's
#: path parameter ``workOrderId`` on the GET and ``workOrderID`` on the PATCH.
READ_PATH = "/workorders/{workOrderId}"
BUILDING_PATH = "/buildings/{buildingID}"
UNIT_PATH = "/units/{unitID}"

#: ``(method, path)`` of each write. Nothing else is ever sent.
WRITES: tuple[tuple[str, str], ...] = (("POST", CREATE_PATH), ("PATCH", UPDATE_PATH))

#: The GETs the writes make: the record before, again just before the send,
#: and after.
WRITE_READS: tuple[str, ...] = (READ_PATH, BUILDING_PATH, UNIT_PATH)

#: The verbs the writes use, and so the only ones the write client allows.
WRITE_METHODS: frozenset[str] = frozenset(method for method, _path in WRITES)

#: The spec's priority enum, by the words a person uses.
PRIORITIES: dict[str, str] = {"high": "HIGH", "medium": "MEDIUM", "low": "LOW"}

#: The most units one edit attaches; each is read to check its building.
MAX_UNITS = 10

#: Longest text sent in one field. Propertyware documents no limit; a longer
#: ask is refused in words rather than cut, since a cut text is not what the
#: approver asked for.
MAX_TEXT = 4000

#: What a request to change a status or a vendor is told. Nothing is sent.
CANT_STATUS_OR_VENDOR = (
    "Propertyware's API can't change a work order's status or its vendor, so I didn't send "
    "anything and nothing changed. Change it in Propertyware: open the work order there to set "
    "its status or assign the vendor."
)

#: Said on both cards and in the create's reply.
STATUS_AND_VENDOR_LINE = (
    "Vendor and status: Propertyware's API can't set them; assign the vendor and set the "
    "status in Propertyware."
)


@dataclass(frozen=True)
class Editable:
    """One field the edit may change."""

    #: The body key, as the spec spells it.
    api: str
    #: The card's word for it.
    label: str
    #: The keys of the work order as READ that move when this field does.
    moves: frozenset[str]


#: Design W4: "description, priority, category, type, dates and units". The
#: dates are the work's own (``startDate``, ``scheduledEndDate``); the
#: entry-permission fields (``dateToEnter`` and its hour and minute) are not.
EDITABLE: tuple[Editable, ...] = (
    Editable("description", "Description", frozenset({"description"})),
    Editable("priority", "Priority", frozenset({"priority"})),
    Editable("category", "Category", frozenset({"category"})),
    Editable("type", "Type", frozenset({"type"})),
    Editable("startDate", "Start date", frozenset({"startDate"})),
    Editable("scheduledEndDate", "Scheduled end date", frozenset({"scheduledEndDate"})),
    # @unverified PW-18 — that a PATCH of unitIDs also moves the read's
    # single unitID, its location text and its lease name, and nothing else.
    Editable("unitIDs", "Units", frozenset({"unitIDs", "unitID", "location", "lease"})),
)

_BY_API: dict[str, Editable] = {field.api: field for field in EDITABLE}

#: Keys of a work order as read that change on any save, or with the
#: calendar, so they are never compared.
VOLATILE_READ_KEYS: frozenset[str] = frozenset({"lastModifiedDateTime", "lastModifiedBy", "age"})

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SPACE_RE = re.compile(r"\s+")


# ── reading what came back ───────────────────────────────────────────────


def record_id(record: Any) -> int | None:
    """A record's ``id``, or ``None`` when the answer is not a record."""
    return mapper.record_id(record) if isinstance(record, Mapping) else None


def display_number(record: Mapping[str, Any]) -> str:
    """How a person finds the work order: Propertyware's own ``number``, the
    one its screens show, bare, and the ``id`` Occupella's tools print, as
    ``#id`` and named as Occupella's record.

    ⚠ ``#`` is only ever Occupella's id. ``open_work_orders`` prints a work
    order as ``#<id>`` ("The #number is the work order's id"), and the run note
    says the record numbers Occupella shows are not Propertyware's. This read
    ``#2041 (id 990700141)``, Propertyware's number behind the same ``#``, so
    the model could not tell which one it may quote and left the number out:
    eval 530 asks for it and missed in 5 of 5 runs (2026-10-05/06).
    """
    number = mapper._int(record.get("number"))
    ident = record_id(record)
    if number is not None and ident is not None:
        return f"{number} (Occupella's record #{ident})"
    if number is not None:
        return f"{number}"
    return f"(Occupella's record #{ident})" if ident is not None else "(no id)"


def units_of(record: Mapping[str, Any]) -> tuple[int, ...] | None:
    """The unit ids a work order as read is attached to, or ``None`` when the
    record cannot say. ``unitIDs`` is the list; the single ``unitID`` is read
    only when the list is absent."""
    # @unverified PW-19 — that the work order's unitIDs reads as a list of unit
    # ids; the spec types it as an untyped object (Collection_long_). Any other
    # shape is read as unknown, never as "no units".
    raw = record.get("unitIDs")
    if isinstance(raw, list):
        found = [mapper._int(item) for item in raw]
        if any(unit is None for unit in found):
            return None
        return tuple(sorted({unit for unit in found if unit is not None}))
    if raw is not None:
        return None
    if "unitID" not in record:
        return None
    single = mapper._int(record.get("unitID"))
    return () if single is None else (single,)


def name_of(record: Mapping[str, Any], *, fallback: str) -> str:
    """A building's or unit's own name, as the customer wrote it."""
    return mapper._first(record, ("name", "abbreviation")) or fallback


# ── checking what the model asked for ────────────────────────────────────


def priority_value(word: str | None) -> str | None:
    """The spec's priority for a person's word ("High", "medium"), or None."""
    return PRIORITIES.get((word or "").strip().lower())


def text_value(value: str | None) -> str | None:
    """A field's text, trimmed; ``None`` when empty."""
    text = (value or "").strip()
    return text or None


def date_value(value: str | None) -> str | None:
    """A ``YYYY-MM-DD`` date that exists, or ``None``."""
    text = (value or "").strip()
    if not _DATE_RE.match(text):
        return None
    try:
        date.fromisoformat(text)
    except ValueError:
        return None
    return text


def forbidden_reply(method: str, template: str, *, remembered: bool = False) -> str:
    """What a 403 on a write means, decided by the operation's beta marker in
    the generated index (design, "built correctly"): a beta write needs
    Propertyware's write beta; any other needs the key's permission. Each
    names an action (rule 16)."""
    key = operation_key(method, template)
    tried = (
        "Propertyware refused this change the last time it was tried, so I didn't send it again. "
        if remembered
        else ""
    )
    unchanged = "Nothing was changed in Propertyware."
    if key in _paths.BETA:
        return (
            f"{tried}This needs Propertyware's write beta: ask Propertyware support to add your "
            f"account to it. {unchanged}"
        )
    permission = _paths.PERMISSIONS.get(key) or "WORK ORDERS - Write"
    return (
        f"{tried}The Propertyware API key saved in Occupella lacks {permission}. Create a key in "
        f"Propertyware that has it (Setup, Administration Setup, API Keys) and save it on the "
        f"Connectors page (Settings → Connectors); if you already gave this key {permission}, "
        f"save the keys there again. {unchanged}"
    )


# ── the create ───────────────────────────────────────────────────────────


def create_body(
    *,
    building_id: int,
    description: str,
    priority: str,
    unit_id: int | None = None,
    work_order_type: str | None = None,
    category: str | None = None,
) -> dict[str, Any]:
    """The ``POST /workorders`` body.

    ``buildingID`` is the one field the spec requires. Both portal flags are
    sent false, always: terms rule 13.
    """
    # @unverified PW-11 — the create minimum: that buildingID, description and
    # priority are enough, that type and category may be left out, and that
    # both publishTo*Portal flags sent false notify nobody.
    body: dict[str, Any] = {
        "buildingID": building_id,
        "description": description,
        "priority": priority,
        "publishToTenantPortal": False,
        "publishToOwnerPortal": False,
    }
    if unit_id is not None:
        body["unitIDs"] = [unit_id]
    if work_order_type is not None:
        body["type"] = work_order_type
    if category is not None:
        body["category"] = category
    return body


def created_id(answer: Any) -> int | None:
    """The new work order's id from the create's answer."""
    # @unverified PW-11 — that the 201 answer is the created WorkOrderDTO, as
    # the spec declares, carrying its id.
    return record_id(answer)


def create_items(
    *,
    place: str,
    description: str,
    priority: str,
    work_order_type: str | None,
    category: str | None,
) -> tuple[str, ...]:
    """The create card's lines, in the customer's words."""
    items = [
        f"Where: {place}",
        f"Problem: {description}",
        f"Priority: {priority.capitalize()}",
    ]
    if work_order_type is not None:
        items.append(f"Type: {work_order_type}")
    if category is not None:
        items.append(f"Category: {category}")
    items.append(STATUS_AND_VENDOR_LINE)
    items.append("Not published to the tenant or owner portal.")
    return tuple(items)


def create_took(body: Mapping[str, Any], record: Mapping[str, Any]) -> bool:
    """Whether the work order read back is the one sent: its building, its
    description, priority, type, category and unit, and neither portal flag
    set (rule 6: a 201 is not the write)."""
    # @unverified PW-11 — that a read of the new work order echoes what the
    # create sent, its description as sent apart from whitespace.
    if mapper._int(record.get("buildingID")) != body["buildingID"]:
        return False
    for key in ("description", "priority", "type", "category"):
        if key in body and not _same(key, body[key], record.get(key)):
            return False
    if "unitIDs" in body:
        units = units_of(record)
        if units is None or not set(body["unitIDs"]) <= set(units):
            return False
    return record.get("publishToTenantPortal") is not True and (
        record.get("publishToOwnerPortal") is not True
    )


# ── the edit ─────────────────────────────────────────────────────────────


def current_value(record: Mapping[str, Any], api: str) -> Any:
    """A work order's value for an editable field, as read."""
    if api == "unitIDs":
        return units_of(record)
    return record.get(api)


def patch_body(current: Mapping[str, Any], wanted: Mapping[str, Any]) -> dict[str, Any]:
    """The ``PATCH`` body: only the fields whose value changes.

    Raises ``ValueError`` (a connector bug, before anything is shown or sent)
    for a field outside :data:`EDITABLE` or a ``None``: JSON Merge Patch
    clears a field sent as null.
    """
    unknown = sorted(set(wanted) - set(_BY_API))
    if unknown:
        raise ValueError(f"not an editable work-order field: {unknown}")
    body: dict[str, Any] = {}
    for field in EDITABLE:
        if field.api not in wanted:
            continue
        new = wanted[field.api]
        if new is None:
            raise ValueError(f"{field.api}: a PATCH never carries null; it would clear the field")
        if _same(field.api, new, current_value(current, field.api)):
            continue
        body[field.api] = sorted(new) if field.api == "unitIDs" else new
    return body


def update_items(
    current: Mapping[str, Any], body: Mapping[str, Any], unit_names: Mapping[int, str]
) -> tuple[str, ...]:
    """The edit card's lines: each changed field's current value, read just
    before, and its new one. The work order's own text is the customer's."""
    items = []
    for field in EDITABLE:
        if field.api not in body:
            continue
        before = current_value(current, field.api)
        if field.api == "unitIDs":
            shown_before = _unit_list(before, unit_names)
            shown_after = _unit_list(body[field.api], unit_names)
        else:
            shown_before, shown_after = _shown(before), _shown(body[field.api])
        items.append(f"{field.label}: {shown_before} → {shown_after}")
    items.append("Status and vendor: unchanged (Propertyware's API can't change them).")
    return tuple(items)


def changed_labels(body: Mapping[str, Any]) -> list[str]:
    """The card's words for the fields a body changes, in card order."""
    return [field.label.lower() for field in EDITABLE if field.api in body]


def moved_on_card(
    approved: Mapping[str, Any], now: Mapping[str, Any], body: Mapping[str, Any]
) -> list[str]:
    """The fields the card showed whose current value is no longer what the
    approver saw."""
    return [
        api for api in body if not _same(api, current_value(now, api), current_value(approved, api))
    ]


def patch_took(
    before: Mapping[str, Any], body: Mapping[str, Any], after: Mapping[str, Any]
) -> list[str]:
    """What the read-back shows wrong: each sent field that does not read as
    sent, and each other field that does not read as it did just before the
    send. Empty means the edit took and touched nothing else (PW-18)."""
    # @unverified PW-18 — that PATCH changes exactly the fields sent and
    # leaves every other field as it was, which is what this read-back checks.
    wrong = [api for api in body if not _same(api, body[api], current_value(after, api))]
    moves: set[str] = set(VOLATILE_READ_KEYS)
    for api in body:
        moves |= _BY_API[api].moves
    for key in sorted((set(before) | set(after)) - moves):
        if _canon(before.get(key)) != _canon(after.get(key)):
            wrong.append(key)
    return wrong


# ── comparing ────────────────────────────────────────────────────────────


def _same(api: str, a: Any, b: Any) -> bool:
    """Whether two values of a field are the same value, as Propertyware
    would store it. Text compares trimmed and with runs of whitespace as one;
    a priority in any case; a date by its day; units as a set."""
    if api == "unitIDs":
        left = None if a is None else tuple(sorted(a))
        right = None if b is None else tuple(sorted(b))
        return left == right
    if api == "priority":
        return _text(a, upper=True) == _text(b, upper=True)
    if api in ("startDate", "scheduledEndDate"):
        return _day(a) == _day(b)
    return _text(a) == _text(b)


def _text(value: Any, *, upper: bool = False) -> str | None:
    if value is None:
        return None
    text = _SPACE_RE.sub(" ", str(value)).strip()
    if not text:
        return None
    return text.upper() if upper else text


def _day(value: Any) -> str | None:
    text = _text(value)
    return text[:10] if text else None


def _canon(value: Any) -> Any:
    """A read value in a form two reads of the same record compare equal by:
    empty text is no value, text is trimmed, an object is compared as JSON and
    a list as the set of its items, in any order."""
    if value is None:
        return None
    if isinstance(value, str):
        return _text(value)
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True, default=str)
    if isinstance(value, list):
        return sorted(json.dumps(item, sort_keys=True, default=str) for item in value)
    return value


def _shown(value: Any, limit: int = 300) -> str:
    text = _text(value)
    if text is None:
        return "(none)"
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _unit_list(ids: Iterable[int] | None, names: Mapping[int, str]) -> str:
    if ids is None:
        return "(not known)"
    shown = [names.get(unit, f"unit {unit}") for unit in ids]
    return ", ".join(shown) if shown else "(none)"


__all__ = [
    "BUILDING_PATH",
    "CANT_STATUS_OR_VENDOR",
    "CREATE_PATH",
    "EDITABLE",
    "MAX_TEXT",
    "MAX_UNITS",
    "PRIORITIES",
    "READ_PATH",
    "STATUS_AND_VENDOR_LINE",
    "UNIT_PATH",
    "UPDATE_PATH",
    "VOLATILE_READ_KEYS",
    "WRITES",
    "WRITE_METHODS",
    "WRITE_READS",
    "Editable",
    "changed_labels",
    "create_body",
    "create_items",
    "create_took",
    "created_id",
    "current_value",
    "date_value",
    "display_number",
    "forbidden_reply",
    "moved_on_card",
    "name_of",
    "patch_body",
    "patch_took",
    "priority_value",
    "record_id",
    "text_value",
    "units_of",
    "update_items",
]
