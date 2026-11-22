"""One company's Rent Manager read pass: read, map, write the mirror.

What the shared layer asks of a connector (``pms.registry.connector_attr``):
:func:`sync_company`, :data:`SYNC_STATE_STAGE`, :data:`DERIVED_TABLES` and
:data:`RUN_NOTE`. The worker tick (``pms.sync_cycle``) finds them lazily.

THE PASS (design "Sync")
    1. Refuse unless the connector is switched on and THIS company has usable
       credentials (no env fallback anywhere: gotcha 70).
    2. Log in for the stored location and read ``/Locations/CurrentLocation``.
       A different location, or one that cannot be confirmed, writes NOTHING:
       every table is reported stale with the reason (design R2). Record ids
       may repeat across locations, and the mirror would overwrite one with
       the other in silence.
    3. Choose the pass from markers stored per company and per saved
       credential set (``pms.watermarks`` rows, :func:`pass_marker`):
       FULL when this connection has no clean full read, or its last one
       began 7 days ago or more, or Rent Manager's filters cannot be trusted
       for it, or Sync now asked for one since (:func:`ask_for_full_pass`:
       only a full pass's counts are totals); RECONCILE when its last clean
       reconcile began 24 hours ago or more; INCREMENTAL otherwise. Before
       the first pass that reads by change, the filter canary
       (``client.filters_honoured``): rows back for a filter that can match
       nothing turn reads by change off for the connection, and every pass
       says so in ``SyncResult.failed``.
    4. Read each collection, the oldest watermark first, within the pass's
       call budget (:data:`CALL_BUDGET`). A FULL pass reads each whole. Any
       other reads each by change (``UpdateDate`` at or after 10 minutes
       before its watermark and :data:`ZONE_MARGIN` more, the newest version
       of a record kept), except the lookups, which are always read whole,
       and on a RECONCILE pass :data:`WHOLE_ON_RECONCILE`; a RECONCILE pass
       adds a complete id-only read of each collection it reads by change,
       which says what Rent Manager no longer has. A read that fails is that
       table's error, one the budget did not reach is stale with "call budget
       reached", and the pass goes on.
    5. Map with the pure ``mapper`` into ``MirrorRows`` and write them with
       ``pms.mirror.write_mirror``: a failed table is STALE, never empty; an
       incomplete one is written, not stamped and not pruned; one read by
       change is written and stamped but pruned only from a whole id list;
       the balance snapshot and the deposits only from whole reads. Right
       before the write, the saved credentials must still be the ones this
       pass logged in with: a disconnect or a re-save while it read writes
       nothing.
    6. Open turnovers for new move-outs among the leases read, once they
       landed.
    7. Stamp each stage whose tables all came through clean.
    8. Move each collection's watermark that came through clean to when the
       pass began; and the pass's own marker when it read everything it could
       and each table it left stale was refused for permission
       (:func:`only_refusals`: a failure no re-read fixes must not make every
       pass a full one).
    9. If the connection was removed while it wrote, delete what it wrote
       (the disconnect's purge may have run before these rows landed).

NOT BUILT
    * ``helixis_lease_transactions``: charges, payments and credits are three
      id sequences in one table and RM-12 is open, so it stays unfilled and
      its readers decline, and the design's 60-day ``TransactionDate`` poll
      of them waits with it. Bill lines likewise (``mapper.map_bill``).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

from helixis.common.company_clock import company_timezone, today_in
from helixis.db.repositories import turnovers as turnovers_repo
from helixis.pms import registry
from helixis.pms.errors import Failure, PmsApiError, RefusedBeforeSendingError
from helixis.pms.http import PmsHttpClient, PmsResponse
from helixis.pms.mirror import SyncResult, TableRead, stamp_freshness, write_mirror
from helixis.pms.paging import MAX_PAGES, read_all
from helixis.pms.watermarks import Watermark, dedupe, since
from helixis.rentmanager import client as rm_client
from helixis.rentmanager import mapper
from helixis.rentmanager.credentials import (
    CredentialsUnusableError,
    fetch_row,
    load_credentials,
    version_of,
)
from helixis.rentmanager.purge import purge_rentmanager_mirror
from helixis.rentmanager.resources import RESOURCES, Resource

logger = logging.getLogger(__name__)

PMS = registry.RENTMANAGER
SYNC_SOURCE = "rentmanager_sync"

#: Mirror table -> the freshness stage it is stamped under, Rentvine's names
#: for the same tables. A PLAIN dict: ``pms_coverage`` reads anything else as
#: "fills nothing". This is what readers trust this connector to fill.
#: ⚠ Two tables share "leases": the stage is stamped only when both are clean.
SYNC_STATE_STAGE: dict[str, str] = {
    "helixis_properties": "properties",
    "helixis_units": "units",
    "helixis_tenants": "tenants",
    "helixis_leases": "leases",
    "helixis_lease_tenants": "leases",
    "helixis_owners": "owners",
    "helixis_vendors": "vendors",
    "helixis_work_orders": "work_orders",
    "helixis_bills": "bills",
    "helixis_gl_accounts": "gl_accounts",
    "helixis_lease_balances": "balances",
}

#: Filled but never stamped: Occupella's own records, opened from what the
#: mirror shows. A turnover opens when a lease gains a move-out date.
DERIVED_TABLES: frozenset[str] = frozenset({"helixis_turnovers"})

#: Mirror table -> the ONE resource its rows (and their ids) come from. Two id
#: sequences in one table collide under ``UNIQUE (company_id, buildium_id)``
#: and overwrite each other in silence (ADR 0010 R3-7). Balances are keyed by
#: the lease, though their amounts are the tenant account's.
TABLE_SOURCES: dict[str, str] = {
    "helixis_properties": "properties",
    "helixis_units": "units",
    "helixis_tenants": "tenants",
    "helixis_leases": "leases",
    "helixis_lease_tenants": "leases",
    "helixis_owners": "owners",
    "helixis_vendors": "vendors",
    "helixis_work_orders": "work_orders",
    "helixis_bills": "bills",
    "helixis_gl_accounts": "gl_accounts",
    "helixis_lease_balances": "leases",
}

#: How far back a move-out may lie and still open a turnover: a turnover is
#: for a unit being made ready now, not one re-let years ago (Rentvine's).
TURNOVER_LOOKBACK_DAYS = 30

#: The three passes (design "Sync"). FULL reads every collection whole;
#: RECONCILE reads by change and adds an id-only read that says what Rent
#: Manager no longer has; INCREMENTAL reads only what changed.
FULL, RECONCILE, INCREMENTAL = "full", "reconcile", "incremental"

#: A pass is FULL when the last clean full read of the connection began this
#: long ago or more (design R6: weekly, and on connect).
FULL_EVERY = timedelta(days=7)
#: A pass is RECONCILE when the last clean reconcile began this long ago or
#: more (design R6: nightly).
RECONCILE_EVERY = timedelta(hours=24)

#: Read whole on a RECONCILE pass, not by change. The balance snapshot and
#: each lease's deposit are worked out from every tenant account and every
#: lease, and a snapshot is replaced whole or not at all; and what changes
#: with the date alone (a lease turning past on its move-out, the links to
#: the tenants who live there now, a unit's occupancy) need not move a
#: record's ``UpdateDate``. So these are at most a day behind, never a week.
# @unverified RM-7 a change to a record's embedded data (an owner's
# ownerships, a contact's phone, a work order's links) moves that record's
# UpdateDate. Where it does not, the change reaches the mirror at the weekly
# full read. Settled by changing one such embed on the probe account and
# reading the record's UpdateDate before and after.
WHOLE_ON_RECONCILE: frozenset[str] = frozenset({"units", "tenants", "leases"})

#: How much further back than the shared 10 minutes (``pms.watermarks.LAG``)
#: a read by change starts. A filter time carries no zone (the format the
#: probe checks), and if Rent Manager reads it in its server's own zone, a
#: server behind UTC would skip that many hours of changes on every pass, in
#: silence, until the next whole read. UTC-12:00 is the furthest behind any
#: zone is, so 12 hours covers every server; the records read twice are kept
#: once (``_newest``) and written again unchanged, and they nearly always fit
#: on the one page a read sends anyway.
# @unverified RM-7 which zone Rent Manager reads a filter time in. Settled by
# the RM-7 read with the value in each zone; then this margin can go.
ZONE_MARGIN = timedelta(hours=12)

#: The most GETs one pass sends (rule 13). At design R6's one request a
#: second it is five minutes of reading: well inside the 4-hour cycle, and a
#: bound on what one pass holds the worker's tick for (``pms.sync_cycle``
#: runs up to 5 companies a tick, one after another). At 1,000 rows a page
#: (RM-5) it reads a portfolio of a few thousand units whole with room to
#: spare (an estimate: no real account's row counts have been seen). A pass
#: that reaches it stops; what it did not reach is stale, keeps its
#: watermark, and is read first by the next pass.
CALL_BUDGET = 300

#: The ``SyncResult.failed`` key for why a pass read everything when it would
#: otherwise have read by change. Not a table: no stage is stale for it, and
#: the Connectors page does not list it (``api/routes/rentmanager.py``).
INCREMENTAL_OFF = "incremental reads"

#: What a table the call budget did not reach is stale with.
BUDGET_REACHED = "call budget reached: not read this pass; the next pass reads it first"

#: Added to a Rent Manager run's input (``run_pms.run_note_for``). It names
#: Rent Manager and no other system (a run note that named the other system
#: was echoed back to a user, run_pms.RENTVINE_RUN_NOTE), lists what is
#: synced and what is not, and says a missing item is not available, never
#: zero. ``test_rentmanager_sync`` holds it to what this module fills.
RUN_NOTE = (
    "\n\n## This workspace's property-management system\n"
    "This workspace runs on Rent Manager, and you are its Rent Manager Agent. Answer "
    "from Occupella's synced Rent Manager data: properties, units (market rent, and "
    "whether each is occupied), leases (start and end dates, and whether each is "
    "active, past or future), "
    "tenants and which current tenant is on which lease, owners (names, and which "
    "properties each owns), vendors, work orders (with the status and priority names "
    "this workspace uses in Rent Manager), vendor bills (what is owed, to whom, and "
    "when it is due), the chart of accounts, rent balances (who owes and how much), "
    "and turnovers (the make-ready checklist for a unit with a move-out date). If no "
    "owner is shown for a property, Occupella does not have its owner: say so, and "
    "never say the property has no owner. Not available in Occupella yet: each "
    "lease's rent amount, rent charges and payments (what a lease was charged and "
    "paid, and when), how old a balance is where no aging is shown, spending by "
    "category or by property, tasks, bank account balances, applicants and owner "
    "statements. Say plainly that a missing item is not available in Occupella yet "
    "and can be checked in Rent Manager; never say it is zero, empty or none. The "
    "record numbers Occupella shows are not the tenant or work order numbers Rent "
    "Manager shows, so never give one as a Rent Manager number. Call the system Rent "
    "Manager and by no other name. Never tell the user to restart, re-ask, re-route or "
    "switch assistants: you are the assistant for this workspace and there is no other "
    "one to send them to. Tell the user to reconnect Rent Manager only when a tool's "
    "answer says to."
)


class RentManagerSyncRefused(Exception):  # noqa: N818 — a refusal, not a fault
    """Nothing was read: the connector is off, or this company has no usable
    Rent Manager connection. Raised before any call to Rent Manager."""


@dataclass(frozen=True)
class Read:
    """What one collection read produced."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    #: Rent Manager named the end of the collection (``Collection.complete``).
    complete: bool = False
    #: The read failed: everything built from it is stale.
    error: str | None = None
    #: What kind of failure that was (``pms.errors.Failure``), when Rent
    #: Manager's answer said: a permission refusal is one no re-read fixes.
    failure: Failure | None = None
    #: The rows are the records changed since the watermark, not the whole
    #: collection (``TableRead.delta``).
    delta: bool = False
    #: Every id Rent Manager has for the collection now, from a complete
    #: id-only read on a RECONCILE pass (``TableRead.keep_ids``), or ``None``.
    keep_ids: frozenset[int] | None = None


