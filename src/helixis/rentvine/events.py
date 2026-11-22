"""Rentvine's ~8 webhook events -> the existing 43-name vocabulary. ADR 0010 (c).

The translation happens **at the door**, so ``webhooks/categorization.py``, the
eight handler modules, the router's routing pass and the Inbox filter all keep
working on the names they already know. Nothing downstream learns that a second
PMS exists.

| Rentvine object | reused name |
|---|---|
| property create/update | ``Rental.Created`` / ``Rental.Updated`` |
| unit create/update | ``RentalUnit.Created`` / ``RentalUnit.Updated`` |
| lease create/update | ``Lease.Created`` / ``Lease.Updated`` |
| work order create/update | ``WorkOrder.Created`` / ``WorkOrder.Updated`` |

⚠ **AN UNRECOGNISED EVENT IS DROPPED, NOT GIVEN A NAME.** Mapping something we
cannot identify onto a Buildium name would hand a handler a payload it will
mis-read, and the mis-reading lands in a landlord's books. Dropping costs
freshness, and on Rentvine polling is PRIMARY (Decision (c)) — so the poll is
what the dropped delivery falls back to, which is the whole reason this
direction is safe.

⚠ **THE FOUR OBJECT TYPES ARE ALL RENTVINE SENDS.** No bills, no payments, no
ledger transactions, no tenants, no vendors, no applicants. That inverts the
Buildium design, where webhooks are the primary freshness mechanism — here they
optimise four object types and everything customers ask about first depends on
the poll.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from typing import Any

#: Rentvine object name -> (the existing event prefix, the mirror table, the
#: id field Rentvine's payload carries, the id field a Buildium envelope uses).
#:
#: ⚠ @unverified — the exact spelling Rentvine puts in a delivery for property, unit and
#: lease. T3 says the UI offers create/update webhooks on these four object
#: types and nothing names the wire format, so :func:`normalise` matches
#: case-insensitively over several plausible spellings rather than pinning
#: one. WORK ORDERS ARE MEASURED (2026-10-04, a real sandbox delivery):
#: ``{"data": {..., "workOrderID": "…"}, "event": {"eventType":
#: "maintenance::workOrder::updated", …}, "auth": {…}}``, i.e. the name is
#: nested under ``event`` and reads domain::object::verb. Settled by a Phase 8
#: probe: one captured delivery per remaining object type names the rest.
OBJECTS: dict[str, dict[str, str]] = {
    "property": {
        "event_prefix": "Rental",
        "table": "helixis_properties",
        "rentvine_id": "propertyID",
        "envelope_id": "PropertyId",
        "path": "/properties/{propertyID}",
    },
    "unit": {
        "event_prefix": "RentalUnit",
        "table": "helixis_units",
        "rentvine_id": "unitID",
        "envelope_id": "UnitId",
        "path": "/properties/{propertyID}/units/{unitID}",
    },
    "lease": {
        "event_prefix": "Lease",
        "table": "helixis_leases",
        "rentvine_id": "leaseID",
        "envelope_id": "LeaseId",
        "path": "/leases/{leaseID}",
    },
    "work_order": {
        "event_prefix": "WorkOrder",
        "table": "helixis_work_orders",
        "rentvine_id": "workOrderID",
        "envelope_id": "WorkOrderId",
        "path": "/maintenance/work-orders/{workOrderID}",
    },
}

#: Spellings that resolve to each object key. Lower-cased, punctuation-stripped.
_OBJECT_ALIASES: dict[str, str] = {
    "property": "property",
    "properties": "property",
    "unit": "unit",
    "units": "unit",
    "propertyunit": "unit",
    "lease": "lease",
    "leases": "lease",
    "workorder": "work_order",
    "workorders": "work_order",
    "maintenanceworkorder": "work_order",
}

#: Rentvine verb -> the existing suffix.
#:
#: ⚠ @unverified — No ``Deleted``. T3 says the UI offers create and update only, and the
#: existing vocabulary HAS ``Rental.Deleted`` — whose handler DELETES the mirror
#: row. Accepting a delete verb we have not seen Rentvine send would put a
#: destructive branch behind a guess (gotcha 79). Settled by a Phase 8 probe:
#: look at what the Rentvine UI's webhook dropdown actually offers.
_VERBS: dict[str, str] = {
    "create": "Created",
    "created": "Created",
    "insert": "Created",
    "new": "Created",
    "update": "Updated",
    "updated": "Updated",
    "edit": "Updated",
    "change": "Updated",
    "changed": "Updated",
}

#: Keys a delivery might carry the object type under.
_OBJECT_KEYS = ("objectType", "object", "entity", "entityType", "type", "resource", "model")
#: Keys a delivery might carry the verb under.
_VERB_KEYS = ("action", "verb", "event", "eventType", "operation", "changeType")
#: Keys a delivery might carry a single combined name under, e.g. "lease.updated".
#: ``eventType`` is measured: Rentvine's ``event.eventType`` is
#: ``maintenance::workOrder::updated``.
_COMBINED_KEYS = ("eventName", "event", "eventType", "name", "topic", "trigger")


class UnmappableEvent(Exception):  # noqa: N818 — a refusal, not a fault
    """This delivery could not be named, so it is not being given a name."""


def _norm(value: Any) -> str:
    """Lower-case, strip everything that is not a letter or digit."""
    return "".join(c for c in str(value).lower() if c.isalnum())


def _resolve_object(candidate: Any) -> str | None:
    return _OBJECT_ALIASES.get(_norm(candidate))


def _resolve_verb(candidate: Any) -> str | None:
    return _VERBS.get(_norm(candidate))


def _split_combined(value: Any) -> tuple[str, str] | None:
    """``"lease.updated"`` / ``"work_order:created"`` -> the two halves.

    ⚠ Tries BOTH orders. ``updated.lease`` is as plausible as ``lease.updated``
    and nothing we have says which Rentvine uses; accepting only one spelling
    would drop half the real deliveries while looking like a working mapper.
    """
    # Only a string is a name. ``str()`` of Rentvine's ``event`` OBJECT would
    # contain ``workOrder`` and some verb-like word somewhere in its text.
    if not isinstance(value, str):
        return None
    text = value
    # ⚠ TWO separator sets, tried in order, and the order is the finding. The
    # obvious one-liner also splits on ``_`` — which turns ``work_order.created``
    # into ``work`` + ``order`` + ``created`` and loses the object name
    # entirely. Splitting on punctuation FIRST keeps snake_case object names
    # intact; falling back to ``_`` afterwards still reads ``lease_updated``.
    # Neither spelling is documented, so covering both is the point.
    for separators in (r"[.:/]", r"[.:/_]"):
        parts = [p for p in re.split(separators, text) if p]
        if len(parts) < 2:
            continue
        head, tail = parts[0], parts[-1]
        for a, b in ((head, tail), (tail, head)):
            obj, verb = _resolve_object(a), _resolve_verb(b)
            if obj and verb:
                return obj, verb
        # Rentvine's measured shape is domain::object::verb, so the object can
        # sit between the ends. The verb must still be at an end, and exactly
        # one object must be named, or the name is ambiguous and refused.
        for verb_part, rest in ((tail, parts[:-1]), (head, parts[1:])):
            verb = _resolve_verb(verb_part)
            objects = {o for part in rest if (o := _resolve_object(part))}
            if verb and len(objects) == 1:
                return objects.pop(), verb
    return None


def identify(payload: dict[str, Any]) -> tuple[str, str]:
    """``(object_key, verb_suffix)`` for a Rentvine delivery, or raise.

    Reads the split fields first and a combined name second, because a payload
    carrying both should be read the unambiguous way. Then the same again one
    level down, under ``event``: that is where Rentvine puts it (measured
    2026-10-04, ``event.eventType``).
    """
    found = _identify_flat(payload)
    nested = payload.get("event")
    if found is None and isinstance(nested, dict):
        found = _identify_flat(nested)
    if found is not None:
        return found
    raise UnmappableEvent(
        "no object type and verb could be identified from "
        f"{sorted(payload)[:12]} — refusing to guess a Buildium event name for it"
    )


def _identify_flat(payload: dict[str, Any]) -> tuple[str, str] | None:
    """:func:`identify` over one level of keys, or ``None``."""
    obj = verb = None
    for key in _OBJECT_KEYS:
        if key in payload and (found := _resolve_object(payload[key])):
            obj = found
            break
    for key in _VERB_KEYS:
        if key in payload and (found := _resolve_verb(payload[key])):
            verb = found
            break
    if obj and verb:
        return obj, verb

    for key in _COMBINED_KEYS:
        if key in payload and (pair := _split_combined(payload[key])):
            return pair
    return None


def entity_id(payload: dict[str, Any], object_key: str) -> int | None:
    """The changed record's Rentvine id, as an int, or ``None``.

    ⚠ Looks in the payload ROOT and one level down under a ``data``/``object``/
    ``payload`` wrapper. Which of those Rentvine uses is unknown; checking both
    is cheap and the cost of missing it is a delivery that cannot be refreshed.
    """
    field = OBJECTS[object_key]["rentvine_id"]
    from helixis.rentvine.mapper import _int  # local: avoids a cycle at import time

    for scope in (payload, *(payload.get(k) for k in ("data", "object", "payload", "record"))):
        if isinstance(scope, dict):
            for key in (field, field.lower(), field.upper(), _norm(field)):
                if key in scope and (value := _int(scope[key])) is not None:
                    return value
    return None


def parent_property_id(payload: dict[str, Any]) -> int | None:
    """A unit delivery's owning property, needed to build its refresh path."""
    from helixis.rentvine.mapper import _int

    for scope in (payload, *(payload.get(k) for k in ("data", "object", "payload", "record"))):
        if isinstance(scope, dict):
            for key in ("propertyID", "propertyId", "property_id", "propertyid"):
                if key in scope and (value := _int(scope[key])) is not None:
                    return value
    return None


