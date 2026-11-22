"""Read Propertyware, write the mirror: one company's pass.

THE PASS
    :func:`sync_company` refuses unless the deployment switch is on
    (``pms.registry.enabled``) and THIS company has credentials (no env
    fallback, gotcha 70). It then chooses the kind of pass
    (:func:`choose_pass`), reads the lists its plan names one request at a
    time and within :data:`CALL_BUDGET`, maps the rows with the pure
    ``mapper``, checks the company still holds the credentials it read with (a
    disconnect during the reads must not be undone by the write), hands the
    result to the shared writer (``pms.mirror.write_mirror``), stamps the
    stages that came through clean (``pms.mirror.stamp_freshness``), opens a
    turnover for each recent move-out, and moves the watermarks and pass
    markers of what came through clean.

THREE KINDS OF PASS (design, "Sync")
    * FULL: every list in ``client.READS`` read whole, exactly as slice 1
      did. On the first pass, when the last FULL pass that ended clean is
      :data:`FULL_EVERY` old, when the connection changed after it (keys
      saved, a test passed: the credentials row's ``updated_at``), when a
      person asked for one after it (Sync now, :func:`ask_for_full_pass`),
      and on every pass while :data:`INCREMENTAL_READS` is off.
    * RECONCILE: when the last clean reconcile is :data:`RECONCILE_EVERY`
      old. The design's reconcile is a complete id-only read of each list
      beside an incremental pass. Propertyware has no parameter that narrows
      a list to its ids, so its reconcile IS a full read: every list read
      whole, and the writer prunes what a complete read no longer lists.
    * INCREMENTAL: otherwise. Each list is read from its watermark
      (``pms.watermarks.since``: ``LAG`` behind it) to the pass's start
      (:func:`window_query`), deduped on (id, change time) and written as a
      delta: nothing pruned, the links rewritten per lease read. A list with
      no watermark (never read clean) is read whole. The charges are not
      read, and the balance snapshot is left as it was, its stage unstamped.

WATERMARKS AND MARKERS
    A list's watermark moves to the pass's start only after a clean pass of
    it: its read complete, and every table it feeds written and not stale. A
    pass's markers move when it read every list it could and each failure
    was a refusal for the API key's permission (a 403, which reading again
    cannot fix: those tables stay stale with their reason, and their lists
    keep their own watermarks). Any other failure (a 5xx, the budget, a
    partial read, a failed write) holds them (:func:`markers_may_move`).
    FULL moves the full and the reconcile markers, RECONCILE its own. Lists
    are read oldest watermark first, so a list a pass failed or did not
    reach leads the next one.

THE CALL BUDGET (rule 13)
    A pass asks for at most :data:`CALL_BUDGET` pages. One that reaches it
    stops: every list it did not finish is stale with
    :data:`CALL_BUDGET_REACHED`, its watermark stays where it was, and so do
    the pass's markers, so the next one is the same kind and starts with them.

STALE, NEVER EMPTY
    A list whose read fails is reported stale (``TableRead(error=...)``) and
    the pass goes on with the others; the writer keeps that table's rows as
    they were and does not stamp it. A read that stopped before Propertyware's
    own ``X-Total-Count`` is written, not stamped, and not pruned from. A 404
    on a list is a failure like any other: never "no rows".

    Each stale reason is a sentence for the customer, which the sync status
    shows (:func:`read_failure`; :func:`stale_reason` words the shared
    writer's own). The status code and Propertyware's text go to the log.

WHAT DEPENDS ON WHAT
    * Lease, work-order and bill-line units are checked against this pass's
      units read (``mapper.resolve_unit``). When that read failed or is
      partial, a lease's or work order's stored unit is left as it was. An
      incremental pass checks them against the mirror's units and the changed
      ones, and never calls a unit missing from that.
    * Owners come from the portfolios read; their properties need a COMPLETE
      WHOLE buildings read, or the stored list is left as it was.
    * Balance totals come from the leases read (``arBalance``); the aging
      needs a COMPLETE charges read, or every bucket is unknown. Both are
      read whole, on FULL and RECONCILE passes only.

WHAT IS NOT FILLED, AND WHY
    ``helixis_lease_transactions``: the charges carry their payments, but a
    ledger without the payment reads would make the payment-history tool say
    "no payments", which is false (design, "built correctly"). The table stays
    unfilled, so ``pms_coverage`` makes its readers decline.
"""

from __future__ import annotations

import enum
import logging
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from helixis.common.company_clock import company_timezone, today_in
from helixis.common.support import contact_support
from helixis.pms import registry
from helixis.pms.errors import Failure, PmsApiError
from helixis.pms.http import PmsHttpClient, operation_key
from helixis.pms.mirror import SyncResult, TableRead, stamp_freshness, write_mirror
from helixis.pms.paging import read_all
from helixis.pms.watermarks import Watermark, dedupe, since
from helixis.propertyware import _paths, client, credentials, mapper

logger = logging.getLogger(__name__)

PMS = client.PMS

#: What this connector stamps ``helixis_mirror_sync_state.source`` with.
SYNC_SOURCE = "propertyware_sync"

#: How far back a move-out may lie and still open a turnover (Rentvine's).
TURNOVER_LOOKBACK_DAYS = 30

#: Mirror table -> the freshness stage it is stamped under: the stage names
#: Rentvine uses for the same tables, so one vocabulary holds. Every table
#: this pass fills is here, and nothing it does not fill (``pms_coverage``
#: reads this map as the tables Propertyware feeds).
#:
#: ⚠ A plain ``dict`` on purpose: ``pms_coverage._stage_map`` reads anything
#: else as "fills nothing".
SYNC_STATE_STAGE: dict[str, str] = {
    "helixis_properties": "properties",
    "helixis_units": "units",
    "helixis_leases": "leases",
    "helixis_tenants": "tenants",
    "helixis_owners": "owners",
    "helixis_vendors": "vendors",
    "helixis_work_orders": "work_orders",
    # Shares the leases stage, as Rentvine's does: a stage is stamped only
    # when every table on it came through clean.
    "helixis_lease_tenants": "leases",
    "helixis_bills": "bills",
    # Shares the bills stage: the lines ride on the same bills read.
    "helixis_bill_lines": "bills",
    "helixis_lease_balances": "balances",
    "helixis_gl_accounts": "gl_accounts",
}