def _monotonic() -> float:
    """The read timer behind the prune window. A function so tests can pin it."""
    return time.monotonic()


def _now() -> datetime:
    """When a pass begins: what its markers are measured from and what its
    watermarks move to. A function so tests can pin it."""
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Which pass runs, and how each collection is read in it.
# ---------------------------------------------------------------------------


def pass_marker(kind: str, version: str) -> str:
    """The watermark row holding when the last clean ``kind`` pass of one
    saved credential set began (``credentials.version_of``). Saving the
    connection again starts it over: new details may name another location,
    whose ids collide with the old ones in silence, so they are read in full
    before anything is read by change."""
    return f"pass:{kind}@{version}"


#: The marker Sync now leaves (:func:`ask_for_full_pass`).
FULL_ASKED = "pass:full-asked"


async def ask_for_full_pass(pool: Any, company_id: str) -> None:
    """Make the next pass of ``company_id`` a full one, in whichever process
    runs it. Sync now, and the sync a passed test starts, ask for one: the
    Connectors page shows that pass's row counts, and only a full pass's are
    totals. A pass by change counts what changed, and "0 properties" would
    read as a portfolio with none (rule 14). The ask stands until a full pass
    that began after it moves the full marker."""
    await Watermark(pool, company_id, FULL_ASKED).advance(_now())


