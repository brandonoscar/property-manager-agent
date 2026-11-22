"""What Rent Manager's two work-order writes send, and what reads them back.

Design R4 lists three first-build writes. Two are built here: create a work
order, and change a work order's status. The third, a note, is NOT built:
the help scrape names no HistoryNotes path or fields for a note on a tenant,
unit or property (RM-15), and "documented endpoints only" is a hard rule
(terms rule 1).

The agent tools that run these through ``pms.gated_write`` are in
``agents/rentmanager_tools.py``. The bodies, the cards, the plans, the
re-reads and the comparisons live HERE, because ``scripts/
rentmanager_unverified.py`` scans this package, and an assumption recorded
anywhere else is one nobody settles. Pure where it can be; the async helpers
below only READ (``gated_write`` is the only sender).

NEVER, IN CODE
* A DELETE: the client has no such operation, and its only write verb is POST.
* Money: no path here names it, and ``gated_write`` refuses one that does.
* ``saveOptions``: a forbidden key, refused before anything leaves. A 412 is
  reported with Rent Manager's reason; no override is ever sent (terms rule 12).
* A collection POST of more than one object: :func:`create_body` builds a
  list holding exactly one, and :func:`create_plan` refuses anything else.
* A guess shown as a fact: every name on a card is the one Rent Manager
  returned (a property's, a unit's, a status's), never the model's words for
  it, and a value Rent Manager did not send is said to be unknown.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from helixis.pms.errors import Failure, PmsApiError
from helixis.pms.gated_write import Send, WritePlan
from helixis.pms.http import PmsHttpClient, PmsResponse, operation_key
from helixis.pms.paging import read_all
from helixis.rentmanager import client as rm_client
from helixis.rentmanager.resources import RESOURCES

NAME = rm_client.NAME
KEY = RESOURCES["work_orders"].key
CREATE_ACTION = f"{rm_client.PMS}_create_work_order"
STATUS_ACTION = f"{rm_client.PMS}_update_work_order_status"
#: Each write's words in "I couldn't ...", and the operation a refusal is
#: remembered under (``pms.refusals``; the tools read both).
CREATE_DOING = "create the work order"
STATUS_DOING = "change the work order's status"
CREATE_OPERATION = operation_key("POST", rm_client.ISSUES_PATH)
STATUS_OPERATION = operation_key("POST", rm_client.ISSUE_PATH)

# @unverified RM-27 a work order read by id carries its links only when they
# are embedded, the way the collection read asks for them.
ISSUE_EMBEDS = "LinkedProperties,LinkedUnits"

# @unverified RM-9 the concurrency value an update must echo. The Overview
# says an update must include UpdateDate (p.4, p.8); the help scrape marks
# ConcurrencyID as the concurrency key on many models, none of them
# ServiceManagerIssues. So an update echoes whichever of the two the record
# it read carries, and refuses when it carries neither. Settled by a status
# change on a test work order and a change back (an unchanged status is
# refused before a card), each followed by a re-read.
CONCURRENCY_FIELDS: tuple[str, ...] = ("UpdateDate", "ConcurrencyID")

# @unverified RM-14 the category lookup's key and name fields, by analogy with
# the status and priority lookups (ServiceManagerStatusID, Name). Settled by
# reading each lookup whole.
CATEGORY_KEY = "ServiceManagerCategoryID"


@dataclass(frozen=True)
class LookupSpec:
    """One of the customer's work-order vocabularies: path, key, and the noun."""

    path: str
    key: str
    noun: str


STATUSES = LookupSpec(
    RESOURCES["work_order_statuses"].path, RESOURCES["work_order_statuses"].key, "status"
)
PRIORITIES = LookupSpec(
    RESOURCES["work_order_priorities"].path, RESOURCES["work_order_priorities"].key, "priority"
)
CATEGORIES = LookupSpec(rm_client.CATEGORIES_PATH, CATEGORY_KEY, "category")


# ── small readers (pure) ──────────────────────────────────────────────────


