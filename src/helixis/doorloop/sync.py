"""Read DoorLoop, write the mirror: one company's pass.

THE PASS
    :func:`sync_company` refuses unless the deployment switch is on
    (``pms.registry.enabled``) and THIS company has a key (no env fallback,
    gotcha 70). It reads every list in :data:`READS` whole, one page at a time
    and within :data:`CALL_BUDGET`, maps the rows with the pure ``mapper``,
    checks the company still holds the key it read with (a disconnect during
    the reads must not be undone by the write), hands the result to the shared
    writer (``pms.mirror.write_mirror``), and stamps the stages that came
    through clean (``pms.mirror.stamp_freshness``).

ALWAYS WHOLE
    No DoorLoop list filters by modified time, and only a task carries one
    (design, "How do we hear about changes?"). So every pass reads every list
    whole, and the writer prunes what a complete read no longer lists. With
    1,000 rows a page, a list of 20,000 rows is 20 requests.

THE CALL BUDGET (rule 13)
    DoorLoop allows a key 500 requests an hour (the Rate Limiting page). A pass
    asks for at most :data:`CALL_BUDGET` pages, so one pass leaves a fifth of
    the hour, above the client's quota hold (a tenth). A pass that reaches the
    budget stops: every list it did not finish is stale with
    :data:`CALL_BUDGET_REACHED`. A second pass in the same hour (a Sync now
    after the cycle's) meets the quota hold or DoorLoop's 429, and the lists
    it had not read go stale there; nothing retries past what DoorLoop allows.
    The lists are read in :data:`READS`' order, the ledger last: an account
    too large for one pass keeps its records fresh and its ledger stale,
    never the other way round.

STALE, NEVER EMPTY
    A list whose read fails is reported stale (``TableRead(error=...)``) and
    the pass goes on with the others; the writer keeps that table's rows as
    they were and does not stamp it. A read that stopped short of DoorLoop's
    own ``total`` is written, not stamped, and not pruned from. Each stale
    reason is a sentence for the customer (:func:`read_failure`,
    :func:`stale_reason`); the status code and DoorLoop's text go to the log.

WHAT DEPENDS ON WHAT
    * A unit's occupancy comes from the leases read (``mapper.Occupancy``):
      from a failed or partial one it is left as it was.
    * Owners' properties come from the properties read, only when complete.
    * The ledger (``helixis_lease_transactions``) is replaced per lease, so it
      is written only when all four of its reads (charges, credits, payments,
      reversed payments) came back complete: one lease's ledger with its
      payments missing would read "no payments". For the same reason it is
      not written when any of its rows has no id of its own: the shared
      writer would replace that lease's ledger without it (DL-11).
    * The balance snapshot is the leases' ``totalBalancedue``; its aging needs
      a complete charges read, or every bucket is unknown.
    * One tasks read fills both work orders and the other tasks; one tenants
      read fills both tenants and applicants (prospects who have applied).
    * A bill's work order comes from the tasks read, only when complete; a
      bill's lines are replaced per bill, from the bill itself.
    * Turnovers open after the write, from the lease tenants' move-out dates,
      only when the leases and every lease's tenants came through whole: a
      lease whose staying tenant was not read would look vacated.
    * A table whose ids collide (two DoorLoop ids, one mirror id) is stale,
      never written (``mapper.collisions``).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from helixis.common.company_clock import company_timezone, today_in
from helixis.common.support import contact_support
from helixis.doorloop import client, credentials, mapper
from helixis.pms import registry
from helixis.pms.errors import Failure, PmsApiError
from helixis.pms.http import PmsHttpClient
from helixis.pms.mirror import SyncResult, TableRead, stamp_freshness, write_mirror
from helixis.pms.paging import read_all

logger = logging.getLogger(__name__)

PMS = client.PMS

#: What this connector stamps ``helixis_mirror_sync_state.source`` with.
SYNC_SOURCE = "doorloop_sync"

#: Mirror table -> the freshness stage it is stamped under: Rentvine's stage
#: names for the same tables. Every table this pass fills is here, and nothing
#: it does not fill (``pms_coverage`` reads this map as the tables DoorLoop
#: feeds).
#:
#: ⚠ A plain ``dict`` on purpose: ``pms_coverage._stage_map`` reads anything
#: else as "fills nothing".
SYNC_STATE_STAGE: dict[str, str] = {
    "helixis_properties": "properties",
    "helixis_units": "units",
    "helixis_leases": "leases",
    # Shares the leases stage: a stage is stamped only when every table on it
    # came through clean.
    "helixis_lease_tenants": "leases",
    "helixis_tenants": "tenants",
    "helixis_owners": "owners",
    "helixis_vendors": "vendors",
    "helixis_work_orders": "work_orders",
    "helixis_tasks": "tasks",
    "helixis_bills": "bills",
    # Shares the bills stage, as Rentvine's and Propertyware's do.
    "helixis_bill_lines": "bills",
    "helixis_gl_accounts": "gl_accounts",
    "helixis_applicants": "applicants",
    "helixis_lease_transactions": "transactions",
    "helixis_lease_balances": "balances",
}

#: Tables this pass fills that are not a mirror of one DoorLoop list: the
#: turnover checklist, opened from the lease tenants' move-out dates
#: (:func:`_start_turnovers`). Never stamped or pruned.
DERIVED_TABLES: frozenset[str] = frozenset({"helixis_turnovers"})

#: How far back a move-out may lie and still open a turnover (Rentvine's and
#: Propertyware's).
TURNOVER_LOOKBACK_DAYS = 30

#: Added to a DoorLoop run's input (``agents.run_pms.run_note_for``). It names
#: DoorLoop and only DoorLoop, says what is synced, and says a missing item is
#: "not available in Occupella yet", never zero or none.
RUN_NOTE = (
    "\n\n## This workspace's property-management system\n"
    "This workspace runs on DoorLoop, and you are its DoorLoop Agent. Answer from "
    "Occupella's synced DoorLoop data: properties, units (a unit is occupied when an "
    "active lease lists it), leases (dates, status, rent and deposits held), tenants and "
    "which tenant is on which lease (current, moved out, or not moved in yet), owners "
    "(names, and which properties each owns), vendors, work orders, tasks (to-dos and "
    "tenant and owner requests), applicants (prospects who have reached the application; "
    "earlier leads are not listed), vendor bills (paid or not, and what each was spent "
    "on), the chart of accounts, rent balances (who owes rent and how much), each lease's "
    "charges, payments and credits, and turnovers (the make-ready checklist for a unit "
    "whose tenants are leaving). If no owner is shown for a property, Occupella does not "
    "have its owner: say so, and never say the property has no owner. If a lease shows no "
    "unit, it covers several units or none Occupella could match: say so, and never say "
    "the lease has no unit. If a rent balance has no aging breakdown, Occupella could not "
    "work it out: give the total, and never call any part of it current. A bill line on "
    "no property is the company's own spend, never a property's. The record numbers "
    "Occupella shows are not DoorLoop's own record numbers, so never give one as a "
    "DoorLoop number. Occupella cannot change anything in DoorLoop yet: never offer to "
    "create or edit a work order, a task, a bill, a charge or a payment there. Not "
    "available in Occupella yet: expenses, vendor credits, owner statements, accounting "
    "reports, files, notes and communications. Say plainly that a missing item is not "
    "available in Occupella yet and can be checked in DoorLoop; never say it is zero, "
    "empty or none. Call the system DoorLoop and by no other name. Never tell the user "
    "to restart, re-ask, re-route or switch assistants: you are the assistant for this "
    "workspace and there is no other one to send them to. Tell the user to reconnect "
    "DoorLoop only when a tool's answer says to."
)

#: Every list a pass reads, in order: ``(name, path, filters)``. The ledger
#: comes last (see the module docstring, THE CALL BUDGET).
READS: tuple[tuple[str, str, Mapping[str, str]], ...] = (
    ("properties", "/properties", {}),
    ("units", "/units", {}),
    ("leases", "/leases", {}),
    ("lease_tenants", "/leases/tenants", {}),
    ("tenants", "/tenants", {}),
    ("owners", "/owners", {}),
    ("vendors", "/vendors", {}),
    # Every type in one read: work orders and the other tasks each take theirs.
    ("tasks", "/tasks", {}),
    ("accounts", "/accounts", {}),
    # Bills only: a vendor credit is not money owed (mapper.map_bill).
    ("vendor_bills", "/vendor-bills", {}),
    ("charges", "/lease-charges", {}),
    ("credits", "/lease-credits", {}),
    ("payments", "/lease-payments", {}),
    ("reversed_payments", "/lease-reversed-payments", {}),
)

#: The reads the ledger is made of, keyed as ``mapper.LEDGER_KINDS``.
LEDGER_READS: tuple[str, ...] = tuple(mapper.LEDGER_KINDS)

#: The most pages one pass asks for: under DoorLoop's 500 an hour, with a
#: hundred left (see the module docstring, THE CALL BUDGET). At
#: ``client.REQUESTS_PER_SEC`` (0.7 a second) 400 pages take about ten
#: minutes. 400 pages of 1,000 rows is 400,000 rows (an estimate of room, NOT
#: measured: no live account has been read).
CALL_BUDGET = 400

#: Each table this pass can report stale, in the words the sync status names
#: it with, in the order it names them.
TABLE_WORDS: dict[str, str] = {
    "helixis_properties": "properties",
    "helixis_units": "units",
    "helixis_leases": "leases",
    "helixis_lease_tenants": "which tenants are on which lease",
    "helixis_tenants": "tenants",
    "helixis_lease_balances": "rent balances",
    "helixis_lease_transactions": "charges, payments and credits",
    "helixis_owners": "owners",
    "helixis_vendors": "vendors",
    "helixis_work_orders": "work orders",
    "helixis_tasks": "tasks",
    "helixis_applicants": "applicants",
    "helixis_bills": "vendor bills",
    "helixis_bill_lines": "what each bill was spent on",
    "helixis_gl_accounts": "the chart of accounts",
}

#: Said after a failure that is likely to pass by itself.
_SYNC_AGAIN = "Sync again in a few minutes."

#: Said after a failure whose fix is Occupella's, not the customer's.
_IF_IT_KEEPS_HAPPENING = contact_support("if it keeps happening.")


class CustomerReason(str):
    """A stale reason this pass words for the customer itself.

    The shared writer passes a table's ``TableRead.error`` through as that
    table's stale reason, the same object, so :func:`stale_reason` tells this
    pass's sentences from the writer's own operator text by their type.
    """

    __slots__ = ()


def read_failure(exc: PmsApiError) -> CustomerReason:
    """Why a list's read failed, in words a person can act on (rule 16). The
    status and DoorLoop's own text stay in the log line beside it."""
    if exc.failure is Failure.CREDENTIALS:
        return CustomerReason(
            "DoorLoop rejected the saved API key. In DoorLoop, open Settings → Zapier & API "
            "Keys, copy the key again, and save it here."
        )
    if exc.failure is Failure.PERMISSION:
        return CustomerReason(
            "DoorLoop accepted the API key, but its access role can't read these. In "
            "DoorLoop, give the key a role that can, or create a key that has one, and save "
            "it here."
        )
    if exc.failure is Failure.UNAVAILABLE:
        return CustomerReason(
            f"DoorLoop kept failing, so Occupella paused calls to it. {_SYNC_AGAIN}"
        )
    # A 403 is DoorLoop's edge limiting this IP (the Rate Limiting page), the
    # same "slow down" as a 429 (``client.STATUS_MEANINGS``).
    if exc.status in (403, 429) or not exc.sent:
        return CustomerReason(f"DoorLoop asked Occupella to slow down. {_SYNC_AGAIN}")
    if exc.status == 0 or exc.status >= 500:
        return CustomerReason(f"DoorLoop did not answer. {_SYNC_AGAIN}")
    return CustomerReason(
        "DoorLoop answered with an error Occupella did not expect; " + _IF_IT_KEEPS_HAPPENING
    )