def filters_marker(honoured: bool, version: str) -> str:
    """The watermark row holding when the filter canary found Rent Manager's
    filters honoured (or ignored) for one saved credential set."""
    return f"filters:{'honoured' if honoured else 'ignored'}@{version}"


def choose_pass(
    now: datetime,
    *,
    full: datetime | None,
    reconciled: datetime | None,
    filters_off: bool,
    asked: datetime | None = None,
) -> str:
    """FULL, RECONCILE or INCREMENTAL, from when the last clean full read and
    the last clean reconcile began, whether filters can be trusted, and when a
    full pass was last asked for (:func:`ask_for_full_pass`)."""
    if filters_off or full is None or now - full >= FULL_EVERY:
        return FULL
    if asked is not None and asked > full:
        return FULL
    if reconciled is None or now - reconciled >= RECONCILE_EVERY:
        return RECONCILE
    return INCREMENTAL


#: How one collection is read on a pass.
WHOLE, BY_CHANGE = "whole", "by change"


def read_mode(kind: str, name: str, watermark: datetime | None) -> str:
    """WHOLE or BY_CHANGE for collection ``name`` on a ``kind`` pass. A
    collection with no watermark was never read cleanly: it is read whole."""
    if kind == FULL or watermark is None or not RESOURCES[name].incremental:
        return WHOLE
    if kind == RECONCILE and name in WHOLE_ON_RECONCILE:
        return WHOLE
    return BY_CHANGE


_NEVER = datetime.min.replace(tzinfo=UTC)


def read_order(watermarks: Mapping[str, datetime | None]) -> list[str]:
    """The collections in the order a pass reads them: the oldest watermark
    first (none at all first of all), so a pass the call budget stopped is
    followed by one that starts with what it did not reach; each lookup just
    before the work orders it names. Ties keep ``RESOURCES``' order."""
    lookups = [name for name, r in RESOURCES.items() if not r.incremental]
    keyed = sorted(
        (name for name, r in RESOURCES.items() if r.incremental),
        key=lambda name: watermarks.get(name) or _NEVER,
    )
    order: list[str] = []
    for name in keyed:
        if name == "work_orders":
            order.extend(lookups)
        order.append(name)
    return order


@dataclass(frozen=True)
class PassPlan:
    """What one pass will do, decided before it reads anything."""

    kind: str
    #: Each collection read by change: its watermark, or ``None`` before its
    #: first clean read.
    watermarks: Mapping[str, datetime | None]
    #: Why the pass reads everything when it otherwise would not.
    note: str | None = None


