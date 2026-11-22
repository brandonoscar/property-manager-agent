"""The Rentvine agent write surface. Phase 6.

The first tool, the status change below, came alone: one write to prove the
whole gate stack (dark flag, role floor, read-merge-write, confirm gate,
idempotency claim, read-back verify) against an API nobody here had called.
The founder added four on 2026-10-03 (``docs/decisions.md``): create a work
order, upload a file, post a lease charge and record a lease payment. Their
bodies and the assumptions under them are in ``rentvine/writes.py``.

⚠ **THE MONEY WRITES ARE PERMANENT.** Rentvine's REVERSAL surface is one
endpoint in the entire API: `POST /accounting/deposits/{id}/unreconcile`, plus
a bill void. There is **no documented way to void a posted lease charge or
payment** (`transaction.isVoided` is a state no operation sets), so both stand
like Buildium's immutable payment: Manager+ only, and the confirm card says it
cannot be undone from Occupella. Neither moves money (terms rule 15): a payment
RECORDS one already received.

⚠ The four are shown on Rentvine runs only (``run_pms``): ADR 0010 promises a
Buildium customer's prompt is unchanged, and four more write schemas on every
Buildium turn would break that for tools that could only refuse there.

GATE ORDER, AND IT IS THE ORDER THAT MATTERS
    dark flag → this company's connection → role floor → resolve the status →
    read the record → confirm → idempotency claim → RE-READ (refuse if the
    record moved since the card) → write → read-back verify (the status, and
    that no field the body carried came back empty).

    ⚠ A refusal that provably sent nothing (a Retry-After hold, the breaker,
    a 4xx rejection, the record moving) is filed RETRYABLE, never
    ``abandoned_unknown``; that one would block the retry for the whole
    window and then claim the change was sent (hardened 2026-10-02).

    ⚠ The dark check is FIRST for gotcha 67's reason: nobody should approve a
    write that cannot happen. A confirm card shown before a flag check teaches
    a manager that their approval means something it does not.

⚠ **THE ROLE FLOOR IS DELIBERATELY STRICTER THAN THE BUILDIUM EQUIVALENT.**
`buildium_record_move_out` is confirm-only because its reversal was PROBED;
`buildium_close_work_order` carries Manager+ because closing latches one-way.
On Rentvine neither is known. Unverified reversibility is treated as
hard-to-undo, which costs a member one escalation and saves a landlord a work
order nobody can reopen.

⚠ **THE TWO OPEN QUESTIONS THIS TOOL RESTS ON ARE MARKED IN
`rentvine/work_orders.py`, NOT HERE** — whether the update is a partial or a
full replace, and whether a completed work order can be moved back. That is
where the endpoint knowledge lives, and more importantly it is where
`scripts/rentvine_unverified.py` LOOKS: it scans `src/helixis/rentvine/` only,
so an assumption recorded in this module is invisible to the Phase 8 work list.
An assumption nobody can find is one nobody settles.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from datetime import date, timedelta
from typing import Any

from agents import function_tool

from helixis.agents.idempotency import (
    SUCCESS_OUTCOMES,
    PreSideEffectError,
    derive_key,
    pending_reply,
    run_guarded,
)
from helixis.agents.run_pms import rentvine_status_tool_enabled, rentvine_write_tool_enabled
from helixis.memory.context import current_memory_context
from helixis.pms import places
from helixis.rentvine import work_orders as wo
from helixis.rentvine import writes
from helixis.rentvine.client import RentvineAPIError, RentvineClient
from helixis.rentvine.credentials import load_rentvine_settings_for_company
from helixis.settings import get_settings

logger = logging.getLogger(__name__)

_VERIFY_MAX_ATTEMPTS = 3
_VERIFY_BACKOFF_SECONDS = 0.2


class _NotAppliedError(PreSideEffectError):
    """The write PROVABLY did not happen, so the attempt may be retried.

    ``PreSideEffectError`` is what the idempotency store files as
    ``failed_retryable``; every other exception out of ``execute`` is filed
    ``abandoned_unknown``, which blocks a retry for the whole window and then
    tells the user "I sent it but couldn't confirm it" about a change that
    never left Occupella. Carries the reply for the user.
    """

    def __init__(self, reply: str):
        self.reply = reply
        super().__init__(reply)


def _provably_not_applied(exc: RentvineAPIError) -> bool:
    """Refused before sending (a hold, the breaker), or REJECTED by Rentvine
    with a 4xx. A 5xx or a transport error is ambiguous: it may have landed."""
    return (not exc.sent) or 400 <= exc.status < 500


def _erased_fields(sent: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Fields the POST carried a value for that the re-read no longer has.

    ⚠ This is the live answer to ``rentvine/work_orders.py``'s open question
    (partial update or full replace). The body re-sends every field, so a
    field that comes back EMPTY was cleared by Rentvine, not by us. Presence
    only: a value that comes back reformatted ("1.00" for 1) is not erasure.
    """

    def _empty(v: Any) -> bool:
        return v is None or (isinstance(v, str) and not v.strip())

    # ⚠ The side-effect triggers (``wo.QUIET``) ride on every update and are not
    # state: the GET never returns them, so reading one back as "cleared" would
    # hold every status change for review (gotcha 191).
    return sorted(
        k
        for k, v in sent.items()
        if k not in wo.NEVER_ECHOED and not _empty(v) and _empty(after.get(k))
    )


_DARK = (
    "The Rentvine connector isn't switched on for this deployment, so I can't change "
    "anything in Rentvine. An administrator has to enable it first."
)
_NOT_CONNECTED = (
    "Rentvine isn't connected for this company, so there's nothing for me to update. "
    "Ask the user to connect Rentvine on the Connectors page (Settings → Connectors)."
)


def _current_company_id() -> str:
    ctx = current_memory_context()
    return ctx.company_id if ctx else ""