#: A list whose read raised something other than a vendor error (ours, logged).
_READ_CRASHED = CustomerReason(
    "Occupella hit a problem reading this from DoorLoop; " + _IF_IT_KEEPS_HAPPENING
)

#: A table whose rows Occupella could not map (a bug of ours, logged).
_MAPPING_FAILED = CustomerReason(
    "Occupella could not read what DoorLoop sent; " + _IF_IT_KEEPS_HAPPENING
)

#: Two DoorLoop records that would share one Occupella record number.
_ID_COLLISION = CustomerReason(
    "Two DoorLoop records would share one Occupella record number, so none of these was "
    "saved this time; " + _IF_IT_KEEPS_HAPPENING
)

#: Every list a pass did not finish because it reached :data:`CALL_BUDGET`.
CALL_BUDGET_REACHED = CustomerReason(
    "Occupella reached this sync's call budget for DoorLoop before it got to these. "
    "Sync again in an hour to read them."
)

#: The ledger, when one of its four reads did not come through whole.
_LEDGER_PART_WAY = CustomerReason(
    "DoorLoop did not send every charge, payment and credit this time, so the history "
    f"already on screen was kept. {_SYNC_AGAIN}"
)

#: The ledger, when a row came without an id of its own.
_LEDGER_NO_ID = CustomerReason(
    "DoorLoop sent charges, payments or credits Occupella could not tell apart, so the "
    "history already on screen was kept; " + _IF_IT_KEEPS_HAPPENING
)