def _ignored_note(when: datetime) -> str:
    return (
        f"{rm_client.NAME} returned records for a filter that can match none "
        f"({rm_client.CANARY_FILTER}) on {when:%Y-%m-%d}, so its filters cannot be "
        "trusted for this connection: every pass reads everything. Saving the "
        "connection again checks once more."
    )


async def plan_pass(pool: Any, company_id: str, version: str, now: datetime) -> PassPlan:
    """The pass the stored markers call for (step 3 of the module docstring),
    before the canary."""

    async def mark(resource: str) -> datetime | None:
        return await Watermark(pool, company_id, resource).read()

    ignored = await mark(filters_marker(False, version))
    kind = choose_pass(
        now,
        full=await mark(pass_marker(FULL, version)),
        reconciled=await mark(pass_marker(RECONCILE, version)),
        filters_off=ignored is not None,
        asked=await mark(FULL_ASKED),
    )
    watermarks = {name: await mark(name) for name, r in RESOURCES.items() if r.incremental}
    return PassPlan(kind, watermarks, None if ignored is None else _ignored_note(ignored))


async def _trust_filters(
    pool: Any, company_id: str, version: str, client: PmsHttpClient, plan: PassPlan, now: datetime
) -> PassPlan:
    """``plan``, unless it reads by change and Rent Manager's filters are not
    yet known to work: then the canary, once per saved credential set."""
    if plan.kind == FULL:
        return plan
    honoured = Watermark(pool, company_id, filters_marker(True, version))
    if await honoured.read() is not None:
        return plan
    try:
        works = await rm_client.filters_honoured(client)
    except (PmsApiError, CallBudgetReached) as exc:
        logger.warning("rentmanager sync %s: the filter check failed — %s", company_id, exc)
        return replace(
            plan,
            kind=FULL,
            note=(
                f"the check that {rm_client.NAME} applies a filter could not be made ({exc}), "
                "so this pass reads everything; it is made again on the next pass"
            ),
        )
    if works:
        await honoured.advance(now)
        logger.info("rentmanager sync %s: Rent Manager applies filters", company_id)
        return plan
    await Watermark(pool, company_id, filters_marker(False, version)).advance(now)
    logger.warning(
        "rentmanager sync %s: Rent Manager ignored a filter that can match nothing; every "
        "pass for this connection reads everything",
        company_id,
    )
    return replace(plan, kind=FULL, note=_ignored_note(now))


# ---------------------------------------------------------------------------
# The call budget (rule 13).
# ---------------------------------------------------------------------------


class CallBudgetReached(Exception):  # noqa: N818 — a stop, not a fault
    """The pass has sent every GET its budget allows; this one was not sent."""


@dataclass
class CallBudget:
    """How many GETs one pass may still send."""

    limit: int
    spent: int = 0

    @property
    def left(self) -> int:
        return max(0, self.limit - self.spent)


class _Budgeted:
    """The pass's client as its reads see it: each GET is counted against the
    pass's budget and refused, unsent, once it is spent.

    It counts at the client because only the client sees every request: a
    read whose total moved is read a second time (``pms.paging.read_all``).
    The shared client's own retries and its one re-login happen inside each
    counted GET, bounded there.
    """

    def __init__(self, client: PmsHttpClient, budget: CallBudget) -> None:
        self._client = client
        self._budget = budget

    async def get(
        self,
        template: str,
        *,
        path_params: Mapping[str, Any] | None = None,
        query: Mapping[str, Any] | None = None,
    ) -> PmsResponse:
        if self._budget.left <= 0:
            raise CallBudgetReached(template)
        self._budget.spent += 1
        return await self._client.get(template, path_params=path_params, query=query)


def _counted(client: PmsHttpClient, budget: CallBudget) -> PmsHttpClient:
    """``client``, every GET counted against ``budget``. Handed only to reads
    that call nothing but ``get`` (``read_all``, ``current_location_id``,
    ``filters_honoured``): the stand-in has no other method."""
    return cast(PmsHttpClient, _Budgeted(client, budget))


async def sync_company(pool: Any, company_id: str) -> SyncResult:
    """Mirror one company's Rent Manager records (the read side).

    Raises :class:`RentManagerSyncRefused`, before any call, when the
    connector is switched off (the shared cycle checks too; this is the belt
    for every other caller) or this company has no usable credentials.
    """
    if not registry.enabled(PMS):
        raise RentManagerSyncRefused("the Rent Manager connector is switched off here")
    try:
        creds = await load_credentials(pool, company_id)
    except CredentialsUnusableError as exc:
        raise RentManagerSyncRefused(f"company {company_id}: {exc}") from exc
    if creds is None:
        raise RentManagerSyncRefused(f"company {company_id} has no Rent Manager connection")
    async with rm_client.build_client(creds) as client:
        return await run_pass(
            pool, company_id, client, location_id=creds.location_id, version=creds.version
        )


def refused_pass(company_id: str, reason: str) -> SyncResult:
    """A pass that wrote nothing: every table this connector fills is stale,
    with the reason (never empty: rule 14)."""
    logger.warning("rentmanager sync %s: nothing written — %s", company_id, reason)
    tables = [*SYNC_STATE_STAGE, *sorted(DERIVED_TABLES)]
    return SyncResult(company_id=company_id, pms=PMS, failed=dict.fromkeys(tables, reason))


async def saved_version(pool: Any, company_id: str) -> str | None:
    """The version of the credentials saved for ``company_id`` now
    (``credentials.version_of``), or ``None`` when none are saved."""
    row = await fetch_row(pool, company_id)
    return version_of(row) if row is not None else None