# ── what the person named, as ids (``pms.places``) ────────────────────────────
#
# ⚠ No read tool prints a property, unit, lease or vendor id: they print names,
# and an id only where a record has none. A write that took ids asked the
# model to invent one, and the card showed whichever record it landed on (the
# upload's card showed the bare id). Each write below takes the person's words
# and resolves them here, AFTER its dark, connection and role gates and BEFORE
# any Rentvine read, so the gate order in the module docstring holds. An int
# is already an id and passes through: the tests drive the cores that way.

_SYSTEM = "Rentvine"


def _mirror_pool() -> Any:
    from helixis.db.engine import get_pool

    return get_pool()


async def _resolve_place(
    property_ref: int | str,
    unit_ref: int | str | None,
    vendor_ref: int | str | None,
    nothing: str,
) -> tuple[int, int | None, int | None] | str:
    """The property, unit and vendor ids for what the person named, or the
    question to ask instead. Ids pass through without touching the mirror."""
    if (
        not isinstance(property_ref, str)
        and not isinstance(unit_ref, str)
        and not isinstance(vendor_ref, str)
    ):
        return int(property_ref), unit_ref, vendor_ref
    company_id, pms = _current_company_id(), writes.mapper.PMS
    async with _mirror_pool().acquire() as conn:
        if isinstance(property_ref, str):
            place = await places.property_named(
                conn,
                company_id=company_id,
                pms=pms,
                system=_SYSTEM,
                asked=property_ref,
                nothing=nothing,
            )
            if isinstance(place, str):
                return place
        else:
            place = places.Match(int(property_ref), f"Property #{property_ref}")
        unit_id: int | None = None
        if isinstance(unit_ref, str) and unit_ref.strip():
            unit = await places.unit_named(
                conn,
                company_id=company_id,
                pms=pms,
                system=_SYSTEM,
                place=place,
                asked=unit_ref,
                nothing=nothing,
            )
            if isinstance(unit, str):
                return unit
            unit_id = unit.id
        elif isinstance(unit_ref, int):
            unit_id = unit_ref
        vendor_id: int | None = None
        if isinstance(vendor_ref, str) and vendor_ref.strip():
            vendor = await places.vendor_named(
                conn,
                company_id=company_id,
                pms=pms,
                system=_SYSTEM,
                asked=vendor_ref,
                nothing=nothing,
            )
            if isinstance(vendor, str):
                return vendor
            vendor_id = vendor.id
        elif isinstance(vendor_ref, int):
            vendor_id = vendor_ref
    return place.id, unit_id, vendor_id


async def _resolve_lease(lease_ref: str, nothing: str) -> places.Match | str:
    """The one lease the person named, or the question to ask instead."""
    async with _mirror_pool().acquire() as conn:
        return await places.lease_named(
            conn,
            company_id=_current_company_id(),
            pms=writes.mapper.PMS,
            system=_SYSTEM,
            asked=lease_ref,
            nothing=nothing,
        )


async def _resolve_record(
    kind: str, record_ref: str, unit_ref: str | None, nothing: str
) -> places.Match | str:
    """The record a file goes on: a property, a unit (``record_ref`` names its
    property), a lease, or a work order by the #number its list prints."""
    company_id, pms = _current_company_id(), writes.mapper.PMS
    async with _mirror_pool().acquire() as conn:
        if kind == "work order":
            return await places.work_order_numbered(
                conn,
                company_id=company_id,
                pms=pms,
                system=_SYSTEM,
                asked=record_ref,
                nothing=nothing,
            )
        if kind == "lease":
            lease = await places.lease_named(
                conn,
                company_id=company_id,
                pms=pms,
                system=_SYSTEM,
                asked=record_ref,
                nothing=nothing,
            )
            if isinstance(lease, str):
                return lease
            return places.Match(lease.id, f"the lease of {lease.label}")
        place = await places.property_named(
            conn, company_id=company_id, pms=pms, system=_SYSTEM, asked=record_ref, nothing=nothing
        )
        if isinstance(place, str) or kind != "unit":
            return place
        if not (unit_ref or "").strip():
            return f"Which unit at {place.label}? {nothing}"
        return await places.unit_named(
            conn,
            company_id=company_id,
            pms=pms,
            system=_SYSTEM,
            place=place,
            asked=unit_ref or "",
            nothing=nothing,
        )


async def _require_manager_role(action: str, *, alt: str) -> str | None:
    """Manager/Admin fail-closed floor. ``None`` means proceed.

    Reuses the Buildium tool module's ``_caller_role`` rather than re-deriving
    it: "what role is the caller" must have one answer in this product, and a
    second implementation is how two lanes end up disagreeing about who may
    write (gotcha 25).
    """
    from helixis.agents.buildium_tools import _caller_role
    from helixis.api.roles import MANAGER, role_at_least

    role = await _caller_role()
    if role is None or not role_at_least(role, MANAGER):
        return (
            f"{action.capitalize()} needs a Manager or Admin on this workspace, so I didn't do "
            f"anything. Ask a teammate with that role, or {alt}."
        )
    return None


async def _open_client() -> tuple[RentvineClient | None, str | None]:
    """A client bound to THIS company's credentials, or a refusal string.

    ⚠ No env fallback anywhere on this path (gotcha 70). A write pointed at the
    wrong account by a development credential is the worst outcome available
    here, and it would verify cleanly against the wrong books.
    """
    if not get_settings().rentvine.enabled:
        return None, _DARK
    company_id = _current_company_id()
    if not company_id:
        return None, _NOT_CONNECTED
    creds = await load_rentvine_settings_for_company(company_id)
    if creds is None:
        return None, _NOT_CONNECTED
    return RentvineClient(creds), None