#: Tables this pass fills that are not a mirror of one Propertyware list, so
#: they are never stamped or pruned: the turnover checklist, opened from the
#: leases' move-out dates (:func:`_start_turnovers`).
DERIVED_TABLES: frozenset[str] = frozenset({"helixis_turnovers"})

#: Added to a Propertyware run's input (``agents.run_pms.run_note_for``). It
#: names Propertyware and only Propertyware, says what is synced, and says a
#: missing item is "not available in Occupella yet", never zero or none.
RUN_NOTE = (
    "\n\n## This workspace's property-management system\n"
    "This workspace runs on Propertyware, and you are its Propertyware Agent. Answer from "
    "Occupella's synced Propertyware data: properties (Propertyware calls them "
    "buildings), units, leases (dates, status and rent), tenants and which current tenant "
    "is on which lease, owners (names, and which properties each owns), vendors, work "
    "orders, vendor bills (what is owed, to whom, and when it is due), rent balances (who "
    "owes rent and how much), and turnovers (the make-ready checklist for a unit with a "
    "move-out date). Spending by category and by property is available too; spend charged "
    "to a portfolio and no building belongs to no single property. If no owner is shown "
    "for a property, Occupella does not have its owner: say so, and never say the "
    "property has no owner. If a lease shows no unit, Occupella could not match it to one "
    "(single-family homes often have none): say so, and never say the lease has no unit. "
    "If a rent balance has no aging breakdown, Occupella could not work it out: give the "
    "total, and never call any part of it current. The record numbers Occupella shows are "
    "not the lease or work order numbers Propertyware shows, so never give one as a "
    "Propertyware number. Propertyware's API cannot change a work order's status or "
    "vendor, so nothing in Occupella can: never edit or create a work order to stand in "
    "for one. Not available in Occupella yet: rent payment history, security "
    "deposits held, owner statements, custom fields, inspections, tasks, rental "
    "applications, and how much is in each bank account. Say plainly that a missing item "
    "is not available in Occupella yet and can be checked in Propertyware; never say it "
    "is zero, empty or none. Call the system Propertyware and by no other name. Never "
    "tell the user to restart, re-ask, re-route or switch assistants: you are the "
    "assistant for this workspace and there is no other one to send them to. Tell the "
    "user to reconnect Propertyware only when a tool's answer says to."
)

#: Filters each read sends besides paging, the sort and any change window.
_FILTERS: dict[str, dict[str, str]] = {
    # @unverified PW-1 — that type=TENANT is honoured; a row typed otherwise
    # is left out anyway (mapper.map_tenant). Settled by the probe's first
    # contacts read.
    "/contacts": {"type": "TENANT"},
    # @unverified PW-6 — that arBalance needs includeOtherBalances=true on the
    # list; settled by one leases read with it and one without.
    "/leases": {"includeOtherBalances": "true"},
    # @unverified PW-1 — that includeDeactivated=true adds inactive accounts
    # beside the active ones ("If no value is specified, only active accounts
    # will be returned"); settled by the probe's first GL accounts read.
    "/accounting/glaccounts": {"includeDeactivated": "true"},
}

#: Each table this pass fills -> the list it is read from. The balance
#: snapshot needs the charges too (:func:`mirror_rows`).
SOURCE: dict[str, str] = {
    "helixis_properties": "/buildings",
    "helixis_units": "/units",
    "helixis_owners": "/portfolios",
    "helixis_tenants": "/contacts",
    "helixis_vendors": "/vendors",
    "helixis_leases": "/leases",
    "helixis_lease_tenants": "/leases",
    "helixis_lease_balances": "/leases",
    "helixis_work_orders": "/workorders",
    "helixis_bills": "/bills",
    "helixis_bill_lines": "/bills",
    "helixis_gl_accounts": "/accounting/glaccounts",
}

#: The balance snapshot: replaced whole from a whole read, on FULL and
#: RECONCILE passes only. No list's watermark waits on it.
BALANCES = "helixis_lease_balances"

#: Lists read on FULL and RECONCILE passes only, whole, and never by window:
#: the charges feed only the balance snapshot's aging (the design's "charges
#: follow the balances"). They carry no watermark.
WHOLE_PASS_ONLY: frozenset[str] = frozenset({"/leases/charges"})

# ── which pass runs ───────────────────────────────────────────────────────

#: How often each kind of pass comes round (design, "Sync"): a full read
#: weekly, a reconcile nightly, and an incremental read on every pass between
#: (the cycle runs one every ``Settings.propertyware.sync_interval_hours``).
FULL_EVERY = timedelta(days=7)
RECONCILE_EVERY = timedelta(hours=24)

#: Off makes every pass FULL, for every company: Propertyware's API is one
#: API, so this is not a per-company switch (the filter canary that turns
#: incremental reads off for one company is Rent Manager's alone).
# @unverified PW-5 PW-13 — that the change window filters and the change-time
# sort are honoured; turn this off (every pass reads whole) if the probe finds
# either refused or ignored, until the windowed read is fixed.
INCREMENTAL_READS = True

#: The most pages one pass asks for (rule 13). At :data:`client.REQUESTS_PER_SEC`
#: (2 a second) 1,440 pages take at least 12 minutes, so a worker tick that
#: syncs ``pms.sync_cycle.COMPANIES_PER_TICK`` (5) companies one after another,
#: each spending all of it, still fits in the hourly tick (3,600 s x 2 / 5),
#: and one pass is a twentieth of the 4-hour interval. 1,440 pages of 500 rows
#: is 720,000 rows, about three times a large account's full read (an estimate
#: from the spec's lists, NOT measured: no live account has been read).
CALL_BUDGET = 1440

#: The stored markers and watermarks, as ``pms.watermarks`` rows named by this
#: connector (:func:`resource`). The third is when a person last asked for a
#: full read (Sync now, :func:`ask_for_full_pass`).
FULL_MARKER = "pass:full"
RECONCILE_MARKER = "pass:reconcile"
FULL_ASKED_MARKER = "pass:full-asked"