async def run_pass(
    pool: Any, company_id: str, client: PmsHttpClient, *, location_id: int, version: str
) -> SyncResult:
    """Steps 2-9 of the module docstring, with a client already built.
    ``version`` is the saved credential set the client logs in with."""
    began_at = _now()
    budget = CallBudget(CALL_BUDGET)
    counted = _counted(client, budget)
    try:
        current = await rm_client.current_location_id(counted)
    except (PmsApiError, CallBudgetReached) as exc:
        return refused_pass(company_id, f"could not confirm the Rent Manager location: {exc}")
    if current is None:
        return refused_pass(
            company_id, "could not confirm the Rent Manager location: its answer named none"
        )
    if current != location_id:
        return refused_pass(
            company_id,
            f"location mismatch: the login reads Rent Manager location {current}, and this "
            f"workspace is connected to location {location_id}",
        )
    plan = await plan_pass(pool, company_id, version, began_at)
    plan = await _trust_filters(pool, company_id, version, counted, plan, began_at)
    logger.info("rentmanager sync %s: a %s pass", company_id, plan.kind)
    today = today_in(await company_timezone(pool, company_id))
    began = _monotonic()
    reads, unreconciled = await _read_collections(counted, plan, budget)
    rows = build_mirror_rows(reads, today=today, read_seconds=_monotonic() - began)
    # ⚠ A pass reads for minutes (one request a second). A disconnect in that
    # time deletes the row and purges the mirror; writing now would put back
    # what the customer just removed. A re-save may name another account.
    try:
        current_version = await saved_version(pool, company_id)
    except Exception as exc:  # noqa: BLE001 — unconfirmed is a reason to write nothing
        return refused_pass(
            company_id,
            f"could not confirm the saved connection before writing: {type(exc).__name__}",
        )
    if current_version != version:
        return refused_pass(
            company_id,
            "the Rent Manager connection was removed or replaced while this pass read; "
            "nothing written",
        )
    result = await write_mirror(pool, company_id, PMS, rows)
    if "helixis_leases" in result.failed:
        # start_turnover reads the lease's property and unit back from the
        # mirror, so it waits for leases that landed.
        logger.info("rentmanager sync %s: no turnovers opened; the leases did not land", company_id)
    else:
        result.rows_by_table["helixis_turnovers"] = await _start_turnovers(
            pool, company_id, reads["leases"].rows, today
        )
    if plan.note is not None:
        result.failed[INCREMENTAL_OFF] = plan.note
    await stamp_freshness(pool, result, SYNC_STATE_STAGE, source=SYNC_SOURCE)
    await _advance(pool, company_id, version, plan, reads, result, unreconciled, began_at)
    logger.info(
        "rentmanager sync %s: the %s pass sent %d of its %d calls",
        company_id,
        plan.kind,
        budget.spent,
        budget.limit,
    )
    return await _unless_disconnected(pool, company_id, result)


#: The tables a collection's rows are written to: each must come through
#: before the collection's watermark moves. The balance snapshot is none of
#: them: it is replaced whole, from two collections, and has no watermark.
_WRITTEN_FROM: Mapping[str, tuple[str, ...]] = {
    name: tuple(
        table
        for table, source in TABLE_SOURCES.items()
        if source == name and table != "helixis_lease_balances"
    )
    for name in RESOURCES
}


def _came_through(name: str, read: Read, result: SyncResult) -> bool:
    """Read whole or by change, confirmed complete, and every table written
    from it landed clean: what a watermark may move on (``pms.watermarks``)."""
    tables = _WRITTEN_FROM[name]
    return (
        read.error is None
        and read.complete
        and bool(tables)
        and all(t in result.rows_by_table and t not in result.failed for t in tables)
    )


#: The reads each table is built from: it is stale when any of them failed.
_BUILT_FROM: Mapping[str, frozenset[str]] = {
    **{table: frozenset({source}) for table, source in TABLE_SOURCES.items()},
    "helixis_work_orders": frozenset(
        {"work_orders", "work_order_statuses", "work_order_priorities"}
    ),
    "helixis_lease_balances": frozenset({"tenants", "leases"}),
}


def only_refusals(result: SyncResult, reads: Mapping[str, Read]) -> bool:
    """Whether every table this pass left stale was left stale by a read Rent
    Manager refused for permission (``Failure.PERMISSION``): a failure no
    re-read fixes, which must not hold every later pass to a full one. Such a
    table stays stale with its reason and keeps its own watermark. Anything
    else (an outage, the call budget, an incomplete read, a pass that read in
    full because filters cannot be trusted) holds the pass's markers."""
    refused = {name for name, read in reads.items() if read.failure is Failure.PERMISSION}
    return all(_BUILT_FROM.get(table, frozenset()) & refused for table in result.failed)


async def _advance(
    pool: Any,
    company_id: str,
    version: str,
    plan: PassPlan,
    reads: Mapping[str, Read],
    result: SyncResult,
    unreconciled: Mapping[str, bool],
    began_at: datetime,
) -> None:
    """Move what this pass earned, and nothing else: each collection that
    came through, to when the pass began; the pass's own marker only when it
    read everything it could and every table it left stale was refused for
    permission (a FULL pass is a reconcile and more)."""
    for name, read in reads.items():
        if RESOURCES[name].incremental and _came_through(name, read, result):
            await Watermark(pool, company_id, name).advance(began_at)
    if not only_refusals(result, reads):
        return
    if plan.kind == FULL:
        kinds: tuple[str, ...] = (FULL, RECONCILE)
    elif plan.kind == RECONCILE and all(unreconciled.values()):
        kinds = (RECONCILE,)
    else:
        return
    for kind in kinds:
        await Watermark(pool, company_id, pass_marker(kind, version)).advance(began_at)