async def _update_work_order_status_impl(
    work_order_id: int,
    status: str,
    work_order_label: str | None = None,
) -> str:
    """Set one Rentvine work order's status. See the module docstring for the gates.

    ⚠ **ONE ``aclose``, in a ``finally``.** The body below has seven early
    returns — a dark flag, a role floor, an unknown status, a no-op, three
    guard outcomes — and an ``aclose()`` beside each of them is the fan-out
    gotchas 25/94 record, where the eighth branch forgets and every refusal
    leaks an httpx client. The first cut did exactly that on the
    unknown-status path; a test asserting the client was closed is what found
    it, because a leak fails nothing and surfaces as a connection count.
    """
    client, refusal = await _open_client()
    if refusal or client is None:
        return refusal or _DARK
    try:
        return await _update_with_client(client, work_order_id, status, work_order_label)
    finally:
        await client.aclose()


async def _update_with_client(
    client: RentvineClient,
    work_order_id: int,
    status: str,
    work_order_label: str | None,
) -> str:
    """The gate stack, with the client's lifetime owned by the caller."""
    denied = await _require_manager_role(
        "changing a work order's status in Rentvine",
        alt="change it in Rentvine directly",
    )
    if denied:
        return denied

    company_id = _current_company_id()
    try:
        # 1. The status VOCABULARY is the account's own, so a name is resolved
        #    against the live list. A model handing over a raw id would move
        #    the work order to whatever status holds that number.
        statuses = await client.get_rows(wo.STATUSES_PATH)
        status_id, names = wo.resolve_status(statuses, status)
        if status_id is None:
            return (
                f"Rentvine has no work-order status called {status!r} on this account. "
                f"The statuses it does have are: {', '.join(names) or '(none returned)'}."
            )

        # 2. READ the whole record. This is what makes the write safe under
        #    both readings of a schema that declares 38 fields and requires
        #    none — see rentvine/work_orders.py.
        current = await client.get_object(
            wo.GET_PATH, id_field="workOrderID", path_params={"workOrderID": int(work_order_id)}
        )
    except RentvineAPIError as exc:
        return _sanitize(exc, "look up the work order")

    # ⚠ work_order_label is DELIBERATELY IGNORED when the record answers. The
    # record is already in hand, so showing the model's string on the confirm
    # card instead of the real description would be a card describing something
    # other than what is about to change (the Buildium tools learned this).
    label = str(current.get("description") or work_order_label or f"work order {work_order_id}")
    if str(wo.current_status_id(current) or "") == str(status_id):
        return f"{label} is already {status} in Rentvine — nothing to do."

    body = wo.merge_update(current, {wo.STATUS_FIELD: status_id})
    key = derive_key(
        company_id,
        "rentvine_update_work_order_status",
        {"work_order_id": int(work_order_id), "status_id": str(status_id)},
    )

    async def _do_update() -> str:
        # ⚠ RE-READ BEFORE SENDING. The approver saw ``body``, built from a
        # read taken before the confirm card; approval can come minutes
        # later. Every field is re-sent, so an edit made in Rentvine in
        # between (a vendor assigned, a note added) would be silently
        # overwritten with the old value, under EITHER reading of the update
        # endpoint. If the record moved, nothing is sent: the approval was for
        # a body that is no longer the right one.
        try:
            fresh = await client.get_object(
                wo.GET_PATH,
                id_field="workOrderID",
                path_params={"workOrderID": int(work_order_id)},
            )
        except RentvineAPIError as exc:
            reply = _sanitize(exc, "re-check the work order before changing it")
            raise _NotAppliedError(reply) from exc
        if wo.merge_update(fresh, {wo.STATUS_FIELD: status_id}) != body:
            raise _NotAppliedError(
                f"{label} changed in Rentvine after you approved, so I didn't send the "
                "status change. Nothing was changed in Rentvine. Ask me again and I'll "
                "show you the current record to approve."
            )
        try:
            await client.post(
                wo.UPDATE_PATH,
                json_body=body,
                path_params={"workOrderID": int(work_order_id)},
            )
        except RentvineAPIError as exc:
            if _provably_not_applied(exc):
                raise _NotAppliedError(_sanitize(exc, "update the work order")) from exc
            raise
        return f"Set {label} to {status} in Rentvine."

    #: Fields the read-back found emptied (see ``_erased_fields``).
    erased: list[str] = []

    async def _verify_status(_result: str) -> bool:
        """RE-GET and assert the field this tool is NAMED for (gotcha 70).

        ⚠ A 2xx is not the write. Buildium's ``PUT /workorders`` returns 200 and
        silently ignores a Status, and this endpoint's semantics are unprobed —
        so the only acceptable evidence is the value coming back changed.
        READ-ONLY, bounded retries for read-after-write lag.
        """
        last: Exception | None = None
        for attempt in range(_VERIFY_MAX_ATTEMPTS):
            try:
                again = await client.get_object(
                    wo.GET_PATH,
                    id_field="workOrderID",
                    path_params={"workOrderID": int(work_order_id)},
                )
                if str(wo.current_status_id(again) or "") == str(status_id):
                    erased[:] = _erased_fields(body, again)
                    # The status landed; a field it cleared is a NEEDS-REVIEW
                    # outcome, not a success (rule 6: re-read the record).
                    return not erased
            except RentvineAPIError as exc:
                if exc.status == 404:
                    return False
                last = exc
            except Exception as exc:  # noqa: BLE001 — transient read failure
                last = exc
            if attempt < _VERIFY_MAX_ATTEMPTS - 1:
                await asyncio.sleep(_VERIFY_BACKOFF_SECONDS * (attempt + 1))
        if last is not None:
            raise last
        return False

    try:
        guard = await run_guarded(
            company_id=company_id,
            idem_key=key,
            action_type="rentvine_update_work_order_status",
            confirm={
                "action": "rentvine_update_work_order_status",
                "title": "Change this work order's status in Rentvine?",
                "details": f"Set {label} to {status} in Rentvine.",
                "items": [
                    f"Work order: {label}",
                    f"New status: {status}",
                    # What the body's QUIET flags ask for, said on the card:
                    # Rentvine's own default for this update is to notify.
                    "Rentvine's vendor notification: off",
                ],
                # ⚠ The ACTUAL body, not a summary of it. This endpoint may be
                # a full replace, so what is being sent is 36 fields — and an
                # approver entitled to see "the status changes" is entitled to
                # see that the rest of the record is being re-sent too.
                "request_body": body,
            },
            execute=_do_update,
            verify=_verify_status,
            window=timedelta(minutes=10),
        )
    except _NotAppliedError as refused:
        return refused.reply
    except RentvineAPIError as exc:
        return _sanitize(exc, "update the work order")

    if guard.outcome == "not_approved":
        return "Okay, I didn't change the work order. Nothing was changed in Rentvine."
    if guard.outcome == "blocked_store_unavailable":
        return (
            "I couldn't safely process this right now, so I did not change the work order — "
            "nothing was changed in Rentvine. Please try again shortly."
        )
    if guard.outcome == "blocked_needs_review" and erased:
        logger.error(
            "rentvine work order %s: status set but fields cleared by the update: %s",
            work_order_id,
            ", ".join(erased),
        )
        return (
            f"Rentvine changed {label} to {status}, but the same update cleared these "
            f"fields on it: {', '.join(erased)}. Please open the work order in Rentvine "
            "and restore them; I've flagged this for review and won't retry."
        )
    if guard.outcome == "blocked_needs_review":
        return (
            f"I sent the status change for {label} to Rentvine but couldn't confirm it went "
            "through, so I'm flagging it for review. Please check Rentvine before retrying."
        )
    if guard.outcome not in SUCCESS_OUTCOMES:
        return pending_reply(guard, action="change the work order", system="Rentvine")
    if guard.result is not None:
        return str(guard.result)
    return f"Set {label} to {status} in Rentvine."