#: The change time each row carries, compared when a window's rows are deduped.
# @unverified PW-1 — that every list's rows carry lastModifiedDateTime as the
# spec declares (a date-time in UTC); settled by the probe's first page of each.
MODIFIED = "lastModifiedDateTime"

#: Each table this pass can report stale, in the words the sync status names
#: it with (``api/routes/propertyware.py``), in the order it names them.
TABLE_WORDS: dict[str, str] = {
    "helixis_properties": "buildings",
    "helixis_units": "units",
    "helixis_leases": "leases",
    "helixis_lease_tenants": "which tenants are on which lease",
    "helixis_tenants": "tenants",
    "helixis_lease_balances": "rent balances",
    "helixis_owners": "owners",
    "helixis_vendors": "vendors",
    "helixis_work_orders": "work orders",
    "helixis_bills": "bills",
    "helixis_bill_lines": "bill lines",
    "helixis_gl_accounts": "GL accounts",
}

#: Said after a failure that is likely to pass by itself.
_SYNC_AGAIN = "Sync again in a few minutes."

#: Said after a failure whose fix is Occupella's, not the customer's.
_IF_IT_KEEPS_HAPPENING = contact_support("if it keeps happening.")


class CustomerReason(str):
    """A stale reason this pass words for the customer itself.

    The shared writer passes a table's ``TableRead.error`` through as that
    table's stale reason, the same object. So :func:`stale_reason` tells this
    pass's sentences from the writer's own operator text by their type, and
    never by matching the writer's words: a reason the writer adds later is
    worded for the customer without a change here.
    """

    __slots__ = ()


def read_failure(path: str, exc: PmsApiError) -> CustomerReason:
    """Why a list's read failed, in words a person can act on (rule 16).

    It becomes the stale reason of every table the list feeds, and the sync
    status shows it after those tables' names. So it never names the list
    itself: tables that failed for one reason are named once, together. The
    status, Propertyware's own error text and the rest stay in the log line
    beside it: they are for an operator, and vendor text is never shown as ours.
    """
    return CustomerReason(_read_failure_words(path, exc))


def _read_failure_words(path: str, exc: PmsApiError) -> str:
    # @unverified PW-2 — that a wrong organization ID is answered with a 401 or
    # a 403, so both reasons name all three values to check. The connect
    # route's test answers rest on the same guess (api/routes/propertyware.py).
    if exc.failure is Failure.CREDENTIALS:
        return (
            "Propertyware rejected the saved credentials. Check the organization ID, "
            "client ID and client secret, and save them again."
        )
    if exc.failure is Failure.PERMISSION:
        needed = _paths.PERMISSIONS.get(operation_key("GET", path))
        lacks = (
            f"Propertyware refused this API key: it needs the {needed} permission."
            if needed
            else "Propertyware refused this API key access."
        )
        return (
            f"{lacks} Create a key in Propertyware that has it and save it here, or check "
            "that the organization ID, client ID and client secret all belong to the same "
            "Propertyware account."
        )
    if exc.failure is Failure.UNAVAILABLE:
        return f"Propertyware kept failing, so Occupella paused calls to it. {_SYNC_AGAIN}"
    if exc.status == 429:
        return f"Propertyware asked Occupella to slow down. {_SYNC_AGAIN}"
    if exc.status == 0 or exc.status >= 500:
        return f"Propertyware did not answer. {_SYNC_AGAIN}"
    return "Propertyware answered with an error Occupella did not expect; " + _IF_IT_KEEPS_HAPPENING


#: A list whose read raised something other than a vendor error (ours, logged).
_READ_CRASHED = CustomerReason(
    "Occupella hit a problem reading this from Propertyware; " + _IF_IT_KEEPS_HAPPENING
)

#: A table whose rows Occupella could not map (a bug of ours, logged).
_MAPPING_FAILED = CustomerReason(
    "Occupella could not read what Propertyware sent; " + _IF_IT_KEEPS_HAPPENING
)

#: Every table a pass did not finish because it reached :data:`CALL_BUDGET`.
CALL_BUDGET_REACHED = CustomerReason(
    "Occupella reached this sync's call budget for Propertyware before it got to these. "
    "The next sync reads them first; sync again to read them now."
)

#: The shared writer's reasons (``pms.mirror.write_mirror``) are written for an
#: operator, and the log keeps them as they are. The sync status says one of
#: two things instead, read from the writer's stable first words only:
#: a read that stopped part-way, or anything else, which Occupella could not
#: save. A reason the writer adds later gets the second, never its own words.
_STOPPED_PART_WAY = f"Propertyware stopped sending them part-way through. {_SYNC_AGAIN}"
_NOT_ALL_SAVED = (
    "Occupella could not save all of them this time. Sync again in a few minutes, and "
    + _IF_IT_KEEPS_HAPPENING
)


def stale_reason(reason: str) -> str:
    """A table's stale reason (``SyncResult.failed``) as the sync status says it.

    The reasons this pass sets (:class:`CustomerReason`: :func:`read_failure`
    and the rest) are written for the customer already and pass through. Any
    other is the shared writer's, written for an operator, and is worded here.
    """
    if isinstance(reason, CustomerReason):
        return str(reason)
    if reason.startswith("incomplete read"):
        return _STOPPED_PART_WAY
    return _NOT_ALL_SAVED


#: Unit outcomes the sync reports, in words for the log.
_UNRESOLVED_WORDS = {
    mapper.NO_UNIT: "names no unit",
    mapper.SINGLE_FAMILY: "names its building as its unit (single-family, PW-8)",
    mapper.NOT_FOUND: "names a unit not in that building",
}

#: The most record ids one log line lists.
_IDS_IN_A_LOG_LINE = 20


class PropertywareSyncRefused(Exception):  # noqa: N818 — a refusal, not a fault
    """The connector is switched off, or this company has no usable
    Propertyware connection. Nothing was read and nothing was written."""


