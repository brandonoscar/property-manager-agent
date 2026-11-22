"""The Propertyware agent tools: create a work order, edit one (design W4).

Shown only on a Propertyware run with Propertyware's writes switched on:
``agents/tool_registries.py`` sets that on copies, and nothing here sets
``is_enabled``. Every write goes through ``pms.gated_write.gated_write``, which
runs the whole gate in one order: the switch, the company, the role floor
(Manager), the plan this module builds (``prepare``), the money refusal, the
confirm card, the idempotency claim, the re-read just before sending
(``recheck``), the one send, and the read-back (``verify``). This module
never sends anything itself and never calls ``run_guarded``.

What each write sends, its card and its read-back are in
``propertyware/writes.py``, where every assumption about Propertyware is
marked for the work list.

⚠ **NEITHER WRITE CAN SET A STATUS OR A VENDOR**: Propertyware's API has no
field for either (W4). A request for one gets
:func:`propertyware_change_work_order_status_or_vendor`, which sends nothing and
shows no card. It is not a write.

⚠ **A 403 ON A WRITE IS WORDED BY THE OPERATION'S BETA MARKER** (design,
"built correctly"). ``gated_write`` words every 403 the same way, for a
vendor whose users hold permissions; on Propertyware the API key does. The
shared sentence is recognised by equality with ``gated_write.refusal``'s own
output, never by matching its words, then replaced and remembered in the
credentials row (``credentials.remember_refused``) so the next attempt within
a day explains instead of showing a card.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from agents import function_tool

from helixis.memory.context import current_memory_context
from helixis.pms import registry
from helixis.pms.errors import Failure, PmsApiError
from helixis.pms.gated_write import Send, WritePlan, gated_write, refusal
from helixis.pms.http import PmsHttpClient, PmsResponse, operation_key
from helixis.propertyware import _paths, client, credentials, mapper, writes

logger = logging.getLogger(__name__)

PMS = client.PMS
NAME = "Propertyware"

_WRITES_OFF = (
    "Changing records in Propertyware from Occupella isn't switched on, so I didn't change "
    "anything. Make the change in Propertyware directly."
)
_NOT_CONNECTED = (
    "Propertyware isn't connected for this workspace, so there's nothing for me to change. "
    "Connect Propertyware on the Connectors page (Settings → Connectors)."
)
_UNUSABLE = (
    "Occupella can't use the Propertyware keys saved for this workspace, so I didn't change "
    "anything. Save them again on the Connectors page (Settings → Connectors)."
)
_LOOKUP_FAILED = (
    "Occupella couldn't look that up in its synced Propertyware data just now, so I didn't "
    "send anything. Ask me again in a moment."
)

#: What ``gated_write`` raises its 403 wording from, to recognise that wording
#: by equality (``refusal`` depends only on the failure, the action and the name).
_FORBIDDEN = PmsApiError(403, Failure.PERMISSION, False, "")

#: The company's Propertyware buildings and one building's units, from the
#: mirror. Rule 11: scoped to the company, and to this system's rows.
_BUILDINGS_SQL = """
    SELECT buildium_id, name FROM helixis_properties
     WHERE company_id = $1::uuid AND pms = $2
"""
_UNITS_SQL = """
    SELECT buildium_id, unit_number FROM helixis_units
     WHERE company_id = $1::uuid AND pms = $2 AND property_id = $3