def _sanitize(exc: RentvineAPIError, action: str) -> str:
    """A Rentvine failure in terms the user can act on.

    ⚠ 401 and 403 are DIFFERENT, and conflating them is a documented product
    bug on the Buildium side (gotcha 102): a 403 means the credential was read,
    accepted, and refused for this operation, so telling somebody to reconnect
    sends them to redo a connection that works. Rentvine makes this sharper —
    its API keys are bound to a role the customer picks at creation, so a
    restricted key is an expected state rather than a fault.
    """
    from helixis.rentvine.transport import Failure

    if exc.failure is Failure.CREDENTIALS:
        return (
            f"I couldn't {action} — Rentvine rejected the credentials. Nothing was changed "
            "in Rentvine. Ask the user to reconnect Rentvine on the Connectors page."
        )
    if exc.failure is Failure.PERMISSION:
        return (
            f"I couldn't {action} — Rentvine accepted the credentials and refused this "
            "operation, which means the API key's role doesn't allow it. That's fixed on the "
            "key's permissions in Rentvine, not by reconnecting. Nothing was changed in Rentvine."
        )
    if exc.failure is Failure.NOT_FOUND:
        return (
            f"I couldn't {action} — Rentvine has no record with that id. "
            "Nothing was changed in Rentvine."
        )
    if exc.failure is Failure.UNAVAILABLE:
        # Occupella's circuit breaker (terms rule 28): nothing was sent.
        remaining = getattr(exc, "remaining", None)
        wait = (
            f"about {max(1, math.ceil(remaining / 60))} minute(s)" if remaining else "a few minutes"
        )
        return (
            f"I couldn't {action} — Rentvine isn't responding right now, so Occupella has "
            f"paused calls to it. Nothing was sent and nothing was changed in Rentvine. "
            f"Try again in {wait}."
        )
    if exc.status == 429:
        # A throttle, or a hold from an earlier one: the change was not sent
        # (``RentvineClient.post`` refuses into a hold and never retries).
        wait = f"about {math.ceil(exc.retry_after)} seconds" if exc.retry_after else "a minute"
        return (
            f"I couldn't {action} — Rentvine asked Occupella to slow down, so the change "
            f"wasn't sent and nothing was changed in Rentvine. Try again in {wait}."
        )
    return f"I couldn't {action} right now. Nothing was changed in Rentvine."


@function_tool
async def rentvine_update_work_order_status(
    work_order_id: int,
    status: str,
    work_order_label: str | None = None,
) -> str:
    """Change a Rentvine work order's status (a write — confirm-gated).

    Use for "mark work order <X> in progress / completed / on hold" when the
    company is on RENTVINE. Confirmation is enforced in code; you do NOT add a
    separate confirmation step.

    ``status`` is the status NAME as it appears in Rentvine ("Completed", "In
    Progress") — not a number. If the account does not have that status, the
    reply lists the ones it does have. Always pass ``work_order_label`` so the
    confirm card and your reply show the work order's description rather than a
    raw id.

    ⚠ This changes ONE field and re-sends the rest of the record unchanged,
    because Rentvine's update endpoint may be a full replace. Only a Manager or
    Admin may use it.
    """
    return await _update_work_order_status_impl(work_order_id, status, work_order_label)


# ── the four creates (founder, 2026-10-03) ────────────────────────────────

_CREATE_WINDOW = timedelta(minutes=10)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PERMANENT = (
    "This can't be undone from Occupella: Rentvine documents no way to void it. "
    "A correction is a separate entry made in Rentvine."
)


def _valid_date(value: str) -> bool:
    if not _DATE_RE.match(value or ""):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _unsuccessful_reply(guard: Any, *, action: str) -> str:
    """What the user is told when a create did NOT complete, worded once for
    the four (the status write words its own, with an erased-field case the
    creates do not have). Each caller checks ``SUCCESS_OUTCOMES`` itself, so no
    outcome can fall through to a success reply."""
    if guard.outcome == "not_approved":
        return f"Okay, I didn't {action}. Nothing was changed in Rentvine."
    if guard.outcome == "blocked_store_unavailable":
        return (
            f"I couldn't safely process this right now, so I did not {action}; "
            "nothing was changed in Rentvine. Please try again shortly."
        )
    if guard.outcome == "blocked_needs_review":
        return (
            f"I sent the request to {action}, but couldn't confirm it in Rentvine, so I'm "
            "flagging it for review. Please check Rentvine before trying again."
        )
    return pending_reply(guard, action=action, system="Rentvine")