def query_for(path: str) -> dict[str, str]:
    """Everything a whole read of ``path`` sends besides ``limit``/``offset``.

    ⚠ No ``lastModifiedDateTimeEnd``: pinned to the pass's start, a whole read
    would leave out every row changed after the pass began, and the complete
    read would then prune them (As built, slice 1)."""
    return {"orderby": client.ORDER_BY_ID, **_FILTERS.get(path, {})}


def _iso(when: datetime) -> str:
    """A UTC date-time as the probe sends it, to the second (floored)."""
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def window_query(path: str, start: datetime, end: datetime) -> dict[str, str]:
    """Everything an incremental read of ``path`` sends besides
    ``limit``/``offset``: the rows changed from ``start`` to ``end`` (the
    pass's start), oldest change first where the list can sort by it."""
    # @unverified PW-5 — that both window ends filter on lastModifiedDateTime,
    # inclusively, and take a UTC date-time written yyyy-MM-ddTHH:mm:ssZ; the
    # start sits LAG behind the watermark either way, so an exclusive start
    # loses nothing. Settled by the probe's three date-filter requests.
    order = client.ORDER_BY_CHANGE if client.sorts_by_change(path) else client.ORDER_BY_ID
    return {
        "orderby": order,
        "lastModifiedDateTimeStart": _iso(start),
        "lastModifiedDateTimeEnd": _iso(end),
        **_FILTERS.get(path, {}),
    }


def _changed_at(value: Any) -> datetime | None:
    """A row's change time as an aware ``datetime`` (UTC when it names no
    zone, as the spec says it is), or ``None``."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def newest_per_id(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A window's rows through ``pms.watermarks.dedupe``: one per id, the
    newest by change time. Compared as times, not as text: Propertyware's
    stamps are not all written one way (``Z`` and ``+00:00`` both occur)."""
    keyed = [
        {"id": row.get("id"), "changed": _changed_at(row.get(MODIFIED)), "at": at}
        for at, row in enumerate(rows)
    ]
    return [rows[k["at"]] for k in dedupe(keyed, id_key="id", modified_key="changed")]


def resource(name: str) -> str:
    """The ``pms.watermarks`` resource for a list (``"/leases"``) or a pass
    marker (:data:`FULL_MARKER`), named for this connector."""
    return f"{PMS}:{name}"


class PassKind(enum.Enum):
    """Which pass runs (:func:`choose_pass`)."""

    FULL = "full"
    RECONCILE = "reconcile"
    INCREMENTAL = "incremental"

    @property
    def whole(self) -> bool:
        """Every list read whole: FULL, and RECONCILE (Propertyware cannot
        read a list's ids alone)."""
        return self is not PassKind.INCREMENTAL


def choose_pass(
    *,
    now: datetime,
    full: datetime | None,
    reconcile: datetime | None,
    connection_changed: datetime | None,
    full_asked: datetime | None = None,
    incremental: bool = True,
) -> tuple[PassKind, str]:
    """Which pass runs, and why (for the log).

    ``full`` and ``reconcile`` are the starts of the last pass of each kind
    that ended clean; ``connection_changed`` is when the credentials row last
    changed; ``full_asked`` is when a person last asked for a full read.
    Anything unknown chooses the wider read.
    """
    if not incremental:
        return PassKind.FULL, "incremental reads are switched off"
    if full is None:
        return PassKind.FULL, "no full pass has ended clean yet"
    if full_asked is not None and full_asked >= full:
        return PassKind.FULL, "a full read was asked for (Sync now)"
    if connection_changed is None:
        return PassKind.FULL, "when the connection last changed is unknown"
    if connection_changed >= full:
        return PassKind.FULL, "the connection changed after the last full pass"
    if now - full >= FULL_EVERY:
        return PassKind.FULL, "the last full pass is a week old"
    if reconcile is None:
        return PassKind.RECONCILE, "no reconcile has ended clean yet"
    if now - reconcile >= RECONCILE_EVERY:
        return PassKind.RECONCILE, "the last reconcile is a day old"
    return PassKind.INCREMENTAL, "the last full pass and the last reconcile are recent"


@dataclass(frozen=True)
class Plan:
    """One pass: its kind, why, when it started (the windows' end and every
    watermark it moves), and each list it reads, in order, with where its
    window starts (``None``: read whole)."""

    kind: PassKind
    why: str
    start: datetime
    reads: tuple[tuple[str, datetime | None], ...]


_EARLIEST = datetime.min.replace(tzinfo=UTC)


async def _stored(pool: Any, company_id: str, name: str) -> datetime | None:
    """A stored watermark or marker. Unreadable is ``None``: the wider read."""
    try:
        return await Watermark(pool, company_id, resource(name)).read()
    except Exception:  # noqa: BLE001 — logged; None reads more, never less
        logger.warning(
            "propertyware sync %s: could not read %s; reading as if never stored",
            company_id,
            name,
            exc_info=True,
        )
        return None


async def plan_pass(
    pool: Any, company_id: str, *, start: datetime, connection_changed: datetime | None
) -> Plan:
    """Read the markers and watermarks, choose the pass, and order its reads."""
    kind, why = choose_pass(
        now=start,
        full=await _stored(pool, company_id, FULL_MARKER),
        reconcile=await _stored(pool, company_id, RECONCILE_MARKER),
        connection_changed=connection_changed,
        full_asked=await _stored(pool, company_id, FULL_ASKED_MARKER),
        incremental=INCREMENTAL_READS,
    )
    paths = [p for p in client.READS if kind.whole or p not in WHOLE_PASS_ONLY]
    marks = {p: await _stored(pool, company_id, p) for p in paths if p not in WHOLE_PASS_ONLY}

    def oldest_first(path: str) -> tuple[bool, datetime, int]:
        mark = marks.get(path)
        return (mark is not None, mark or _EARLIEST, client.READS.index(path))

    ordered = sorted(paths, key=oldest_first)
    return Plan(
        kind=kind,
        why=why,
        start=start,
        reads=tuple(
            (p, None if kind.whole or marks.get(p) is None else since(marks[p])) for p in ordered
        ),
    )