"""

#: The most names listed when several match.
_NAMES_LISTED = 10


def _company_id() -> str:
    ctx = current_memory_context()
    return ctx.company_id if ctx else ""


def _pool() -> Any:
    from helixis.db.engine import get_pool

    return get_pool()


async def _open_client() -> tuple[PmsHttpClient | None, str | None]:
    """This company's write client, or the reply that says why there is none.

    The switch first (gotcha 67: nobody should approve a write that cannot
    happen), then this company's own credentials, never a deployment
    setting (gotcha 70). ``gated_write`` checks both again.
    """
    if not registry.writes_enabled(PMS):
        return None, _WRITES_OFF
    company_id = _company_id()
    if not company_id:
        return None, _NOT_CONNECTED
    try:
        creds = await credentials.load_credentials(_pool(), company_id)
    except credentials.PropertywareCredentialsError:
        return None, _UNUSABLE
    if creds is None:
        return None, _NOT_CONNECTED
    return client.build_write_client(creds), None


async def _record(pw: PmsHttpClient, template: str, **path_params: int) -> dict[str, Any] | None:
    """One record by id, or ``None`` when the answer is not a record."""
    body = (await pw.get(template, path_params=path_params)).body
    return body if isinstance(body, dict) else None


def _read_refused(exc: PmsApiError, template: str, what: str) -> str | None:
    """Propertyware's meaning for a read the plan needed, or ``None`` to leave
    the failure to ``gated_write``'s wording."""
    unchanged = "Nothing was changed in Propertyware."
    if exc.failure is Failure.NOT_FOUND:
        return f"Propertyware has no {what}. {unchanged}"
    if exc.failure is Failure.PERMISSION:
        needed = _paths.PERMISSIONS.get(operation_key("GET", template)) or "permission"
        return (
            f"The Propertyware API key saved in Occupella lacks {needed}, which reading the "
            f"{what.split(' ', 1)[0]} needs. Create a key in Propertyware that has it (Setup, "
            "Administration Setup, API Keys) and save it on the Connectors page (Settings → "
            f"Connectors). {unchanged}"
        )
    return None


async def _refused_before(company_id: str, method: str, template: str) -> str | None:
    """The explanation when Propertyware refused this write within the day,
    else ``None``. Best effort: an unreadable memory means "try"."""
    try:
        when = await credentials.refused_at(_pool(), company_id, operation_key(method, template))
    except Exception:  # noqa: BLE001 — the memory only saves a card that would fail
        logger.warning("propertyware: reading the remembered refusals failed", exc_info=True)
        return None
    if credentials.still_refused(when):
        return writes.forbidden_reply(method, template, remembered=True)
    return None


async def _explain_forbidden(
    reply: str, *, method: str, template: str, action: str, company_id: str
) -> str:
    """``gated_write``'s 403 sentence, replaced by Propertyware's meaning and
    remembered; any other reply as it is."""
    if reply != refusal(_FORBIDDEN, action=action, name=NAME):
        return reply
    try:
        await credentials.remember_refused(_pool(), company_id, operation_key(method, template))
    except Exception:  # noqa: BLE001 — the explanation stands without the memory
        logger.warning("propertyware: remembering a refused write failed", exc_info=True)
    return writes.forbidden_reply(method, template)


def _matches(rows: list[tuple[int, str | None]], wanted: str) -> list[tuple[int, str]]:
    """Rows whose name is ``wanted``, else whose id is, else whose name holds it."""
    text = wanted.strip()
    folded = text.casefold()
    named = [(i, n) for i, n in rows if n and n.strip().casefold() == folded]
    if named:
        return named
    if text.isdigit():
        by_id = [(i, n or str(i)) for i, n in rows if i == int(text)]
        if by_id:
            return by_id
    return [(i, n) for i, n in rows if n and folded and folded in n.casefold()]


def _pick(found: list[tuple[int, str]], wanted: str, what: str) -> int | str:
    """The one id ``found`` names, or the question to ask instead."""
    if len(found) == 1:
        return found[0][0]
    if not found:
        if wanted.strip().isdigit():
            # An id the last sync may not have seen: the GET that follows checks it.
            return int(wanted.strip())
        return (
            f'No {what} in Occupella\'s synced Propertyware data matches "{wanted}". '
            f"Which {what} is it? Nothing was sent."
        )
    # With the id: two with the same name can then still be told apart.
    names = ", ".join(f"{n} (id {i})" for i, n in sorted(found, key=lambda f: f[1])[:_NAMES_LISTED])
    return f'Several {what}s match "{wanted}": {names}. Which one? Nothing was sent.'


