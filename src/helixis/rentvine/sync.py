"""Read Rentvine, write the mirror. ADR 0010 Decision (a), Phase 4.

Flag-gated and per-company. Nothing here writes to Rentvine — the client it
uses has no write method at all.

⚠ **This has never run against a Rentvine server.** Every ``@unverified`` tag in
this module and in ``mapper.py``/``client.py`` names an assumption a single live
call would settle; Phase 8's job is to settle them, and
``grep -rn '@unverified' src/helixis/rentvine/`` is its work list.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import asyncpg

from helixis.common.company_clock import company_timezone, today_in
from helixis.db.repositories.mirror_sync_state import get_stage_sync, record_sync
from helixis.rentvine import mapper
from helixis.rentvine.client import RentvineAPIError, RentvineClient, RentvineTruncatedError
from helixis.rentvine.credentials import load_rentvine_settings_for_company, saved_version
from helixis.rentvine.purge import purge_rentvine_mirror
from helixis.settings import Settings, get_settings

logger = logging.getLogger(__name__)

#: Collections that page normally: ``page``/``pageSize`` declared, and
#: ``pagination-total-pages`` returned, so the loop follows the server's own
#: count rather than guessing.
SOURCES: tuple[tuple[str, str], ...] = (
    ("helixis_properties", "/properties"),
    ("helixis_tenants", "/tenants"),
    ("helixis_owners", "/owners"),
    ("helixis_vendors", "/vendors"),
    ("helixis_work_orders", "/maintenance/work-orders"),
    # The chart of accounts (2026-10-02): what a bill line's chargeAccountID
    # names, so spend can be grouped by category.
    ("helixis_gl_accounts", "/accounting/accounts"),
)

#: Slack on the prune's "untouched since this read began" test
#: (:func:`_prune_source`), in seconds.
#:
#: Another writer can put a row in a ``SOURCES`` table while this read runs:
#: the Operations page's Re-sync runs in the web process beside the worker's
#: poll (``sync_cycle._initial_running`` is per process), and the webhook
#: refresh writes single rows. Such a row exists in Rentvine and is missing
#: from this read's list only because the read began too early to see it. The
#: window is the read's own length on the app's monotonic clock, applied to the
#: database's ``now()``, so no two clocks are ever compared; this margin covers
#: the moment between measuring it and the DELETE starting.
PRUNE_TOUCH_MARGIN_S = 60.0

#: ⚠ **LEASES COME FROM THE EXPORT, AND RENT FROM ONE READ PER LEASE.**
#:
#: ``GET /leases`` declares no paging and the server sends a 25-row slice
#: (measured 2026-09-24: 25 of 28). It was kept for one reason, that it carried
#: ``rentAmount`` and the export does not — and that reason was never true:
#: **none of the 25 rows it returned carried a ``rentAmount`` key at all**
#: (measured 2026-10-01 from the stored rows), although the spec declares one.
#:
#: So, from documented read-only operations only (terms rules 5/7; founder
#: decision 2026-10-01 in ``docs/decisions.md``):
#:
#: * :data:`LEASES_EXPORT_PATH` lists EVERY lease, paged, and — although the
#:   spec declares no response headers for it — the live server sends
#:   ``pagination-total-items`` / ``-total-pages`` on every page
#:   (``transport.MEASURED_PAGINATION_HEADERS``). ``iter_all`` reconciles
#:   against them, so "complete" is the server's fact, never our guess.
#: * :data:`LEASE_PATH` (``getLease``) supplies the rent, filled and non-zero on
#:   the lease measured. Read only for the leases :func:`_pick_rent_reads`
#:   selects, at most :data:`LEASE_RENT_READS_PER_SYNC` a pass.
#: * The export's own ``lease.tenants`` carries every tenant's ``contactID`` and
#:   ``isActive`` (all 28 leases, 35 entries, measured), so lease-tenant links
#:   cost no extra call.
LEASES_EXPORT_PATH = "/leases/export"
LEASE_PATH = "/leases/{leaseID}"

#: The export's declared default page size, which the server was measured to
#: honour (two pages of 25 and 3). A larger ask is unmeasured.
LEASES_EXPORT_PAGE_SIZE = 25

#: The most ``getLease`` calls one pass makes (founder, 2026-10-01). A pass
#: that needs more reads the first this many, keeps every other lease's stored
#: rent, reports leases INCOMPLETE and prunes nothing; the next pass continues.
LEASE_RENT_READS_PER_SYNC = 250

#: The key, inside ``helixis_leases.raw``, holding the UTC date this lease's
#: rent was last read. Every lease that is not Closed is re-read at most ONCE A
#: DAY, oldest-read first, inside :data:`LEASE_RENT_READS_PER_SYNC` (founder,
#: 2026-10-02). A Closed lease is read once and then only when it changes
#: (founder, 2026-10-03; :func:`_pick_rent_reads`).
#:
#: ⚠ THE DAILY RE-READ IS THE ONLY THING THAT CATCHES A RENT CHANGE. Measured
#: 2026-10-02: lease 6's rent charge moved $800 -> $825 and neither the
#: export's nor getLease's ``dateTimeModified`` moved (three readings over two
#: minutes). A payment does move it; a rent change does not, so the change
#: check never sees one. This replaced the weekly 1/7 rotation of 2026-10-01,
#: which also re-read the same seventh of the account on EVERY pass of its day
#: once the sync ran every 4 hours.
RENT_READ_ON = "occupella_rent_read_on"

#: The key, inside ``helixis_leases.raw``, holding the export's
#: ``dateTimeModified`` as it stood when this lease's rent was last read.
#: ``raw`` itself is refreshed from the export every pass; this key is carried
#: over until the next rent read, and comparing against it is how a changed
#: lease is found. ⚠ The export's own value, never ``getLease``'s: the two
#: routes report DIFFERENT timestamps for the same lease (measured 2026-10-01:
#: the export's was that morning, getLease's three weeks older).
RENT_READ_MARKER = "occupella_rent_read_at_modified"

#: The key, inside ``helixis_leases.raw``, saying this lease's TRANSACTIONS have
#: been read at least once (``getLease`` with ``includes=transactions``, the
#: same call that reads the rent). A lease without it is read first, ahead of
#: every rent re-read, so a mirror that existed before rent history was synced
#: fills in at the cap's pace rather than behind the daily re-read.
TXN_READ_MARKER = "occupella_transactions_read"

#: ⚠ **THE TRANSACTIONS SEARCH ONLY PICKS WHICH LEASES TO RE-READ.** Its
#: ``transaction`` record carries no ``leaseID`` (the spec's schema), and a
#: lease names no ledger, so a row cannot be tied to a lease without guessing
#: from its unit, and a unit has had other tenants. The money itself always
#: comes from ``getLease``, which is lease-scoped by construction.
#:
#: It is a BACKUP, not the primary signal. Measured live 2026-10-02 with a
#: control: posting a payment moved that lease's export ``dateTimeModified`` to
#: the minute it posted, while untouched leases kept theirs, so the lease's own
#: change check (:data:`RENT_READ_MARKER`) already catches a new payment. What
#: the search still adds, unmeasured, is the case that may not move it (a void,
#: say). So a failed or ignored search is LOGGED and never makes the history
#: stale: it would otherwise have held the rent readers back on any account
#: whose search the server did not filter.
TRANSACTIONS_SEARCH_PATH = "/accounting/transactions/search"

#: How far behind the last clean stamp the change search starts. Rentvine's
#: timestamps carry no zone in the spec, and a day covers any offset.
TXN_CHANGE_OVERLAP = timedelta(days=1)

#: The most pages the change search reads. Past this the pass treats the
#: change list as unknown (transactions STALE) rather than reading on.
TXN_CHANGE_MAX_PAGES = 20

#: ⚠ **UNITS ARE FETCHED PER PROPERTY, AND THAT IS DELIBERATE N+1.**
#: ``GET /properties/units/export`` is the bulk route and looks like the obvious
#: choice — but its ``unit_export`` schema carries **no ``propertyID``**. A unit
#: row with no property is not a cheaper unit row, it is a broken one: it breaks
#: the very relationship columns ADR 0010 Decision (a) exists to protect, and
#: `property_snapshot` would report a property with no units.
#:
#: So the per-property route is used even though it is one call per property.
#: The fan-out is bounded by the property count (66 on the largest account seen
#: here), it runs in a nightly sync rather than a chat turn, and the client's
#: concurrency gate bounds it.
#:
#: ✅ SETTLED 2026-09-23 by probe K against the `demoaccount41` sandbox, 25 rows
#: sampled: ``row_keys: ["property", "unit"]``. **The link IS on the row**, as a
#: SIBLING record rather than a column on the unit — so the paragraph above is
#: right about ``unit_export`` and the flat "the export drops propertyID"
#: reading of it is wrong. The N+1 is a choice now, not an impossibility.
#:
#: ⚠ WHAT STILL STANDS BETWEEN HERE AND ONE CALL, because it is not a one-line
#: swap and the naive version is a silent zero. ``ROW_ENVELOPE`` names ONE key
#: per collection and ``client._rows_from`` unwraps to it, so an export row read
#: that way is a unit with nothing to attach it to — and ``map_unit`` would find
#: no property, exactly the shape gotcha 158 produced. A collapse therefore
#: needs a client path that hands back the WHOLE row (both records), which is a
#: deliberate addition to a module whose one public read deliberately does not
#: offer it. Recorded rather than built: the envelope's own subject was wrong
#: until this same day (``MEASURED_SUBJECT``), so the route has not yet been
#: read correctly by anything even once.
UNITS_PATH = "/properties/{propertyID}/units"

#: Where the units a per-property read could not return come from.
#:
#: ✅ Founder decision, 2026-09-24 (``design/rentvine-terms-compliance.md``
#: §5a): the per-property route pages without declaring it (a 120-unit
#: property sent 20), and this export DOES declare ``page``/``pageSize``. It is
#: read only when a property came back short, and only the missing units are
#: taken from it. Their occupancy is the export's own ``isVacant`` flag, and
#: UNKNOWN (never "vacant") when the flag is missing
#: (``mapper.export_unit_occupied``).
UNITS_EXPORT_PATH = "/properties/units/export"

#: The spec's own declared default for the export. It sends no pagination
#: headers, so the loop's end is the full-page heuristic, which is only right
#: when the page size asked for is one the server honours. Completeness is
#: then checked per property against the total its own read reported.
UNITS_EXPORT_PAGE_SIZE = 25

#: How many per-property unit calls may fail while NONE has answered yet,
#: before the walk stops trying.
#:
#: ⚠ **THIS IS AN EVIDENCE RULE, NOT A TOLERANCE.** Once ONE property has
#: answered, the route works for this key and every later failure is that
#: property's own — so the walk runs to the end however many fail, because
#: abandoning the 26 properties behind a permanent 404 at property 40 is the
#: partial-table dishonesty gotcha 147 deferred. Until then, this many refusals
#: with nothing answering is taken as the ROUTE or the KEY being wrong rather
#: than the properties, and pressing on would spend one doomed call per property
#: — each carrying ``RETRY_BACKOFF_S`` if the status is retryable (gotcha 147) —
#: to learn what the third failure already said.
UNIT_FANOUT_EVIDENCE = 3

#: What this connector stamps `helixis_mirror_sync_state.source` with.
#:
#: The column is free text and nothing branches on it, so this is a label for
#: whoever reads the table rather than a discriminator — but it is the one
#: place a row says which connector wrote it, which is worth having when two
#: connectors write the same tables.
SYNC_SOURCE = "rentvine_sync"

#: The portfolio route the owner-to-property join reads (one paged read).
PORTFOLIOS_PATH = "/portfolios"

#: Every table read through a PAGED collection, so one whose end Rentvine did
#: not name is worth reporting (``sync_cycle._unconfirmed_paged_sources``).
#: Bills are paged but read outside ``SOURCES`` (one row per line, grouped).
PAGED_TABLES: frozenset[str] = frozenset(
    {t for t, _ in SOURCES} | {"helixis_bills", "helixis_applicants"}
)

#: The lease-export siblings that carry rent balances (``mapper.map_lease_balance``).
BALANCE_SIBLINGS = ("balances", "unpaidCharges")

#: Rental applications (``sync._sync_applicants``).
APPLICATIONS_PATH = "/screening/applications"

#: Where bills come from. ``GET /accounting/bills`` has no amount and no paid
#: flag; this search has both, one row per bill LINE (``mapper.group_payables``).
#: Read with NO filters: ``isPaid=false`` would be cheaper, but an open-only
#: read can never see a bill get paid, and a filter this container cannot
#: watch take effect would leave "complete" meaning nothing.
PAYABLES_PATH = "/accounting/payables/search"

#: The key in :attr:`SyncResult.failed` when the owner-to-property join could
#: not run to completion. NOT a mirror table: the owners themselves synced
#: fine, only their property links are stale, so it must not stop the owners
#: stage being stamped fresh (``_record_freshness`` reads table keys only).
OWNER_LINKS = "owner_property_links"

#: Columns an upsert must NOT overwrite on conflict, per table.
#:
#: ⚠ ``helixis_owners.property_ids`` is filled by the portfolio join AFTER the
#: owners are written, and only when that join is provably complete. If the
#: owners upsert overwrote it, every sync (and every single-record refresh)
#: would wipe the links to ``[]`` first, and a failed portfolio read would
#: leave them wiped. Written on INSERT (as ``[]``, which reads as "no owner
#: known", never "no owner"), preserved on UPDATE.
_PRESERVED_ON_UPDATE: dict[str, frozenset[str]] = {
    "helixis_owners": frozenset({"property_ids"}),
}

#: Mirror table → the bare stage name ``helixis_mirror_sync_state`` is keyed on.
#:
#: ⚠ **THE TWO VOCABULARIES ARE DIFFERENT AND THAT IS NOT COSMETIC.**
#: :class:`SyncResult` keys on the full ``helixis_*`` table name; 0057's table
#: and :data:`mirror_sync_state.CORE_TABLES` key on the bare stage name the
#: Buildium backfill has always written. Stamping ``helixis_properties`` there
#: would store a row no reader ever looks at — ``assess`` filters on
#: ``CORE_TABLES`` — so the mirror would go on being unable to report itself
#: stale while a table full of rows said otherwise. A guard reads BOTH sides
#: (gotcha 28): every mapper table appears here, and every stage name here is
#: one the freshness verdict actually considers.
SYNC_STATE_STAGE: dict[str, str] = {
    "helixis_properties": "properties",
    "helixis_units": "units",
    "helixis_leases": "leases",
    "helixis_tenants": "tenants",
    "helixis_owners": "owners",
    "helixis_vendors": "vendors",
    "helixis_work_orders": "work_orders",
    # ⚠ SHARES the leases stage: CORE_TABLES has no lease-tenants stage, and
    # adding one would change the Buildium staleness verdict. _record_freshness
    # stamps a stage only when EVERY table mapped to it is clean.
    "helixis_lease_tenants": "leases",
    "helixis_bills": "bills",
    # ⚠ SHARES the bills stage: the lines ride on the same payables read.
    "helixis_bill_lines": "bills",
    "helixis_lease_balances": "balances",
    # Reference data: the Buildium backfill stamps it under this name too, and
    # it is deliberately outside CORE_TABLES (see REFERENCE_STAGES).
    "helixis_gl_accounts": "gl_accounts",
    "helixis_applicants": "applicants",
    # The Buildium backfill stamps the same name for the same table.
    "helixis_lease_transactions": "transactions",
}

#: Stages that are stamped but sit OUTSIDE ``mirror_sync_state.CORE_TABLES``
#: on purpose: reference data that refreshes on its own cadence and would only
#: add noise to the staleness footer. The Buildium backfill stamps the same
#: names (``record_failure("gl_accounts", …)``), so one vocabulary holds.
REFERENCE_STAGES: frozenset[str] = frozenset({"gl_accounts", "applicants", "transactions"})

#: Tables the sync fills that are NOT a mirror of a Rentvine collection, so
#: they are neither stamped nor reconciled by Phase 8: Occupella's own
#: records, opened from what the mirror shows. ``pms_coverage.FED_BY`` adds
#: them to what Rentvine feeds.
#:
#: * ``helixis_turnovers``: the make-ready checklist, opened when an export
#:   lease gains a move-out date (:func:`_start_turnovers`). Nothing reads its
#:   absence as a fact, so it needs no stamp to be trusted.
DERIVED_TABLES: frozenset[str] = frozenset({"helixis_turnovers"})

#: How far back a move-out date may lie and still open a turnover. The first
#: sync of an account sees every past lease's move-out; a turnover is for a
#: unit being made ready now, not one that was re-let years ago.
TURNOVER_LOOKBACK_DAYS = 30

#: Columns each mirror table's upsert writes, beyond the ones every table gets
#: (``account_id``, ``company_id``, ``raw``, ``pms``). Taken from the INSERT
#: statements in ``db/repositories/entities.py``; a drift guard in
#: ``tests/unit/test_rentvine_sync.py`` reads BOTH sides so these cannot
#: disagree with the Buildium path (gotcha 28).
TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "helixis_properties": (
        "name",
        "rental_sub_type",
        "address_line1",
        "city",
        "state",
        "postal_code",
        "is_active",
    ),
    "helixis_units": ("property_id", "unit_number", "market_rent", "is_occupied"),
    "helixis_leases": (
        "property_id",
        "unit_id",
        "status",
        "lease_from_date",
        "lease_to_date",
        "rent",
        "security_deposit",
    ),
    "helixis_tenants": ("first_name", "last_name", "email", "phone"),
    "helixis_owners": (
        "first_name",
        "last_name",
        "company_name",
        "email",
        "phone",
        "property_ids",
    ),
    "helixis_vendors": (
        "name",
        "is_company",
        "first_name",
        "last_name",
        "company_name",
        "is_active",
        "category_id",
        "category_name",
        "phone",
        "email",
    ),
    # Written by ``_replace_balances``, not ``_upsert`` (the table has no
    # ``buildium_id``); listed so the drift guard holds it to the Buildium
    # INSERT. ``snapshot_at`` is written by the column default / ``now()``.
    "helixis_lease_balances": (
        "lease_id",
        "property_id",
        "unit_id",
        "total_balance",
        "balance_0_30",
        "balance_31_60",
        "balance_61_90",
        "balance_over_90",
        "snapshot_at",
    ),
    "helixis_bills": (
        "vendor_id",
        "bill_date",
        "due_date",
        "total_amount",
        "is_paid",
        "reference_number",
        "work_order_id",
    ),
    "helixis_applicants": (
        "first_name",
        "last_name",
        "email",
        "phone",
        "status",
        "applicant_group_id",
        "property_id",
        "unit_id",
        "tenant_id",
    ),
    "helixis_gl_accounts": (
        "account_number",
        "name",
        "type",
        "sub_type",
        "is_active",
        "is_bank_account",
        "parent_gl_account_id",
    ),
    "helixis_work_orders": (
        "task_id",
        "property_id",
        "unit_id",
        "title",
        "status",
        "priority",
        "category_id",
        "assigned_to_user_id",
        # Migration 0105. Read by the stalled counts (first_scan, morning_brief).
        "pms_updated_at",
    ),
}


#: ``helixis_leases`` columns for a lease whose rent was NOT read this pass:
#: everything but ``rent``, so the stored rent survives the upsert untouched.
LEASE_COLUMNS_KEEP_RENT = tuple(c for c in TABLE_COLUMNS["helixis_leases"] if c != "rent")


class RentvineSyncDisabled(Exception):  # noqa: N818 — "disabled" is the semantics
    """The connector is off, or this company has no Rentvine connection.

    ⚠ Deliberately NOT named ``*Error``. A Buildium customer having no Rentvine
    connection is the ordinary state of almost every company in the deployment,
    and a name ending in Error invites a caller to log it as a fault — which is
    how a canary nobody reads gets born (gotcha 84).
    """


@dataclass
class SyncResult:
    """What one sync did, in terms an operator can act on."""

    company_id: str
    rows_by_table: dict[str, int] = field(default_factory=dict)
    #: Rows the mapper produced with no usable identity. NEVER written.
    skipped_no_id: dict[str, int] = field(default_factory=dict)
    #: Tables whose fetch failed, with the reason. A failure here means the
    #: table's mirror is STALE, not empty — the distinction gotcha 106 exists
    #: for, and the caller must not report an unsynced table as "no records".
    failed: dict[str, str] = field(default_factory=dict)
    #: Tables whose collection Rentvine confirmed a total for, versus tables
    #: where the loop guessed the end from a short page.
    #:
    #: ⚠ **ABSENCE HAS THREE CAUSES AND ONLY ONE OF THEM IS A FAILURE.** A
    #: table is missing from this set when (a) its fetch failed — it is in
    #: :attr:`failed`, and its mirror is STALE rather than empty; (b) the
    #: paging loop stopped on a short page with no pagination headers, so the
    #: rows ARE mirrored and the count is simply not claimed to be the total;
    #: or (c) it is ``helixis_units``, which Rentvine cannot page at all, so
    #: nothing could establish completeness. (Leases COULD be confirmed, from
    #: the export's headers, and are claimed when they were.) Reading absence as failure would
    #: report a healthy sync as broken; reading it as completeness is the
    #: fabricated total this set exists to prevent.
    confirmed_complete: set[str] = field(default_factory=set)
    #: Rows this pass DELETED because Rentvine no longer has them, by table.
    #: Only ever set after a read the server confirmed complete: the lease
    #: export with an uncapped rent pass, the payables search (bills), or a
    #: ``SOURCES`` list (with the units of a property that list no longer
    #: has, :func:`_prune_source`). Read by ``sync_cycle``'s log line.
    pruned: dict[str, int] = field(default_factory=dict)
    #: Owners whose ``property_ids`` this sync rewrote from the portfolio
    #: join, or ``None`` when the join did not run to completion (then the
    #: links are left as they were and :data:`OWNER_LINKS` is in
    #: :attr:`failed`). Read by ``sync_cycle``'s log line.
    owner_links: int | None = None
    #: Properties the join could not give an owner (no usable ``portfolioID``
    #: on the property row). Not a failure:
    #: those properties simply have no owner on record. Read by the log line.
    owners_unlinked_properties: int = 0

    @property
    def total_rows(self) -> int:
        return sum(self.rows_by_table.values())

    @property
    def ok(self) -> bool:
        return not self.failed

    @property
    def fetched_nothing(self) -> bool:
        """Tables failed and none answered: a connection that does not work at
        all, which the cycle reports as an ERROR (Sentry's threshold). A table
        that answered with nothing, such as a quiet incremental read, is in
        ``rows_by_table`` with 0 and keeps this false."""
        answered = [table for table in self.rows_by_table if table not in self.failed]
        return bool(self.failed) and not answered and self.total_rows == 0


async def sync_company(
    pool: asyncpg.Pool,
    company_id: str,
    *,
    settings: Settings | None = None,
    client: RentvineClient | None = None,
) -> SyncResult:
    """Mirror one company's Rentvine entities.

    ⚠ Two gates, and they answer DIFFERENT questions (gotchas 72/73): the
    deployment flag says whether this connector runs at all, and the presence of
    a credentials row says whether THIS company is a Rentvine customer. Neither
    substitutes for the other.

    ⚠ A pass that loads its own login keeps checking it: the saved login is
    read again before every stage and once after the last write, so a
    disconnect or a re-save while the pass runs stops it (:func:`_check_login`,
    :func:`_purge_if_disconnected`). A caller that passes ``client=`` owns its
    login, and nothing is checked.
    """
    app = settings or get_settings()
    if not app.rentvine.enabled:
        raise RentvineSyncDisabled("the Rentvine connector is off for this deployment")

    owned = client is None
    #: The saved login this pass started with (``credentials.version_of``).
    started: str | None = None
    if client is None:
        # ⚠ Read BEFORE the load. A re-save landing between the two then shows
        # as a replaced login at the first stage, and the pass stops; read after,
        # it would record the new login while the client holds the old one.
        started = await saved_version(pool, company_id)
        creds = await load_rentvine_settings_for_company(company_id)
        if creds is None:
            # Not an error: this company is simply not a Rentvine customer.
            raise RentvineSyncDisabled(f"company {company_id} has no Rentvine connection")
        client = RentvineClient(creds, company_id=company_id)

    async def before_stage() -> None:
        if owned:
            await _check_login(pool, company_id, started)

    result = SyncResult(company_id=company_id)
    try:
        property_ids: list[int] = []
        property_portfolio: dict[int, int | None] = {}
        for table, path in SOURCES:
            await before_stage()
            began = _monotonic()
            try:
                collected = await client.iter_all(path)
            except RentvineAPIError as exc:
                # ⚠ One table failing must not abandon the rest, and it must not
                # look like an empty table either. Recorded per table so the
                # caller can say which part of the mirror is stale.
                logger.warning("rentvine sync %s: %s failed — %s", company_id, path, exc)
                result.failed[table] = str(exc)
                continue
            rows = collected.rows
            # ⚠ ONLY when Rentvine itself said where the collection ends. This
            # line used to run unconditionally, so `confirmed_complete` — whose
            # own docstring promises "confirmed a total for, versus ... guessed
            # the end from a short page" — made the opposite of that
            # distinction, and claimed a server-confirmed total off a heuristic
            # stop. A fetch that guessed still SYNCS and still reports its row
            # count; what it no longer does is claim the count is the total.
            if collected.stop_was_documented:
                result.confirmed_complete.add(table)
            else:
                logger.warning(
                    "rentvine sync %s: %s stopped on a short page with no "
                    "pagination headers — %d rows mirrored but NOT claimed "
                    "complete (the server may have more)",
                    company_id,
                    path,
                    len(rows),
                )
            written, skipped, written_ids = await _write_rows(pool, table, rows, company_id)
            result.rows_by_table[table] = written
            if skipped:
                result.skipped_no_id[table] = skipped
            if collected.stop_was_documented:
                result.pruned.update(
                    await _prune_source(
                        pool,
                        company_id,
                        table,
                        written_ids,
                        skipped=skipped,
                        read_seconds=_monotonic() - began,
                    )
                )
            if table == "helixis_properties":
                # ⚠ The ids that were actually WRITTEN, not the ids the raw rows
                # carried. A property whose id did not coerce is skipped from the
                # mirror, and fetching its units anyway would write unit rows
                # whose `property_id` points at a property the mirror does not
                # have — the broken relationship ADR 0010 Decision (a) exists to
                # prevent, arriving from the side nobody watches. Re-deriving the
                # list here would also be a second copy of the coercion rule
                # (gotcha 25), which is how two answers to one question start.
                property_ids = written_ids
                points: dict[int, tuple[float, float]] = {}
                for raw in rows:
                    pid = mapper.MAPPERS[table](raw).get("buildium_id")
                    if pid is not None and int(pid) in written_ids:
                        property_portfolio[int(pid)] = mapper.property_portfolio_id(raw)
                        if (point := mapper.property_point(raw)) is not None:
                            points[int(pid)] = point
                await _store_property_points(pool, company_id, points)

        await before_stage()
        await _link_owner_properties(pool, client, company_id, property_portfolio, result)
        await before_stage()
        await _sync_leases(pool, client, company_id, result)
        await before_stage()
        await _sync_units(pool, client, company_id, property_ids, result)
        await before_stage()
        await _sync_bills(pool, client, company_id, result)
        await before_stage()
        await _sync_applicants(pool, client, company_id, result)
        await _record_freshness(pool, result)
    finally:
        if owned:
            await client.aclose()
    if owned:
        await _purge_if_disconnected(pool, company_id)
    return result


#: Why a pass that loaded its own login stopped (:func:`_check_login`). Plain,
#: because ``sync_cycle.sync_one`` logs it as it is.
_LOGIN_REMOVED = (
    "the Rentvine connection was removed while this sync ran; it stopped and "
    "deleted the Rentvine mirror again"
)
_LOGIN_REPLACED = (
    "the Rentvine connection was replaced while this sync ran; it stopped, and "
    "the next sync reads with the new one"
)


async def _check_login(pool: asyncpg.Pool, company_id: str, started: str | None) -> None:
    """Before each stage of a pass that loaded its own login: raise
    :class:`RentvineSyncDisabled` unless the saved login is still the one the
    pass started with (``credentials.version_of``).

    ⚠ A pass reads for minutes, and the disconnect (``api/routes/rentvine.py``)
    deletes the credentials row and then purges. A pass that had loaded the
    login before that went on calling Rentvine with keys the customer had
    withdrawn, and went on writing after the purge, so Rentvine data outlived
    the disconnect.

    * **Gone**: no further stage runs (no Rentvine call, no write), the mirror
      is purged again (idempotent; it removes what this pass wrote after the
      route's purge), and the reason is raised. ``sync_cycle.sync_one`` logs it
      at INFO: a disconnect is not a fault.
    * **Replaced** (saved again, perhaps for another account): the pass stops
      and purges nothing; the next pass reads with the new login.

    One primary-key read per stage, never a poll inside one (rule 13). A stage
    already reading finishes first, and what it writes after a disconnect is
    purged here, before the next stage, or by :func:`_purge_if_disconnected`.
    A read that fails raises: the pass stops rather than go on unconfirmed, and
    ``sync_one`` logs the database error as the fault it is.
    """
    now = await saved_version(pool, company_id)
    if now is None:
        await purge_rentvine_mirror(pool, company_id)
        raise RentvineSyncDisabled(f"company {company_id}: {_LOGIN_REMOVED}")
    if now != started:
        raise RentvineSyncDisabled(f"company {company_id}: {_LOGIN_REPLACED}")


async def _purge_if_disconnected(pool: asyncpg.Pool, company_id: str) -> None:
    """After the last write and the freshness stamps: when the login is gone,
    purge again and raise, as :func:`_check_login` does.

    The last stage's rows and the stamps land after the last stage check, so a
    disconnect in that window would otherwise leave them behind. A re-save in
    that window keeps them: they were read with the login saved when the stage
    began, and the next pass rewrites every table with the new one.
    """
    try:
        now = await saved_version(pool, company_id)
    except Exception:  # noqa: BLE001 — logged below; the pass keeps its result
        # ⚠ A check that cannot be made (a database error) keeps the result:
        # the rows were written for a login that was still saved when the last
        # stage began. If the customer disconnected after that, those rows
        # outlive the disconnect, which is why this is an exception log and not
        # a quiet one.
        logger.exception("rentvine sync %s: the after-write disconnect check failed", company_id)
        return
    if now is None:
        await purge_rentvine_mirror(pool, company_id)
        raise RentvineSyncDisabled(f"company {company_id}: {_LOGIN_REMOVED}")


def _today() -> date:
    """Today's UTC date, for the daily rent re-read. A function so tests can pin it."""
    return datetime.now(UTC).date()


def _monotonic() -> float:
    """The read timer behind :data:`PRUNE_TOUCH_MARGIN_S`. A function so tests
    can pin it."""
    return time.monotonic()


@dataclass
class OwnerJoin:
    """What :func:`owner_property_join` concluded. ``problem`` set means the
    join must not be written; ``owns`` and ``unlinked`` are then meaningless."""

    owns: dict[int, list[int]]
    unlinked: int
    problem: str | None = None


def owner_property_join(
    property_portfolio: dict[int, int | None], portfolio_rows: list[dict[str, Any]]
) -> OwnerJoin:
    """owner contactID → the property ids they own, from COMPLETE reads.

    The one home of the join (gotcha 25): the sync writes it and
    ``scripts/rentvine_owner_links_check.py`` prints it for the founder to
    hold against Rentvine's own screen.

    ⚠ SHAPE GUARDS, because the ``/portfolios`` row shape is not yet measured
    (``mapper.portfolio_owner_contact_ids``). A wrong envelope or a renamed
    ``contacts`` key would parse as "no portfolios" or "no owners", and writing
    that would tell every owner they own nothing. So a complete list that
    lacks a portfolio a property names, or that names no owner at all while
    properties point at it, is a contradiction and comes back as ``problem``.
    """
    portfolio_owners: dict[int, set[int]] = {}
    for row in portfolio_rows:
        port, owners = mapper.portfolio_owner_contact_ids(row)
        if port is not None:
            portfolio_owners.setdefault(port, set()).update(owners)
    missing = {p for p in property_portfolio.values() if p is not None} - portfolio_owners.keys()
    if missing:
        return OwnerJoin(
            {},
            0,
            f"{len(missing)} portfolio(s) named by a property were not in the complete "
            "portfolio list (row shape?)",
        )
    # A property that names no portfolio gets NO owner rather than a guessed
    # one. On the owner side that reads as "not known" (``_owner_of_property``
    # prints nothing), never as "unowned".
    owns: dict[int, list[int]] = {}
    unlinked = 0
    for prop, port in sorted(property_portfolio.items()):
        if port is None:
            unlinked += 1
            continue
        for owner in sorted(portfolio_owners[port]):
            owns.setdefault(owner, []).append(prop)
    if unlinked < len(property_portfolio) and not owns:
        return OwnerJoin({}, unlinked, "no portfolio named an owner contact (row shape?)")
    return OwnerJoin(owns, unlinked)


async def _link_owner_properties(
    pool: asyncpg.Pool,
    client: RentvineClient,
    company_id: str,
    property_portfolio: dict[int, int | None],
    result: SyncResult,
) -> None:
    """Fill ``helixis_owners.property_ids`` from Rentvine's portfolios.

    Rentvine has no owner→property list. A property names the portfolio that
    owns it (``portfolioID``, already on the rows the properties stage
    fetched) and a portfolio lists its owner contacts. One paged
    ``GET /portfolios`` read joins the two: an owner owns every property of
    every portfolio they are on.

    ⚠ ONLY FROM COMPLETE READS. The links are rewritten only when the
    properties AND the portfolios were each read to a server-named end.
    Otherwise nothing is written, the old links stand, and :data:`OWNER_LINKS`
    is reported in :attr:`SyncResult.failed`. A join over a read that missed a
    page would drop an owner from a property they own, and "no owner on
    record" from a missed page is the fabricated absence gotchas 26/55 are
    about. A property with no usable portfolio is left unlinked and counted
    in :attr:`SyncResult.owners_unlinked_properties`, not failed; a property
    naming a portfolio the complete list lacks is a shape contradiction and
    fails the join (:func:`owner_property_join`).
    """
    if "helixis_properties" in result.failed or "helixis_owners" in result.failed:
        result.failed[OWNER_LINKS] = "properties or owners did not sync, links left as they were"
        return
    if "helixis_properties" not in result.confirmed_complete:
        result.failed[OWNER_LINKS] = (
            "the property list was not confirmed complete, links left as they were"
        )
        return
    try:
        collected = await client.iter_all(PORTFOLIOS_PATH)
    except RentvineAPIError as exc:
        logger.warning("rentvine sync %s: %s failed — %s", company_id, PORTFOLIOS_PATH, exc)
        result.failed[OWNER_LINKS] = f"portfolios: {exc}"
        return
    if not collected.stop_was_documented:
        result.failed[OWNER_LINKS] = (
            "the portfolio list was not confirmed complete, links left as they were"
        )
        return
    join = owner_property_join(property_portfolio, collected.rows)
    if join.problem is not None:
        result.failed[OWNER_LINKS] = f"{join.problem}, links left as they were"
        return
    owns, unlinked = join.owns, join.unlinked

    async with pool.acquire() as conn, conn.transaction():
        stored = await conn.fetch(
            "SELECT buildium_id FROM helixis_owners WHERE company_id = $1::uuid "
            "AND pms = 'rentvine'",
            company_id,
        )
        updated = 0
        for row in stored:
            owner_id = int(row["buildium_id"])
            # [] here is DEFINITIVE: both lists were complete, and this owner
            # is on no portfolio that owns a property.
            await conn.execute(
                "UPDATE helixis_owners SET property_ids = $3::bigint[], updated_at = now() "
                "WHERE company_id = $1::uuid AND buildium_id = $2 AND pms = 'rentvine'",
                company_id,
                owner_id,
                owns.get(owner_id, []),
            )
            updated += 1
    result.owner_links = updated
    result.owners_unlinked_properties = unlinked


async def _sync_leases(
    pool: asyncpg.Pool,
    client: RentvineClient,
    company_id: str,
    result: SyncResult,
) -> None:
    """Every lease from the export, rent from ``getLease``, links from the
    export's tenants — and a prune only when all of it is provably complete.

    See :data:`LEASES_EXPORT_PATH` for the route choice. The completeness rule
    (founder, 2026-10-01): the export's END must be the server's fact
    (``Collection.stop_was_documented``: its headers present and its row count
    reconciled against ``pagination-total-items``), its lease ids distinct, and
    the rent pass neither capped nor interrupted. Anything less is STALE, not
    empty (gotchas 149/160/162): reported in :attr:`SyncResult.failed`, never
    stamped fresh, and NOTHING is pruned.
    """
    try:
        collected = await client.iter_all(
            LEASES_EXPORT_PATH,
            page_size=LEASES_EXPORT_PAGE_SIZE,
            # Rent balances ride on the same rows (sync._replace_balances).
            # OPTIONAL: a live row without them must cost the balance, never
            # the lease read this whole stage stands on.
            optional_companions=BALANCE_SIBLINGS,
        )
    except RentvineAPIError as exc:
        logger.warning("rentvine sync %s: %s failed — %s", company_id, LEASES_EXPORT_PATH, exc)
        result.failed["helixis_leases"] = str(exc)
        result.failed["helixis_lease_tenants"] = "skipped: the lease export failed first"
        result.failed["helixis_lease_balances"] = "skipped: the lease export failed first"
        result.failed["helixis_lease_transactions"] = "skipped: the lease export failed first"
        return
    leases = [row for row in collected.rows if isinstance(row, dict)]
    # Lifted off BEFORE anything reads the lease: the lease row and its raw
    # stay exactly what they were before balances were read.
    balance_rows = [
        {**lease, **{k: lease.pop(k, None) for k in BALANCE_SIBLINGS}} for lease in leases
    ]
    ids = [mapper.map_lease(lease)["buildium_id"] for lease in leases]
    known_ids = [i for i in ids if i is not None]

    gaps: list[str] = []
    if not collected.stop_was_documented:
        gaps.append("the export sent no pagination headers, so its end is a guess")
    if len(set(known_ids)) != len(leases):
        gaps.append(f"{len(leases)} export rows carried {len(set(known_ids))} distinct lease ids")
    # Balances need only the export to be complete; the rent pass's own gaps
    # (a cap, an interrupted read) say nothing about what anyone owes.
    await _replace_balances(pool, company_id, balance_rows, list(gaps), result)

    marks, read_on = await _stored_rent_marks(pool, company_id)
    txn_read = await _stored_transaction_reads(pool, company_id)
    touched, touch_gap = await _leases_with_new_money(pool, client, company_id, leases)
    today = _today()
    wanted = _pick_rent_reads(
        leases, marks, today, history_read=txn_read, touched=touched, read_on=read_on
    )
    rents, move_ins, txns, interrupted = await _read_rents(
        client, company_id, wanted[:LEASE_RENT_READS_PER_SYNC]
    )
    if len(wanted) > LEASE_RENT_READS_PER_SYNC:
        gaps.append(
            f"rent read for {len(rents)} of {len(wanted)} leases due "
            f"(cap {LEASE_RENT_READS_PER_SYNC}); the rest next sync"
        )
    if interrupted:
        gaps.append(f"rent reads stopped after {len(rents)}: {interrupted}")

    txn_now = {lease_id for lease_id, rows in txns.items() if rows is not None}
    written, skipped = await _write_leases(
        pool,
        company_id,
        leases,
        rents,
        marks,
        txn_read=txn_read | txn_now,
        read_on=read_on,
        today=today,
    )
    result.rows_by_table["helixis_leases"] = written
    # A partial export still names real move-outs, so this runs before the
    # completeness check below; it only ever opens, never prunes.
    result.rows_by_table["helixis_turnovers"] = await _start_turnovers(pool, company_id, leases)
    result.rows_by_table["helixis_lease_transactions"] = await _write_lease_transactions(
        pool, company_id, txns
    )
    # ⚠ STALE, not empty, until EVERY lease's history has been read at least
    # once and nothing this pass left a lease's change unread: the readers of
    # this table (rent collected this month, the rent-not-collected detector)
    # read a lease with no Payment row as "has not paid" (rule 14).
    txn_gaps = list(gaps)
    if touch_gap:
        # The backup only (see TRANSACTIONS_SEARCH_PATH): logged, not a gap.
        logger.warning("rentvine sync %s: change search skipped — %s", company_id, touch_gap)
    never = [i for i in set(known_ids) if i not in txn_read and i not in txn_now]
    if never:
        txn_gaps.append(f"{len(never)} lease(s) whose transactions have never been read")
    if blank := [i for i, rows in txns.items() if rows is None]:
        txn_gaps.append(f"{len(blank)} lease read(s) came back without a transactions list")
    if txn_gaps:
        result.failed["helixis_lease_transactions"] = "; ".join(txn_gaps)
    if skipped:
        result.skipped_no_id["helixis_leases"] = skipped

    links = [
        {
            **link,
            "move_in_date": move_ins.get(link["lease_buildium_id"], {}).get(
                link["tenant_buildium_id"]
            ),
        }
        for lease in leases
        for link in mapper.map_lease_tenant_links(lease)
    ]
    result.rows_by_table["helixis_lease_tenants"] = await _write_lease_tenants(
        pool, company_id, links
    )

    if gaps:
        reason = "; ".join(gaps)
        logger.warning(
            "rentvine sync %s: leases INCOMPLETE, nothing pruned — %s", company_id, reason
        )
        result.failed["helixis_leases"] = reason
        # The links written are current, but a link that should be REMOVED is
        # still there until a complete pass prunes it: stale, not empty.
        result.failed["helixis_lease_tenants"] = f"not pruned this sync: {reason}"
        return
    result.pruned.update(await _prune_leases(pool, company_id, known_ids, links))
    result.confirmed_complete.update({"helixis_leases", "helixis_lease_tenants"})
    if not txn_gaps:
        result.confirmed_complete.add("helixis_lease_transactions")


async def _replace_balances(
    pool: asyncpg.Pool,
    company_id: str,
    rows: list[dict[str, Any]],
    gaps: list[str],
    result: SyncResult,
) -> None:
    """Replace this company's Rentvine balance snapshot, or leave it alone.

    Same shape as the Buildium path (``entities.replace_lease_balances``): a
    point-in-time snapshot, rows ONLY for leases that owe, so a lease that
    paid up drops out. Readers count every row as a delinquent lease and an
    empty table as "every lease is current", which is why this replaces only
    when it can stand behind the whole snapshot:

    - the export is complete (``gaps`` empty: a server-named end, distinct
      lease ids), and
    - EVERY lease's balance came through (:data:`mapper.BALANCE_UNKNOWN` on
      none). One unreadable balance and the snapshot would leave that tenant
      out, which reads as "current".

    Otherwise nothing is written or deleted, the old snapshot stands, and the
    table is reported STALE (``failed``), so it is not stamped fresh.
    """
    # Aging is counted in the COMPANY's calendar, not the server's UTC one.
    today = today_in(await company_timezone(pool, company_id))
    picked = [mapper.lease_balance_known(row, today) for row in rows]
    unknown = sum(1 for p in picked if p is mapper.BALANCE_UNKNOWN)
    if unknown:
        gaps.append(f"{unknown} of {len(rows)} lease(s) came without a readable balance")
    if gaps:
        result.failed["helixis_lease_balances"] = "; ".join(gaps)
        return
    owing = [p for p in picked if isinstance(p, dict)]
    import json

    async with pool.acquire() as conn, conn.transaction():
        # Leases that paid up (or left Rentvine) drop out; the count is exact.
        gone = await conn.execute(
            "DELETE FROM helixis_lease_balances WHERE company_id = $1::uuid "
            "AND pms = 'rentvine' AND NOT (lease_id = ANY($2::bigint[]))",
            company_id,
            [row["lease_id"] for row in owing],
        )
        for row in owing:
            # ⚠ Every value a parameter, none a literal: the bind contract
            # (tests/unit/_pg_bind_contract.py) pairs columns with arguments
            # by position. ``snapshot_at`` comes from its column default.
            await conn.execute(
                """
                INSERT INTO helixis_lease_balances
                    (company_id, lease_id, account_id, pms, property_id, unit_id,
                     total_balance, balance_0_30, balance_31_60, balance_61_90,
                     balance_over_90, raw)
                VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb)
                ON CONFLICT (company_id, lease_id) DO UPDATE SET
                    pms             = EXCLUDED.pms,
                    property_id     = EXCLUDED.property_id,
                    unit_id         = EXCLUDED.unit_id,
                    total_balance   = EXCLUDED.total_balance,
                    balance_0_30    = EXCLUDED.balance_0_30,
                    balance_31_60   = EXCLUDED.balance_31_60,
                    balance_61_90   = EXCLUDED.balance_61_90,
                    balance_over_90 = EXCLUDED.balance_over_90,
                    snapshot_at     = now(),
                    raw             = EXCLUDED.raw
                """,
                company_id,
                row["lease_id"],
                0,  # account_id: a Buildium account id; see _upsert
                mapper.PMS,
                row["property_id"],
                row["unit_id"],
                row["total_balance"],
                row["balance_0_30"],
                row["balance_31_60"],
                row["balance_61_90"],
                row["balance_over_90"],
                json.dumps(row["raw"], default=str),
            )
    result.rows_by_table["helixis_lease_balances"] = len(owing)
    result.confirmed_complete.add("helixis_lease_balances")
    # Said every time, even when nothing dropped out (sync_cycle logs it).
    result.pruned["helixis_lease_balances"] = _deleted(gone)


async def _stored_rent_marks(
    pool: asyncpg.Pool, company_id: str
) -> tuple[dict[int, str], dict[int, str]]:
    """``({lease id: marker}, {lease id: date last read})`` for this company's
    Rentvine leases whose rent was read at least once. A lease absent from the
    first has never been read; one absent from the second was read before the
    date was kept, and counts as due."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT buildium_id, raw->>'{RENT_READ_MARKER}' AS mark, "  # noqa: S608 — module constants
            f"raw->>'{RENT_READ_ON}' AS read_on "
            "FROM helixis_leases WHERE company_id = $1::uuid AND pms = 'rentvine'",
            company_id,
        )
    marks = {int(r["buildium_id"]): r["mark"] for r in rows if r["mark"] is not None}
    read_on = {int(r["buildium_id"]): r.get("read_on") for r in rows if r.get("read_on")}
    return marks, read_on


def _pick_rent_reads(
    leases: list[dict[str, Any]],
    marks: dict[int, str],
    today: date,
    *,
    history_read: AbstractSet[int] | None = None,
    touched: AbstractSet[int] = frozenset(),
    read_on: Mapping[int, str] | None = None,
) -> list[int]:
    """The leases whose rent (and transactions) this pass should read, most
    urgent first: never read, or its transactions never read (not in
    ``history_read``, when that is given); changed since the last read
    (the export's ``dateTimeModified`` against :data:`RENT_READ_MARKER`, or
    money moved on it: ``touched``); then every lease not read TODAY
    (:data:`RENT_READ_ON`), the longest-unread first, so a pass over the cap
    still reaches every lease in turn.

    ⚠ **A CLOSED LEASE LEAVES THE DAILY TIER ONLY** (founder, 2026-10-03). It
    is still read when never read, when the export says it changed, and when
    money moved on it. Daily, every lease of an account over 250 reads a pass
    times the passes in a day (about 1,500 at the 4-hour sync) was due again
    before the last was read, so no pass completed and nothing was ever
    pruned; most of such an account is Closed leases whose rent cannot change
    anything. A lease whose status is unknown stays daily: an unknown is
    never read as Closed (CLAUDE.md rule 14)."""
    stamps = read_on or {}
    due_today = today.isoformat()
    tiers: tuple[list[int], list[int], list[int]] = ([], [], [])
    for lease in leases:
        mapped = mapper.map_lease(lease)
        lease_id = mapped["buildium_id"]
        if lease_id is None:
            continue
        mark = marks.get(lease_id)
        modified = lease.get("dateTimeModified")
        if mark is None or (history_read is not None and lease_id not in history_read):
            tiers[0].append(lease_id)
        elif lease_id in touched or (modified is not None and str(modified) != mark):
            tiers[1].append(lease_id)
        elif stamps.get(lease_id) != due_today and mapped["status"] != "Closed":
            tiers[2].append(lease_id)
    # Oldest read first; a lease with no date (read before dates were kept)
    # sorts as the oldest of all.
    tiers[2].sort(key=lambda i: stamps.get(i) or "")
    return [*tiers[0], *tiers[1], *tiers[2]]


async def _read_rents(
    client: RentvineClient, company_id: str, lease_ids: list[int]
) -> tuple[
    dict[int, Any],
    dict[int, dict[int, date]],
    dict[int, list[dict[str, Any]] | None],
    str | None,
]:
    """``getLease`` for each id, in order. Returns ``(rent by lease, move-in
    dates by lease, transaction rows by lease, why it stopped early or None)``.
    A lease's transactions are ``None`` when the include did not come back as
    a list: unknown, so its stored history is left alone.

    ⚠ STOPS on the first failure, a 429 included (after the client's own
    bounded, Retry-After-honouring retries): pressing on is more load on an
    account that just asked for less (terms rules 1-3), and every lease not
    read keeps its stored rent and is due again next pass."""
    rents: dict[int, Any] = {}
    move_ins: dict[int, dict[int, date]] = {}
    txns: dict[int, list[dict[str, Any]] | None] = {}
    for lease_id in lease_ids:
        try:
            body = await client.get(
                LEASE_PATH,
                path_params={"leaseID": lease_id},
                query={"includes": "tenants,transactions"},
            )
        except RentvineAPIError as exc:
            return rents, move_ins, txns, str(exc)
        lease = body.get("lease") if isinstance(body, dict) else None
        if not isinstance(lease, dict):
            return rents, move_ins, txns, f"lease {lease_id}: no `lease` object in the response"
        rents[lease_id] = mapper.map_lease(lease)["rent"]
        move_ins[lease_id] = mapper.lease_tenant_move_ins(body.get("tenants"))
        txns[lease_id] = mapper.lease_transactions(body.get("transactions"), lease_id)
    return rents, move_ins, txns, None


async def _start_turnovers(
    pool: asyncpg.Pool, company_id: str, leases: list[dict[str, Any]]
) -> int:
    """Open a turnover (desk card and checklist row) for each export lease
    whose move-out date is new or moved, and is no older than
    :data:`TURNOVER_LOOKBACK_DAYS` in the company's calendar. Returns how many
    were opened this pass.

    The same trigger as Buildium's ``Lease.MoveOut.Created``
    (``turnovers_repo.start_turnover``), on its polled path: a pass that sees
    an unchanged move-out touches nothing, so a turnover the manager
    dismissed, or a card they handled, stays that way. Best-effort per lease:
    a turnover is never the reason a lease pass fails.
    """
    from helixis.db.repositories import turnovers as turnovers_repo

    today = today_in(await company_timezone(pool, company_id))
    earliest = today - timedelta(days=TURNOVER_LOOKBACK_DAYS)
    opened = 0
    for lease in leases:
        lease_id = mapper.map_lease(lease)["buildium_id"]
        move_out = mapper.lease_move_out(lease)
        if lease_id is None or move_out is None or move_out < earliest:
            continue
        try:
            if await turnovers_repo.start_turnover(
                pool,
                company_id=company_id,
                lease_id=lease_id,
                move_out_date=move_out,
                pms=mapper.PMS,
                only_if_new=True,
            ):
                opened += 1
        except Exception:  # noqa: BLE001 — best-effort, logged, never fails the pass
            logger.exception("rentvine sync %s: turnover for lease %s failed", company_id, lease_id)
    return opened


async def _stored_transaction_reads(pool: asyncpg.Pool, company_id: str) -> set[int]:
    """This company's Rentvine leases whose transactions were read at least once."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT buildium_id FROM helixis_leases WHERE company_id = $1::uuid "
            "AND pms = 'rentvine' AND raw ? $2",
            company_id,
            TXN_READ_MARKER,
        )
    return {int(r["buildium_id"]) for r in rows}


async def _leases_with_new_money(
    pool: asyncpg.Pool,
    client: RentvineClient,
    company_id: str,
    leases: list[dict[str, Any]],
) -> tuple[set[int], str | None]:
    """The leases whose unit (or, failing that, property) had lease money
    created, changed or voided since the last clean transactions stamp, and why
    that list is unknown, or ``None``. See :data:`TRANSACTIONS_SEARCH_PATH`.

    Before the first stamp there is nothing to compare against and nothing to
    ask: every lease without :data:`TXN_READ_MARKER` is read first anyway.
    Over-reading is the safe direction here (a lease re-read for nothing costs
    one call); under-reading is the stale-payment failure this exists for, so
    a row that names neither unit nor property makes the list unknown.

    A row modified before the window means the filter was ignored, and the
    list is then treated as unknown.
    """
    # ⚠ @unverified — the format `dateTimeModifiedMin` accepts, how
    # `transactionTypeIDs[]` must be encoded, and whether a lease transaction
    # carries its `unit`/`property` here. Each failure mode is caught above
    # (ignored filter, unplaced row) and reads as STALE; a live pass with an
    # earlier stamp settles them.
    stamp = await get_stage_sync(pool, company_id, "transactions")
    if stamp is None:
        return set(), None
    since = stamp.synced_at - TXN_CHANGE_OVERLAP
    try:
        collected = await client.iter_all(
            TRANSACTIONS_SEARCH_PATH,
            query={
                "dateTimeModifiedMin": since.strftime("%Y-%m-%d 00:00:00"),
                "transactionTypeIDs[]": sorted(
                    {*mapper.LEASE_TRANSACTION_TYPE, *mapper.LEASE_TRANSACTION_UNSIGNED}
                ),
            },
            optional_companions=("unit", "property"),
            max_pages=TXN_CHANGE_MAX_PAGES,
        )
    except RentvineAPIError as exc:
        return set(), f"the changed-transactions search failed: {exc}"
    by_unit: dict[int, set[int]] = {}
    by_property: dict[int, set[int]] = {}
    for lease in leases:
        mapped = mapper.map_lease(lease)
        if mapped["buildium_id"] is None:
            continue
        if mapped.get("unit_id") is not None:
            by_unit.setdefault(mapped["unit_id"], set()).add(mapped["buildium_id"])
        if mapped.get("property_id") is not None:
            by_property.setdefault(mapped["property_id"], set()).add(mapped["buildium_id"])
    lease_kinds = {*mapper.LEASE_TRANSACTION_TYPE, *mapper.LEASE_TRANSACTION_UNSIGNED}
    touched: set[int] = set()
    unplaced = 0
    floor = since.date() - TXN_CHANGE_OVERLAP
    for row in collected.rows:
        if not isinstance(row, dict):
            continue
        modified = mapper._date(row.get("dateTimeModified"))
        if modified is not None and modified < floor:
            return set(), (
                "the changed-transactions search returned a row modified before "
                "its dateTimeModifiedMin, so the filter was not honoured"
            )
        if mapper._int(row.get("transactionTypeID")) not in lease_kinds:
            continue
        unit, prop = row.get("unit"), row.get("property")
        unit_id = mapper._int(unit.get("unitID")) if isinstance(unit, dict) else None
        prop_id = mapper._int(prop.get("propertyID")) if isinstance(prop, dict) else None
        if unit_id is not None and unit_id in by_unit:
            touched |= by_unit[unit_id]
        elif prop_id is not None and prop_id in by_property:
            touched |= by_property[prop_id]
        else:
            unplaced += 1
    if unplaced:
        return touched, (
            f"{unplaced} changed lease transaction(s) named no unit or property "
            "this mirror holds a lease for"
        )
    return touched, None


async def _write_lease_transactions(
    pool: asyncpg.Pool,
    company_id: str,
    txns: dict[int, list[dict[str, Any]] | None],
) -> int:
    """Replace each READ lease's history, per lease, in one transaction.

    Replace, not merge: the include is "the lease's non-voided transactions",
    so a voided payment simply stops coming back, and only a delete removes
    it. A lease whose list is ``None`` (unknown) is left exactly as it was.
    Buildium's column set (``entities.upsert_lease_transaction``); ``account_id``
    is a Buildium account id and 0 here, as for the balances.
    """
    import json

    written = 0
    async with pool.acquire() as conn, conn.transaction():
        for lease_id, rows in txns.items():
            if rows is None:
                continue
            await conn.execute(
                "DELETE FROM helixis_lease_transactions WHERE company_id = $1::uuid "
                "AND pms = 'rentvine' AND lease_id = $2",
                company_id,
                lease_id,
            )
            for row in rows:
                await conn.execute(
                    """
                    INSERT INTO helixis_lease_transactions
                        (buildium_id, account_id, company_id, lease_id, pms,
                         transaction_date, transaction_type, total_amount, raw)
                    VALUES ($1, $2, $3::uuid, $4, $5, $6, $7, $8, $9::jsonb)
                    ON CONFLICT (company_id, buildium_id) DO UPDATE SET
                        pms = EXCLUDED.pms,
                        lease_id = EXCLUDED.lease_id,
                        transaction_date = EXCLUDED.transaction_date,
                        transaction_type = EXCLUDED.transaction_type,
                        total_amount = EXCLUDED.total_amount,
                        raw = EXCLUDED.raw,
                        updated_at = now()
                    """,
                    row["buildium_id"],
                    0,
                    company_id,
                    row["lease_id"],
                    mapper.PMS,
                    row["transaction_date"],
                    row["transaction_type"],
                    row["total_amount"],
                    json.dumps(row["raw"], default=str),
                )
                written += 1
    return written


async def _write_leases(
    pool: asyncpg.Pool,
    company_id: str,
    leases: list[dict[str, Any]],
    rents: dict[int, Any],
    marks: dict[int, str],
    *,
    txn_read: AbstractSet[int] = frozenset(),
    read_on: Mapping[int, str] | None = None,
    today: date | None = None,
) -> tuple[int, int]:
    """Upsert every export lease. ``raw`` is the export row, refreshed every
    pass; the rent and :data:`RENT_READ_MARKER` change only for a lease whose
    rent was read now, and otherwise carry over untouched.
    :data:`TXN_READ_MARKER` is set for every lease in ``txn_read`` (read now,
    or at some earlier pass)."""
    written = skipped = 0
    for lease in leases:
        mapped = mapper.map_lease(lease)
        lease_id = mapped["buildium_id"]
        if lease_id is None:
            skipped += 1
            continue
        raw = dict(lease)
        if lease_id in rents:
            mapped["rent"] = rents[lease_id]
            modified = lease.get("dateTimeModified")
            raw[RENT_READ_MARKER] = "" if modified is None else str(modified)
            raw[RENT_READ_ON] = (today or _today()).isoformat()
            columns = TABLE_COLUMNS["helixis_leases"]
        else:
            if lease_id in marks:
                raw[RENT_READ_MARKER] = marks[lease_id]
            if read_on and lease_id in read_on:
                raw[RENT_READ_ON] = read_on[lease_id]
            columns = LEASE_COLUMNS_KEEP_RENT
        if lease_id in txn_read:
            raw[TXN_READ_MARKER] = True
        mapped["raw"] = raw
        await _upsert(pool, "helixis_leases", columns, mapped, company_id)
        written += 1
    if skipped:
        logger.warning(
            "rentvine sync %s: %d of %d export leases had no usable integer id and were SKIPPED",
            company_id,
            skipped,
            len(leases),
        )
    return written, skipped


async def _write_lease_tenants(
    pool: asyncpg.Pool, company_id: str, links: list[dict[str, Any]]
) -> int:
    """Upsert the ACTIVE links. ``move_in_date`` is only ever filled in, never
    blanked: the export carries none, so a pass that did not read the lease
    keeps the date an earlier read stored."""
    async with pool.acquire() as conn:
        for link in links:
            await conn.execute(
                """
                INSERT INTO helixis_lease_tenants
                    (company_id, lease_buildium_id, tenant_buildium_id, status,
                     move_in_date, pms)
                VALUES ($1::uuid, $2, $3, $4, $5, $6)
                ON CONFLICT (company_id, lease_buildium_id, tenant_buildium_id)
                DO UPDATE SET
                    status = EXCLUDED.status,
                    move_in_date = COALESCE(
                        EXCLUDED.move_in_date, helixis_lease_tenants.move_in_date
                    ),
                    pms = EXCLUDED.pms,
                    updated_at = now()
                """,
                company_id,
                link["lease_buildium_id"],
                link["tenant_buildium_id"],
                link["status"],
                link["move_in_date"],
                mapper.PMS,
            )
    return len(links)


async def _prune_leases(
    pool: asyncpg.Pool,
    company_id: str,
    lease_ids: list[int],
    links: list[dict[str, Any]],
) -> dict[str, int]:
    """Delete what Rentvine no longer has. Called ONLY after a complete pass.

    Links first, in the same transaction: every Rentvine link that is not an
    active (lease, tenant) pair in the export — a tenant gone inactive, or a
    lease gone altogether. Then the leases the export no longer lists.
    ``pms = 'rentvine'`` on both: a targeted delete on tables that HAVE the
    column (unlike the purge's 30-table sweep, gotcha 112), so a Buildium row
    in the same tables can never be touched.
    """
    lease_side = [link["lease_buildium_id"] for link in links]
    tenant_side = [link["tenant_buildium_id"] for link in links]
    async with pool.acquire() as conn, conn.transaction():
        links_gone = await conn.execute(
            """
            DELETE FROM helixis_lease_tenants lt
             WHERE lt.company_id = $1::uuid
               AND lt.pms = 'rentvine'
               AND NOT EXISTS (
                     SELECT 1 FROM unnest($2::bigint[], $3::bigint[]) AS keep(l, t)
                      WHERE keep.l = lt.lease_buildium_id
                        AND keep.t = lt.tenant_buildium_id)
            """,
            company_id,
            lease_side,
            tenant_side,
        )
        leases_gone = await conn.execute(
            """
            DELETE FROM helixis_leases
             WHERE company_id = $1::uuid
               AND pms = 'rentvine'
               AND NOT (buildium_id = ANY($2::bigint[]))
            """,
            company_id,
            lease_ids,
        )
        # The history of a lease Rentvine no longer lists goes with it.
        txns_gone = await conn.execute(
            """
            DELETE FROM helixis_lease_transactions
             WHERE company_id = $1::uuid
               AND pms = 'rentvine'
               AND NOT (lease_id = ANY($2::bigint[]))
            """,
            company_id,
            lease_ids,
        )
    return {
        "helixis_lease_tenants": _deleted(links_gone),
        "helixis_leases": _deleted(leases_gone),
        "helixis_lease_transactions": _deleted(txns_gone),
    }


async def _prune_source(
    pool: asyncpg.Pool,
    company_id: str,
    table: str,
    kept_ids: list[int],
    *,
    skipped: int,
    read_seconds: float,
) -> dict[str, int]:
    """Delete this company's Rentvine rows that a COMPLETE ``SOURCES`` read no
    longer returns. Called only when Rentvine named the end of the collection.

    ⚠ **WITHOUT THIS THE MIRROR COULD NOT EVICT.** These tables were
    upsert-only, so a vendor deleted in Rentvine went on being offered for work
    and a property taken off the books went on counting toward occupancy, for
    as long as the connection lasted. Buildium evicts through its ``*.Deleted``
    webhooks (``entities.delete_property`` and the rest); Rentvine's are
    refused until their signature scheme is known, so this poll is the only
    delete path there is. By the spec, the property, tenant, owner and vendor
    lists return ACTIVE records only (``isActive`` defaults to 1; NOT VERIFIED
    live), so a record deactivated in Rentvine leaves too, exactly as a fresh
    connection would never have loaded it.

    It still deletes nothing when:

    * a row was SKIPPED for having no usable id: that record exists in
      Rentvine and is simply missing from ``kept_ids``;
    * an id came back TWICE: a record added or removed mid-read moves the page
      boundaries, so one row repeats and another is never seen, and the unseen
      one would be deleted while it still exists. The count still reconciled,
      which is why ``iter_all`` cannot catch this one.

    And it never deletes a row touched after this read began
    (:data:`PRUNE_TOUCH_MARGIN_S`). The units of a property that is gone go
    with it, in the same transaction, as a bill's lines go with the bill.
    ``pms = 'rentvine'`` on every statement: a targeted delete on tables that
    HAVE the column, unlike the purge's 30-table sweep (gotcha 112), so a
    Buildium row can never be touched.
    """
    if skipped or len(set(kept_ids)) != len(kept_ids):
        logger.warning(
            "rentvine sync %s: %s NOT pruned this pass — %s",
            company_id,
            table,
            f"{skipped} row(s) had no usable id"
            if skipped
            else "the read returned an id twice, so a page boundary moved mid-read",
        )
        return {}
    window = read_seconds + PRUNE_TOUCH_MARGIN_S
    async with pool.acquire() as conn, conn.transaction():
        gone = await conn.execute(
            f"DELETE FROM {table} "  # noqa: S608 — the name is a SOURCES constant
            "WHERE company_id = $1::uuid AND pms = 'rentvine' "
            "AND NOT (buildium_id = ANY($2::bigint[])) "
            "AND updated_at < now() - make_interval(secs => $3::double precision)",
            company_id,
            kept_ids,
            window,
        )
        pruned = {table: _deleted(gone)}
        if table == "helixis_properties":
            units_gone = await conn.execute(
                "DELETE FROM helixis_units "
                "WHERE company_id = $1::uuid AND pms = 'rentvine' "
                "AND NOT (property_id = ANY($2::bigint[])) "
                "AND updated_at < now() - make_interval(secs => $3::double precision)",
                company_id,
                kept_ids,
                window,
            )
            pruned["helixis_units"] = _deleted(units_gone)
    return pruned


def _deleted(status: Any) -> int:
    """asyncpg's ``"DELETE 3"`` as ``3``; anything unreadable as ``0``."""
    try:
        return int(str(status).rsplit(" ", 1)[-1])
    except ValueError:
        return 0


#: A stored point within this many metres of the one Rentvine sends is the same
#: point. Float equality would also hold (measured on the live PostGIS: an
#: 8-decimal coordinate round-trips exactly), but a tolerance cannot churn.
POINT_UNCHANGED_M = 1.0


async def _store_property_points(
    pool: asyncpg.Pool, company_id: str, points: dict[int, tuple[float, float]]
) -> int:
    """Put each property where Rentvine says it is. Returns how many moved.

    Rentvine sends ``latitude``/``longitude`` on every property, and nothing
    stored them, so no Rentvine property had a point (gotcha 188). The point is
    what the jurisdiction stack, the flood/FMR intel and "near" questions read;
    ``sync_cycle`` fills those from it after each pass, as Buildium's daily
    mirror sync does.

    ⚠ **A MOVED point clears the property's jurisdiction stack**, so the next
    enrichment pass re-resolves it. Buildium's geocode cache never invalidates
    (gotcha 107), and keeping a stack resolved from the old place would answer
    "which state's law" for an address the account no longer claims. A point
    that has not moved is not written at all, so the stack is not re-resolved
    on every pass.

    Best-effort: properties are already written, so a failure here is logged
    and the pass goes on. A missing column (an older database) lands here too.
    """
    if not points:
        return 0
    ids = list(points)
    try:
        async with pool.acquire() as conn:
            status = await conn.execute(
                """
                UPDATE helixis_properties p
                   SET geog = ST_MakePoint(v.lng, v.lat)::geography,
                       geocoded_at = now(),
                       state_fips = NULL,
                       county_fips = NULL,
                       place_fips = NULL,
                       county_name = NULL,
                       place_name = NULL,
                       jurisdiction_resolved_at = NULL
                  FROM unnest($2::bigint[], $3::float8[], $4::float8[]) AS v(id, lat, lng)
                 WHERE p.company_id = $1::uuid
                   AND p.pms = 'rentvine'
                   AND p.buildium_id = v.id
                   AND (p.geog IS NULL
                        OR NOT ST_DWithin(
                            p.geog, ST_MakePoint(v.lng, v.lat)::geography, $5::float8
                        ))
                """,
                company_id,
                ids,
                [points[i][0] for i in ids],
                [points[i][1] for i in ids],
                POINT_UNCHANGED_M,
            )
    except (asyncpg.PostgresError, OSError) as exc:
        logger.warning("rentvine sync %s: storing property points failed — %s", company_id, exc)
        return 0
    moved = _deleted(status)
    if moved:
        logger.info(
            "rentvine sync %s: placed %d propert(ies) at the coordinates Rentvine sent",
            company_id,
            moved,
        )
    return moved


async def _write_bill_lines(
    pool: asyncpg.Pool, company_id: str, records: list[dict[str, Any]]
) -> int:
    """Replace each written bill's lines (``mapper.bill_lines``), per bill, in
    one transaction, as the Buildium path does (delete then insert), so a line
    edited or removed in Rentvine cannot leave a stale row behind."""
    written = 0
    async with pool.acquire() as conn, conn.transaction():
        for record in records:
            bill_id = mapper.map_bill(record)["buildium_id"]
            if bill_id is None:
                continue
            await conn.execute(
                "DELETE FROM helixis_bill_lines WHERE company_id = $1::uuid "
                "AND pms = 'rentvine' AND bill_id = $2",
                company_id,
                bill_id,
            )
            for ln in mapper.bill_lines(record):
                await conn.execute(
                    """
                    INSERT INTO helixis_bill_lines
                        (company_id, bill_id, line_index, pms, property_id, unit_id,
                         gl_account_id, amount, memo, ledger_id, ledger_type)
                    VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                    """,
                    company_id,
                    ln["bill_id"],
                    ln["line_index"],
                    mapper.PMS,
                    ln["property_id"],
                    ln["unit_id"],
                    ln["gl_account_id"],
                    ln["amount"],
                    ln["memo"],
                    ln["ledger_id"],
                    ln["ledger_type"],
                )
                written += 1
    return written


async def _sync_applicants(
    pool: asyncpg.Pool,
    client: RentvineClient,
    company_id: str,
    result: SyncResult,
) -> None:
    """Every rental application, one row each (``mapper.map_applicant``).

    Read outside ``SOURCES`` because the row carries ``status``, ``property``
    and ``lease`` beside the application, and those are read as OPTIONAL
    siblings: a missing one costs that field (unknown), never the read.
    Nothing is pruned: an application Rentvine stops returning keeps its last
    status, which a leasing count reads as one more applicant on file, not a
    missing one.
    """
    try:
        collected = await client.iter_all(
            APPLICATIONS_PATH, optional_companions=("status", "property", "lease")
        )
    except RentvineAPIError as exc:
        logger.warning("rentvine sync %s: %s failed — %s", company_id, APPLICATIONS_PATH, exc)
        result.failed["helixis_applicants"] = str(exc)
        return
    written, skipped, _ = await _write_rows(pool, "helixis_applicants", collected.rows, company_id)
    result.rows_by_table["helixis_applicants"] = written
    if skipped:
        result.skipped_no_id["helixis_applicants"] = skipped
    if collected.stop_was_documented:
        result.confirmed_complete.add("helixis_applicants")


async def _sync_bills(
    pool: asyncpg.Pool,
    client: RentvineClient,
    company_id: str,
    result: SyncResult,
) -> None:
    """Every bill, paid and unpaid, from one paged payables read.

    ⚠ PAID BILLS TOO. The readers that switch on for Rentvine with this table
    include 12-month vendor spend (``operations.list_vendor_ops``), and an
    unpaid-only mirror would make that the unpaid subset with nothing saying
    so. Rentvine sends no paid DATE on this row, so unlike the Buildium path
    there is no 365-day paid window to apply: the whole book is read.

    ⚠ PRUNED ONLY FROM A COMPLETE READ. A bill voided or deleted in Rentvine
    stops appearing, and a mirror that kept it would count it in "what do we
    owe" forever, so this company's Rentvine bills that the read did not
    return are deleted, but only when Rentvine named the end of the
    collection. A read that guessed its end deletes nothing.
    """
    try:
        collected = await client.iter_all(
            PAYABLES_PATH,
            companion="transaction",
            nullable_companions=("ledger", "property"),
            # The unit is detail: a missing key costs the line's unit_id only.
            optional_companions=("unit",),
        )
    except RentvineAPIError as exc:
        logger.warning("rentvine sync %s: %s failed — %s", company_id, PAYABLES_PATH, exc)
        result.failed["helixis_bills"] = str(exc)
        result.failed["helixis_bill_lines"] = "skipped: the payables read failed first"
        return
    records, _voided = mapper.group_payables(collected.rows)
    written, skipped, ids = await _write_rows(pool, "helixis_bills", records, company_id)
    result.rows_by_table["helixis_bills"] = written
    if skipped:
        result.skipped_no_id["helixis_bills"] = skipped
    result.rows_by_table["helixis_bill_lines"] = await _write_bill_lines(pool, company_id, records)
    if not collected.stop_was_documented or skipped:
        return
    result.confirmed_complete.update({"helixis_bills", "helixis_bill_lines"})
    async with pool.acquire() as conn:
        gone = await conn.execute(
            "DELETE FROM helixis_bills WHERE company_id = $1::uuid AND pms = 'rentvine' "
            "AND NOT (buildium_id = ANY($2::bigint[]))",
            company_id,
            ids,
        )
        # The lines of a bill that is gone go with it.
        await conn.execute(
            "DELETE FROM helixis_bill_lines WHERE company_id = $1::uuid AND pms = 'rentvine' "
            "AND NOT (bill_id = ANY($2::bigint[]))",
            company_id,
            ids,
        )
    # Said even when it removed nothing (sync_cycle logs every prune).
    result.pruned["helixis_bills"] = _deleted(gone)


async def _sync_units(
    pool: asyncpg.Pool,
    client: RentvineClient,
    company_id: str,
    property_ids: list[int],
    result: SyncResult,
) -> None:
    """One call per property — see :data:`UNITS_PATH` for why that is the
    correct choice rather than the lazy one.

    ⚠ **ONE PROPERTY'S FAILURE DOES NOT ABANDON THE PROPERTIES BEHIND IT.** This
    used to ``return`` on the first refusal, so a permanent 403 at property 40
    starved 26 later properties of their units on every nightly run — and left
    ``rows_by_table`` unset while the units it HAD already upserted sat in the
    mirror, which reads as "nothing was written" to every consumer including the
    reconciler. The count is therefore always reported and ``failed`` carries the
    incompleteness, because "part of this table is stale" is a state this result
    can express and silence about real rows is not (gotcha 147's deferred
    question, answered: record and continue).
    """
    if "helixis_properties" in result.failed:
        # ⚠ The property fetch failed, so this list is empty for a reason that
        # has nothing to do with units. Syncing zero units here and reporting
        # success would be gotcha 104's derived-stage trap: a stage that never
        # ran, stamped as though it found nothing.
        result.failed["helixis_units"] = "skipped: the property fetch failed first"
        return
    written_total = skipped_total = 0
    answered = attempted = 0
    failures: list[tuple[int, str]] = []
    #: property -> (unit ids it DID return, the total its headers reported)
    short: dict[int, tuple[set[int], int | None]] = {}
    gave_up = False
    for pid in property_ids:
        attempted += 1
        try:
            rows = await client.get_rows(UNITS_PATH, path_params={"propertyID": pid})
        except RentvineTruncatedError as exc:
            # The route answered (so it counts as evidence it works) but short:
            # keep the units that arrived, and name the property as incomplete.
            logger.warning("rentvine sync %s: %s", company_id, exc.detail)
            rows = exc.rows
            got = {i for i in (mapper._int(r.get("unitID")) for r in rows) if i is not None}
            short[pid] = (got, exc.server_total)
        except RentvineAPIError as exc:
            logger.warning(
                "rentvine sync %s: units for property %s failed — %s", company_id, pid, exc
            )
            failures.append((pid, str(exc)))
            if answered == 0 and len(failures) >= UNIT_FANOUT_EVIDENCE:
                gave_up = True
                break
            continue
        answered += 1
        # unit rows carry `propertyID` (unlike unit_export), but the route already
        # scopes them, so the parent is known either way.
        for row in rows:
            row.setdefault("propertyID", pid)
        written, skipped, _ = await _write_rows(pool, "helixis_units", rows, company_id)
        written_total += written
        skipped_total += skipped
    if short:
        filled, still_short = await _fill_short_properties(pool, client, company_id, short)
        written_total += filled
        failures.extend(still_short)
    # ⚠ Reported even on a partial walk, and even at zero. The rows are in the
    # mirror either way; a table absent from this dict is indistinguishable from
    # one that wrote nothing.
    result.rows_by_table["helixis_units"] = written_total
    if skipped_total:
        result.skipped_no_id["helixis_units"] = skipped_total
    if failures:
        result.failed["helixis_units"] = _units_failure_summary(
            failures, attempted=attempted, total=len(property_ids), gave_up=gave_up
        )
    # ⚠ NOT added to confirmed_complete, for the same reason leases are not:
    # this route declares no `page` either, so a property with more units than
    # whatever slice the server chooses to return would come back short with
    # nothing saying so. The per-property scope makes that unlikely and does not
    # make it impossible — a 200-unit building is an ordinary thing to manage.
    # ✅ SETTLED 2026-09-24 on demoaccount41: a 120-unit property sent 20.
    # The short read is raised by `client.get_rows`, written, and named in
    # `failed` (above); the rest waits on the same rule-7 decision as leases.


async def _fill_short_properties(
    pool: asyncpg.Pool,
    client: RentvineClient,
    company_id: str,
    short: dict[int, tuple[set[int], int | None]],
) -> tuple[int, list[tuple[int, str]]]:
    """Take the units the per-property reads missed from the documented export.

    Returns ``(units written, properties still incomplete)``. A property counts
    as complete only when what it returned plus what the export added reaches
    the total ITS OWN read reported: the export sends no pagination headers, so
    its own end is a guess and is never the evidence.
    """
    try:
        export = await client.iter_all(
            UNITS_EXPORT_PATH, page_size=UNITS_EXPORT_PAGE_SIZE, companion="property"
        )
    except RentvineAPIError as exc:
        logger.warning("rentvine sync %s: %s failed — %s", company_id, UNITS_EXPORT_PATH, exc)
        return 0, [
            (pid, f"sent {len(got)} of {total}; the units export failed: {exc}")
            for pid, (got, total) in short.items()
        ]
    extra: dict[int, list[dict[str, Any]]] = {pid: [] for pid in short}
    for row in export.rows:
        sibling = row.get("property")
        pid = mapper._int(sibling.get("propertyID")) if isinstance(sibling, dict) else None
        uid = mapper._int(row.get("unitID"))
        if pid in short and uid is not None and uid not in short[pid][0]:
            extra[pid].append(row)
    written = 0
    still: list[tuple[int, str]] = []
    for pid, (got, total) in short.items():
        rows = extra[pid]
        if rows:
            n, _skipped, _ = await _write_rows(
                pool, "helixis_units", rows, company_id, map_fn=mapper.map_unit_export
            )
            written += n
        have = len(got) + len(rows)
        if total is None or have < total:
            still.append((pid, f"{have} of {total if total is not None else 'more'} units read"))
    return written, still


def _units_failure_summary(
    failures: list[tuple[int, str]],
    *,
    attempted: int,
    total: int,
    gave_up: bool,
) -> str:
    """Name EVERY property whose units could not be read, plus the first reason.

    ⚠ **THE IDS ARE NOT TRUNCATED.** "3 of 66 properties failed" says the mirror
    is partial and not WHICH part, so the one thing an operator would do next —
    open those properties in Rentvine — is unavailable to them. That is gotcha
    26's truncated-read shape arriving in a failure message rather than a tool
    result: a count without the handles is a fact nobody can act on.

    ⚠ And the walk giving up EARLY is stated separately from the failures,
    because they are different claims. A property that failed is one we asked
    about; one that was never tried is unknown, and collapsing the two would
    report properties as broken when nothing ever looked at them (gotchas
    55/57's decline-to-assert).
    """
    ids = ", ".join(str(pid) for pid, _ in failures)
    head = f"{len(failures)} of {attempted} properties' units failed or came back short"
    if gave_up:
        head += (
            f"; stopped after {len(failures)} failures with none answering, so"
            f" {total - attempted} properties were never tried"
        )
    return f"{head} (properties {ids}) — first: {failures[0][1]}"


async def _record_freshness(pool: asyncpg.Pool, result: SyncResult) -> None:
    """Stamp ``helixis_mirror_sync_state`` for each table this pass refreshed.

    ⚠ **WITHOUT THIS A RENTVINE MIRROR CAN NEVER REPORT ITSELF STALE.** 0057's
    table is the only thing that can: ``mirror_sync_state.assess`` reads it,
    :data:`mirror_sync_state.STALE_AFTER_HOURS` is the threshold, and the chat
    footer, ``GET /mirror/freshness`` and the connectors card's ``lastSyncAt``
    all hang off the verdict. The Buildium backfill has written those rows
    since 0057 and this connector wrote none, so a nightly Rentvine sync that
    broke a week ago produced a mirror that answered every question with
    week-old figures and nothing anywhere saying so — the confident-wrong-number
    shape (gotchas 26/57), on the one mechanism built to prevent it.

    ⚠ **A TABLE IN :attr:`SyncResult.failed` IS NOT STAMPED, and units are why
    that is a real rule rather than a tidy one.** ``_sync_units`` reports its
    row count even on a PARTIAL walk, so ``helixis_units`` can carry a non-zero
    count AND a failure at once. Stamping it ``synced_at = now()`` would claim
    a current mirror for a table the same result says is part stale — and the
    stamp is what the footer trusts, so the claim would outrank the truth.

    ⚠ **The derived-stage rule (gotcha 104) is enforced upstream, deliberately
    not restated here.** ``_sync_units`` records ``"skipped: the property fetch
    failed first"`` in :attr:`SyncResult.failed` when its work-list never
    existed, so the failed-table check above already covers it and a
    ``_STAGE_WORK_LIST_FROM`` map copied from the backfill could not change any
    output (gotcha 58 — do not pin a distinction that does not exist). What
    holds the property is a test that a failed property fetch leaves units
    unstamped, not a second copy of the rule.

    Best-effort by construction: ``record_sync`` swallows its own failures, and
    freshness bookkeeping must never be the reason a sync that worked reports
    as broken (gotcha 7).
    """
    # ⚠ ONE stamp per STAGE, and only when every table mapped to it is clean.
    # Two tables share "leases" (the links ride on it), and a per-table loop
    # would stamp it from whichever came last — so capped leases plus clean
    # links would have called a stale lease mirror fresh. The row count is the
    # stage's FIRST table's (helixis_leases for "leases"), as before.
    tables_by_stage: dict[str, list[str]] = {}
    for table, stage in SYNC_STATE_STAGE.items():
        tables_by_stage.setdefault(stage, []).append(table)
    for stage, tables in tables_by_stage.items():
        if any(t in result.failed for t in tables):
            continue
        if any(t not in result.rows_by_table for t in tables):
            # Never reached this pass at all. A stamp here would say "we asked
            # and your account has none", which is the fabricated absence
            # `row_count` exists to prevent (gotchas 55/106).
            continue
        await record_sync(
            pool,
            company_id=result.company_id,
            table_name=stage,
            row_count=result.rows_by_table[tables[0]],
            source=SYNC_SOURCE,
        )


async def _write_rows(
    pool: asyncpg.Pool,
    table: str,
    rows: list[dict[str, Any]],
    company_id: str,
    *,
    map_fn: Any = None,
) -> tuple[int, int, list[int]]:
    """Upsert one collection. ``map_fn`` overrides the table's mapper, for a
    source whose row shape differs (``mapper.map_unit_export``).

    Returns ``(written, skipped_without_id, written_ids)``. The third is what
    lets the units stage fan out over properties the mirror actually holds
    rather than over whatever the API happened to return.
    """
    map_fn = map_fn or mapper.MAPPERS[table]
    columns = TABLE_COLUMNS[table]
    written = skipped = 0
    written_ids: list[int] = []
    first_skipped_keys: list[str] = []
    for raw in rows:
        mapped = map_fn(raw)
        if mapped.get("buildium_id") is None:
            # A row whose identity did not coerce to an integer is skipped and
            # COUNTED rather than written with a fabricated id, because a
            # made-up identity would collide with a real record on the next
            # sync. The first one's KEYS are kept because they are the whole
            # diagnosis — see the warning below.
            skipped += 1
            if not first_skipped_keys and isinstance(raw, dict):
                first_skipped_keys = sorted(raw)
            continue
        await _upsert(pool, table, columns, mapped, company_id)
        written += 1
        written_ids.append(int(mapped["buildium_id"]))
    if skipped:
        # ⚠ THE KEYS ARE THE DIAGNOSIS, AND THE PREVIOUS WORDING WAS A GUESS
        # THAT POINTED AT THE WRONG LAYER. This line used to say a skip means
        # "ADR 0010 Decision (a)'s numeric-id assumption needs re-examining,
        # not working around". The first live Phase 8 run (2026-09-23) skipped
        # 26 of 26 units and 5 of 5 owners for an entirely different reason —
        # the row was a WRAPPER that `_row_envelopes` declared a record — and
        # the ids involved (`unitID='2'`, `contactID='5'`) coerce perfectly.
        # An operator following the old sentence would have gone after the
        # data model instead of one line in a generated map.
        #
        # The row's own keys separate the two without guessing, which is why
        # they are printed rather than reasoned about: `['token', 'unit']` is
        # an envelope problem, `['unitID', …]` with a non-numeric id is the
        # ADR one.
        every_row = bool(rows) and skipped == len(rows)
        logger.log(
            logging.ERROR if every_row else logging.WARNING,
            "rentvine sync %s: %d of %d %s rows had no usable integer id and were "
            "SKIPPED%s. The first skipped row's keys were %s — record-shaped "
            "wrappers there mean this collection's row envelope is wrong (see "
            "rentvine/_row_envelopes.py); the record's own field names mean the id "
            "itself did not coerce, which is ADR 0010 Decision (a)'s assumption.",
            company_id,
            skipped,
            len(rows),
            table,
            (
                " (EVERY ROW — this table will read as EMPTY while the sync reports "
                "success, so reconcile against the API count before believing it)"
                if every_row
                else ""
            ),
            first_skipped_keys,
        )
    return written, skipped, written_ids


async def _upsert(
    pool: asyncpg.Pool,
    table: str,
    columns: tuple[str, ...],
    mapped: dict[str, Any],
    company_id: str,
) -> None:
    """One mirror row, on the SAME conflict target the Buildium path uses.

    ⚠ ``ON CONFLICT (company_id, buildium_id)`` — unchanged from migration 0049,
    for both systems. That is ADR 0010 Decision (a)'s whole point: because the
    PMS's record id lives in the existing identity column, no upsert needs a
    per-PMS conflict target and the Buildium path is untouched.
    """
    import json

    all_cols = ("buildium_id", "account_id", "company_id", "pms", *columns, "raw")
    placeholders = []
    values: list[Any] = []
    for i, col in enumerate(all_cols, start=1):
        if col == "company_id":
            placeholders.append(f"${i}::uuid")
            values.append(company_id)
        elif col == "raw":
            placeholders.append(f"${i}::jsonb")
            values.append(json.dumps(mapped.get("raw", {}), default=str))
        elif col == "account_id":
            # ⚠ `account_id bigint NOT NULL` on 14 of the 21 mirror tables holds
            # the BUILDIUM account id, and a Rentvine row has no such thing
            # (ADR 0010, finding R3-5). Nothing in the mirror reads it — the
            # tenancy boundary is `company_id` everywhere — so a constant 0
            # satisfies the constraint without asserting a Buildium account
            # that does not exist. It is NOT a Rentvine identifier and must
            # never be read as one.
            placeholders.append(f"${i}")
            values.append(0)
        else:
            placeholders.append(f"${i}")
            values.append(mapped.get(col))
    keep = _PRESERVED_ON_UPDATE.get(table, frozenset())
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in ("pms", *columns, "raw") if c not in keep)
    sql = (
        f"INSERT INTO {table} ({', '.join(all_cols)}) "  # noqa: S608 — names from
        f"VALUES ({', '.join(placeholders)}) "  # the module constants above
        f"ON CONFLICT (company_id, buildium_id) DO UPDATE SET {updates}, updated_at = now()"
    )
    async with pool.acquire() as conn:
        await conn.execute(sql, *values)