async def _unless_disconnected(pool: Any, company_id: str, result: SyncResult) -> SyncResult:
    """``result``, unless the connection was removed while this pass wrote.

    The disconnect deletes the row and then purges, so a pass that checked
    before the delete and wrote after the purge would leave Rent Manager data
    behind a disconnect. Then this purges again (idempotent) and reports a
    pass that wrote nothing. A check that cannot be made keeps the result: the
    rows were written for a connection that existed when the write began.

    A re-save that lands during the write keeps what was written: purging
    would empty the portfolio for a password change, and the pass the
    re-save's test starts rewrites every table (a complete read prunes rows
    the new details do not have).
    """
    try:
        if await saved_version(pool, company_id) is not None:
            return result
        await purge_rentmanager_mirror(pool, company_id)
    except Exception:  # noqa: BLE001 — logged; never the reason a pass crashes
        logger.exception("rentmanager sync %s: the after-write disconnect check failed", company_id)
        return result
    return refused_pass(
        company_id,
        "the Rent Manager connection was removed while this pass wrote; what it wrote was "
        "deleted again",
    )


async def _read_collections(
    client: PmsHttpClient, plan: PassPlan, budget: CallBudget
) -> tuple[dict[str, Read], dict[str, bool]]:
    """Every collection, read as ``plan`` says (step 4 of the module
    docstring); and, on a RECONCILE pass, each collection it could not
    reconcile, with whether that was Rent Manager refusing a read for
    permission."""
    reads: dict[str, Read] = {}
    unreconciled: dict[str, bool] = {}
    for name in read_order(plan.watermarks):
        resource = RESOURCES[name]
        watermark = plan.watermarks.get(name)
        start = since(watermark)
        if read_mode(plan.kind, name, watermark) == WHOLE or start is None:
            reads[name] = await _read(client, resource, resource.query(), budget)
            continue
        query = resource.changed_query(start - ZONE_MARGIN)
        read = await _read(client, resource, query, budget)
        read = replace(read, rows=_newest(resource, read.rows), delta=True)
        if plan.kind == RECONCILE:
            if read.error is None:
                keep, refused = await _read_ids(client, name, resource, budget)
            else:
                keep, refused = None, read.failure is Failure.PERMISSION
            if keep is None:
                unreconciled[name] = refused
            read = replace(read, keep_ids=keep)
        reads[name] = read
    return reads, unreconciled


#: Ids an id-only read lists that the mirror leaves out all the same, by
#: collection: the reconcile must not keep them either.
_NOT_MIRRORED: Mapping[str, Callable[[dict[str, Any]], bool]] = {
    "bills": mapper.bill_is_reversed,
}


async def _read_ids(
    client: PmsHttpClient, name: str, resource: Resource, budget: CallBudget
) -> tuple[frozenset[int] | None, bool]:
    """Every id Rent Manager has for ``resource`` now, or ``None`` when that
    cannot be stated: the read failed or was not confirmed whole, or a row
    came without a usable id. ``None`` prunes nothing. Second: whether a
    missing list was Rent Manager refusing the read for permission."""
    read = await _read(client, resource, resource.ids_query(), budget)
    if read.error is not None or not read.complete:
        return None, read.failure is Failure.PERMISSION
    left_out = _NOT_MIRRORED.get(name)
    ids: set[int] = set()
    for row in read.rows:
        ident = mapper.record_id(row, resource.key)
        if ident is None:
            logger.warning(
                "rentmanager sync: the id-only read of %s listed a row without a usable id; "
                "nothing is pruned from it",
                resource.path,
            )
            return None, False
        if left_out is None or not left_out(row):
            ids.add(ident)
    return frozenset(ids), False


#: Where each row's change time rides while the versions are compared.
_CHANGED = "__changed_at"