async def _building_id(company_id: str, wanted: str) -> int | str:
    rows = await _pool().fetch(_BUILDINGS_SQL, company_id, PMS)
    found = _matches([(int(r["buildium_id"]), r["name"]) for r in rows], wanted)
    return _pick(found, wanted, "building")


async def _unit_ids(
    pw: PmsHttpClient, company_id: str, building_id: int, wanted: list[str]
) -> tuple[list[int], dict[int, str]] | str:
    """The ids of the named units of ``building_id``, and names for the card
    (each picked unit's own, read from Propertyware; the building's other
    units' as synced), or the reply instead. Each picked unit is read: it
    must belong to that building."""
    rows = await _pool().fetch(_UNITS_SQL, company_id, PMS, building_id)
    candidates = [(int(r["buildium_id"]), r["unit_number"]) for r in rows]
    ids: list[int] = []
    names: dict[int, str] = {i: n for i, n in candidates if n}
    for one in wanted:
        picked = _pick(_matches(candidates, one), one, "unit")
        if isinstance(picked, str):
            return picked
        if picked in ids:
            continue
        try:
            unit = await _record(pw, writes.UNIT_PATH, unitID=picked)
        except PmsApiError as exc:
            if exc.failure is Failure.NOT_FOUND:
                unit = None
            else:
                said = _read_refused(exc, writes.UNIT_PATH, f"unit with id {picked}")
                if said is None:
                    raise
                return said
        if unit is None or _building_of(unit) != building_id:
            return (
                f'There is no unit "{one}" in that building in Propertyware, so I didn\'t send '
                "anything. Which unit is it?"
            )
        ids.append(picked)
        names[picked] = writes.name_of(unit, fallback=f"unit {picked}")
    return ids, names


def _building_of(record: Mapping[str, Any]) -> int | None:
    """The building a unit or work order record names."""
    return mapper._int(record.get("buildingID"))


# ── create ───────────────────────────────────────────────────────────────