def message_id(company_id: str, raw_body: bytes) -> str:
    """The delivery's identity for the queue's dedup key.

    ⚠ **Derived from the BYTES, and scoped by company.** ``queue.dedup_key``
    keys on ``AccountId`` + name + time + entity ids, and every Rentvine
    envelope carries the same synthetic ``AccountId: 0`` — so without this two
    companies' structurally identical events in the same second would collide
    on the unique index and the second would be silently dropped. ``MessageId``
    is the existing escape hatch for exactly this (the email and risk bridges
    use it) and the queue already folds it in.

    ⚠ It hashes the WHOLE body rather than an id plus a timestamp, and the
    direction that fails is the safe one: a byte-identical redelivery dedups,
    two different updates to one record stay distinct, and a redelivery whose
    bytes were re-serialised processes twice — which is harmless, because the
    refresh is an upsert.
    """
    digest = hashlib.sha256(f"rentvine:{company_id}:".encode() + raw_body).hexdigest()
    return f"rentvine:{digest[:32]}"


def to_envelope(
    payload: dict[str, Any],
    *,
    company_id: str,
    raw_body: bytes,
    now: datetime | None = None,
) -> dict[str, Any]:
    """A Rentvine delivery, rewritten as the Buildium-shaped envelope the queue
    and the handlers already understand.

    Raises :class:`UnmappableEvent` when the delivery cannot be named — the
    caller acknowledges and drops rather than inventing a name.
    """
    object_key, verb = identify(payload)
    spec = OBJECTS[object_key]
    record_id = entity_id(payload, object_key)
    if record_id is None:
        raise UnmappableEvent(
            f"a {object_key} {verb.lower()} delivery carried no usable "
            f"{spec['rentvine_id']} — there is nothing to refresh, and an "
            f"envelope with no entity id would enqueue work for no record"
        )

    envelope: dict[str, Any] = {
        "EventName": f"{spec['event_prefix']}.{verb}",
        "EventDateTime": (now or datetime.now(UTC)).isoformat(),
        # ⚠ A SYNTHETIC. `account_id bigint NOT NULL` is Buildium's account
        # number and a Rentvine delivery has no such thing (ADR 0010, R3-5).
        # Zero satisfies the column without asserting a Buildium account that
        # does not exist, and `MessageId` below is what actually keeps two
        # companies' events apart. It is NOT a Rentvine identifier.
        "AccountId": 0,
        "MessageId": message_id(company_id, raw_body),
        spec["envelope_id"]: record_id,
        # The delivery verbatim, so nothing downstream has to re-derive what
        # arrived and a Phase 8 probe can read the real shape off a real row.
        "RentvinePayload": payload,
        "Pms": "rentvine",
    }
    if object_key == "unit" and (parent := parent_property_id(payload)) is not None:
        envelope["PropertyId"] = parent
    return envelope


def refresh_target(payload: dict[str, Any], envelope: dict[str, Any]) -> tuple[str, str, dict]:
    """``(mirror_table, path_template, path_params)`` for the one record to re-read.

    Raises :class:`UnmappableEvent` when a unit delivery does not name its
    property — the per-unit route needs it and there is no unit-only GET.
    """
    object_key, _verb = identify(payload)
    spec = OBJECTS[object_key]
    record_id = entity_id(payload, object_key)
    params: dict[str, Any] = {spec["rentvine_id"]: record_id}
    if object_key == "unit":
        parent = envelope.get("PropertyId") or parent_property_id(payload)
        if parent is None:
            raise UnmappableEvent(
                "a unit delivery named no property, and Rentvine's only unit GET "
                "is nested under one — refusing to guess the parent"
            )
        params["propertyID"] = parent
    return spec["table"], spec["path"], params


__all__ = [
    "OBJECTS",
    "UnmappableEvent",
    "entity_id",
    "identify",
    "message_id",
    "parent_property_id",
    "refresh_target",
    "to_envelope",
]
