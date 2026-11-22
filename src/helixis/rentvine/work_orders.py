"""Changing ONE field on a Rentvine work order, safely. Phase 6.

THE FINDING THIS MODULE EXISTS FOR
    ``POST /maintenance/work-orders/{workOrderID}`` declares **38 body fields
    with ``required: []``**, while ``POST /maintenance/work-orders`` (create)
    declares 43 and requires 8. Two readings, and the captured spec cannot
    separate them:

    (a) **partial update** — send only what changes, and ``required: []`` is a
        correct description of a PATCH-shaped endpoint; or
    (b) **full replace with a permissive schema** — send everything, and an
        omitted field is BLANKED.

    Under (b), a one-field update erases 37 fields on a landlord's work order:
    the vendor, the dates, the instructions, the description. It would return
    200 and nobody would look. That is Buildium gotcha 20's shape — "PUTs are
    full-replace… a partial-body PUT is a compliance bug, not just a data bug"
    — arriving on an endpoint whose VERB gives no hint, because Rentvine has no
    PUT at all and every update is a POST.

    **So this reads the record, merges the one change, and sends the whole
    thing back.** That is correct under BOTH readings: under (a) it sets each
    field to the value it already had, and under (b) it is the only safe body.
    ⚠ @unverified — which reading is true. Settled by a Phase 8 probe: change
    one field on a sandbox work order with a one-field body and re-read it.
    Until then the merge costs one extra GET and cannot be wrong.

⚠ **TWO FIELDS ARE DELIBERATELY NEVER ECHOED, AND THEY ARE THE RIGHT TWO.**
Measured against the spec: 36 of the 38 writable fields also appear in the GET
response, so a merge can reconstruct nearly the whole body. The two it cannot
read back are ``sendVendorNotification`` and ``sendReview`` — which are not
state, they are SIDE-EFFECT TRIGGERS. Echoing them would email a vendor every
time somebody nudged a status. The two unmergeable fields turn out to be
exactly the two that must not be merged, which is luck, and it is recorded
here so a future reader does not "fix" it.

⚠ **AND NOT ECHOING THEM WAS NOT ENOUGH.** The spec declares
``sendVendorNotification`` with ``"default": true`` ("Send notification to
vendor on changes"), so a body that LEAVES IT OUT asks for the notification.
Every status change made through Occupella mailed the work order's vendor, a
message nobody confirmed (terms rule 14, gotcha 191). Both triggers are now
sent explicitly false (:data:`QUIET`).
"""

from __future__ import annotations

from typing import Any

from helixis.rentvine._query_index import BODY_FIELDS

# MEASURED 2026-10-02 by `scripts/rentvine_write_probe.py` against sandbox
# work order 9 (14 populated writable fields, a vendor among them):
#
# * THE UPDATE IS A PARTIAL UPDATE. A one-field body changed the status and
#   every other field survived. So the read-merge-write below is belt and
#   braces rather than the only thing between a status change and an erased
#   work order. It stays: it is correct under both readings, and the re-read
#   before sending (agents/rentvine_tools.py) is what catches an edit made in
#   Rentvine after the manager approved, which a one-field body would not.
# * A COMPLETED WORK ORDER CAN BE MOVED BACK. The probe set the status and set
#   it back, and the re-GET read the original. Unlike Buildium's latch
#   (gotcha 20), a Rentvine status change is reversible. The write tool's
#   Manager+ floor was chosen for unverified reversibility; relaxing it is a
#   product decision this evidence now permits, not one taken here.
#
# The probe's earlier "0 populated writable fields" refusal was its own
# envelope bug (the GET returns {"workOrder": ..., "schedulingStatusID": ...}),
# fixed in the same session.

#: The operation this module writes through.
UPDATE_OP = "POST /maintenance/work-orders/{workOrderID}"
UPDATE_PATH = "/maintenance/work-orders/{workOrderID}"
GET_PATH = "/maintenance/work-orders/{workOrderID}"
STATUSES_PATH = "/maintenance/work-order/statuses"

#: Fields that exist on the write surface but must never be re-sent.
#:
#: ⚠ They are not state. ``sendVendorNotification`` mails the vendor and
#: ``sendReview`` asks for a review — echoing either on every status change
#: turns one edit into a stream of messages to somebody else's contractor.
#: They are also absent from the GET response, so there is nothing to echo
#: even if we wanted to; that absence is what makes this safe rather than
#: careful.
NEVER_ECHOED: frozenset[str] = frozenset({"sendVendorNotification", "sendReview"})