def _newest(resource: Resource, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per record, its newest version (``pms.watermarks.dedupe``): the
    10 minutes a read starts behind its watermark come back twice, and a
    record changed during the read can come back in two versions. Compared
    as instants, never as text: two versions on either side of a change of
    UTC offset would otherwise pick the older."""
    tagged = [{**row, _CHANGED: mapper.changed_at(row)} for row in rows]
    newest = dedupe(tagged, id_key=resource.key, modified_key=_CHANGED)
    return [{k: v for k, v in row.items() if k != _CHANGED} for row in newest]


async def _read(
    client: PmsHttpClient, resource: Resource, query: Mapping[str, str], budget: CallBudget
) -> Read:
    """One collection read, whole or narrowed by ``query``, within the
    budget. A classified failure is the table's error; a request this
    connector should never have built is logged as the fault it is, and is
    that table's error too; a read the budget never reached is stale."""
    if budget.left <= 0:
        return Read(error=BUDGET_REACHED)
    try:
        got = await read_all(
            client,
            resource.path,
            strategy=rm_client.PAGING,
            query=query,
            max_pages=min(MAX_PAGES, budget.left),
        )
    except CallBudgetReached:
        return Read(error=BUDGET_REACHED)
    except PmsApiError as exc:
        logger.warning("rentmanager sync: %s failed — %s", resource.path, exc)
        return Read(error=str(exc), failure=exc.failure)
    except RefusedBeforeSendingError as exc:
        logger.error(
            "rentmanager sync: %s refused before sending, a connector fault: %s",
            resource.path,
            exc,
        )
        return Read(error=f"refused before sending: {exc}")
    if not got.complete:
        logger.warning(
            "rentmanager sync: %s read %d row(s), NOT confirmed complete (total %s)%s",
            resource.path,
            len(got.rows),
            got.total,
            "; the call budget ran out" if budget.left <= 0 else "",
        )
    return Read(rows=got.rows, complete=got.complete)


# ---------------------------------------------------------------------------
# Pure: reads -> MirrorRows.
# ---------------------------------------------------------------------------


def _unless_reversed(obj: dict[str, Any]) -> dict[str, Any] | None:
    return None if mapper.bill_is_reversed(obj) else mapper.map_bill(obj)


#: Tables whose rows are one mapped record each, from one resource.
_ONE_TO_ONE: dict[str, Callable[[dict[str, Any]], dict[str, Any] | None]] = {
    "helixis_properties": mapper.map_property,
    "helixis_units": mapper.map_unit,
    "helixis_tenants": mapper.map_tenant,
    "helixis_owners": mapper.map_owner,
    "helixis_vendors": mapper.map_vendor,
    "helixis_gl_accounts": mapper.map_gl_account,
    "helixis_bills": _unless_reversed,
}


def _source(reads: Mapping[str, Read], table: str) -> Read:
    return reads[TABLE_SOURCES[table]]


def _table(
    read: Read, rows: list[dict[str, Any]], read_seconds: float, *, complete: bool | None = None
) -> TableRead:
    """A collection's mapped rows as the writer takes them. Read by change,
    they are written and stamped, and pruned only from a whole id list."""
    return TableRead(
        rows=rows,
        complete=read.complete if complete is None else complete,
        read_seconds=read_seconds,
        delta=read.delta,
        keep_ids=read.keep_ids if read.delta else None,
    )


def build_mirror_rows(
    reads: Mapping[str, Read], *, today: date, read_seconds: float = 0.0
) -> dict[str, TableRead]:
    """Every table this connector fills, from one pass's reads. Pure.

    ``read_seconds`` runs from the FIRST read's start to the write, so a
    prune spares every row another writer touched while this pass ran.
    """
    out: dict[str, TableRead] = {}
    for table, map_row in _ONE_TO_ONE.items():
        read = _source(reads, table)
        if read.error is not None:
            out[table] = TableRead(error=read.error)
            continue
        rows = [row for raw in read.rows if (row := map_row(raw)) is not None]
        out[table] = _table(read, rows, read_seconds)
    out["helixis_work_orders"] = _work_orders(reads, read_seconds)
    leases = _source(reads, "helixis_leases")
    # The balance snapshot and the deposits are worked out from every tenant
    # account and every lease: a pass that read either by change leaves both
    # as they were (a snapshot is replaced whole or not at all).
    whole = not (leases.delta or _source(reads, "helixis_tenants").delta)
    if leases.error is not None:
        failed = ["helixis_leases", "helixis_lease_tenants"]
        failed += ["helixis_lease_balances"] if whole else []
        for table in failed:
            out[table] = TableRead(error=f"the lease read failed: {leases.error}")
        return out
    mapped = [mapper.map_lease(raw, today) for raw in leases.rows]
    current = mapper.current_leases(leases.rows, today) if whole else {}
    deposits = _deposits(reads, current) if whole else None
    if deposits is not None:
        for row in mapped:
            row["security_deposit"] = deposits.get(row["buildium_id"])
    links = [link for raw in leases.rows if (link := mapper.lease_tenant_link(raw, today))]
    out["helixis_leases"] = _table(leases, mapped, read_seconds)
    # Read by change, exactly the leases read have their links rewritten: one
    # that has no current tenant now loses its link, every other keeps its own.
    read_ids = (mapper.lease_id(raw) for raw in leases.rows) if leases.delta else ()
    out["helixis_lease_tenants"] = TableRead(
        rows=links,
        complete=leases.complete,
        read_seconds=read_seconds,
        delta=leases.delta,
        parents=frozenset(lid for lid in read_ids if lid is not None),
    )
    if whole:
        out["helixis_lease_balances"] = _balances(reads, mapped, current, today)
    return out


def _work_orders(reads: Mapping[str, Read], read_seconds: float) -> TableRead:
    """Work orders, their status and priority in the customer's own words.

    Both lookups must have been read: without them no work order has a
    status word, and a work order with none drops out of most "open work"
    readers. So a failed lookup is a stale table. A work order whose status id
    the lookup does not name is written WITHOUT its status column, so it keeps
    the last word it had, and the table is incomplete (not stamped).
    """
    issues = _source(reads, "helixis_work_orders")
    if issues.error is not None:
        return TableRead(error=issues.error)
    for lookup in ("work_order_statuses", "work_order_priorities"):
        failed = reads[lookup].error
        if failed is not None:
            return TableRead(
                error=f"the {RESOURCES[lookup].path} lookup failed, so the work orders "
                f"stay as they were: {failed}"
            )
    statuses = mapper.status_names(reads["work_order_statuses"].rows)
    priorities = mapper.priority_names(reads["work_order_priorities"].rows)
    rows: list[dict[str, Any]] = []
    unnamed = 0
    for raw in issues.rows:
        row = mapper.map_work_order(raw, statuses, priorities)
        if not mapper.work_order_status_known(raw, statuses):
            # Never blank a word the work order had with "no status".
            del row["status"]
            unnamed += 1
        rows.append(row)
    if unnamed:
        logger.warning(
            "rentmanager sync: %d work order(s) carry a status the statuses lookup does "
            "not name; written, NOT stamped",
            unnamed,
        )
    return _table(issues, rows, read_seconds, complete=issues.complete and not unnamed)


def _deposits(
    reads: Mapping[str, Read], current: Mapping[int, int]
) -> dict[int, Decimal | None] | None:
    """``{lease id: the deposit held}`` for each tenant account's current
    lease; every other lease reads ``None`` (unknown on that lease).

    ``None`` overall when the deposits cannot be placed this pass (either read
    failed or was not confirmed whole): the column is then left out of every
    lease row, so each lease keeps the deposit it had.
    """
    tenants, leases = _source(reads, "helixis_tenants"), _source(reads, "helixis_leases")
    if tenants.error or leases.error or not (tenants.complete and leases.complete):
        return None
    out: dict[int, Decimal | None] = {}
    for tenant in tenants.rows:
        tid = mapper.tenant_id(tenant)
        lid = current.get(tid) if tid is not None else None
        if lid is not None:
            out[lid] = mapper.tenant_deposit(tenant)
    return out


def _balances(
    reads: Mapping[str, Read],
    leases: list[dict[str, Any]],
    current: Mapping[int, int],
    today: date,
) -> TableRead:
    """The balance snapshot: one row per tenant account that owes, on its
    current lease. Readers count every row as a delinquent lease and an
    empty table as "everyone is current", so it is replaced only from a
    snapshot this pass can stand behind whole; otherwise the old one stands.
    """
    tenants = _source(reads, "helixis_tenants")
    lease_read = _source(reads, "helixis_lease_balances")
    for read, what in ((tenants, "tenant"), (lease_read, "lease")):
        if read.error is not None:
            return TableRead(error=f"the {what} read failed; the old snapshot stands")
        if not read.complete:
            return TableRead(
                error=f"the {what} read was not confirmed whole; the old snapshot stands"
            )
    by_id = {row["buildium_id"]: row for row in leases if row["buildium_id"] is not None}
    rows: list[dict[str, Any]] = []
    unknown = unplaced = 0
    for tenant in tenants.rows:
        total = mapper.tenant_balance(tenant)
        if total is None:
            unknown += 1
            continue
        if total <= 0:
            continue
        tid = mapper.tenant_id(tenant)
        lid = current.get(tid) if tid is not None else None
        lease = by_id.get(lid) if lid is not None else None
        if lease is None:
            unplaced += 1
            continue
        rows.append(mapper.balance_row(tenant, lease, total, today))
    problems = []
    if unknown:
        problems.append(f"{unknown} tenant account(s) came without a readable balance")
    if unplaced:
        problems.append(f"{unplaced} tenant account(s) that owe have no lease to put it on")
    if problems:
        return TableRead(error="; ".join(problems) + "; the old snapshot stands")
    return TableRead(rows=rows, complete=True)


async def _start_turnovers(
    pool: Any, company_id: str, leases: list[dict[str, Any]], today: date
) -> int:
    """Open a turnover for each lease whose move-out is new or moved, and no
    older than :data:`TURNOVER_LOOKBACK_DAYS`. Returns how many opened.

    The polled path of the shared trigger (``only_if_new=True``): an
    unchanged move-out touches nothing, so a turnover the manager dismissed
    stays dismissed. Best-effort per lease: never the reason a pass fails.
    """
    earliest = today - timedelta(days=TURNOVER_LOOKBACK_DAYS)
    opened = 0
    for obj in leases:
        lid, move_out = mapper.lease_id(obj), mapper.lease_move_out(obj)
        if lid is None or move_out is None or move_out < earliest:
            continue
        try:
            if await turnovers_repo.start_turnover(
                pool,
                company_id=company_id,
                lease_id=lid,
                move_out_date=move_out,
                pms=PMS,
                only_if_new=True,
            ):
                opened += 1
        except Exception:  # noqa: BLE001 — best-effort, logged, never fails the pass
            logger.exception("rentmanager sync %s: turnover for lease %s failed", company_id, lid)
    return opened


__all__ = [
    "BUDGET_REACHED",
    "BY_CHANGE",
    "CALL_BUDGET",
    "DERIVED_TABLES",
    "FULL",
    "FULL_ASKED",
    "FULL_EVERY",
    "INCREMENTAL",
    "INCREMENTAL_OFF",
    "PMS",
    "RECONCILE",
    "RECONCILE_EVERY",
    "RUN_NOTE",
    "SYNC_SOURCE",
    "SYNC_STATE_STAGE",
    "TABLE_SOURCES",
    "TURNOVER_LOOKBACK_DAYS",
    "WHOLE",
    "WHOLE_ON_RECONCILE",
    "ZONE_MARGIN",
    "CallBudget",
    "CallBudgetReached",
    "PassPlan",
    "Read",
    "RentManagerSyncRefused",
    "ask_for_full_pass",
    "build_mirror_rows",
    "choose_pass",
    "filters_marker",
    "only_refusals",
    "pass_marker",
    "plan_pass",
    "read_mode",
    "read_order",
    "refused_pass",
    "run_pass",
    "saved_version",
    "sync_company",
]