async def _create_work_order_impl(
    property: str,  # noqa: A002 — the model's word for it
    description: str,
    unit: str | None = None,
    priority: str = "Medium",
    work_order_type: str | None = None,
    category: str | None = None,
) -> str:
    """Create a Propertyware work order. See :func:`propertyware_create_work_order`."""
    text = writes.text_value(description)
    if text is None:
        return (
            "A work order needs a description of the problem. Nothing was created in Propertyware."
        )
    level = writes.priority_value(priority or "Medium")
    if level is None:
        return "A Propertyware work order's priority is High, Medium or Low. Nothing was created."
    kind, group = writes.text_value(work_order_type), writes.text_value(category)
    if any(len(v) > writes.MAX_TEXT for v in (text, kind or "", group or "")):
        return (
            f"That is longer than the {writes.MAX_TEXT} characters Occupella sends in one field; "
            "shorten it and ask again. Nothing was created in Propertyware."
        )
    wanted_building = (property or "").strip()
    if not wanted_building:
        return "Which building is the work order for? Nothing was created in Propertyware."
    pw, refused = await _open_client()
    if pw is None:
        return refused or _WRITES_OFF
    company_id = pw.company_id or ""

    async def prepare() -> WritePlan | str:
        before = await _refused_before(company_id, "POST", writes.CREATE_PATH)
        if before is not None:
            return before
        try:
            building_id = await _building_id(company_id, wanted_building)
        except Exception:  # noqa: BLE001 — a lookup failure sends nothing
            logger.warning("propertyware create: building lookup failed", exc_info=True)
            return _LOOKUP_FAILED
        if isinstance(building_id, str):
            return building_id
        try:
            building = await _record(pw, writes.BUILDING_PATH, buildingID=building_id)
        except PmsApiError as exc:
            said = _read_refused(exc, writes.BUILDING_PATH, f"building with id {building_id}")
            if said is None:
                raise
            return said
        if building is None:
            return f"Propertyware has no building with id {building_id}. Nothing was created."
        place = writes.name_of(building, fallback=f"building {building_id}")
        unit_id: int | None = None
        if unit and unit.strip():
            try:
                found = await _unit_ids(pw, company_id, building_id, [unit])
            except PmsApiError:
                raise
            except Exception:  # noqa: BLE001 — a lookup failure sends nothing
                logger.warning("propertyware create: unit lookup failed", exc_info=True)
                return _LOOKUP_FAILED
            if isinstance(found, str):
                return found
            (unit_id,), names = found
            place = f"{place}, unit {names[unit_id]}"
        body = writes.create_body(
            building_id=building_id,
            description=text,
            priority=level,
            unit_id=unit_id,
            work_order_type=kind,
            category=group,
        )
        return WritePlan(
            action_type="propertyware_create_work_order",
            action="create the work order",
            title="Create this work order in Propertyware?",
            details=f"Create a {level.lower()}-priority work order at {place} in Propertyware.",
            items=writes.create_items(
                place=place,
                description=text,
                priority=level,
                work_order_type=kind,
                category=group,
            ),
            send=Send("POST", writes.CREATE_PATH, body),
            key={
                "building": building_id,
                "unit": unit_id,
                "text": text,
                "priority": level,
                "type": kind,
                "category": group,
            },
            done=lambda response: _created(response, place),
        )

    async def verify(plan: WritePlan, response: PmsResponse) -> bool:
        new_id = writes.created_id(response.body)
        if new_id is None:
            return False
        record = await _record(pw, writes.READ_PATH, workOrderId=new_id)
        return record is not None and writes.create_took(plan.send.body, record)

    async with pw:
        reply = await gated_write(client=pw, prepare=prepare, verify=verify)
    return await _explain_forbidden(
        reply,
        method="POST",
        template=writes.CREATE_PATH,
        action="create the work order",
        company_id=company_id,
    )


def _created(response: PmsResponse, place: str) -> str:
    record = response.body if isinstance(response.body, dict) else {}
    return (
        f"Created work order {writes.display_number(record)} at {place} in Propertyware. "
        f"{writes.STATUS_AND_VENDOR_LINE}"
    )


# ── edit ─────────────────────────────────────────────────────────────────