#: What every update body sends for those triggers: FALSE, never omitted.
#:
#: ⚠ Omitted is not off. The spec's ``"default": true`` on
#: ``sendVendorNotification`` means a body without it mails the vendor.
#: ``sendReview`` defaults false and is pinned anyway, so the body says what it
#: means instead of leaning on a default nobody has watched the server apply.
QUIET: dict[str, bool] = dict.fromkeys(NEVER_ECHOED, False)

#: The field this module's one tool is named for.
STATUS_FIELD = "workOrderStatusID"


def writable_fields() -> frozenset[str]:
    """What the spec says the update accepts, minus the side-effect triggers.

    Read from the GENERATED index rather than typed here, so it cannot drift
    from the captured spec (the index is regenerated by
    ``scripts/rentvine_query_index.py`` and a contract test fails on drift).
    """
    declared = BODY_FIELDS.get(UPDATE_OP)
    if not declared:
        # ⚠ An empty index entry is not "the endpoint takes no body" — it is
        # "we could not read one", and that is the $ref/allOf trap this repo
        # has hit five times. Refuse rather than send a body built from
        # nothing.
        raise RentvineWorkOrderError(
            f"the generated body index knows no fields for {UPDATE_OP} — refusing to "
            f"build an update body from an empty field list"
        )
    return frozenset(declared) - NEVER_ECHOED


class RentvineWorkOrderError(Exception):  # noqa: N818 — a refusal, not a fault
    """The update could not be built from what was in hand."""


def merge_update(current: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    """The full body to POST: every readable writable field, plus ``changes``,
    plus :data:`QUIET` (both side-effect triggers sent false).

    ``current`` is the record as the GET returned it (already unwrapped from
    its envelope). ``changes`` is what the caller is actually setting.

    ⚠ **A field absent from ``current`` is OMITTED, not sent as null.** Under
    reading (b) that means it is blanked — but sending an explicit null blanks
    it under BOTH readings, so omission is strictly safer, and the alternative
    would invent a value we never read. What it costs is stated rather than
    hidden: a field the GET does not return cannot be preserved by any merge.

    ⚠ **A change naming a field the spec does not declare RAISES.** Silently
    dropping it would report success for an edit that never happened — the
    200-but-lie this whole module is built around.
    """
    allowed = writable_fields()
    unknown = sorted(k for k in changes if k not in allowed)
    if unknown:
        raise RentvineWorkOrderError(
            f"{UPDATE_OP} declares no field(s) {unknown}; declared fields are "
            f"{sorted(allowed)[:8]}… — refusing to send a change the endpoint will ignore"
        )
    body = {k: v for k, v in current.items() if k in allowed and v is not None}
    body.update(changes)
    # Last, so nothing above can turn a trigger back on (a change naming one
    # already raised: neither is in ``writable_fields``).
    body.update(QUIET)
    return body


def status_choices(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """``{lowercased status name: id}`` from the account's own status list.

    ⚠ **The vocabulary is per ACCOUNT, not per vendor.**
    ``GET /maintenance/work-order/statuses`` is a real collection a customer
    edits, so "Completed" is not guaranteed to exist and its id is not
    guaranteed to be stable across accounts. Resolving a NAME against the live
    list is what stops a model passing a raw id and moving a work order to
    whatever status happens to hold that number.
    """
    out: dict[str, Any] = {}
    for row in rows:
        name = row.get("name") or row.get("statusName") or row.get("title")
        ident = row.get("workOrderStatusID") or row.get("id")
        if name and ident is not None:
            out[str(name).strip().lower()] = ident
    return out


def resolve_status(rows: list[dict[str, Any]], wanted: str) -> tuple[Any | None, list[str]]:
    """``(id, all_names)`` for ``wanted``, or ``(None, all_names)``.

    Returns the full list either way so a refusal can NAME the real options
    instead of telling somebody their status does not exist and stopping —
    pointing only at a prohibition is what makes a model improvise.
    """
    choices = status_choices(rows)
    names = sorted(choices)
    return choices.get(str(wanted).strip().lower()), names


def current_status_id(record: dict[str, Any]) -> Any | None:
    """The work order's status id as the GET returned it."""
    for key in (STATUS_FIELD, STATUS_FIELD.lower(), "workorderstatusid"):
        if key in record:
            return record[key]
    return None


__all__ = [
    "GET_PATH",
    "NEVER_ECHOED",
    "QUIET",
    "STATUSES_PATH",
    "STATUS_FIELD",
    "UPDATE_OP",
    "UPDATE_PATH",
    "RentvineWorkOrderError",
    "current_status_id",
    "merge_update",
    "resolve_status",
    "status_choices",
    "writable_fields",
]