async def refresh_one(
    pool: asyncpg.Pool,
    client: RentvineClient,
    *,
    table: str,
    path: str,
    path_params: dict[str, Any],
    id_field: str,
    company_id: str,
) -> bool:
    """Re-read ONE record from Rentvine and upsert it. Returns whether it landed.

    This is the whole of what a Rentvine webhook buys. Decision (c) makes
    polling primary and webhooks an optimisation for four object types, so this
    is a targeted freshness improvement on top of a mirror the poll already
    maintains — which is why a failure here is logged and NOT fatal: the next
    poll writes the same row.

    ⚠ **It goes through ``_write_rows``, not its own INSERT.** The identity
    rule, the skip-on-unusable-id rule and the conflict target are the same ones
    the full sync uses, because two ways of writing a mirror row is how the two
    drift into disagreeing (gotcha 25). A record whose id does not coerce is
    skipped here for exactly the reason it is skipped there.
    """
    try:
        row = await client.get_object(path, id_field=id_field, path_params=path_params)
    except RentvineAPIError as exc:
        logger.warning(
            "rentvine refresh %s: %s %s failed — %s; the poll is the fallback",
            company_id,
            table,
            path_params,
            exc,
        )
        return False
    written, _skipped, _ids = await _write_rows(pool, table, [row], company_id)
    return written == 1


__all__ = [
    "APPLICATIONS_PATH",
    "OWNER_LINKS",
    "BALANCE_SIBLINGS",
    "PAGED_TABLES",
    "REFERENCE_STAGES",
    "PAYABLES_PATH",
    "PORTFOLIOS_PATH",
    "OwnerJoin",
    "SOURCES",
    "SYNC_SOURCE",
    "SYNC_STATE_STAGE",
    "TABLE_COLUMNS",
    "owner_property_join",
    "RentvineSyncDisabled",
    "SyncResult",
    "refresh_one",
    "sync_company",
]