async def _lease(client: RentvineClient, lease_id: int, includes: str) -> Any:
    return await client.get(
        writes.LEASE_PATH, path_params={"leaseID": int(lease_id)}, query={"includes": includes}
    )


async def _create_work_order_impl(
    property_id: int | str,
    description: str,
    unit_id: int | str | None = None,
    priority: str = "Medium",
    vendor_id: int | str | None = None,
) -> str:
    """Create a Rentvine work order. See :func:`rentvine_create_work_order`.
    A str is the person's words for the record (:func:`_resolve_place`)."""
    text = (description or "").strip()
    if not text:
        return "A work order needs a description of the problem. Nothing was created."
    priority_id = writes.PRIORITY_ID.get((priority or "Medium").strip().lower())
    if priority_id is None:
        return "A work order's priority is Low, Medium or High. Nothing was created."
    client, refusal = await _open_client()
    if refusal or client is None:
        return refusal or _DARK
    try:
        ids = await _resolve_place(property_id, unit_id, vendor_id, "Nothing was created.")
        if isinstance(ids, str):
            return ids
        place_id, unit_ref, vendor_ref = ids
        return await _create_work_order_with(
            client, place_id, unit_ref, text, priority_id, vendor_ref
        )
    finally:
        await client.aclose()


async def _create_work_order_with(
    client: RentvineClient,
    property_id: int,
    unit_id: int | None,
    text: str,
    priority_id: str,
    vendor_id: int | None,
) -> str:
    company_id = _current_company_id()
    try:
        status_id = writes.open_status_id(await client.get_rows(wo.STATUSES_PATH))
        prop = await client.get_object(
            "/properties/{propertyID}",
            id_field="propertyID",
            path_params={"propertyID": property_id},
        )
        unit: dict[str, Any] = {}
        if unit_id is not None:
            unit = await client.get_object(
                writes.UNIT_PATH,
                id_field="unitID",
                path_params={"propertyID": property_id, "unitID": int(unit_id)},
            )
        vendor: dict[str, Any] = {}
        if vendor_id is not None:
            vendor = await client.get_object(
                "/vendors/{contactID}",
                id_field="contactID",
                path_params={"contactID": int(vendor_id)},
            )
    except RentvineAPIError as exc:
        return _sanitize(exc, "look up the property for the work order")
    if status_id is None:
        return (
            "This Rentvine account has no work-order status marked Open, so I can't tell "
            "which status a new work order starts in. Nothing was created; please create "
            "it in Rentvine."
        )
    is_vacant = False
    if unit_id is not None:
        vacant = writes.unit_is_vacant(unit)
        if vacant is None:
            return (
                "Rentvine's record for that unit doesn't say whether it has a lease, and a "
                "new work order has to say whether the unit is vacant. Nothing was "
                "created; please create it in Rentvine."
            )
        is_vacant = vacant
    # The card names what the SERVER returned, never the model's words for it.
    place = str(prop.get("name") or f"property {property_id}")
    if unit_id is not None:
        place = f"Unit {unit.get('name') or unit_id} at {place}"
    who = str(vendor.get("name") or f"vendor {vendor_id}") if vendor_id is not None else None
    priority = next(k for k, v in writes.PRIORITY_ID.items() if v == priority_id).capitalize()
    body = writes.work_order_body(
        property_id=property_id,
        unit_id=None if unit_id is None else int(unit_id),
        description=text,
        status_id=status_id,
        priority_id=priority_id,
        is_vacant=is_vacant,
        vendor_id=None if vendor_id is None else int(vendor_id),
    )
    items = [f"Where: {place}", f"Problem: {text}", f"Priority: {priority}"]
    if unit_id is not None:
        items.append(f"Unit vacant: {'yes' if is_vacant else 'no'}")
    items.append(f"Vendor: {who} (not notified)" if who else "Vendor: none assigned")
    items.append("Not shared with the tenant or owner portal; nobody is emailed.")
    key = derive_key(
        company_id,
        "rentvine_create_work_order",
        {
            "property": property_id,
            "unit": unit_id,
            "text": text,
            "priority": priority_id,
            "vendor": vendor_id,
        },
    )
    created: dict[str, int | None] = {"id": None}

    async def _do_create() -> str:
        try:
            response = await client.post(writes.CREATE_WORK_ORDER_PATH, json_body=body)
        except RentvineAPIError as exc:
            # A refusal that provably sent nothing is retryable; anything
            # ambiguous propagates, so a create is never sent twice.
            if _provably_not_applied(exc):
                raise _NotAppliedError(_sanitize(exc, "create the work order")) from exc
            raise
        created["id"] = writes.created_work_order_id(response)
        ref = f" (work order {created['id']})" if created["id"] else ""
        return f"Created the work order for {place} in Rentvine{ref}: {text}"

    async def _verify_created(_result: str) -> bool:
        """RE-GET the new work order (rule 6): it exists, at this property,
        with this description."""
        if created["id"] is None:
            return False
        again = await client.get_object(
            wo.GET_PATH, id_field="workOrderID", path_params={"workOrderID": created["id"]}
        )
        same_place = writes.mapper._int(again.get("propertyID")) == property_id
        return same_place and text in str(again.get("description") or "")

    try:
        guard = await run_guarded(
            company_id=company_id,
            idem_key=key,
            action_type="rentvine_create_work_order",
            confirm={
                "action": "rentvine_create_work_order",
                "title": "Create this work order in Rentvine?",
                "details": f"Create a {priority.lower()}-priority work order for {place}.",
                "items": items,
                "request_body": body,
            },
            execute=_do_create,
            verify=_verify_created,
            window=_CREATE_WINDOW,
        )
    except _NotAppliedError as refused:
        return refused.reply
    except RentvineAPIError as exc:
        return _sanitize(exc, "create the work order")
    if guard.outcome not in SUCCESS_OUTCOMES:
        return _unsuccessful_reply(guard, action="create the work order")
    return str(guard.result) if guard.result is not None else f"Created the work order for {place}."