@dataclass(frozen=True)
class ResourceRead:
    """One list's read: its rows, whether Propertyware confirmed it whole,
    when it started (``time.monotonic``), why it failed, if it did, and
    whether it was a window (the rows changed since the watermark) rather
    than the whole list."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    complete: bool = False
    started: float = 0.0
    error: str | None = None
    delta: bool = False
    #: Propertyware refused the read for the API key's permission (a 403,
    #: ``Failure.PERMISSION``): reading again cannot fix it, so it does not
    #: hold the pass's markers (:func:`markers_may_move`).
    refused: bool = False


@dataclass
class Report:
    """What the mapping found worth saying, logged by :func:`sync_company`."""

    #: ``{table: {outcome: [record ids]}}`` for rows that name no unit
    #: Occupella can match (the design's "the sync reports the lease").
    unresolved_units: dict[str, dict[str, list[int | None]]] = field(default_factory=dict)
    #: Contacts a tenant read returned typed as something else, left out.
    not_tenants: int = 0
    #: Vendor credits left out of the bills.
    credits: int = 0
    #: Leases whose ``arBalance`` did not come through (balances go stale).
    balances_unknown: list[int | None] = field(default_factory=list)
    #: Owing leases whose aging could not be worked out (buckets unknown).
    aging_unknown: list[int] = field(default_factory=list)


def _monotonic() -> float:
    """The read timer. A function so a test can pin it."""
    return time.monotonic()


def _now() -> datetime:
    """The pass's start, in UTC. A function so a test can pin it."""
    return datetime.now(UTC)


async def _read(
    pw: PmsHttpClient, path: str, query: Mapping[str, str], *, delta: bool
) -> ResourceRead:
    """One list's read, whole or a window (``delta``). A failure is returned
    as stale, never raised."""
    started = _monotonic()
    try:
        got = await read_all(pw, path, strategy=client.PAGING, query=query)
    except client.CallBudgetSpent:
        logger.warning("propertyware sync: %s stopped part-way: call budget reached", path)
        return ResourceRead(started=started, error=CALL_BUDGET_REACHED, delta=delta)
    except PmsApiError as exc:
        logger.warning("propertyware sync: %s failed; the table goes stale: %s", path, exc)
        return ResourceRead(
            started=started,
            error=read_failure(path, exc),
            delta=delta,
            refused=exc.failure is Failure.PERMISSION,
        )
    except Exception:  # noqa: BLE001 — logged, and only this list goes stale
        logger.exception("propertyware sync: %s failed unexpectedly", path)
        return ResourceRead(started=started, error=_READ_CRASHED, delta=delta)
    if not got.complete:
        logger.warning(
            "propertyware sync: %s read %d of %s rows; written, not stamped, not pruned",
            path,
            len(got.rows),
            got.total if got.total is not None else "an unknown number of",
        )
    # ⚠ Only a window is deduped. On a whole read, an id that came back twice
    # is the writer's sign that a page boundary moved, so a row may have been
    # skipped: it then prunes nothing. Deduping would hide that.
    rows = newest_per_id(got.rows) if delta else got.rows
    return ResourceRead(rows=rows, complete=got.complete, started=started, delta=delta)


async def _read_planned(
    pw: PmsHttpClient, plan: Plan, budget: client.CallBudget
) -> dict[str, ResourceRead]:
    """Every read the plan names, in its order, until the budget is spent;
    every list after that is stale with :data:`CALL_BUDGET_REACHED`."""
    reads: dict[str, ResourceRead] = {}
    for path, window_start in plan.reads:
        delta = window_start is not None
        if budget.spent:
            reads[path] = ResourceRead(error=CALL_BUDGET_REACHED, delta=delta)
            continue
        if window_start is None:
            query = query_for(path)
        else:
            query = window_query(path, window_start, plan.start)
        reads[path] = await _read(pw, path, query, delta=delta)
    return reads


def _note_units(report: Report, table: str, record_id: int | None, outcome: str) -> None:
    if outcome in _UNRESOLVED_WORDS:
        by_outcome = report.unresolved_units.setdefault(table, {})
        by_outcome.setdefault(outcome, []).append(record_id)


def _unit_index(
    units_read: ResourceRead, known_units: Mapping[int, int | None] | None
) -> mapper.UnitIndex | None:
    """What the lease, work-order and bill-line units are checked against.

    A whole units read is the index, complete when the read was. A window
    holds only the units that changed, so the mirror's units (``known_units``)
    are laid under it, and the index is never complete: a unit missing from
    it may exist, so a row naming one keeps its stored unit. A failed whole
    read gives no index at all (slice 1's rule).
    """
    if units_read.error is None and not units_read.delta:
        return mapper.UnitIndex.from_rows(units_read.rows, complete=units_read.complete)
    if units_read.error is not None and known_units is None:
        return None
    building_of: dict[int, int | None] = dict(known_units or {})
    if units_read.error is None:
        building_of.update(mapper.UnitIndex.from_rows(units_read.rows, complete=False).building_of)
    return mapper.UnitIndex(building_of=building_of, complete=False)


def _ids_of(rows: Iterable[Mapping[str, Any]]) -> frozenset[int]:
    return frozenset(i for i in (mapper.record_id(row) for row in rows) if i is not None)