#: The balance snapshot, when the leases did not come through whole.
_LEASES_PART_WAY = CustomerReason(
    "DoorLoop stopped sending the leases part-way through, so the balances already on "
    f"screen were kept. {_SYNC_AGAIN}"
)

#: The shared writer's own reasons, worded for the customer by their stable
#: first words (Propertyware's split).
_STOPPED_PART_WAY = f"DoorLoop stopped sending them part-way through. {_SYNC_AGAIN}"
_NOT_ALL_SAVED = (
    "Occupella could not save all of them this time. Sync again in a few minutes, and "
    + _IF_IT_KEEPS_HAPPENING
)


def stale_reason(reason: str) -> str:
    """A table's stale reason (``SyncResult.failed``) as the sync status says it."""
    if isinstance(reason, CustomerReason):
        return str(reason)
    if reason.startswith("incomplete read"):
        return _STOPPED_PART_WAY
    return _NOT_ALL_SAVED


class DoorLoopSyncRefused(Exception):  # noqa: N818 — a refusal, not a fault
    """The connector is switched off, or this company has no usable DoorLoop
    key. Nothing was read and nothing was written."""


@dataclass(frozen=True)
class ListRead:
    """One list's read: its rows and whether DoorLoop confirmed them whole,
    or why it is stale."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    complete: bool = False
    error: str | None = None
    started: float = 0.0


def _monotonic() -> float:
    return time.monotonic()


async def _read(dl: PmsHttpClient, path: str, filters: Mapping[str, str]) -> ListRead:
    """One list, whole. A failure is returned as stale, never raised."""
    started = _monotonic()
    try:
        got = await read_all(dl, path, strategy=client.PAGING, query=filters)
    except client.CallBudgetSpent:
        logger.warning("doorloop sync: %s stopped part-way: call budget reached", path)
        return ListRead(started=started, error=CALL_BUDGET_REACHED)
    except PmsApiError as exc:
        logger.warning("doorloop sync: %s failed; the table goes stale: %s", path, exc)
        return ListRead(started=started, error=read_failure(exc))
    except Exception:  # noqa: BLE001 — logged, and only this list goes stale
        logger.exception("doorloop sync: %s failed unexpectedly", path)
        return ListRead(started=started, error=_READ_CRASHED)
    if not got.complete:
        logger.warning(
            "doorloop sync: %s read %d of %s rows; written, not stamped, not pruned",
            path,
            len(got.rows),
            got.total if got.total is not None else "an unknown number of",
        )
    return ListRead(rows=got.rows, complete=got.complete, started=started)


async def read_lists(dl: PmsHttpClient, budget: client.CallBudget) -> dict[str, ListRead]:
    """Every list in :data:`READS`, in order, until the budget is spent; every
    list after that is stale with :data:`CALL_BUDGET_REACHED`."""
    reads: dict[str, ListRead] = {}
    for name, path, filters in READS:
        if budget.spent:
            reads[name] = ListRead(error=CALL_BUDGET_REACHED)
            continue
        reads[name] = await _read(dl, path, filters)
    return reads


def mirror_rows(reads: Mapping[str, ListRead], *, today: date, now: float) -> dict[str, TableRead]:
    """This pass's reads as ``pms.mirror.MirrorRows``. Pure apart from logging.

    ``today`` is the company's calendar date (aging); ``now`` is
    ``time.monotonic()`` just before the write, so each table's prune spares
    rows touched since its own read began.
    """
    out: dict[str, TableRead] = {}

    def done(read: ListRead, rows: list[dict[str, Any]], **extra: Any) -> TableRead:
        return TableRead(
            rows=rows,
            complete=read.complete,
            read_seconds=max(0.0, now - read.started),
            **extra,
        )

    def guarded(name: str, build: Callable[[], TableRead]) -> None:
        """``build()`` as the table's read; a mapping bug stales that table."""
        try:
            out[name] = build()
        except Exception as exc:  # noqa: BLE001 — a mapping bug stales one table, loudly
            logger.exception("doorloop sync: mapping %s failed (%s)", name, type(exc).__name__)
            out[name] = TableRead(error=_MAPPING_FAILED)

    def table(name: str, source: str, build: Callable[[ListRead], TableRead]) -> None:
        read = reads[source]
        if read.error is not None:
            out[name] = TableRead(error=read.error)
            return

        def checked() -> TableRead:
            clashing = mapper.collisions(read.rows)
            if clashing:
                logger.error(
                    "doorloop sync: %s ids collide in %s: %s", len(clashing), name, clashing
                )
                return TableRead(error=_ID_COLLISION)
            return build(read)

        guarded(name, checked)

    def kept(rows: list[dict[str, Any] | None]) -> list[dict[str, Any]]:
        return [row for row in rows if row is not None]

    leases_read = reads["leases"]
    props_read = reads["properties"]
    tasks_read = reads["tasks"]
    # What one table takes from another's read. A bug here leaves the column
    # as it was (``None`` is "this pass cannot say"), loudly.
    occupancy: mapper.Occupancy | None = None
    owned: dict[int, list[int]] | None = None
    bill_orders: dict[int, int] | None = None
    try:
        if leases_read.error is None:
            occupancy = mapper.Occupancy.from_leases(
                leases_read.rows, complete=leases_read.complete
            )
        if props_read.error is None and props_read.complete:
            owned = mapper.owners_by_id(props_read.rows)
        if tasks_read.error is None and tasks_read.complete:
            bill_orders = mapper.bill_work_orders(tasks_read.rows)
    except Exception as exc:  # noqa: BLE001 — logged; the columns are left as they were
        logger.exception(
            "doorloop sync: occupancy, ownership or bill work orders failed (%s)",
            type(exc).__name__,
        )

    table(
        "helixis_properties",
        "properties",
        lambda r: done(r, [mapper.map_property(x) for x in r.rows]),
    )
    table(
        "helixis_units", "units", lambda r: done(r, [mapper.map_unit(x, occupancy) for x in r.rows])
    )
    table("helixis_leases", "leases", lambda r: done(r, [mapper.map_lease(x) for x in r.rows]))
    table(
        "helixis_lease_tenants",
        "lease_tenants",
        lambda r: done(r, kept([mapper.lease_tenant_link(x) for x in r.rows])),
    )
    table(
        "helixis_tenants",
        "tenants",
        lambda r: done(r, kept([mapper.map_tenant(x) for x in r.rows])),
    )
    table(
        "helixis_owners", "owners", lambda r: done(r, [mapper.map_owner(x, owned) for x in r.rows])
    )
    table("helixis_vendors", "vendors", lambda r: done(r, [mapper.map_vendor(x) for x in r.rows]))
    table(
        "helixis_work_orders",
        "tasks",
        lambda r: done(r, kept([mapper.map_work_order(x) for x in r.rows])),
    )
    table("helixis_tasks", "tasks", lambda r: done(r, kept([mapper.map_task(x) for x in r.rows])))
    table(
        "helixis_applicants",
        "tenants",
        lambda r: done(r, kept([mapper.map_applicant(x) for x in r.rows])),
    )
    table(
        "helixis_gl_accounts",
        "accounts",
        lambda r: done(r, [mapper.map_gl_account(x) for x in r.rows]),
    )
    table(
        "helixis_bills",
        "vendor_bills",
        lambda r: done(r, [mapper.map_bill(x, bill_orders) for x in r.rows]),
    )
    table(
        "helixis_bill_lines",
        "vendor_bills",
        lambda r: done(
            r, [line for bill in r.rows for line in mapper.bill_lines(bill)], parents=_bill_ids(r)
        ),
    )
    guarded("helixis_lease_transactions", lambda: _ledger(reads, now))
    if leases_read.error is not None:
        out["helixis_lease_balances"] = TableRead(error=leases_read.error)
    else:
        guarded(
            "helixis_lease_balances",
            lambda: _balances(leases_read, reads["charges"], today, now),
        )
    return out