async def _upload_file_impl(
    document_name: str,
    attach_to: str,
    record_id: int | str,
    title: str | None = None,
    unit: str | None = None,
) -> str:
    """Upload a Documents-library file to a Rentvine record. See
    :func:`rentvine_upload_file`. A str is the person's words for the record
    (:func:`_resolve_record`; ``unit`` with it for a unit)."""
    from helixis.db.engine import get_pool
    from helixis.db.repositories import docs_links as docs_repo
    from helixis.storage import StorageError, download_object

    kind = (attach_to or "").strip().lower().replace("_", " ").replace("workorder", "work order")
    object_type = writes.ATTACH_OBJECT_TYPE.get(kind)
    if object_type is None:
        return (
            "I can attach a file to a property, unit, lease or work order in Rentvine. "
            "Which should this go on?"
        )
    company_id = _current_company_id()
    if not company_id:
        return _NOT_CONNECTED
    # Only the company's Documents library, never a path on this server (gotcha 176).
    docs = await docs_repo.list_documents(get_pool(), company_id=company_id)
    needle = (document_name or "").strip().lower()
    matches = [d for d in docs if d.name.strip().lower() == needle] or [
        d for d in docs if needle and needle in d.name.lower()
    ]
    if not docs:
        return (
            "Your Documents library is empty, so there is no file for me to upload. "
            "Nothing was sent to Rentvine."
        )
    if not matches:
        names = ", ".join(d.name for d in docs[:10])
        return f'No document matches "{document_name}". Your library has: {names}.'
    if len(matches) > 1:
        names = ", ".join(d.name for d in matches[:10])
        return f"Several documents match: {names}. Which one?"
    doc = matches[0]
    client, refusal = await _open_client()
    if refusal or client is None:
        return refusal or _DARK
    try:
        if isinstance(record_id, str):
            named = await _resolve_record(
                kind, record_id, unit, "Nothing was uploaded to Rentvine."
            )
            if isinstance(named, str):
                return named
            record_id, target = named.id, named.label
        else:
            target = f"{kind} {int(record_id)}"
        file_title = (title or doc.name).strip()[:255]
        query = {"objectTypeID": object_type, "objectID": int(record_id), "includes": "attachment"}
        key = derive_key(
            company_id,
            "rentvine_upload_file",
            {
                "document": doc.id,
                "type": object_type,
                "record": int(record_id),
                "title": file_title,
            },
        )
        sent: dict[str, Any] = {"response": None}

        async def _do_upload() -> str:
            try:
                content = await download_object(
                    bucket="company_documents", path=doc.storage_path, settings=get_settings()
                )
            except StorageError as exc:
                raise _NotAppliedError(
                    f'I couldn\'t read "{doc.name}" from your Documents library, so nothing '
                    "was uploaded to Rentvine. Please try again shortly."
                ) from exc
            files = {"file": (file_title, content, doc.content_type or "application/octet-stream")}
            try:
                sent["response"] = await client.post(writes.FILES_PATH, files=files, query=query)
            except RentvineAPIError as exc:
                if _provably_not_applied(exc):
                    raise _NotAppliedError(_sanitize(exc, "upload the file")) from exc
                raise
            return f'Uploaded "{file_title}" to {target} in Rentvine.'

        async def _verify_uploaded(_result: str) -> bool:
            """The upload's own response is the only read-back the documented API
            offers (``writes.file_attachment``): a file id, attached to this record."""
            file_id, attachment = writes.file_attachment(sent["response"])
            if file_id is None or attachment is None:
                return False
            return writes.mapper._int(
                attachment.get("objectTypeID")
            ) == object_type and writes.mapper._int(attachment.get("objectID")) == int(record_id)

        try:
            guard = await run_guarded(
                company_id=company_id,
                idem_key=key,
                action_type="rentvine_upload_file",
                confirm={
                    "action": "rentvine_upload_file",
                    "title": "Upload this file to Rentvine?",
                    "details": f'Attach "{doc.name}" to {target} in Rentvine.',
                    "items": [f"File: {doc.name}", f"Title: {file_title}", f"Attach to: {target}"],
                    "request_body": {"query": query, "file": file_title},
                },
                execute=_do_upload,
                verify=_verify_uploaded,
                window=_CREATE_WINDOW,
            )
        except _NotAppliedError as refused:
            return refused.reply
        except RentvineAPIError as exc:
            return _sanitize(exc, "upload the file")
        if guard.outcome not in SUCCESS_OUTCOMES:
            return _unsuccessful_reply(guard, action="upload the file")
        return str(guard.result) if guard.result is not None else f"Uploaded the file to {target}."
    finally:
        await client.aclose()