async def _update_work_order_impl(
    work_order_id: int,
    description: str | None = None,
    priority: str | None = None,
    category: str | None = None,
    work_order_type: str | None = None,
    start_date: str | None = None,
    scheduled_end_date: str | None = None,
    units: list[str] | None = None,
) -> str:
    """Edit a Propertyware work order. See :func:`propertyware_update_work_order`."""
    wanted: dict[str, Any] = {}
    for api, value in (
        ("description", description),
        ("category", category),
        ("type", work_order_type),
    ):
        if value is None:
            continue
        text = writes.text_value(value)
        if text is None:
            return (
                f"Occupella doesn't empty a work order's {api} in Propertyware. Nothing was sent."
            )
        if len(text) > writes.MAX_TEXT:
            return (
                f"That {api} is longer than the {writes.MAX_TEXT} characters Occupella sends in "
                "one field; shorten it and ask again. Nothing was sent."
            )
        wanted[api] = text
    if priority is not None:
        level = writes.priority_value(priority)
        if level is None:
            return "A Propertyware work order's priority is High, Medium or Low. Nothing was sent."
        wanted["priority"] = level
    for api, value in (("startDate", start_date), ("scheduledEndDate", scheduled_end_date)):
        if value is None:
            continue
        day = writes.date_value(value)
        if day is None:
            return f"Dates are YYYY-MM-DD, and {value!r} isn't one. Nothing was sent."
        wanted[api] = day
    unit_names = [u.strip() for u in (units or []) if u and u.strip()]
    if units is not None and not unit_names:
        return "Occupella doesn't remove every unit from a work order. Nothing was sent."
    if len(unit_names) > writes.MAX_UNITS:
        return f"One change attaches at most {writes.MAX_UNITS} units. Nothing was sent."
    if not wanted and not unit_names:
        return (
            "Tell me what to change on the work order: its description, priority, category, "
            "type, start date, scheduled end date or units. Propertyware's API can't change its "
            "status or its vendor."
        )
    pw, refused = await _open_client()
    if pw is None:
        return refused or _WRITES_OFF
    company_id = pw.company_id or ""
    seen: dict[str, dict[str, Any]] = {}

    async def read_it() -> dict[str, Any] | None:
        return await _record(pw, writes.READ_PATH, workOrderId=int(work_order_id))

    async def prepare() -> WritePlan | str:
        before = await _refused_before(company_id, "PATCH", writes.UPDATE_PATH)
        if before is not None:
            return before
        try:
            current = await read_it()
        except PmsApiError as exc:
            said = _read_refused(exc, writes.READ_PATH, f"work order with id {work_order_id}")
            if said is None:
                raise
            return said
        if current is None:
            return f"Propertyware has no work order with id {work_order_id}. Nothing was sent."
        number = writes.display_number(current)
        changes = dict(wanted)
        names: dict[int, str] = {}
        if unit_names:
            building_id = _building_of(current)
            if building_id is None:
                return (
                    f"Work order {number} names no building in Propertyware, so I can't check "
                    "its units. Nothing was sent."
                )
            try:
                found = await _unit_ids(pw, company_id, building_id, unit_names)
            except PmsApiError:
                raise
            except Exception:  # noqa: BLE001 — a lookup failure sends nothing
                logger.warning("propertyware edit: unit lookup failed", exc_info=True)
                return _LOOKUP_FAILED
            if isinstance(found, str):
                return found
            changes["unitIDs"], names = found
        body = writes.patch_body(current, changes)
        if not body:
            return (
                f"Work order {number} already reads that way in Propertyware, so there is "
                "nothing to change."
            )
        seen["approved"] = seen["before"] = current
        where = str(current.get("location") or "").strip() or (f"building {_building_of(current)}")
        labels = writes.changed_labels(body)
        return WritePlan(
            action_type="propertyware_update_work_order",
            action="change the work order",
            title="Change this work order in Propertyware?",
            details=f"Change the {', '.join(labels)} of work order {number} at {where} in "
            "Propertyware.",
            items=writes.update_items(current, body, names),
            send=Send("PATCH", writes.UPDATE_PATH, body, {"workOrderID": int(work_order_id)}),
            key={"id": int(work_order_id), **body},
            done=lambda _response: (
                f"Changed the {', '.join(labels)} of work order {number} in Propertyware. "
                "Its status and vendor are unchanged."
            ),
        )

    async def recheck(plan: WritePlan) -> Send | None:
        now = await read_it()
        if now is None or writes.moved_on_card(seen["approved"], now, plan.send.body):
            return None
        seen["before"] = now
        return plan.send

    async def verify(plan: WritePlan, response: PmsResponse) -> bool:
        after = await read_it()
        if after is None:
            return False
        wrong = writes.patch_took(seen["before"], plan.send.body, after)
        if wrong:
            logger.warning(
                "propertyware edit of work order %s: the read-back differs in %s",
                work_order_id,
                wrong,
            )
        return not wrong

    async with pw:
        reply = await gated_write(client=pw, prepare=prepare, verify=verify, recheck=recheck)
    return await _explain_forbidden(
        reply,
        method="PATCH",
        template=writes.UPDATE_PATH,
        action="change the work order",
        company_id=company_id,
    )


# ── status and vendor: not a write ───────────────────────────────────────