def _bill_ids(read: ListRead) -> frozenset[int]:
    """The bills a read covered. Each bill carries all of its own lines, so a
    partial read still covers, whole, every bill it did read."""
    return frozenset(i for i in (mapper.record_id(bill) for bill in read.rows) if i is not None)


def _ledger(reads: Mapping[str, ListRead], now: float) -> TableRead:
    """The ledger, replaced per lease, only from four complete reads."""
    parts = [reads[name] for name in LEDGER_READS]
    for part in parts:
        if part.error is not None:
            return TableRead(error=part.error)
    if not all(part.complete for part in parts):
        return TableRead(error=_LEDGER_PART_WAY)
    if mapper.collisions(row for part in parts for row in part.rows):
        return TableRead(error=_ID_COLLISION)
    rows: list[dict[str, Any]] = []
    for name in LEDGER_READS:
        rows.extend(r for r in (mapper.ledger_row(name, x) for x in reads[name].rows) if r)
    unkeyed = sum(1 for row in rows if row["buildium_id"] is None)
    if unkeyed:
        # @unverified DL-11 — that every ledger row carries its own "id".
        logger.error(
            "doorloop sync: %d ledger row(s) carry no usable id; the ledger is kept as it was",
            unkeyed,
        )
        return TableRead(error=_LEDGER_NO_ID)
    # Every lease's ledger was read whole, so every lease named here or in the
    # leases read ends with exactly these rows (none is a real answer).
    leases = {row["lease_id"] for row in rows} | {
        lease_id
        for lease_id in (mapper.record_id(x) for x in reads["leases"].rows)
        if lease_id is not None
    }
    earliest = min(part.started for part in parts)
    return TableRead(
        rows=rows,
        complete=True,
        read_seconds=max(0.0, now - earliest),
        parents=frozenset(leases),
    )