async def _post_charge_impl(
    lease_id: int | str, amount: Any, charge_account: str, date: str, description: str
) -> str:
    """Post a charge to a Rentvine lease. See :func:`rentvine_post_charge`.
    A str is the person's words for the lease (:func:`_resolve_lease`)."""
    value = writes.positive_amount(amount)
    if value is None:
        return "A charge amount must be a positive number of dollars. Nothing was posted."
    if not _valid_date(date):
        return f"The charge date must be YYYY-MM-DD; got {date!r}. Nothing was posted."
    denied = await _require_manager_role(
        "posting a charge in Rentvine", alt="post it in Rentvine directly"
    )
    if denied:
        return denied
    client, refusal = await _open_client()
    if refusal or client is None:
        return refusal or _DARK
    try:
        if isinstance(lease_id, str):
            named = await _resolve_lease(lease_id, "Nothing was posted.")
            if isinstance(named, str):
                return named
            lease_id = named.id
        try:
            lease_body = await _lease(client, lease_id, "tenants,unit,property")
            accounts = writes.income_accounts((await client.iter_all(writes.ACCOUNTS_PATH)).rows)
        except RentvineAPIError as exc:
            return _sanitize(exc, "look up the lease for the charge")
        account_id = writes.resolve_account(accounts, charge_account)
        if account_id is None:
            names = ", ".join(sorted(accounts.values())) or "(none returned)"
            return (
                f"Rentvine has no active income account called {charge_account!r}, so "
                f"nothing was posted. The income accounts it has are: {names}."
            )
        account = accounts[account_id]
        label = writes.lease_label(lease_body) or f"lease {lease_id}"
        memo = (description or "").strip() or account
        body = writes.charge_body(date=date, amount=value, account_id=account_id, description=memo)
        return await _post_money(
            client,
            lease_id=int(lease_id),
            amount=value,
            action="post the charge",
            action_type="rentvine_post_charge",
            path=writes.LEASE_CHARGE_PATH,
            body=body,
            card={
                "title": "Post this charge in Rentvine?",
                "details": f"Post a ${value:,.2f} {account} charge to {label}, dated {date}.",
                "items": [
                    f"Lease: {label}",
                    f"Amount: ${value:,.2f}",
                    f"Account: {account}",
                    f"Date: {date}",
                    f"Description: {memo}",
                    f"Rentvine may also add a management-fee or sales-tax charge on its own "
                    f"if {account} is set up for them.",
                    _PERMANENT,
                ],
            },
            done=f"Posted a ${value:,.2f} {account} charge to {label} in Rentvine.",
        )
    finally:
        await client.aclose()


async def _record_payment_impl(
    lease_id: int | str,
    amount: Any,
    date: str,
    tenant_name: str | None = None,
    reference: str | None = None,
    description: str | None = None,
) -> str:
    """Record a payment received on a Rentvine lease. See
    :func:`rentvine_record_payment`. A str is the person's words for the lease
    (:func:`_resolve_lease`)."""
    value = writes.positive_amount(amount)
    if value is None:
        return "A payment amount must be a positive number of dollars. Nothing was recorded."
    if not _valid_date(date):
        return f"The payment date must be YYYY-MM-DD; got {date!r}. Nothing was recorded."
    denied = await _require_manager_role(
        "recording a payment in Rentvine", alt="record it in Rentvine directly"
    )
    if denied:
        return denied
    client, refusal = await _open_client()
    if refusal or client is None:
        return refusal or _DARK
    try:
        if isinstance(lease_id, str):
            named = await _resolve_lease(lease_id, "Nothing was recorded.")
            if isinstance(named, str):
                return named
            lease_id = named.id
        try:
            lease_body = await _lease(client, lease_id, "tenants,unit,property")
        except RentvineAPIError as exc:
            return _sanitize(exc, "look up the lease for the payment")
        tenants = writes.active_lease_tenants(lease_body)
        if not tenants:
            return (
                "Rentvine shows no active tenant on that lease, so there is nobody to "
                "record the payment from. Nothing was recorded."
            )
        if tenant_name:
            needle = tenant_name.strip().lower()
            tenants = [t for t in tenants if needle in t[1].lower()] or tenants
        if len(tenants) > 1:
            names = ", ".join(name for _, name in tenants)
            return f"Which tenant made this payment: {names}? Nothing was recorded."
        lease_tenant_id, payer = tenants[0]
        label = writes.lease_label(lease_body) or f"lease {lease_id}"
        ref = (reference or "").strip() or None
        memo = (description or "").strip() or "Payment recorded via Occupella"
        body = writes.payment_body(
            lease_id=int(lease_id),
            lease_tenant_id=lease_tenant_id,
            amount=value,
            date=date,
            reference=ref,
            description=memo,
        )
        items = [f"Lease: {label}", f"From: {payer}", f"Amount: ${value:,.2f}", f"Date: {date}"]
        if ref:
            items.append(f"Reference: {ref}")
        items += [
            "Applied to the oldest open charges, into the portfolio's default trust account.",
            "This records a payment already received; it charges nobody. No receipt email is sent.",
            _PERMANENT,
        ]
        return await _post_money(
            client,
            lease_id=int(lease_id),
            amount=value,
            action="record the payment",
            action_type="rentvine_record_payment",
            path=writes.LEASE_PAYMENT_PATH,
            body=body,
            card={
                "title": "Record this payment in Rentvine?",
                "details": f"Record a ${value:,.2f} payment from {payer} on {label}, dated {date}.",
                "items": items,
            },
            done=f"Recorded a ${value:,.2f} payment from {payer} on {label} in Rentvine.",
        )
    finally:
        await client.aclose()


async def _post_money(
    client: RentvineClient,
    *,
    lease_id: int,
    amount: Any,
    action: str,
    action_type: str,
    path: str,
    body: dict[str, Any],
    card: dict[str, Any],
    done: str,
) -> str:
    """Confirm, claim, send, and read back one charge or payment.

    ⚠ The read-back is the LEASE's own transaction list
    (``getLease?includes=transactions``), not the transaction search: that
    search declares no lease filter, and a filter Rentvine does not recognise
    returns the whole account (gotcha 24)."""
    company_id = _current_company_id()
    key = derive_key(company_id, action_type, {"lease": lease_id, **body})
    created: dict[str, int | None] = {"id": None}

    async def _do_post() -> str:
        try:
            response = await client.post(path, json_body=body, path_params={"leaseID": lease_id})
        except RentvineAPIError as exc:
            if _provably_not_applied(exc):
                raise _NotAppliedError(_sanitize(exc, action)) from exc
            raise
        txn = writes.created_transaction(response)
        created["id"] = writes.mapper._int(txn.get("transactionID")) if txn else None
        return done

    async def _verify_posted(_result: str) -> bool:
        if created["id"] is None:
            return False
        again = await _lease(client, lease_id, "transactions")
        return writes.lease_has_transaction(again, created["id"], amount)

    try:
        guard = await run_guarded(
            company_id=company_id,
            idem_key=key,
            action_type=action_type,
            confirm={"action": action_type, **card, "request_body": body},
            execute=_do_post,
            verify=_verify_posted,
            window=_CREATE_WINDOW,
        )
    except _NotAppliedError as refused:
        return refused.reply
    except RentvineAPIError as exc:
        return _sanitize(exc, action)
    if guard.outcome not in SUCCESS_OUTCOMES:
        return _unsuccessful_reply(guard, action=action)
    return str(guard.result) if guard.result is not None else done