async def _work_order_status_or_vendor_impl() -> str:
    """What a request to change a status or a vendor is told. Sends nothing."""
    return writes.CANT_STATUS_OR_VENDOR


# ── the tools ────────────────────────────────────────────────────────────


@function_tool
async def propertyware_create_work_order(
    property: str,  # noqa: A002 — the model's word for it
    description: str,
    unit: str | None = None,
    priority: str = "Medium",
    work_order_type: str | None = None,
    category: str | None = None,
) -> str:
    """Create a work order in Propertyware (a write — confirm-gated).

    Use for "open a work order for the leak in 2B at Maple Court" when the
    company is on PROPERTYWARE. Confirmation is enforced in code; you do NOT
    add a separate confirmation step. ``property`` is the building's name as
    Occupella's synced data shows it (part of the name is enough) or its
    Propertyware id; ``unit`` is the unit's name or number in that building,
    for a unit's problem. ``priority`` is High, Medium or Low.
    ``work_order_type`` and ``category`` are optional, in the account's own
    words.

    Propertyware's API cannot assign a vendor or set a status: the work order
    starts with no vendor in the account's first status, and it is not
    published to the tenant or owner portal. Tell the user to assign a vendor
    or change the status in Propertyware.
    """
    return await _create_work_order_impl(
        property, description, unit, priority, work_order_type, category
    )


@function_tool
async def propertyware_update_work_order(
    work_order_id: int,
    description: str | None = None,
    priority: str | None = None,
    category: str | None = None,
    work_order_type: str | None = None,
    start_date: str | None = None,
    scheduled_end_date: str | None = None,
    units: list[str] | None = None,
) -> str:
    """NOT for a status or a vendor: "mark it complete", "it's done", "put it on
    hold", "reopen it" or "assign a vendor" go to
    ``propertyware_change_work_order_status_or_vendor`` ALONE, never to this
    tool, and no edit stands in for one (no note that it is done, no end date).

    Changes a Propertyware work order's description, priority, category,
    type, start date, scheduled end date or units (a write — confirm-gated).
    Use when the company is on PROPERTYWARE. ``work_order_id`` is the #number
    ``open_work_orders`` prints. Pass ONLY what the user asked to change.
    ``priority`` is High, Medium or Low; dates are YYYY-MM-DD; ``units`` are
    unit names or numbers in the work order's building. Confirmation is
    enforced in code; you do NOT add a separate confirmation step.
    """
    return await _update_work_order_impl(
        work_order_id,
        description,
        priority,
        category,
        work_order_type,
        start_date,
        scheduled_end_date,
        units,
    )


@function_tool
async def propertyware_change_work_order_status_or_vendor() -> str:
    """Change a Propertyware work order's STATUS (mark it complete or done,
    in progress or on hold, reopen or cancel it) or its VENDOR (assign,
    change or remove one): the one tool for those requests.

    Propertyware's API can do neither, so this sends nothing and shows no
    card: it returns what to tell the user. Call it ALONE: no edit or create
    alongside it, and never another system's tool.
    """
    return await _work_order_status_or_vendor_impl()


#: The tools here that show no card: they send nothing and answer in words, so
#: ``tool_registries.connector_tools`` lets one run while a write's card waits.
#: Every other tool here is a write and waits its turn (one card at a time).
NO_CARD: frozenset[str] = frozenset({"propertyware_change_work_order_status_or_vendor"})


def propertyware_tools() -> list[Any]:
    """The Propertyware agent tools. ``tool_registries.connector_tools`` shows
    them only on a Propertyware run with Propertyware's writes switched on."""
    return [
        propertyware_create_work_order,
        propertyware_update_work_order,
        propertyware_change_work_order_status_or_vendor,
    ]


__all__ = [
    "NO_CARD",
    "propertyware_create_work_order",
    "propertyware_tools",
    "propertyware_update_work_order",
    "propertyware_change_work_order_status_or_vendor",
]