def mirror_rows(
    reads: Mapping[str, ResourceRead],
    *,
    today: date,
    now: float,
    balances: bool = True,
    known_units: Mapping[int, int | None] | None = None,
) -> tuple[dict[str, TableRead], Report]:
    """This pass's reads as ``pms.mirror.MirrorRows``, and what to report.

    Pure apart from logging a mapping failure. ``today`` is the company's
    calendar date (aging); ``now`` is ``time.monotonic()`` just before the
    write, so each table's prune spares rows touched since its own read began.
    A window's tables are deltas (``TableRead.delta``). ``balances`` is off on
    an incremental pass: the snapshot is then left out, so it is neither
    written nor stamped. ``known_units`` is the mirror's units, for a window
    of the units (:func:`_unit_index`).
    """
    report = Report()
    out: dict[str, TableRead] = {}

    def table(name: str, build: Callable[[ResourceRead], TableRead]) -> None:
        read = reads[SOURCE[name]]
        if read.error is not None:
            out[name] = TableRead(error=read.error)
            return
        try:
            out[name] = build(read)
        except Exception as exc:  # noqa: BLE001 — a mapping bug stales one table, loudly
            logger.exception("propertyware sync: mapping %s failed (%s)", name, type(exc).__name__)
            out[name] = TableRead(error=_MAPPING_FAILED)

    def done(read: ResourceRead, rows: list[dict[str, Any]], **extra: Any) -> TableRead:
        return TableRead(
            rows=rows,
            complete=read.complete,
            read_seconds=max(0.0, now - read.started),
            delta=read.delta,
            **extra,
        )

    units = _unit_index(reads["/units"], known_units)
    buildings_read = reads["/buildings"]
    # Which buildings each portfolio holds needs EVERY building: a window
    # names only the ones that changed.
    by_portfolio = (
        mapper.portfolio_buildings(buildings_read.rows)
        if buildings_read.error is None and buildings_read.complete and not buildings_read.delta
        else None
    )

    table("helixis_properties", lambda r: done(r, [mapper.map_building(row) for row in r.rows]))
    table("helixis_units", lambda r: done(r, [mapper.map_unit(row) for row in r.rows]))
    table(
        "helixis_owners",
        lambda r: done(r, mapper.owners_from_portfolios(r.rows, by_portfolio)),
    )

    def tenants(read: ResourceRead) -> TableRead:
        rows = []
        for row in read.rows:
            mapped = mapper.map_tenant(row)
            if mapped is None:
                report.not_tenants += 1
            else:
                rows.append(mapped)
        return done(read, rows)

    table("helixis_tenants", tenants)
    table("helixis_vendors", lambda r: done(r, [mapper.map_vendor(x) for x in r.rows]))

    def leases(read: ResourceRead) -> TableRead:
        rows = []
        for row in read.rows:
            mapped, outcome = mapper.map_lease(row, units)
            _note_units(report, "helixis_leases", mapped["buildium_id"], outcome)
            rows.append(mapped)
        return done(read, rows)

    def links(read: ResourceRead) -> TableRead:
        rows = [link for row in read.rows for link in mapper.lease_tenant_links(row)]
        # A window's links are rewritten per lease it read: EVERY one, so a
        # lease that now has no current tenant loses its old link.
        return done(read, rows, parents=_ids_of(read.rows) if read.delta else frozenset())

    table("helixis_leases", leases)
    table("helixis_lease_tenants", links)

    def snapshot(read: ResourceRead) -> TableRead:
        charges_read = reads["/leases/charges"]
        if charges_read.error == CALL_BUDGET_REACHED:
            # The pass stopped before the aging could be worked out: the
            # snapshot is a table it did not reach, kept as it was.
            return TableRead(error=CALL_BUDGET_REACHED)
        usable = charges_read.error is None and charges_read.complete
        by_lease, by_id = mapper.charges_by_lease(charges_read.rows if usable else [])
        rows: list[dict[str, Any]] = []
        for row in read.rows:
            lease_id = mapper.record_id(row)
            charges = by_lease.get(lease_id, []) if usable and lease_id is not None else None
            picked = mapper.lease_balance(row, charges, by_id, units, today)
            if picked is mapper.BALANCE_UNKNOWN:
                report.balances_unknown.append(lease_id)
            elif isinstance(picked, dict):
                if picked["balance_0_30"] is None:
                    report.aging_unknown.append(picked["lease_id"])
                rows.append(picked)
        if report.balances_unknown:
            # One lease left out of the snapshot would read as "current".
            return TableRead(
                error=CustomerReason(
                    f"Propertyware sent {len(report.balances_unknown)} of {len(read.rows)} "
                    "leases without a balance, so the balances already on screen were kept. "
                    + _SYNC_AGAIN
                )
            )
        return done(read, rows)

    if balances:
        table(BALANCES, snapshot)

    def work_orders(read: ResourceRead) -> TableRead:
        rows = []
        for row in read.rows:
            mapped, outcome = mapper.map_work_order(row, units)
            _note_units(report, "helixis_work_orders", mapped["buildium_id"], outcome)
            rows.append(mapped)
        return done(read, rows)

    table("helixis_work_orders", work_orders)

    def bills(read: ResourceRead) -> TableRead:
        rows = []
        for row in read.rows:
            mapped = mapper.map_bill(row)
            if mapped is None:
                report.credits += 1
            else:
                rows.append(mapped)
        return done(read, rows)

    def lines(read: ResourceRead) -> TableRead:
        rows: list[dict[str, Any]] = []
        parents: set[int] = set()
        for row in read.rows:
            if mapper.is_vendor_credit(row):
                continue
            bill_id = mapper.record_id(row)
            if bill_id is not None:
                parents.add(bill_id)
            rows.extend(mapper.bill_lines(row, units))
        return done(read, rows, parents=frozenset(parents))

    table("helixis_bills", bills)
    table("helixis_bill_lines", lines)
    table("helixis_gl_accounts", lambda r: done(r, [mapper.map_gl_account(x) for x in r.rows]))
    return out, report


def _ids(ids: list[Any]) -> str:
    shown = ", ".join(str(i) for i in ids[:_IDS_IN_A_LOG_LINE])
    more = len(ids) - _IDS_IN_A_LOG_LINE
    return f"{shown} and {more} more" if more > 0 else shown


def _log_report(company_id: str, report: Report) -> None:
    for table, outcomes in sorted(report.unresolved_units.items()):
        for outcome, ids in sorted(outcomes.items()):
            logger.warning(
                "propertyware sync %s: %d %s row(s) %s, so no unit is recorded: %s",
                company_id,
                len(ids),
                table,
                _UNRESOLVED_WORDS[outcome],
                _ids(ids),
            )
    if report.not_tenants:
        logger.warning(
            "propertyware sync %s: %d contact(s) typed other than TENANT came back for the "
            "tenant read and were left out (was the type filter ignored?)",
            company_id,
            report.not_tenants,
        )
    if report.credits:
        logger.info(
            "propertyware sync %s: %d vendor credit(s) left out of the bills",
            company_id,
            report.credits,
        )
    if report.balances_unknown:
        logger.warning(
            "propertyware sync %s: balances STALE, %d lease(s) without a readable arBalance: %s",
            company_id,
            len(report.balances_unknown),
            _ids(report.balances_unknown),
        )
    if report.aging_unknown:
        logger.info(
            "propertyware sync %s: aging unknown for %d owing lease(s): %s",
            company_id,
            len(report.aging_unknown),
            _ids(report.aging_unknown),
        )