def _balances(leases: ListRead, charges: ListRead, today: date, now: float) -> TableRead:
    """The balance snapshot: replaced whole, so never from a partial leases
    read, and never with a lease whose balance did not come through (one left
    out would read as current)."""
    if not leases.complete:
        return TableRead(error=_LEASES_PART_WAY)
    usable = charges.error is None and charges.complete
    by_lease = mapper.open_charges_by_lease(charges.rows) if usable else {}
    rows: list[dict[str, Any]] = []
    unknown = 0
    for lease in leases.rows:
        lease_id = mapper.record_id(lease)
        open_charges = by_lease.get(lease_id, []) if usable and lease_id is not None else None
        picked = mapper.lease_balance(lease, open_charges, today)
        if picked is mapper.BALANCE_UNKNOWN:
            unknown += 1
        elif isinstance(picked, dict):
            rows.append(picked)
    if unknown:
        return TableRead(
            error=CustomerReason(
                f"DoorLoop sent {unknown} of {len(leases.rows)} leases without a balance, so the "
                f"balances already on screen were kept. {_SYNC_AGAIN}"
            )
        )
    return TableRead(rows=rows, complete=True, read_seconds=max(0.0, now - leases.started))


async def _start_turnovers(
    pool: Any, company_id: str, links: list[dict[str, Any]], today: date
) -> int:
    """Open a turnover (checklist row and desk card) for each lease every one
    of whose tenants has a move-out date (``mapper.lease_move_outs``), when
    that date is new or moved and no older than
    :data:`TURNOVER_LOOKBACK_DAYS` in the company's calendar. Returns how many
    were opened.

    Propertyware's and Rentvine's polled path
    (``turnovers_repo.start_turnover(only_if_new=True)``): an unchanged
    move-out touches nothing, so a turnover the manager dismissed stays
    dismissed. Best-effort per lease, never the reason a pass fails. Runs
    after the leases are written: ``start_turnover`` reads each lease's
    property and unit from the mirror.
    """
    from helixis.db.repositories import turnovers as turnovers_repo

    earliest = today - timedelta(days=TURNOVER_LOOKBACK_DAYS)
    opened = 0
    for lease_id, move_out in sorted(mapper.lease_move_outs(links).items()):
        if move_out < earliest:
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
            logger.exception("doorloop sync %s: turnover for lease %s failed", company_id, lease_id)
    return opened