def as_id(value: Any) -> int | None:
    """A record id as Rent Manager sends one: a whole number, never a bool."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isascii() and value.strip().isdigit():
        return int(value.strip())
    return None


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _fold(text: str) -> str:
    return " ".join(text.split()).casefold()


def one_record(body: Any) -> dict[str, Any] | None:
    """The record a read by id answered: an object, or a list holding one."""
    # @unverified RM-27 a read by id answers the record itself; a list holding
    # one record is accepted too, as some collection-shaped answers are.
    if isinstance(body, list) and len(body) == 1:
        body = body[0]
    return body if isinstance(body, dict) else None


@dataclass(frozen=True)
class Lookup:
    """A vocabulary as read: id -> the customer's own name, and whether Rent
    Manager confirmed the list whole."""

    names: Mapping[int, str]
    complete: bool

    def resolve(self, asked: str) -> int | None:
        """The one id whose name is ``asked`` (case and spacing ignored), or
        ``None`` when none or more than one matches. Never a guess."""
        wanted = _fold(asked)
        hits = [ident for ident, name in self.names.items() if _fold(name) == wanted]
        return hits[0] if len(hits) == 1 else None

    def listed(self) -> str:
        return ", ".join(sorted(self.names.values(), key=str.casefold))


def lookup_of(rows: list[dict[str, Any]], key: str, *, complete: bool) -> Lookup:
    names: dict[int, str] = {}
    for row in rows:
        ident, name = as_id(row.get(key)), _text(row.get("Name"))
        if ident is not None and name:
            names[ident] = name
    return Lookup(names=names, complete=complete)


def unknown_name(spec: LookupSpec, asked: str, found: Lookup, *, unchanged: str) -> str:
    """The reply for a name the customer's vocabulary does not hold. A list
    that was not read whole is said to be partial, never offered as all."""
    if not found.complete:
        return (
            f"I couldn't read {NAME}'s whole list of work-order {spec.noun} names, so I "
            f'can\'t tell whether "{asked}" is one. {unchanged} Try again in a minute, '
            f"or make the change in {NAME} directly."
        )
    if not found.names:
        return (
            f"{NAME} returned no work-order {spec.noun} names for this account, so I can't "
            f'set "{asked}". {unchanged}'
        )
    return (
        f'{NAME} has no work-order {spec.noun} called "{asked}". This account\'s are: '
        f"{found.listed()}. {unchanged}"
    )


def links_of(issue: Mapping[str, Any]) -> tuple[frozenset[int], frozenset[int]]:
    """The property ids and unit ids a work order is linked to."""
    # @unverified RM-20 the LinkedUnits and LinkedProperties embeds carry
    # UnitID and PropertyID (as the sync reads them, mapper.map_work_order).
    units = [u for u in issue.get("LinkedUnits") or [] if isinstance(u, dict)]
    props = [p for p in issue.get("LinkedProperties") or [] if isinstance(p, dict)]
    unit_ids = {i for u in units if (i := as_id(u.get("UnitID"))) is not None}
    prop_ids = {i for p in props if (i := as_id(p.get("PropertyID"))) is not None}
    prop_ids |= {i for u in units if (i := as_id(u.get("PropertyID"))) is not None}
    return frozenset(prop_ids), frozenset(unit_ids)


# ── reads the writes make (never a send) ─────────────────────────────────


async def read_lookup(client: PmsHttpClient, spec: LookupSpec) -> Lookup:
    found = await read_all(
        client,
        spec.path,
        strategy=rm_client.PAGING,
        query={"fields": f"{spec.key},Name"},
    )
    return lookup_of(found.rows, spec.key, complete=found.complete)


async def read_issue(client: PmsHttpClient, issue_id: int) -> dict[str, Any] | None:
    """One work order, read whole with its links, or ``None`` when Rent
    Manager says there is none this connection can see (a 404 on one record
    is "not found or not permitted", never a deletion)."""
    try:
        resp = await client.get(
            rm_client.ISSUE_PATH,
            path_params={KEY: issue_id},
            query={"embeds": ISSUE_EMBEDS},
        )
    except PmsApiError as exc:
        if exc.failure is Failure.NOT_FOUND:
            return None
        raise
    return one_record(resp.body)


async def _read_named(
    client: PmsHttpClient, template: str, key: str, ident: int, fields: str
) -> dict[str, Any] | None:
    try:
        resp = await client.get(template, path_params={key: ident}, query={"fields": fields})
    except PmsApiError as exc:
        if exc.failure is Failure.NOT_FOUND:
            return None
        raise
    return one_record(resp.body)


# ── which property and unit the person means ─────────────────────────────

#: This company's synced Rent Manager properties: what the model sees by name
#: in every other tool, since none of them shows an id. Both predicates are
#: load-bearing (rule 11): without ``company_id`` another workspace's
#: properties answer, without ``pms`` another system's.
PROPERTIES_SQL = (
    "SELECT buildium_id, name FROM helixis_properties WHERE company_id = $1::uuid AND pms = $2"
)
#: One property's synced units, for the same company and system.
UNITS_SQL = (
    "SELECT buildium_id, unit_number FROM helixis_units "
    "WHERE company_id = $1::uuid AND pms = $2 AND property_id = $3"
)

#: How many names a "which one?" reply lists before it says how many more.
_LISTED = 8


def _named(rows: Sequence[Mapping[str, Any]], key: str, asked: str) -> list[Mapping[str, Any]]:
    """The rows whose ``key`` is ``asked`` (case and spacing ignored); failing
    that, the rows whose ``key`` contains it. A unit is matched whole only:
    "1" must never mean "11"."""
    wanted = _fold(asked)
    exact = [r for r in rows if _fold(str(r.get(key) or "")) == wanted]
    if exact or key == "unit_number":
        return exact
    return [r for r in rows if wanted and wanted in _fold(str(r.get(key) or ""))]


def _some(names: list[str]) -> str:
    shown = sorted(names, key=str.casefold)[:_LISTED]
    more = len(names) - len(shown)
    return ", ".join(shown) + (f" and {more} more" if more > 0 else "")


def _same_name(what: str, asked: str, unchanged: str) -> str:
    """Two records share the name asked for: no name tells them apart."""
    return (
        f'Two or more {what} called "{asked}" in {NAME}, so I can\'t tell which one you '
        f"mean. {unchanged} Create the work order in {NAME}, or rename one of them there."
    )


async def find_place(
    pool: Any, company_id: str, property_name: str, unit_name: str | None
) -> tuple[int, int | None] | str:
    """The property and unit the person named, as Rent Manager ids, from this
    company's mirror; or the reply that says why not. Rent Manager is read by
    id afterwards, so the card shows its own names (and refuses a mirror that
    has gone stale)."""
    unchanged = f"Nothing was created in {NAME}."
    asked = " ".join((property_name or "").split())
    if not asked:
        return f"Which property is the work order for? {unchanged}"
    async with pool.acquire() as conn:
        properties = [dict(r) for r in await conn.fetch(PROPERTIES_SQL, company_id, rm_client.PMS)]
        if not properties:
            return (
                f"Occupella hasn't synced any properties from {NAME} for this workspace yet, so I "
                f"can't tell which one you mean. {unchanged} Run Sync now on the Connectors page "
                f"(Settings → Connectors → {NAME}), or create the work order in {NAME}."
            )
        found = _named(properties, "name", asked)
        if len(found) > 1 and len({_fold(str(r.get("name") or "")) for r in found}) == 1:
            return _same_name("properties are", str(found[0].get("name")), unchanged)
        if len(found) != 1:
            names = [str(r["name"]) for r in (found or properties) if r.get("name")]
            lead = (
                f'Several properties match "{asked}"'
                if found
                else f'No property called "{asked}" is synced from {NAME}'
            )
            return f"{lead}: {_some(names)}. Which one? {unchanged}"
        property_id = as_id(found[0].get("buildium_id"))
        if property_id is None:
            return f'I couldn\'t read the id of "{asked}" from the synced data. {unchanged}'
        if unit_name is None or not unit_name.strip():
            return property_id, None
        units = [
            dict(r) for r in await conn.fetch(UNITS_SQL, company_id, rm_client.PMS, property_id)
        ]
    unit = re.sub(r"^\s*unit\s+", "", unit_name, flags=re.IGNORECASE)
    hits = _named(units, "unit_number", unit)
    if len(hits) > 1:
        place = found[0].get("name") or asked
        return _same_name(f"units at {place} are", str(hits[0].get("unit_number")), unchanged)
    if len(hits) != 1 or (unit_id := as_id(hits[0].get("buildium_id"))) is None:
        names = [str(r["unit_number"]) for r in units if r.get("unit_number")]
        listed = f" Its units are: {_some(names)}." if names else ""
        return (
            f'{found[0].get("name") or asked} has no unit "{unit.strip()}" synced from {NAME}.'
            f"{listed} {unchanged}"
        )
    return property_id, unit_id


# ── create a work order ───────────────────────────────────────────────────


@dataclass(frozen=True)
class Place:
    """Where a work order is, in the names Rent Manager returned."""

    property_id: int
    property_name: str
    unit_id: int | None = None
    unit_name: str | None = None

    def words(self) -> str:
        if self.unit_id is None:
            return self.property_name
        return f"Unit {self.unit_name or self.unit_id} at {self.property_name}"


def create_body(
    *,
    title: str,
    description: str,
    place: Place,
    priority_id: int | None,
    category_id: int | None,
) -> list[dict[str, Any]]:
    """The create request: a LIST HOLDING EXACTLY ONE object (a collection
    POST takes a list, and a replayed list of several would duplicate them).

    No ``ServiceManagerIssueID``: an object without its key is a create
    (api-notes §5, inferred). No ``StatusID``: Rent Manager's starting status
    for a new work order applies, since nothing says which of the customer's
    statuses means "new". A priority or category not asked for is left out,
    never guessed.
    """
    # @unverified RM-26 a work order is created by POST /ServiceManagerIssues
    # with a list of one object carrying Title, Description, PriorityID and
    # CategoryID, linked to its property and unit by LinkedProperties and
    # LinkedUnits entries naming PropertyID and UnitID. The Overview says
    # linked data is ignored on POST unless marked "atomic create" (p.4), so
    # the read-back checks the links took. Settled by one create on a test
    # property, approved by Brandon, then re-read with the links embedded.
    obj: dict[str, Any] = {"Title": title}
    if description:
        obj["Description"] = description
    if priority_id is not None:
        obj["PriorityID"] = priority_id
    if category_id is not None:
        obj["CategoryID"] = category_id
    obj["LinkedProperties"] = [{"PropertyID": place.property_id}]
    if place.unit_id is not None:
        obj["LinkedUnits"] = [{"UnitID": place.unit_id}]
    return [obj]


def create_plan(
    *,
    title: str,
    description: str,
    place: Place,
    priority: tuple[int, str] | None,
    category: tuple[int, str] | None,
) -> WritePlan:
    """The card and the exact request for one new work order."""
    body = create_body(
        title=title,
        description=description,
        place=place,
        priority_id=priority[0] if priority else None,
        category_id=category[0] if category else None,
    )
    if not (isinstance(body, list) and len(body) == 1 and isinstance(body[0], dict)):
        raise ValueError("a Rent Manager create posts a list holding exactly one object")
    where = place.words()
    done = f'Created the work order "{title}" for {where} in {NAME}.'
    return WritePlan(
        action_type=CREATE_ACTION,
        action=CREATE_DOING,
        title=f"Create this work order in {NAME}?",
        details=f'Create the work order "{title}" for {where} in {NAME}.',
        items=(
            f"Where: {where}",
            f"Title: {title}",
            f"Description: {description}" if description else "Description: none",
            f"Priority: {priority[1]}"
            if priority
            else f"Priority: not set ({NAME} applies its own default)",
            f"Category: {category[1]}" if category else "Category: not set",
            f"Status: the one {NAME} gives a new work order",
        ),
        send=Send("POST", rm_client.ISSUES_PATH, body),
        key={
            "property": place.property_id,
            "unit": place.unit_id,
            "title": title,
            "description": description,
            "priority": priority[0] if priority else None,
            "category": category[0] if category else None,
        },
        done=lambda _response: done,
    )


async def plan_create(
    client: PmsHttpClient,
    *,
    property_id: int,
    unit_id: int | None,
    title: str,
    description: str,
    priority: str | None,
    category: str | None,
) -> WritePlan | str:
    """Read what the card names, then plan the create. A string is a reply
    with no card: nothing was sent."""
    unchanged = f"Nothing was created in {NAME}."
    # @unverified RM-27 a property and a unit read by id carry Name (and a unit
    # its PropertyID), as the collection reads do.
    prop = await _read_named(
        client, rm_client.PROPERTY_PATH, "PropertyID", property_id, "PropertyID,Name"
    )
    if prop is None:
        return (
            f"{NAME} has no property with that id that this connection can see. {unchanged} "
            f"Check the property in {NAME}, or ask me to look it up by name first."
        )
    place = Place(property_id, _text(prop.get("Name")) or f"property {property_id}")
    if unit_id is not None:
        unit = await _read_named(
            client, rm_client.UNIT_PATH, "UnitID", unit_id, "UnitID,Name,PropertyID"
        )
        if unit is None:
            return (
                f"{NAME} has no unit with that id that this connection can see. {unchanged} "
                f"Check the unit in {NAME}, or ask me to look it up first."
            )
        if as_id(unit.get("PropertyID")) != property_id:
            return (
                f"That unit is not at {place.property_name} in {NAME}, so I didn't create "
                f"anything. {unchanged} Check which property the unit belongs to."
            )
        place = Place(property_id, place.property_name, unit_id, _text(unit.get("Name")))
    chosen: dict[str, tuple[int, str] | None] = {"priority": None, "category": None}
    for spec, asked in ((PRIORITIES, priority), (CATEGORIES, category)):
        if asked is None or not asked.strip():
            continue
        found = await read_lookup(client, spec)
        ident = found.resolve(asked)
        if ident is None:
            return unknown_name(spec, asked.strip(), found, unchanged=unchanged)
        chosen[spec.noun] = (ident, found.names[ident])
    return create_plan(
        title=title,
        description=description,
        place=place,
        priority=chosen["priority"],
        category=chosen["category"],
    )


_LOCATION_ID = re.compile(r"/ServiceManagerIssues/(\d+)(?:[/?#]|$)", re.IGNORECASE)


def created_id(response: PmsResponse) -> int | None:
    """The new work order's id, from Rent Manager's answer, or ``None``."""
    # @unverified RM-26 the create answers the created record (a list of one,
    # or the object) carrying its ServiceManagerIssueID; the Overview also
    # promises a Location header naming it (p.8). Settled by the same create.
    body = response.body
    record = body[0] if isinstance(body, list) and len(body) == 1 else body
    if isinstance(record, dict) and (ident := as_id(record.get(KEY))) is not None:
        return ident
    for name, value in response.headers.items():
        if name.lower() == "location" and (found := _LOCATION_ID.search(str(value))):
            return int(found.group(1))
    return None


def create_took(sent: Mapping[str, Any], issue: Mapping[str, Any]) -> bool:
    """Whether the record read back is the one the card showed: its title,
    description, priority and category, and its links (rule 6: a 2xx is not
    the write)."""
    if _text(issue.get("Title")) != _text(sent.get("Title")):
        return False
    if "Description" in sent and _text(issue.get("Description")) != _text(sent["Description"]):
        return False
    for field in ("PriorityID", "CategoryID"):
        if field in sent and as_id(issue.get(field)) != sent[field]:
            return False
    props, units = links_of(issue)
    wanted_props, wanted_units = links_of(sent)
    return wanted_props <= props and wanted_units <= units


async def verify_create(client: PmsHttpClient, plan: WritePlan, response: PmsResponse) -> bool:
    """Read the new work order back and compare. No id in the answer is a
    "could not confirm", never a success."""
    ident = created_id(response)
    if ident is None:
        return False
    issue = await read_issue(client, ident)
    body = plan.send.body
    sent = body[0] if isinstance(body, list) and body else {}
    return issue is not None and create_took(sent, issue)


# ── change a work order's status ──────────────────────────────────────────


@dataclass(frozen=True)
class Seen:
    """What the approver saw of the work order: anything else may move."""

    issue_id: int
    title: str | None
    status_id: int | None
    links: tuple[frozenset[int], frozenset[int]]


def seen_of(issue_id: int, issue: Mapping[str, Any]) -> Seen:
    return Seen(issue_id, _text(issue.get("Title")), as_id(issue.get("StatusID")), links_of(issue))


def concurrency_of(issue: Mapping[str, Any]) -> dict[str, Any]:
    """The concurrency values the record carries, to echo unchanged."""
    return {k: issue[k] for k in CONCURRENCY_FIELDS if issue.get(k) not in (None, "")}


def status_body(issue_id: int, status_id: int, concurrency: Mapping[str, Any]) -> dict[str, Any]:
    """The update: the id, the new status, and the concurrency value. Nothing
    else, so nothing else can change."""
    # @unverified RM-9 an update to one record is a POST to it carrying only
    # the fields to change ("Fields that are not passed are not updated",
    # Overview p.8) plus the concurrency value. Settled by the status change
    # and change back RM-9 names: re-read, and every other field is as it was.
    return {KEY: issue_id, "StatusID": status_id, **concurrency}


def status_send(issue_id: int, status_id: int, concurrency: Mapping[str, Any]) -> Send:
    return Send(
        "POST",
        rm_client.ISSUE_PATH,
        status_body(issue_id, status_id, concurrency),
        {KEY: issue_id},
    )


def status_plan(
    *,
    seen: Seen,
    concurrency: Mapping[str, Any],
    new_status: tuple[int, str],
    current_name: str,
) -> WritePlan:
    title = seen.title or "this work order"
    new_id, new_name = new_status
    done = f'Set the work order "{title}" to {new_name} in {NAME}.'
    return WritePlan(
        action_type=STATUS_ACTION,
        action=STATUS_DOING,
        title=f"Change this work order's status in {NAME}?",
        details=f'Set the work order "{title}" to {new_name} in {NAME}.',
        items=(f"Work order: {title}", f"Status: {current_name} → {new_name}"),
        send=status_send(seen.issue_id, new_id, concurrency),
        key={"id": seen.issue_id, "status": new_id},
        done=lambda _response: done,
        # A 409's resend may carry a fresher value; nothing else may move.
        volatile=frozenset(CONCURRENCY_FIELDS),
    )


@dataclass(frozen=True)
class StatusChange:
    """A planned status change and what its re-read must still match."""

    plan: WritePlan
    seen: Seen
    new_status_id: int


async def plan_status(client: PmsHttpClient, *, issue_id: int, status: str) -> StatusChange | str:
    """Read the work order and the customer's statuses, then plan. A string
    is a reply with no card: nothing was sent."""
    unchanged = f"Nothing was changed in {NAME}."
    issue = await read_issue(client, issue_id)
    if issue is None:
        return (
            f"{NAME} has no work order with that id that this connection can see. "
            f"{unchanged} Check the work order in {NAME}, or ask me to find it first."
        )
    statuses = await read_lookup(client, STATUSES)
    new_id = statuses.resolve(status)
    if new_id is None:
        return unknown_name(STATUSES, status.strip(), statuses, unchanged=unchanged)
    seen = seen_of(issue_id, issue)
    new_name = statuses.names[new_id]
    if seen.status_id == new_id:
        return f"That work order is already {new_name} in {NAME}, so there's nothing to change."
    concurrency = concurrency_of(issue)
    if not concurrency:
        return (
            f"{NAME}'s record for that work order didn't carry the value it needs to accept a "
            f"change safely, so I didn't send one. {unchanged} Change its status in {NAME} "
            "directly."
        )
    # The current status in the customer's words, or said to be unknown:
    # never another status's name, never a guess.
    if seen.status_id is None:
        current = f"not given by {NAME}"
    else:
        current = statuses.names.get(seen.status_id, f"one {NAME}'s status list doesn't name")
    plan = status_plan(
        seen=seen, concurrency=concurrency, new_status=(new_id, new_name), current_name=current
    )
    return StatusChange(plan=plan, seen=seen, new_status_id=new_id)


def status_recheck(change: StatusChange, issue: Mapping[str, Any] | None) -> Send | None:
    """The request to send now, from a fresh read: the approved one with the
    record's current concurrency value, or ``None`` when anything the
    approver saw has moved (the person gets a new card)."""
    if issue is None or seen_of(change.seen.issue_id, issue) != change.seen:
        return None
    concurrency = concurrency_of(issue)
    if not concurrency:
        return None
    return status_send(change.seen.issue_id, change.new_status_id, concurrency)


def status_took(issue: Mapping[str, Any] | None, status_id: int) -> bool:
    return issue is not None and as_id(issue.get("StatusID")) == status_id


__all__ = [
    "CATEGORIES",
    "CONCURRENCY_FIELDS",
    "CREATE_ACTION",
    "CREATE_DOING",
    "CREATE_OPERATION",
    "PRIORITIES",
    "STATUSES",
    "STATUS_ACTION",
    "STATUS_DOING",
    "STATUS_OPERATION",
    "Lookup",
    "Place",
    "Seen",
    "StatusChange",
    "as_id",
    "concurrency_of",
    "create_body",
    "create_plan",
    "create_took",
    "created_id",
    "find_place",
    "links_of",
    "lookup_of",
    "plan_create",
    "plan_status",
    "read_issue",
    "read_lookup",
    "seen_of",
    "status_body",
    "status_plan",
    "status_recheck",
    "status_took",
    "unknown_name",
    "verify_create",
]