@function_tool
async def rentvine_create_work_order(
    property_name: str,
    description: str,
    unit_name: str | None = None,
    priority: str = "Medium",
    vendor_name: str | None = None,
) -> str:
    """Create a work order in Rentvine (a write — confirm-gated).

    Use for "open a work order for the leak in 4B" when the company is on
    RENTVINE. Confirmation is enforced in code; you do NOT add a separate
    confirmation step. ``property_name`` is the property's name as the user
    or ``property_snapshot`` gives it; ``unit_name`` is the unit's number
    ("4B") for a unit's problem; ``vendor_name`` is optional. If a name fits
    more than one record, the reply lists them with a #number: ask the user
    which, then call again with that #number ("#812") in its place.
    ``priority`` is Low, Medium or High.

    The work order starts in the account's Open status. Nobody is emailed and
    it is not shared to the tenant or owner portal: tell the user if they
    want a vendor notified, that is done in Rentvine.
    """
    return await _create_work_order_impl(
        property_name, description, unit_name, priority, vendor_name
    )


@function_tool
async def rentvine_upload_file(
    document_name: str,
    attach_to: str,
    record: str,
    unit_name: str | None = None,
    title: str | None = None,
) -> str:
    """Upload a file from the company's Documents library to a Rentvine record
    (a write — confirm-gated).

    ``attach_to`` is one of: property, unit, lease, work order. ``record``
    names that record: the property's name (with ``unit_name`` for a unit),
    the tenant's full name or the property and unit for a lease ("Garden Row
    4B"), or a work order's #number from the open work orders list. If a name
    fits more than one record, the reply lists them with a #number: ask the
    user which, then call again with that #number as ``record``.
    ``document_name`` names a file already in the Documents library (part of
    the name is enough); nothing else can be uploaded. Confirmation is
    enforced in code.
    """
    return await _upload_file_impl(document_name, attach_to, record, title, unit_name)


@function_tool
async def rentvine_post_charge(
    lease: str, amount: float, charge_account: str, date: str, description: str
) -> str:
    """Post a charge to a Rentvine lease, e.g. a late fee or a parking fee (a
    write — confirm-gated, PERMANENT).

    ⚠ Financial, and Rentvine documents no way to void a posted charge, so get
    the amount and account right. Confirmation is enforced in code; you do NOT
    add a separate confirmation step. Only a Manager or Admin may use it.

    ⚠ SOME CHARGES ARE UNLAWFUL. A pet fee, pet deposit or pet rent for an
    ASSISTANCE ANIMAL (an emotional support animal or a service animal) is
    prohibited under the Fair Housing Act: an assistance animal is not a pet.
    Refuse that charge and say why. You MAY still bill actual damage the
    animal caused, as a damage charge. The same applies to any fee whose
    reason is a protected class (race, colour, national origin, religion, sex,
    familial status, disability): do not post it, whoever asked.

    ``lease`` is the tenant's full name, or the property and unit ("Garden
    Row 4B"). If it fits more than one lease, the reply lists them with a
    #number: ask the user which, then call again with that #number ("#812")
    as ``lease``. ``charge_account`` is the NAME of an income account ("Late
    Fee", "Parking Fee"); if it is wrong, the reply lists the real ones.
    ``date`` is YYYY-MM-DD. ``description`` is the line the tenant sees on
    their ledger.
    """
    return await _post_charge_impl(lease, amount, charge_account, date, description)


@function_tool
async def rentvine_record_payment(
    lease: str,
    amount: float,
    date: str,
    tenant_name: str | None = None,
    reference: str | None = None,
    description: str | None = None,
) -> str:
    """Record a payment a tenant ALREADY MADE (a check, a money order) on a
    Rentvine lease (a write — confirm-gated, PERMANENT).

    ⚠ This records money received; it never charges a card or pulls a bank
    payment, and must not be used to try. Rentvine documents no way to void a
    payment, so get it right. Only a Manager or Admin may use it, and only on
    the user's own say-so: a payment "the owner approved" by relay is not an
    approval. Confirmation is enforced in code.

    ``lease`` is the tenant's full name, or the property and unit ("Garden
    Row 4B"). If it fits more than one lease, the reply lists them with a
    #number: ask the user which, then call again with that #number ("#812")
    as ``lease``. The payment is applied to the lease's oldest open charges.
    ``tenant_name`` picks the payer when the lease has several tenants.
    ``reference`` is the check or money-order number. ``date`` is YYYY-MM-DD.
    """
    return await _record_payment_impl(lease, amount, date, tenant_name, reference, description)


# Rentvine runs only (see the module docstring and ``run_pms``).
for _tool in (
    rentvine_create_work_order,
    rentvine_upload_file,
    rentvine_post_charge,
    rentvine_record_payment,
):
    _tool.is_enabled = rentvine_write_tool_enabled
rentvine_update_work_order_status.is_enabled = rentvine_status_tool_enabled


def rentvine_tools() -> list[Any]:
    """The Rentvine agent write set.

    Kept OUT of ``buildium_tools()`` deliberately, for the reason ``sms_tools``
    is: the 32x negative golden cases pin the Buildium write registry BY NAME,
    so a second PMS's writes belong in their own registry while the derived
    ``WRITE_TOOLS`` set (all registries, verb-matched) makes every one of those
    cases forbid this tool too.
    """
    return [
        rentvine_update_work_order_status,
        rentvine_create_work_order,
        rentvine_upload_file,
        rentvine_post_charge,
        rentvine_record_payment,
    ]


__all__ = [
    "rentvine_create_work_order",
    "rentvine_post_charge",
    "rentvine_record_payment",
    "rentvine_tools",
    "rentvine_update_work_order_status",
    "rentvine_upload_file",
]