def _landed(result: SyncResult, table: str) -> bool:
    """Whether ``table`` was written this pass and is not stale."""
    return table in result.rows_by_table and table not in result.failed


async def _still_connected(pool: Any, creds: credentials.DoorLoopCredentials) -> bool:
    """Whether the company still holds the key this pass read with.

    ⚠ A pass reads for minutes. A disconnect in that time deletes the row and
    purges the mirror (``api/routes/doorloop.py``), and writing this pass's
    rows afterwards would put the purged data back. A new key saved in that
    time may be another DoorLoop account's, so rows read with the old one are
    not written either.
    """
    try:
        now = await credentials.load_credentials(pool, creds.company_id)
    except credentials.DoorLoopCredentialsError:
        return False
    return now == creds


async def sync_company(pool: Any, company_id: str, *, http: Any = None) -> SyncResult:
    """Mirror one company's DoorLoop account. Returns what was written, what
    is stale and why (``SyncResult.failed``), and what was pruned.

    ⚠ Two gates that answer different questions (gotchas 72/73): the switch
    says whether this connector runs in this deployment at all, and the
    credentials row says whether THIS company is on DoorLoop. The shared cycle
    checks the switch too; this is the belt for any other caller.

    ``http`` is for tests only (``client.build_client``).
    """
    if not registry.enabled(PMS):
        raise DoorLoopSyncRefused("the DoorLoop connector is switched off")
    try:
        creds = await credentials.load_credentials(pool, company_id)
    except credentials.DoorLoopCredentialsError as exc:
        raise DoorLoopSyncRefused(str(exc)) from None
    if creds is None:
        raise DoorLoopSyncRefused(f"company {company_id} has no DoorLoop connection")

    budget = client.CallBudget(CALL_BUDGET)
    dl = client.build_client(creds, http=http, budget=budget)
    try:
        reads = await read_lists(dl, budget)
    finally:
        await dl.aclose()
    unreached = [name for name, read in reads.items() if read.error == CALL_BUDGET_REACHED]
    if unreached:
        logger.warning(
            "doorloop sync %s: call budget reached (%d pages); stale: %s",
            company_id,
            budget.used,
            ", ".join(unreached),
        )

    today = today_in(await company_timezone(pool, company_id))
    rows = mirror_rows(reads, today=today, now=_monotonic())
    if not await _still_connected(pool, creds):
        raise DoorLoopSyncRefused(
            f"company {company_id} disconnected DoorLoop or saved another key while the "
            "sync read; nothing was written"
        )
    result = await write_mirror(pool, company_id, PMS, rows)
    await stamp_freshness(pool, result, SYNC_STATE_STAGE, source=SYNC_SOURCE)
    # A lease whose staying tenant was not read would look vacated: turnovers
    # open only from whole leases and whole lease tenants.
    if _landed(result, "helixis_leases") and _landed(result, "helixis_lease_tenants"):
        result.rows_by_table["helixis_turnovers"] = await _start_turnovers(
            pool, company_id, reads["lease_tenants"].rows, today
        )
    logger.info(
        "doorloop sync %s: %d pages; %d rows written; stale: %s",
        company_id,
        budget.used,
        result.total_rows,
        ", ".join(sorted(result.failed)) or "none",
    )
    return result


__all__ = [
    "CALL_BUDGET",
    "CALL_BUDGET_REACHED",
    "DERIVED_TABLES",
    "LEDGER_READS",
    "PMS",
    "READS",
    "RUN_NOTE",
    "SYNC_SOURCE",
    "SYNC_STATE_STAGE",
    "TABLE_WORDS",
    "TURNOVER_LOOKBACK_DAYS",
    "CustomerReason",
    "DoorLoopSyncRefused",
    "ListRead",
    "mirror_rows",
    "read_failure",
    "read_lists",
    "stale_reason",
    "sync_company",
]