async def _start_turnovers(
    pool: Any, company_id: str, leases: list[dict[str, Any]], today: date
) -> int:
    """Open a turnover (checklist row and desk card) for each lease whose
    move-out date is new or moved, and is no older than
    :data:`TURNOVER_LOOKBACK_DAYS` in the company's calendar. Returns how many
    were opened.

    Rentvine's polled path (``turnovers_repo.start_turnover(only_if_new=True)``):
    a pass that sees an unchanged move-out touches nothing, so a turnover the
    manager dismissed stays dismissed. Best-effort per lease: a turnover is
    never the reason a pass fails. Runs after the leases are written, because
    ``start_turnover`` reads each lease's property and unit from the mirror.
    """
    from helixis.db.repositories import turnovers as turnovers_repo

    earliest = today - timedelta(days=TURNOVER_LOOKBACK_DAYS)
    opened = 0
    for lease in leases:
        lease_id = mapper.record_id(lease)
        move_out = mapper.lease_move_out(lease)
        if lease_id is None or move_out is None or move_out < earliest:
            continue
        try:
            if await turnovers_repo.start_turnover(
                pool,
                company_id=company_id,
                lease_id=lease_id,
                move_out_date=move_out,
                pms=PMS,
                only_if_new=True,
            ):
                opened += 1
        except Exception:  # noqa: BLE001 — best-effort, logged, never fails the pass
            logger.exception(
                "propertyware sync %s: turnover for lease %s failed", company_id, lease_id
            )
    return opened


async def _still_connected(pool: Any, creds: credentials.PropertywareCredentials) -> bool:
    """Whether the company still holds the credentials this pass read with.

    ⚠ A pass reads for minutes. A disconnect in that time deletes the row and
    purges the mirror (``api/routes/propertyware.py``), and writing this pass's
    rows afterwards would put the purged data back. New keys saved in that time
    may be another Propertyware account's, so rows read with the old ones are
    not written either. Checked just before the write, which narrows the window
    to the write itself: nothing the shared writer holds stops a purge that
    lands mid-write.
    """
    try:
        now = await credentials.load_credentials(pool, creds.company_id)
    except credentials.PropertywareCredentialsError:
        return False
    return now == creds


async def ask_for_full_pass(pool: Any, company_id: str) -> bool:
    """Make this company's next pass read everything whole: what Sync now
    asks for (``api/routes/propertyware.py``).

    A person who clicks Sync now means "read it all again": the counts the
    page shows are then the account's, never the changes since the last pass
    (a window's "0 buildings" would read as having none, rule 14), and a
    record deleted in Propertyware leaves the mirror now, not at the nightly
    reconcile. Stored as a marker, so it holds whichever process runs the
    pass. Best-effort: ``False`` (logged) when it could not be stored, and the
    pass then runs as its markers say.
    """
    try:
        await Watermark(pool, company_id, resource(FULL_ASKED_MARKER)).advance(_now())
    except Exception:  # noqa: BLE001 — logged; Sync now still syncs
        logger.warning(
            "propertyware sync %s: could not record the full read asked for; the next "
            "pass runs as its markers say",
            company_id,
            exc_info=True,
        )
        return False
    return True


#: The mirror's units for this company, for an incremental pass's unit checks.
_MIRROR_UNITS_SQL = (
    "SELECT buildium_id, property_id FROM helixis_units WHERE company_id = $1::uuid AND pms = $2"
)


async def _mirror_units(pool: Any, company_id: str) -> dict[int, int | None] | None:
    """``{unit id: building id}`` from the mirror, or ``None`` when unreadable
    (the window's own units are then all there is)."""
    try:
        rows = await pool.fetch(_MIRROR_UNITS_SQL, company_id, PMS)
    except Exception:  # noqa: BLE001 — logged; the window's units still check
        logger.warning(
            "propertyware sync %s: could not read the mirror's units", company_id, exc_info=True
        )
        return None
    known: dict[int, int | None] = {}
    for row in rows:
        unit, building = row["buildium_id"], row["property_id"]
        if unit is not None:
            known[int(unit)] = int(building) if building is not None else None
    return known


def _tables_of(path: str) -> tuple[str, ...]:
    """The tables a list's watermark waits on: every one it feeds but the
    balance snapshot, which is replaced whole on its own schedule."""
    return tuple(t for t, source in SOURCE.items() if source == path and t != BALANCES)


def clean_lists(reads: Mapping[str, ResourceRead], result: SyncResult) -> list[str]:
    """The lists whose watermark may move: the read complete, and every table
    it feeds written and not stale (``pms.watermarks``: "advance only after a
    clean pass"). The charges carry none (:data:`WHOLE_PASS_ONLY`)."""
    return [
        path
        for path, read in reads.items()
        if path not in WHOLE_PASS_ONLY
        and read.error is None
        and read.complete
        and all(t in result.rows_by_table and t not in result.failed for t in _tables_of(path))
    ]


def refused_lists(reads: Mapping[str, ResourceRead]) -> list[str]:
    """The lists Propertyware refused for the API key's permission."""
    return [path for path, read in reads.items() if read.refused]


def markers_may_move(reads: Mapping[str, ResourceRead], result: SyncResult) -> bool:
    """Whether the pass's markers move: every list read complete, or refused
    for the API key's permission, and every stale table one a refused list
    feeds.

    ⚠ A refusal reading again cannot fix: a key without one Read permission
    is refused on every pass, and holding the markers for it would make every
    pass read everything whole. Its tables stay stale with their reason and
    its list keeps its own watermark. Anything else (a 5xx, the budget, a
    partial read, a failed write or mapping) may pass by itself, and holds
    the markers until a pass gets through it.
    """
    refused = set(refused_lists(reads))
    if not all(r.refused or (r.error is None and r.complete) for r in reads.values()):
        return False
    return all(SOURCE.get(table) in refused for table in result.failed)


async def _move(pool: Any, company_id: str, name: str, to: datetime) -> bool:
    """Advance one watermark or marker. Best-effort: one left behind only
    makes the next pass read more."""
    try:
        await Watermark(pool, company_id, resource(name)).advance(to)
    except Exception:  # noqa: BLE001 — logged; never the reason a pass fails
        logger.warning(
            "propertyware sync %s: could not move %s; the next pass reads from where it was",
            company_id,
            name,
            exc_info=True,
        )
        return False
    return True


async def _advance(
    pool: Any,
    company_id: str,
    plan: Plan,
    reads: Mapping[str, ResourceRead],
    result: SyncResult,
) -> None:
    """Move what this pass earned to its start: each clean list's watermark,
    and the pass's markers when :func:`markers_may_move`."""
    moved = clean_lists(reads, result)
    for path in moved:
        await _move(pool, company_id, path, plan.start)
    may_move = markers_may_move(reads, result)
    if may_move and plan.kind.whole:
        if plan.kind is PassKind.FULL:
            await _move(pool, company_id, FULL_MARKER, plan.start)
        # A full read reconciles everything it reads, so it moves both.
        await _move(pool, company_id, RECONCILE_MARKER, plan.start)
    refused = refused_lists(reads)
    if may_move and not refused:
        ending = "ended clean"
    elif may_move:
        ending = (
            f"was refused only for the key's permission ({', '.join(refused)}), so its "
            "markers move and those tables stay stale"
        )
    elif plan.kind.whole:
        ending = "did NOT end clean, so the next pass reads everything whole again"
    else:
        ending = "did not end clean"
    held = sorted(set(reads) - set(moved) - WHOLE_PASS_ONLY)
    logger.info(
        "propertyware sync %s: %s pass %s; watermarks moved: %s; held: %s",
        company_id,
        plan.kind.value,
        ending,
        ", ".join(moved) or "none",
        ", ".join(held) or "none",
    )


async def sync_company(pool: Any, company_id: str, *, http: Any = None) -> SyncResult:
    """Mirror one company's Propertyware account. Returns what was written,
    what is stale and why (``SyncResult.failed``), and what was pruned.

    ⚠ Two gates that answer different questions (gotchas 72/73): the switch
    says whether this connector runs in this deployment at all, and the
    credentials row says whether THIS company is on Propertyware. The shared
    cycle checks the switch too; this is the belt for any other caller.

    ``http`` is for tests only (``client.build_client``); production passes
    nothing.
    """
    if not registry.enabled(PMS):
        raise PropertywareSyncRefused("the Propertyware connector is switched off")
    try:
        connection = await credentials.load_connection(pool, company_id)
    except credentials.PropertywareCredentialsError as exc:
        raise PropertywareSyncRefused(str(exc)) from None
    if connection is None:
        raise PropertywareSyncRefused(f"company {company_id} has no Propertyware connection")
    creds = connection.credentials

    plan = await plan_pass(pool, company_id, start=_now(), connection_changed=connection.changed_at)
    logger.info(
        "propertyware sync %s: %s pass (%s); %d list(s) by window, %d whole",
        company_id,
        plan.kind.value,
        plan.why,
        sum(1 for _p, w in plan.reads if w is not None),
        sum(1 for _p, w in plan.reads if w is None),
    )
    budget = client.CallBudget(CALL_BUDGET)
    pw = client.build_client(creds, http=http, budget=budget)
    try:
        reads = await _read_planned(pw, plan, budget)
    finally:
        await pw.aclose()
    unreached = [p for p, r in reads.items() if r.error == CALL_BUDGET_REACHED]
    if unreached:
        logger.warning(
            "propertyware sync %s: call budget reached (%d pages); stale, read first next pass: %s",
            company_id,
            budget.used,
            ", ".join(unreached),
        )

    known_units = await _mirror_units(pool, company_id) if reads["/units"].delta else None
    today = today_in(await company_timezone(pool, company_id))
    rows, report = mirror_rows(
        reads, today=today, now=_monotonic(), balances=plan.kind.whole, known_units=known_units
    )
    _log_report(company_id, report)
    if not await _still_connected(pool, creds):
        raise PropertywareSyncRefused(
            f"company {company_id} disconnected Propertyware or saved other keys while "
            "the sync read; nothing was written"
        )
    result = await write_mirror(pool, company_id, PMS, rows)
    await stamp_freshness(pool, result, SYNC_STATE_STAGE, source=SYNC_SOURCE)
    if "helixis_leases" in result.rows_by_table and "helixis_leases" not in result.failed:
        result.rows_by_table["helixis_turnovers"] = await _start_turnovers(
            pool, company_id, reads["/leases"].rows, today
        )
    # Last: a watermark moves only once everything this pass does with the
    # rows it read is done, turnovers included.
    await _advance(pool, company_id, plan, reads, result)
    return result


__all__ = [
    "BALANCES",
    "CALL_BUDGET",
    "CALL_BUDGET_REACHED",
    "DERIVED_TABLES",
    "FULL_ASKED_MARKER",
    "FULL_EVERY",
    "FULL_MARKER",
    "INCREMENTAL_READS",
    "MODIFIED",
    "PMS",
    "RECONCILE_EVERY",
    "RECONCILE_MARKER",
    "RUN_NOTE",
    "SOURCE",
    "SYNC_SOURCE",
    "SYNC_STATE_STAGE",
    "TABLE_WORDS",
    "TURNOVER_LOOKBACK_DAYS",
    "WHOLE_PASS_ONLY",
    "CustomerReason",
    "PassKind",
    "Plan",
    "PropertywareSyncRefused",
    "Report",
    "ResourceRead",
    "ask_for_full_pass",
    "choose_pass",
    "clean_lists",
    "markers_may_move",
    "mirror_rows",
    "newest_per_id",
    "plan_pass",
    "query_for",
    "read_failure",
    "refused_lists",
    "resource",
    "stale_reason",
    "sync_company",
    "window_query",
]
