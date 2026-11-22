"""Mirror-backed read tools — portfolio aggregates from the local entity mirror.

The hybrid read policy (founder-approved 2026-07-05): AGGREGATE, portfolio-
wide questions answer from the 0038 entity mirror (one local SQL query,
webhook-fresh within seconds, daily re-sync backstop), while specific-record
current-truth reads, every read-before-write, and ALL writes stay on the live
Buildium API tools in :mod:`buildium_tools`. Buildium remains the source of
truth; the mirror is a fast, honest cache.

Honesty contract: every reply states it came from Occupella's synced copy and
carries an "as of" timestamp derived from the mirror rows themselves. An
empty mirror never fakes an answer — the tool says so and points the agent
at the live tools / the connector Sync button.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from datetime import UTC, date, datetime
from typing import Any

from agents import function_tool

from helixis.agents.run_pms import rentvine_mirror_tool_enabled
from helixis.agents.tenancy import (
    FORMER,
    INCOMING,
    is_current_tenancy,
    is_upcoming_tenancy,
    tenancy_is_unknown,
    tenancy_label,
)
from helixis.agents.visual_tools import queue_map
from helixis.buildium import credentials
from helixis.buildium.maintenance_status import CLOSED_STATUSES, open_status_sql
from helixis.common.company_clock import company_timezone, today_in
from helixis.db.engine import get_pool
from helixis.db.pms_coverage import RENTVINE, coverage_for
from helixis.db.repositories import cockpit, entity_aliases
from helixis.db.repositories import geo as geo_repo
from helixis.db.repositories import mirror_sync_state as sync_state
from helixis.db.repositories import operations as operations_repo
from helixis.db.tenant_links import (
    FUTURE_LINK_STATUS,
    MOVED_OUT_LINK_STATUS,
    current_link_sql,
    is_current_link,
)
from helixis.geo import deadline_math, jurisdiction
from helixis.geo.geocode import geocode, geocoding_supported
from helixis.geo.intel_freshness import describe_age, stale_sources
from helixis.memory.context import current_memory_context
from helixis.pms import registry
from helixis.rentvine.credentials import fetch_rentvine_row
from helixis.security.prompt_delimit import wrap_untrusted
from helixis.settings import get_settings

logger = logging.getLogger(__name__)

# Metres per mile — the tool speaks miles (natural for a PM), PostGIS speaks metres.
_MILE_M = 1609.34
#: The deployment cannot turn an address into a point at all.
#:
#: ⚠ This fires on the PROVIDER, never on the raw key. It used to read
#: `if not settings.maps.api_key`, which on the keyless census default refused
#: a search that works perfectly and told the customer to set a key the
#: product does not need — an instruction that cannot change anything
#: (gotchas 46/102/106). It can only be true now on google/mapbox, where a key
#: really is the blocker, so naming the variable is still correct advice.
_MAPS_UNCONFIGURED = (
    "Location lookup isn't set up yet — this deployment's map provider needs "
    "an API key (set HELIXIS_MAPS__API_KEY). I can still answer non-location "
    "questions about your properties."
)
_GEO_NOT_READY = (
    "Location search isn't available yet — the property geocoding migration "
    "hasn't been applied, so I don't have coordinates for your properties."
)

#: Empty mirror, Buildium still CONNECTED — a sync that has not run yet.
_NOT_SYNCED = (
    "The local Buildium mirror is empty for this company — no data has been "
    "synced yet. Use the live Buildium tools for this question, and suggest "
    "running Sync data on the Buildium connector (Settings → Connectors)."
)
#: Empty mirror, NO credential on file. ⚠ The message above sends the reader
#: to the live Buildium tools, which need the same key the mirror needed —
#: so on a disconnected account it names the one route that also cannot work.
#: Same class as the staleness footer below (gotcha 46): a true sentence
#: whose instruction is impossible, because the layer holding the cause is
#: not the layer wording the message.
_NOT_CONNECTED = (
    "There is no Buildium connection on file for this company, so there is "
    "nothing synced AND the live Buildium tools cannot be used either — they "
    "need the same credentials. Do NOT describe this as an empty portfolio: "
    "say the connection is missing and point at Settings → Connectors → "
    "Buildium to reconnect."
)
#: Empty mirror, credential on file, and a COMPLETED properties sync that came
#: back with zero rows. ⚠ This is the state where the product works perfectly
#: and both messages above are false: nothing failed, nothing is pending, and
#: "run Sync data" is an instruction that cannot change anything. A customer
#: who follows it re-runs a sync that already succeeded, sees the same empty
#: screen, and concludes the product is broken — which is the "looks like
#: there was no fallback" failure this exists to close.
_EMPTY_PORTFOLIO = (
    "Buildium HAS been synced for this company and it returned zero rental "
    "properties. The mirror is empty because the connected Buildium account "
    "has no rental properties in it — NOT because a sync failed or is "
    "pending. Do NOT suggest running Sync data and do NOT describe this as a "
    "sync problem: say the connected Buildium account has no properties yet, "
    "and that adding one in Buildium brings it through automatically."
)
_NO_COMPANY = "I couldn't determine which company this session belongs to."

#: Empty mirror on a RENTVINE company. ⚠ All three messages above are wrong
#: for it, and the worst one fired first: no Buildium credential reads as
#: "disconnected", so a customer who had just connected Rentvine in the wizard
#: was told to reconnect Buildium. There are no live Rentvine read tools to
#: fall back on, and the web app has no Rentvine connector card to send them
#: to, so the message names the one thing that will change it: the sync.
_RENTVINE_NOT_SYNCED = (
    "Nothing has synced from Rentvine for this company yet. The first sync "
    "starts as soon as the Rentvine connection is saved and tested, and it then "
    "re-reads Rentvine on a schedule, so this fills in on its own. There are no "
    "live Rentvine lookup tools for this question: say the data hasn't arrived "
    "yet and to check back shortly. Do NOT suggest the Buildium tools, and do "
    "NOT describe this as a missing Buildium connection."
)
#: Rentvine synced and returned nothing — the same truth as _EMPTY_PORTFOLIO.
_RENTVINE_EMPTY_PORTFOLIO = (
    "Rentvine HAS been synced for this company and it returned zero "
    "properties. The mirror is empty because the connected Rentvine account "
    "has no properties in it, NOT because a sync failed or is pending. Say the "
    "connected Rentvine account has no properties yet, and that adding one in "
    "Rentvine brings it through on the next sync."
)


def _mirror_not_synced(name: str) -> str:
    """:data:`_RENTVINE_NOT_SYNCED` for another mirror-only system (Rent
    Manager, Propertyware). ``_mirror_not_synced("Rentvine")`` equals the
    Rentvine constant, which ``test_pms_run_wording`` pins."""
    return (
        f"Nothing has synced from {name} for this company yet. The first sync "
        f"starts as soon as the {name} connection is saved and tested, and it then "
        f"re-reads {name} on a schedule, so this fills in on its own. There are no "
        f"live {name} lookup tools for this question: say the data hasn't arrived "
        "yet and to check back shortly. Do NOT suggest the Buildium tools, and do "
        "NOT describe this as a missing Buildium connection."
    )


def _mirror_empty_portfolio(name: str) -> str:
    """:data:`_RENTVINE_EMPTY_PORTFOLIO` for another mirror-only system, pinned
    the same way as :func:`_mirror_not_synced`."""
    return (
        f"{name} HAS been synced for this company and it returned zero "
        f"properties. The mirror is empty because the connected {name} account "
        "has no properties in it, NOT because a sync failed or is pending. Say the "
        f"connected {name} account has no properties yet, and that adding one in "
        f"{name} brings it through on the next sync."
    )


def _company_id() -> str:
    ctx = current_memory_context()
    return ctx.company_id if ctx else ""


def _money(n: float | None) -> str:
    return f"${(n or 0):,.0f}"


def _listed(items: list[str]) -> str:
    """'a', 'a and b', 'a, b and c'."""
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _where(property_name: str | None, unit_number: str | None) -> str:
    """'Garden Row · Unit 3C' — whichever parts exist."""
    parts = [property_name, f"Unit {unit_number}" if unit_number else None]
    return " · ".join(p for p in parts if p)


#: Which mirrored work orders / tasks count as still open.
#:
#: ⚠ EXCLUSION, not an allowlist, and that direction is the decision. An
#: unrecognised or newly-added Buildium status reads as OPEN, which over-reports
#: maintenance; an allowlist would silently drop it, and "this tenant has no
#: open maintenance" is exactly the false absence gotcha 26 calls this
#: product's worst failure mode. Over-reporting is visible and arguable;
#: under-reporting looks like a clean answer.
#:
#: ⚠ RECONCILED 2026-08-29. This used to be a second, independent spelling of
#: "open" that disagreed with ``buildium_tools``' allowlists — a ``Deferred``
#: work order was open here and closed there, so ``portfolio_overview`` and
#: ``buildium_open_maintenance_by_unit`` could answer the same question with
#: different numbers. Worse, an allowlist and a denylist drift in OPPOSITE
#: directions when Buildium adds a status: the API path would silently start
#: DROPPING that work while the mirror kept it.
#:
#: The vocabulary now lives once, in ``buildium/maintenance_status`` — the same
#: precedent as ``buildium/money.py::bill_amount`` (gotcha 25). Re-exported
#: under the old name so the mirror's existing call sites keep reading.
CLOSED_MAINTENANCE_STATUSES = CLOSED_STATUSES

#: ⚠ Maintenance is joined at PROPERTY level, not unit, and the rows say so in
#: those words. ``helixis_work_orders.unit_id`` and ``helixis_tasks.unit_id``
#: are NULL on every mirrored row (verified on the live DB 2026-08-27: 0 of 60
#: work orders and 0 of 90 tasks carry a unit), while ``property_id`` is
#: populated on all of them. Claiming unit precision we do not have is how a
#: confident false statement gets made (gotcha 41), so the wording is honest and
#: the gap is named here instead.
_MAINTENANCE_SCOPE_NOTE = (
    "Open-maintenance counts are per PROPERTY, not per unit — the synced work "
    "orders and tasks carry a property but no unit. At a single-unit property "
    "they are the same thing; at a multi-unit one, treat the count as "
    "'something is open at this address'."
)


#: What a security-deposit figure is, said once per result rather than per row.
#:
#: ⚠ It is the amount NAMED ON THE LEASE, not a confirmation the money was
#: received and not the balance held in trust — the ledger is the authority for
#: what is actually held. Rendering an agreed figure as an amount held is the
#: confident-false-claim shape of gotchas 41/51 pointed at money a tenant is
#: owed back, which is the single worst place in this product to be wrong.
_DEPOSIT_MEANING_NOTE = (
    "Deposit figures are the amount named on the lease agreement — not "
    "confirmation it was paid, and not the balance held in trust. Check the "
    "lease ledger before quoting a return amount."
)


def _deposit_note(rows: list[Any], pms_name: str | None = None) -> str | None:
    """The one line to say about deposits across a set of lease rows.

    ⚠ **``0.00`` is a VALUE, not an absence, and this is where that matters.**
    Live-verified 2026-08-29 across all 134 mirrored leases: Buildium
    populates ``SecurityDeposit`` on every row, and one account reads 0.00 on
    all 75 of its leases while the other two carry nonzero amounts up to
    $1,800. So a portfolio at zero throughout is a portfolio that does not
    record deposits in Buildium — ONE fact — and reporting it as fifty
    tenants who each owe no deposit would be fifty fabricated facts about
    real people (gotchas 26/55: never let a systemic gap render as a
    per-record finding).

    ⚠ Computed, never hardcoded — the same discipline as the paid-bill gap
    notice (gotcha 57). The day an account starts recording deposits the
    caveat becomes false, and a false caveat teaches the model to discount
    the true ones.

    ⚠ Unknown does not collapse into zero: rows whose deposit is ``None``
    (no ``AccountDetails`` block at all) are excluded from the judgement
    entirely rather than counted as zeros, so a partial sync cannot
    manufacture the "records no deposits" claim.
    """
    known = [r["security_deposit"] for r in rows if r["security_deposit"] is not None]
    if not known:
        return None  # nothing to say — decline to assert, per gotcha 55
    if any(float(d) > 0 for d in known):
        return _DEPOSIT_MEANING_NOTE
    # Named for the company's own system (``pms_name``): it said Buildium to
    # every company, a Rent Manager one whose deposits all read 0 included.
    return (
        f"{pms_name or 'The property management system'} records no security "
        "deposit on any of these leases ($0.00 on every one). That is a property "
        f"of how this account is set up in {pms_name or 'it'}, not evidence that "
        "each tenant owes nothing — do not tell anyone their deposit is zero on "
        "the strength of it."
    )


#: Attaches open work-order and task counts to any row exposing a
#: ``company_id`` and ``property_id``. Kept as ONE string so the two rollups
#: cannot drift into counting "open" differently from each other — the same
#: reason ``buildium/money.py::bill_amount`` exists (gotcha 25).
#: ⚠ EVERY WORK ORDER IS ALSO A TASK. Buildium backs each work order with a
#: to-do item and we mirror both, so counting the two lists and adding them
#: double-counts every work order in the portfolio. Live-verified 2026-08-28:
#: **93 of 93 work orders carry a Task.Id and all 93 have a matching
#: helixis_tasks row.** Blue Ridge rendered 60 + 90 = 150 open maintenance
#: items where 90 exist — a 67% over-count on the number that feeds
#: "should I renew this tenant" and "should I send a late notice".
#:
#: ⚠ The exclusion is scoped to tasks behind an OPEN work order, NOT to every
#: backing task, and that narrowness is load-bearing. A work order's status
#: LATCHES at Completed and cannot be reopened through the API (gotcha 20), so
#: a task can legitimately be open behind a closed work order. A blanket
#: exclusion would drop it and turn a visible over-count into a confident false
#: zero — strictly worse (gotchas 26/55). The four states resolve:
#:     WO open,   task open   -> 1 (the work order)
#:     WO open,   task closed -> 1 (the work order)
#:     WO closed, task open   -> 1 (the task — the work is still open)
#:     WO closed, task closed -> 0
#:
#: The join runs on ``helixis_work_orders.task_id`` (migration 0076), populated
#: on 93 of 93 rows — not on the raw jsonb.
_OPEN_MAINTENANCE_SELECT = f"""
       (SELECT COUNT(*) FROM helixis_work_orders w
         WHERE w.company_id = {{alias}}.company_id
           AND w.property_id = {{alias}}.property_id
           AND {open_status_sql("w.status")}) AS open_work_orders,
       (SELECT COUNT(*) FROM helixis_tasks tk
         WHERE tk.company_id = {{alias}}.company_id
           AND tk.property_id = {{alias}}.property_id
           AND {open_status_sql("tk.status")}
           AND NOT EXISTS (
                 SELECT 1 FROM helixis_work_orders wb
                  WHERE wb.company_id = tk.company_id
                    AND wb.task_id = tk.buildium_id
                    AND {open_status_sql("wb.status")})) AS open_tasks
"""


def _dupe_key(row: Any) -> tuple[Any, Any, Any]:
    """Vendor + amount + due date — the shape a double-billed invoice takes.

    ⚠ DELIBERATELY NOT a duplicate DETECTOR. A legitimate per-property
    recurring charge has exactly this shape too: Blue Ridge carries 16 gutter-
    cleaning bills at $265 all due the same day, one per property, and every
    one of them is real. So this groups rows for a human to look at and the
    footer says so in those words. Calling them duplicates on this signal
    alone would be gotcha 41's confident false claim, applied to money.
    """
    return (row["vendor_name"], row["total_amount"], row["due_date"])


def _maintenance_phrase(open_work_orders: int | None, open_tasks: int | None) -> str:
    """', 3 open maintenance items' — or '' when there are none.

    ⚠ Returns empty rather than 'no open maintenance' deliberately: these
    strings go into per-row lines that are already dense, and the footer states
    that the counts are present on every row, so an absent phrase means zero
    rather than unknown. The footer is what makes that readable.
    """
    total = int(open_work_orders or 0) + int(open_tasks or 0)
    if not total:
        return ""
    return f", {total} open maintenance item{'s' if total != 1 else ''}"


def _as_of_line(latest: datetime | None) -> str:
    """Freshness footer. The mirror updates within seconds via webhooks and
    at least daily via the worker re-sync, so ``latest`` is normally recent.

    ⚠ **IT DELIBERATELY NAMES NO VENDOR, AND IT USED TO NAME BUILDIUM.** This
    line is appended to EVERY mirror read (``test_mirror_freshness_contract``
    forces it onto all of them) and the model paraphrases it into the answer,
    so for a Rentvine company it was a plainly false statement about a named
    third party on every question they asked. The mirror has held two
    connectors' rows since ADR 0010 Decision (a); the footer's job is to say
    *this is synced, not live*, and the vendor's name was never load-bearing
    for that.

    ⚠ **Naming the RIGHT vendor was considered and declined, with a cost.**
    The cheap source is real — ``helixis_mirror_sync_state.source`` already
    records which connector wrote each stage (``rentvine.sync.SYNC_SOURCE``),
    and ``_freshness_line`` has those rows in hand — but ``get_freshness``
    returns ``{table: synced_at}``, so surfacing it means widening a
    repository function the connectors card also reads, and it still cannot
    answer on the legacy branch below, where no stamped row exists and the
    vendor is genuinely unknown. A footer that names the vendor on one branch
    and not the other is worse than one that never does.
    """
    if latest is None:
        return "(from Occupella's synced property data)"
    age = datetime.now(UTC) - (latest if latest.tzinfo else latest.replace(tzinfo=UTC))
    minutes = int(age.total_seconds() // 60)
    if minutes < 2:
        when = "moments ago"
    elif minutes < 120:
        when = f"{minutes} min ago"
    else:
        when = f"{minutes // 60}h ago"
    return f"(from Occupella's synced property data, updated {when})"


async def _latest_updated(pool: Any, company_id: str) -> datetime | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT GREATEST(
                (SELECT MAX(updated_at) FROM helixis_properties WHERE company_id = $1::uuid),
                (SELECT MAX(updated_at) FROM helixis_leases     WHERE company_id = $1::uuid),
                (SELECT MAX(snapshot_at) FROM helixis_lease_balances WHERE company_id = $1::uuid)
            ) AS latest
            """,
            company_id,
        )
    latest = row["latest"] if row else None
    return latest


async def _buildium_is_connected(company_id: str) -> bool | None:
    """Is a Buildium credential on file? ``None`` when we cannot tell.

    A plain SELECT of the (still-encrypted) row — no ``HELIXIS_DATA_KEY``, no
    Buildium call — so it is cheap enough to ask on the one path that needs it.
    ``None`` is UNKNOWN and never collapses into "disconnected": inventing a
    cause is worse than naming none (gotcha 55).
    """
    try:
        return await credentials.fetch_buildium_row(company_id) is not None
    except Exception:  # pragma: no cover - defensive; a footer never fails a read
        logger.warning("freshness: could not read Buildium credential state", exc_info=True)
        return None


async def _mirror_will_catch_up(company_id: str) -> bool | None:
    """Is ANY property-management connection on file to refresh this mirror?

    ``None`` when we could not tell — never collapsed into "no" (gotcha 55).

    ⚠ **DELIBERATELY NOT :func:`_buildium_is_connected`, and the two must not
    be merged.** That one answers *can the live Buildium tools work*, which is
    a question about one vendor's credential and is correctly Buildium-shaped.
    This one answers *will anything ever sync this mirror again*, and since
    ADR 0010 Decision (a) the mirror has two possible writers — so asking the
    Buildium question here told every Rentvine customer, on a connection that
    was working perfectly, that their figures were frozen and they should go
    and reconnect a system they have never used. One question per function is
    what keeps the second connector from inheriting the first one's answer
    (gotcha 93's shared-helper trap).

    ⚠ **Every system's credentials, not two.** Rent Manager and Propertyware
    keep theirs in their own tables (:func:`_holds_pms_connector_credentials`);
    asking only Buildium and Rentvine told a working Rent Manager company its
    figures would never catch up and to reconnect.

    ⚠ **It names no vendor, and that is the honest outcome rather than a
    shortcut.** It returns ``False`` only when NO credential row exists —
    and in that state the credentials row was the only record of which system
    they used, so there is nothing left to name. Reading ``pms`` off the
    mirror rows (0101's discriminator) would sometimes recover it; it would
    also be a second answer to "which PMS" beside ``db.company_pms``, for a
    state reachable only when a disconnect did not purge (gotcha 62 measured
    exactly one such company). The footer says *reconnect in Settings →
    Connectors*, which lists both and cannot be the wrong instruction.

    ⚠ **``db.company_pms`` is NOT reused here, and its own docstring says
    why**: it fails OPEN to ``'buildium'``, which for this caller produces the
    precise wrong sentence this function exists to stop.
    """
    try:
        if await credentials.fetch_buildium_row(company_id) is not None:
            return True
        if await fetch_rentvine_row(company_id) is not None:
            return True
        return await _holds_pms_connector_credentials(company_id)
    except Exception:  # pragma: no cover - defensive; a footer never fails a read
        logger.warning("freshness: could not read PMS connection state", exc_info=True)
        return None


async def _holds_pms_connector_credentials(company_id: str) -> bool:
    """Whether the company holds keys for a connector built on ``helixis.pms``
    (Rent Manager, Propertyware): a row in that system's credentials table,
    as the registry names it (never a caller's string). Raises on a failed
    read, which :func:`_mirror_will_catch_up` turns into "could not tell"."""
    async with get_pool().acquire() as conn:
        for key, found in registry.PMS.items():
            if key in (registry.BUILDIUM, registry.RENTVINE):
                continue  # their own fetchers, above
            held = await conn.fetchval(
                f"SELECT 1 FROM public.{found.credentials_table} "  # noqa: S608
                "WHERE company_id = $1::uuid",
                company_id,
            )
            if held is not None:
                return True
    return False


async def _empty_after_a_successful_sync(company_id: str) -> bool:
    """Did a COMPLETED properties sync come back with zero rows?

    ``row_count`` has been stored on every stage since 0057 and read by
    nothing. It is the only fact that separates the two remaining empty-mirror
    states, and `mirror_sync_state.get_stage_sync` documents why a stamped
    ``properties`` row is real evidence that Buildium was asked and answered.

    ⚠ **Every uncertain answer is False**, which keeps today's wording: no row
    (never synced, or the stage failed and was deliberately left unstamped), an
    unreadable table, a NULL count, or a count we cannot read all mean "not
    enough evidence to tell this customer their books are empty". A stored
    count ABOVE zero beside an empty mirror is a third thing again — rows that
    were synced and are now missing — and "run Sync data" is the right advice
    there, so it deliberately falls through too.
    """
    try:
        stage = await sync_state.get_stage_sync(get_pool(), company_id, "properties")
    except Exception:  # pragma: no cover - defensive; a footer never fails a read
        logger.warning("freshness: could not read properties sync state", exc_info=True)
        return False
    return stage is not None and stage.row_count == 0


async def _not_synced(company_id: str) -> str:
    """The empty-mirror message — THREE states that look identical on screen.

    ⚠ All three show zero rows and they need different instructions.
    A connected account that has not synced yet should reach for the live
    Buildium tools; a disconnected one cannot use those either, because they
    want the same credential the sync wanted; and an account whose sync ran
    and returned nothing should be told the truth, because both other messages
    describe a failure that did not happen and ask for an action that cannot
    help. Naming them the same way sends two thirds of the callers to a door
    that is also locked.

    UNKNOWN keeps the connected wording: it is the older message, and
    claiming a missing connection we could not verify would be a fresh false
    statement about the account (gotcha 55).

    ⚠ A Rentvine company is asked FIRST. Its PMS is known from its Rentvine
    credential row, so it is connected by definition, and the Buildium check
    below would call it disconnected. The same holds for every system read
    from the mirror only (Rent Manager, Propertyware), told in its own name.
    A failed lookup falls through to the Buildium wording, which is what every
    company got before.
    """
    mirror_pms = await _mirror_pms(company_id)
    if mirror_pms is not None and mirror_pms.key == RENTVINE:
        if await _empty_after_a_successful_sync(company_id):
            return _RENTVINE_EMPTY_PORTFOLIO
        return _RENTVINE_NOT_SYNCED
    if mirror_pms is not None:
        if await _empty_after_a_successful_sync(company_id):
            return _mirror_empty_portfolio(mirror_pms.name)
        return _mirror_not_synced(mirror_pms.name)
    if await _buildium_is_connected(company_id) is False:
        return _NOT_CONNECTED
    if await _empty_after_a_successful_sync(company_id):
        return _EMPTY_PORTFOLIO
    return _NOT_SYNCED


#: How long a confirmed Occupella write may still be missing from the mirror.
#:
#: **DERIVED FROM MEASUREMENT, not chosen.** `LeaseTransaction.Created` webhook
#: delivery measured on helixis-test 2026-09-10, n=248 — p50 **1.1s**, p95
#: **9.7s**, p99 **14.1s**, max **19.1s**. 30s is that maximum plus headroom.
#:
#: ⚠ The raw p90 over ALL 298 rows reads **1,528s**, and acting on that figure
#: would have been wrong. The long tail is ONE cluster — 50 events, a single
#: hour (2026-08-27 07:00), one company, every one of them 1,488-1,605s — i.e.
#: a seeding backfill, not delivery lag. Excluding that hour is what produces
#: the numbers above. Gotcha 37's rule ("never characterise from one day")
#: pointed at latency, and gotcha 47's ("say which numbers are measured and
#: which are inferred") pointed at a percentile.
#:
#: Re-derive it, do not nudge it: the query is in gotcha 96.
RECENT_WRITE_WINDOW_SECONDS = 30


async def _recent_write_note(pool: Any, company_id: str) -> str:
    """ "Occupella changed this account seconds ago" — or nothing at all.

    THE DEFECT: a confirmed Buildium write reaches the mirror only when
    Buildium's webhook arrives, and nothing anywhere invalidated or annotated
    the mirror in the meantime (`call_budget` clears the per-RUN memo, which is
    a different thing). So a manager who approves a $2,400 charge and then asks
    "what's their balance now?" inside the next ~20 seconds is answered from
    data that does not have it — confidently, about money, seconds after
    watching the change get approved. That is gotcha 26's absence-claim class
    with a number instead of an empty list.

    ⚠ **Only OCCUPELLA'S OWN writes count.** A `source='webhook'` row is
    Buildium telling us about somebody else's change, which by definition
    already arrived through the mirror — counting it would fire this caveat on
    ordinary inbound traffic and it would never stop. The discriminator is
    `record_write`'s label prefix, IMPORTED rather than retyped: gotcha 73
    records `"approved"` spelled out in three modules, one drift away from
    silencing every customer.

    ⚠ **Unreadable ledger → SAY NOTHING, which is the opposite of the consent
    ledger and the role floor** (gotchas 55/72/89). Those refuse because acting
    without the fact is the harm. Here the caveat is advisory and the base rate
    of a write in the last 30 seconds is tiny, so failing silent costs almost
    nothing and returns exactly today's behaviour — while failing loud would
    put a caveat on EVERY read for the duration of an outage, and a caveat that
    fires when it is not true trains the model to discount the ones that are
    (gotcha 57's false-caveat rule).

    ⚠ **Deliberately not scoped to the table the write touched.** A charge
    followed by "what properties do I have?" gets a caveat it does not need.
    Mapping action types onto mirror tables is a second registry to keep in
    step for a 30-second window; over-caveating there is cheap and a missed
    caveat on money is not. Recorded as a decision, not an oversight.
    """
    try:
        from helixis.agents.write_ledger import LABEL_PREFIX

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                # Company-scoped (gotcha 47). Index scan on
                # helixis_activity_log(company_id, created_at DESC) — measured
                # 0.098ms / 2 buffers, so this is free on every mirror read.
                """
                SELECT action_type
                  FROM helixis_activity_log
                 WHERE company_id = $1::uuid
                   AND created_at > now() - make_interval(secs => $2)
                   AND source_label LIKE $3
                 ORDER BY created_at DESC
                 LIMIT 1
                """,
                company_id,
                RECENT_WRITE_WINDOW_SECONDS,
                f"{LABEL_PREFIX} %",
            )
    except Exception:  # noqa: BLE001 — a footer never fails a read; see above
        logger.warning("freshness: could not read the recent-write ledger", exc_info=True)
        return ""
    if row is None:
        return ""
    # ⚠ DELIBERATELY NOT "moments ago". `_as_of_line` already spends that exact
    # phrase on a DIFFERENT fact — how recently the MIRROR synced — and the two
    # sit in the same footer, so reusing it puts "updated moments ago" beside
    # "changed moments ago" and leaves a model to guess which is which. A test
    # in test_mirror_freshness_and_aliases asserts on that phrase for the
    # freshness meaning, which is what caught the collision.
    # Only on this rare path is the company's system looked up. A change in
    # Buildium arrives by webhook in seconds; every other system's arrives with
    # its next sync, so "20 seconds" was wrong for them, and so was the name.
    cov = await coverage_for(pool, company_id)
    if cov.pms is not None and cov.pms != registry.BUILDIUM:
        return (
            f" ⚠ Occupella wrote to this {cov.name} account within the last"
            f" {RECENT_WRITE_WINDOW_SECONDS} seconds, and this data is Occupella's"
            f" synced copy, which may not show the change until {cov.name}'s next"
            " sync. Say so rather than reporting the figure as current."
        )
    if cov.pms is None:
        return (
            f" ⚠ Occupella wrote to this account within the last"
            f" {RECENT_WRITE_WINDOW_SECONDS} seconds, so a change made just now may"
            " not be reflected above. Say so rather than reporting the figure as"
            " current."
        )
    return (
        f" ⚠ Occupella wrote to this Buildium account within the last"
        f" {RECENT_WRITE_WINDOW_SECONDS} seconds, and Buildium's update can take"
        " up to 20 seconds to reach this data — so a change made just now may"
        " not be reflected above. Say so rather than reporting the figure as"
        " current."
    )


async def _freshness_line(pool: Any, company_id: str, as_of: datetime | None = None) -> str:
    """Per-table-aware freshness footer (migration 0057).

    Reads ``helixis_mirror_sync_state``; falls back to the legacy 3-table
    GREATEST when no rows exist (pre-migration / never-backfilled). The
    per-table view fixes the honesty gap the research investigation flagged
    (§1.11): a fresh lease row used to hide day-stale bills entirely — now
    the stalest CORE table gets a named warning once it's >26h behind.

    ``as_of`` lets a caller keep a timestamp it already holds that is MORE
    specific than the verdict's. ``delinquency_report`` reads
    ``helixis_lease_balances.snapshot_at`` for the rows it is actually
    printing; ``verdict.as_of`` is the NEWEST core table, which for that tool
    would be a flattering number about a different table. The override changes
    only the "updated N ago" wording — the staleness verdict and the
    disconnected-credential branch below are computed the same way either way,
    because those are what the caller was missing.
    """
    # ⚠ Computed ONCE and appended at every exit below. This function returns
    # from three places and a note added to only one of them is the shape
    # gotcha 94 records — the passthrough branch silently exempt, invisible
    # because the missing footer looks identical to "no recent write".
    recent = await _recent_write_note(pool, company_id)
    freshness = await sync_state.get_freshness(pool, company_id)
    if not freshness:
        legacy = as_of if as_of is not None else await _latest_updated(pool, company_id)
        return _as_of_line(legacy) + recent
    # ⚠ The VERDICT is shared with GET /mirror/freshness (the page header);
    # only the wording below is ours. Two copies of "is this stale" would
    # eventually have the chat footer and the grid above it disagreeing about
    # the same account on the same screen — gotcha 78's second copy of the
    # statute, pointed at freshness.
    verdict = sync_state.assess(freshness)
    line = _as_of_line(as_of if as_of is not None else verdict.as_of)
    if not (verdict.is_stale and verdict.stalest_table):
        return line + recent

    line += f" ⚠ {verdict.stalest_table.replace('_', ' ')} last synced {verdict.stale_hours}h ago."
    # ⚠ Age alone reads as a LAG — something running late that will catch up.
    # When the credential is gone nothing will ever sync again, so the same
    # sentence sends the reader to wait for an event that cannot happen
    # (gotcha 46's shape: the layer holding the cause is not the layer wording
    # the message). Asked only once the data is already stale, so the healthy
    # path pays nothing.
    if await _mirror_will_catch_up(company_id) is False:
        line += (
            " There is no property-management connection on file, so this"
            " will NOT catch up on its own — these figures are a snapshot"
            " from that date, not current. Say so, and point at"
            " Settings → Connectors to reconnect."
        )
    return line + recent


async def _portfolio_overview_impl() -> str:
    company_id = _company_id()
    if not company_id:
        return _NO_COMPANY
    pool = get_pool()
    summaries = await cockpit.list_property_summaries(pool, company_id)
    if not summaries:
        return await _not_synced(company_id)

    lines = ["Portfolio overview:"]
    total_units = total_occ = total_unknown = total_wos = total_expiring = 0
    total_delinquent = 0.0
    # ``None`` = the company's PMS sends no balances (``cockpit.py``). Summing
    # it as 0 would print "$0 outstanding" for every Rentvine portfolio.
    balances_known = all(s.delinquent_total is not None for s in summaries)
    for s in summaries:
        bits = [f"{s.occupied_count}/{s.unit_count} occupied", *_occupancy_rest(s)]
        if s.open_work_orders:
            bits.append(f"{s.open_work_orders} open work orders")
        if s.expiring_leases_90d:
            bits.append(f"{s.expiring_leases_90d} leases expiring ≤90d")
        if s.delinquent_leases:
            bits.append(f"{_money(s.delinquent_total)} delinquent")
        lines.append(f"- {s.name or f'#{s.buildium_id}'}: " + ", ".join(bits))
        total_units += s.unit_count
        total_occ += s.occupied_count
        total_unknown += s.occupancy_unknown_count
        total_wos += s.open_work_orders
        total_expiring += s.expiring_leases_90d
        total_delinquent += s.delinquent_total or 0.0
    lines.append(
        f"Totals: {len(summaries)} properties, {total_occ}/{total_units} units occupied, "
        + (f"{total_unknown} occupancy unknown, " if total_unknown else "")
        + f"{total_wos} open work orders, {total_expiring} leases expiring ≤90d"
        + (f", {_money(total_delinquent)} outstanding." if balances_known else ".")
    )
    if not balances_known:
        cov = await coverage_for(pool, company_id)
        lines.append(cov.not_sent("rent balances", "each tenant's ledger"))
    lines.append(await _freshness_line(pool, company_id))
    return "\n".join(lines)


@function_tool
async def portfolio_overview() -> str:
    """One-shot rollup of the WHOLE portfolio from Occupella's live-synced
    Buildium data: per-property occupancy, vacancies, open work orders,
    leases expiring within 90 days, and delinquent balances, plus totals.

    Use this for portfolio-wide / multi-property questions like:
      - "How's the portfolio doing?" / "What needs attention today?"
      - "Which properties have vacancies?"
      - "Where are my open work orders concentrated?"

    Instant (no Buildium API calls). For the current truth about ONE
    specific record, or before any write, use the live buildium_* tools.
    """
    return await _portfolio_overview_impl()


async def _current_lease_count(pool: Any, company_id: str, *, owing: int) -> int | None:
    """How many ACTIVE leases have no outstanding balance — the paid-up set.

    Returns ``None`` when the number cannot be stated honestly, and the
    None-cases are the whole point of this being a function rather than a
    subquery:

    * the lease mirror is empty → we know nothing about who is current, and
      "0 tenants are paid up" would be a fabricated absence (gotcha 26/55);
    * the arithmetic goes negative → more balance rows than active leases,
      which means a balance is attached to a lease this count can't see, and
      a wrong number stated confidently is worse than no number (gotcha 41).

    ⚠ Counted from ACTIVE leases only. Counting all 75 would include the 25
    ended ones and report former tenants as "paid up" — the same false-tense
    claim gotcha 51 fixed on the person surface, arriving through arithmetic.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT COUNT(*) AS active
              FROM helixis_leases
             WHERE company_id = $1::uuid
               AND LOWER(COALESCE(status, '')) = 'active'
            """,
            company_id,
        )
    if not row:
        return None
    # ⚠ An unreadable count reads as ZERO ACTIVE LEASES deliberately, and that
    # is not a shortcut — it routes straight into the negative-remainder guard
    # below, so "couldn't read it" and "the arithmetic is impossible" produce
    # the same silence through one path instead of two.
    #
    # An earlier version had a separate ``raw is None -> return None`` branch.
    # Mutation testing showed it was INDISTINGUISHABLE from this: both ended at
    # ``return None`` for every input. A branch no test can tell apart is dead
    # code wearing the costume of a safety check (gotcha 48's shape), so it is
    # gone rather than kept for the comment value.
    raw = row.get("active") if hasattr(row, "get") else row["active"]
    active = int(raw) if raw is not None else 0
    remainder = active - owing
    # ``remainder == 0`` PRINTS — "0 other active leases carry no balance" is a
    # true and useful answer to "who's paid up?" (nobody). Only a negative
    # remainder, which means the arithmetic cannot be trusted, stays silent.
    return remainder if remainder >= 0 else None


async def _delinquency_report_impl() -> str:
    company_id = _company_id()
    if not company_id:
        return _NO_COMPANY
    pool = get_pool()
    # ⚠ Before the query, not after it: an empty balances table is the answer
    # "every lease is current" below, and for a PMS that sends no balances that
    # sentence is a false claim about every tenant's money.
    cov = await coverage_for(pool, company_id)
    if not cov.sees("helixis_lease_balances"):
        return cov.not_sent("rent balances", "each tenant's ledger")
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT b.lease_id, b.total_balance,
                   b.balance_0_30, b.balance_31_60, b.balance_61_90, b.balance_over_90,
                   b.snapshot_at, p.name AS property_name, u.unit_number,
                   {_OPEN_MAINTENANCE_SELECT.format(alias="b")},
                   (SELECT string_agg(
                            NULLIF(TRIM(CONCAT(t.first_name, ' ', t.last_name)), ''),
                            ', ' ORDER BY t.last_name NULLS LAST)
                      FROM helixis_lease_tenants lt
                      JOIN helixis_tenants t
                        ON t.buildium_id = lt.tenant_buildium_id
                       AND t.company_id = lt.company_id
                     WHERE lt.company_id = b.company_id
                       AND lt.lease_buildium_id = b.lease_id) AS tenant_names
              FROM helixis_lease_balances b
              LEFT JOIN helixis_properties p
                ON p.buildium_id = b.property_id AND p.company_id = b.company_id
              LEFT JOIN helixis_units u
                ON u.buildium_id = b.unit_id AND u.company_id = b.company_id
             WHERE b.company_id = $1::uuid
             ORDER BY b.total_balance DESC NULLS LAST
            """,
            company_id,
        )
    if not rows:
        # Distinguish "everyone paid" from "never synced" via the mirror.
        summaries = await cockpit.list_property_summaries(pool, company_id)
        if not summaries:
            return await _not_synced(company_id)
        # ⚠ And from "the balances stage never landed". Properties can sync
        # while balances fail (a Rentvine export with no readable balances is
        # refused whole, sync._replace_balances), and an empty table then is
        # not "everyone is current". Only a stage stamp, written by a clean
        # balances pass on either PMS path, makes it so (rule 14).
        if await sync_state.get_stage_sync(pool, company_id, "balances") is None:
            return (
                "Rent balances haven't synced for this workspace yet, so I can't say "
                f"whether anyone is behind. Check each tenant's ledger in "
                f"{cov.name or 'your property management system'}. "
                + await _freshness_line(pool, company_id)
            )
        # ⚠ `_freshness_line`, NOT the legacy `_as_of_line(_latest_updated(...))`.
        # This sentence is the STRONGEST claim the tool makes — nobody owes you
        # anything — and it was paired with the most flattering timestamp
        # available: a GREATEST over properties/leases/balances, so a property
        # row synced five minutes ago printed "updated 5 min ago" over a
        # week-stale balances table. That is verbatim the honesty gap migration
        # 0057 exists to close, still live on the absence path (gotcha 26 — an
        # assertion that nothing is there is the one that needs the caveat
        # most, and this tool had it exactly backwards: warned when it found
        # something, flattered when it did not).
        return "No outstanding balances — every lease is current. " + await _freshness_line(
            pool, company_id
        )

    lines = ["Outstanding balances (largest first):"]
    total = 0.0
    for r in rows:
        where = _where(r["property_name"], r["unit_number"])
        aging_bits = []
        if r["balance_31_60"]:
            aging_bits.append(f"{_money(float(r['balance_31_60']))} 31-60d")
        if r["balance_61_90"]:
            aging_bits.append(f"{_money(float(r['balance_61_90']))} 61-90d")
        if r["balance_over_90"]:
            aging_bits.append(f"{_money(float(r['balance_over_90']))} 90d+")
        aging = f" ({', '.join(aging_bits)})" if aging_bits else ""
        # Tenant names come from the LOCAL roster join — the live overdue-
        # balances tool showed "unidentified tenant" whenever the per-lease
        # name resolution didn't happen (2026-07-05 screenshot); the mirror
        # has the roster on hand, so name every row it can.
        names = r["tenant_names"]
        label = where or f"Lease #{r['lease_id']}"
        who = f"{names} ({label})" if names else label
        # ⚠ The maintenance count rides ON the row (gotcha 41: make each line
        # self-contained so there is no join left to mis-wire). Collections
        # questions ask "is anything open at their unit before I send a late
        # notice" — that used to cost one buildium_list_work_orders_for_unit
        # per delinquent lease, which is what burned the refusal ceiling and
        # returned nothing at all on 2026-08-27.
        maint = _maintenance_phrase(r["open_work_orders"], r["open_tasks"])
        lines.append(f"- {who}: {_money(float(r['total_balance'] or 0))}{aging}{maint}")
        total += float(r["total_balance"] or 0)
    lines.append(f"Total outstanding: {_money(total)} across {len(rows)} leases.")
    # ⚠ THE OTHER SIDE OF THE SAME QUESTION. This tool returns only leases that
    # HAVE a balance, so "which tenants are completely paid up?" — battery v13
    # q05 — was unanswerable from it. The model called this, got 20 rows, and
    # then opened buildium_agent THREE times to enumerate the rest: 152s, hit
    # the refusal ceiling, rescued by deadline synthesis.
    #
    # The count is one query on data already in the mirror (50 active leases,
    # 20 owing → 30 current). Same fix as gotchas 41/50/51/53 one step over:
    # the answer was one join away and the tool returned half of it.
    #
    # ⚠ COUNT ONLY, deliberately. Naming 30 tenants doubles this result's size
    # on EVERY collections question to serve one rarer question — and the model
    # can ask for the roster if it needs it. Stating the number is what stops
    # the enumeration; listing them is not required to.
    current = await _current_lease_count(pool, company_id, owing=len(rows))
    if current is not None:
        lines.append(
            f"{current} other active lease(s) carry NO outstanding balance — "
            "those tenants are current. That figure is the whole paid-up set; "
            "do NOT enumerate leases to work it out."
        )
    lines.append(
        "Every row above already carries its open-maintenance count — a row "
        "with no count has none. You have what you need; do NOT look maintenance "
        "up per lease."
    )
    lines.append(_MAINTENANCE_SCOPE_NOTE)
    # ⚠ The snapshot is KEPT and passed through: it is the balances table's
    # own timestamp, which is more specific than the verdict's "newest core
    # table" and is the honest number for the rows just printed. What this was
    # missing is the VERDICT — no ⚠ warning when the balances table is days
    # behind, and no "there is no Buildium connection, so this will NOT catch
    # up" branch. On the one tool whose output sends a late notice to a real
    # person, a frozen snapshot reading as a live balance is a wrong action,
    # not a wrong number.
    snapshot = max((r["snapshot_at"] for r in rows if r["snapshot_at"]), default=None)
    lines.append(await _freshness_line(pool, company_id, as_of=snapshot))
    return "\n".join(lines)


@function_tool
async def delinquency_report() -> str:
    """Who is behind on rent, from Occupella's live-synced Buildium data:
    every lease with an outstanding balance, the tenant's name, its
    property/unit, the amount, aging buckets (31-60 / 61-90 / 90+ days),
    HOW MANY OPEN MAINTENANCE ITEMS are at that address, and the total.

    Use this for questions like:
      - "Who's behind on rent?" / "Who owes us money?"
      - "Any 60+ day delinquencies?"
      - "How much rent is outstanding?"
      - "Build me a collections plan" / "who should get a late notice?"

    The maintenance count is ON each row, so a collections question that
    says "check whether they have open maintenance before recommending a
    late notice" is answerable from this ONE call. Do not follow it with a
    per-lease maintenance lookup.

    Instant (no Buildium API calls). Before taking ACTION on a specific
    delinquent lease, confirm current truth with the live buildium_* tools.
    """
    return await _delinquency_report_impl()


#: How many leases already past their end date ``expiring_leases`` lists. The
#: count always covers every one, and a line says how many were left off
#: (gotcha 26: never a silent slice).
_PAST_END_LIMIT = 25


async def _expiring_leases_impl(days: int) -> str:
    company_id = _company_id()
    if not company_id:
        return _NO_COMPANY
    days = max(1, min(int(days or 90), 365))
    pool = get_pool()
    async with pool.acquire() as conn:
        # ⚠ Tenant, balance and open maintenance are selected HERE, not looked
        # up per lease afterwards. Renewal questions ("for each expiring lease,
        # renew / renew-with-increase / don't-renew, backed by how they've paid
        # and what maintenance is open") need all three, and this tool used to
        # return none of them — so the model resolved each one record at a
        # time. Measured on the live runs of 2026-08-27:
        # buildium_find_tenant_for_unit x8, property_snapshot x6,
        # buildium_list_work_orders_for_unit x6, 26 refusals, no answer.
        # All three are already in the mirror, one join away.
        rows = await conn.fetch(
            f"""
            SELECT l.buildium_id, l.lease_to_date, l.rent, l.security_deposit,
                   (l.lease_to_date < now()) AS past_end,
                   p.name AS property_name,
                   u.unit_number,
                   {_OPEN_MAINTENANCE_SELECT.format(alias="l")},
                   (SELECT bal.total_balance FROM helixis_lease_balances bal
                     WHERE bal.company_id = l.company_id
                       AND bal.lease_id = l.buildium_id) AS total_balance,
                   (SELECT string_agg(
                            NULLIF(TRIM(CONCAT(t.first_name, ' ', t.last_name)), ''),
                            ', ' ORDER BY t.last_name NULLS LAST)
                      FROM helixis_lease_tenants lt
                      JOIN helixis_tenants t
                        ON t.buildium_id = lt.tenant_buildium_id
                       AND t.company_id = lt.company_id
                     WHERE lt.company_id = l.company_id
                       AND lt.lease_buildium_id = l.buildium_id
                       AND {current_link_sql()}) AS tenant_names
              FROM helixis_leases l
              LEFT JOIN helixis_properties p
                ON p.buildium_id = l.property_id AND p.company_id = l.company_id
              LEFT JOIN helixis_units u
                ON u.buildium_id = l.unit_id AND u.company_id = l.company_id
             WHERE l.company_id = $1::uuid AND l.status = 'Active'
               AND l.lease_to_date < now() + make_interval(days => $2)
             ORDER BY l.lease_to_date
            """,
            company_id,
            days,
        )
    # ⚠ An Active lease whose end date has PASSED is still running (month to
    # month, or a renewal never recorded). The window used to start at now(),
    # so those leases fell out of the query and an empty result said "No active
    # leases expire": measured 2026-10-03, 7 of the Rentvine sandbox's 8 active
    # leases, and the same on Buildium (gotcha 189). They are listed apart.
    upcoming = [r for r in rows if not r.get("past_end")]
    lapsed = sorted(
        (r for r in rows if r.get("past_end")), key=lambda r: r["lease_to_date"], reverse=True
    )
    if not rows:
        summaries = await cockpit.list_property_summaries(pool, company_id)
        if not summaries:
            return await _not_synced(company_id)
        return f"No active leases expire in the next {days} days. " + await _freshness_line(
            pool, company_id
        )
    cov = await coverage_for(pool, company_id)
    # ⚠ Seen AND landed: the footer below reads "no amount owed" as "owes
    # nothing", which is false for a workspace whose balances stage never
    # wrote a snapshot (see delinquency_report's empty path).
    balances = cov.sees("helixis_lease_balances") and (
        await sync_state.get_stage_sync(pool, company_id, "balances") is not None
    )
    names = cov.sees("helixis_lease_tenants")

    def _row(r: Any) -> str:
        where = _where(r["property_name"], r["unit_number"])
        date = r["lease_to_date"].date().isoformat() if r["lease_to_date"] else "?"
        when = f"ended {date}, still active" if r.get("past_end") else f"expires {date}"
        rent = f", {_money(float(r['rent']))}/mo" if r["rent"] else ""
        label = where or f"Lease #{r['buildium_id']}"
        # Name the tenant on the row rather than leaving the reader to match it
        # against a second tool's output — that cross-result join is the one
        # gotcha 41 recorded the model mis-wiring, confidently and wrongly.
        who = f"{r['tenant_names']} — " if r["tenant_names"] else ""
        balance = r["total_balance"] if balances else None
        owed = f", owes {_money(float(balance))}" if balance and float(balance) > 0 else ""
        # A lease about to end is a deposit-return deadline forming, so the
        # figure belongs ON the row: leaving it off is what sends the model
        # hydrating lease by lease (gotcha 50). Only a POSITIVE amount prints
        # — a per-row "$0.00" repeated fifty times reads as fifty findings
        # about fifty tenants, when it is one fact about the account, and
        # _deposit_note says that fact once instead.
        deposit = r["security_deposit"]
        dep = f", deposit {_money(float(deposit))}" if deposit and float(deposit) > 0 else ""
        maint = _maintenance_phrase(r["open_work_orders"], r["open_tasks"])
        return f"- {who}{label}: {when}{rent}{owed}{dep}{maint}"

    if upcoming:
        lines = [f"Leases expiring in the next {days} days:", *(_row(r) for r in upcoming)]
    else:
        lines = [f"No active leases expire in the next {days} days."]
    if lapsed:
        n = len(lapsed)
        lines.append(
            f"{n} active lease{'s are' if n != 1 else ' is'} already past "
            f"{'their' if n != 1 else 'its'} end date (most recently ended first). NOT "
            f"expired: {cov.name or 'your property management system'} still has each "
            "one active, so it is running month to month or its renewal was never "
            "recorded. Each is a renewal decision that is already due:"
        )
        lines.extend(_row(r) for r in lapsed[:_PAST_END_LIMIT])
        if n > _PAST_END_LIMIT:
            lines.append(
                f"Showing the {_PAST_END_LIMIT} most recently ended of {n} leases past "
                "their end date; the count covers all of them."
            )
    # ⚠ The deposit is claimed for the rows only when one is on file. Rentvine
    # sends none (rentvine/mapper.py stores NULL), and "each row carries its
    # lease deposit" over rows that show none read as "no deposit": unknown
    # rendered as none (CLAUDE.md rule 14).
    deposits_known = any(r["security_deposit"] is not None for r in rows)
    if balances and names:
        lines.append(
            "Each row already carries its tenant, what they owe, "
            + ("its lease deposit " if deposits_known else "")
            + "and its open-maintenance count — everything a renew / "
            "renew-with-increase / don't-renew call needs. "
            "No amount owed means the lease is not in the synced balance snapshot, "
            "i.e. it owes nothing; no maintenance count means nothing open. "
            "Do NOT look these up per lease."
        )
    else:
        # ⚠ The sentence above says "no amount owed means nothing overdue". For
        # a PMS that sends no balances every row has no amount, so it would
        # clear every tenant for renewal on data we never had.
        missing = [w for w, ok in (("rent balances", balances), ("tenant names", names)) if not ok]
        lines.append(
            cov.not_sent(_listed(missing), "each lease")
            + " These rows carry no amount owed for that reason, which is NOT the "
            "same as nothing overdue. No maintenance count means nothing open."
        )
    if (deposit_note := _deposit_note(list(rows), cov.name)) is not None:
        lines.append(deposit_note)
    elif not deposits_known:
        lines.append(
            "Occupella has no security deposit on file for these leases, so none is "
            "shown. That is NOT the same as no deposit: check it in "
            f"{cov.name or 'your property management system'}."
        )
    lines.append(_MAINTENANCE_SCOPE_NOTE)
    lines.append(await _freshness_line(pool, company_id))
    return "\n".join(lines)


@function_tool
async def expiring_leases(days: int = 90) -> str:
    """Active leases expiring within the next N days (default 90), from
    Occupella's live-synced Buildium data — TENANT NAME, property, unit, end
    date, rent, current balance owed, and how many open maintenance items
    are at that address — soonest first. Then, listed apart, every active
    lease ALREADY PAST its end date (month to month, or a renewal never
    recorded): a renewal decision already due, never "nothing expiring".

    Use this for renewal-planning questions like:
      - "Which leases expire this quarter?"
      - "Any renewals coming up at Garden Row?"
      - "What expires in the next 30 days?"
      - "For each expiring lease, should I renew, raise the rent, or let
        it go?" — the payment and maintenance evidence for that call is
        already on every row.

    One call answers the whole renewal question. Do not follow it with a
    per-lease tenant, property or maintenance lookup.

    Instant (no Buildium API calls).
    """
    return await _expiring_leases_impl(days)


def _exact_money(n: float) -> str:
    """'$1,250' when whole, '$1,249.99' when not. A rent roll is read to the
    cent, and :func:`_money` rounds every figure to the dollar."""
    return f"${n:,.0f}" if float(n).is_integer() else f"${n:,.2f}"


#: How many leases ``rent_roll`` lists, highest rent first. The count and the
#: total always cover EVERY active lease; the footer says how many rows were
#: left off and how to see them (gotcha 26: never a silent slice).
_RENT_ROLL_LIMIT = 25


async def _rent_roll_impl(property_query: str = "") -> str:
    """Every active lease with its rent, from the mirror. See :func:`rent_roll`.

    ⚠ **AN UNREAD RENT IS NOT $0.** A Rentvine lease's rent comes from one
    ``getLease`` read per lease, at most 250 a pass (``rentvine/sync.py``), so a
    new or large account has leases whose rent is still NULL. Each says "rent
    not read yet", and the total says it leaves them out (CLAUDE.md rule 14);
    adding them in as zero would understate the portfolio's rent.
    """
    company_id = _company_id()
    if not company_id:
        return _NO_COMPANY
    query = (property_query or "").strip()
    pool = get_pool()
    cov = await coverage_for(pool, company_id)
    # Before the leases stage has landed, an empty roll would be a guess.
    if not cov.sees("helixis_leases"):
        return cov.not_sent("leases", "your rent roll")
    async with pool.acquire() as conn:
        # Tenant and balance are selected on the row (gotcha 41: never make
        # the model join two results).
        rows = await conn.fetch(
            f"""
            SELECT l.buildium_id, l.rent, l.lease_to_date,
                   p.name AS property_name,
                   u.unit_number,
                   (SELECT bal.total_balance FROM helixis_lease_balances bal
                     WHERE bal.company_id = l.company_id
                       AND bal.lease_id = l.buildium_id) AS total_balance,
                   (SELECT string_agg(
                            NULLIF(TRIM(CONCAT(t.first_name, ' ', t.last_name)), ''),
                            ', ' ORDER BY t.last_name NULLS LAST)
                      FROM helixis_lease_tenants lt
                      JOIN helixis_tenants t
                        ON t.buildium_id = lt.tenant_buildium_id
                       AND t.company_id = lt.company_id
                     WHERE lt.company_id = l.company_id
                       AND lt.lease_buildium_id = l.buildium_id
                       AND {current_link_sql()}) AS tenant_names
              FROM helixis_leases l
              LEFT JOIN helixis_properties p
                ON p.buildium_id = l.property_id AND p.company_id = l.company_id
              LEFT JOIN helixis_units u
                ON u.buildium_id = l.unit_id AND u.company_id = l.company_id
             WHERE l.company_id = $1::uuid AND l.status = 'Active'
               AND ($2 = '' OR p.name ILIKE '%' || $2 || '%')
             ORDER BY l.rent DESC NULLS LAST, p.name, u.unit_number
            """,
            company_id,
            query,
        )
    if not rows:
        if query:
            return f"No active lease at a property matching '{query}'. " + await _freshness_line(
                pool, company_id
            )
        if not await cockpit.list_property_summaries(pool, company_id):
            return await _not_synced(company_id)
        return "No active leases. " + await _freshness_line(pool, company_id)
    # ⚠ Seen AND landed, as in expiring_leases: "no amount owed" reads as
    # "owes nothing", which is false where the balances never wrote a snapshot.
    balances = cov.sees("helixis_lease_balances") and (
        await sync_state.get_stage_sync(pool, company_id, "balances") is not None
    )
    names = cov.sees("helixis_lease_tenants")
    today = today_in(await company_timezone(pool, company_id))
    known = [float(r["rent"]) for r in rows if r["rent"] is not None]
    unread = len(rows) - len(known)
    plural = "s" if len(rows) != 1 else ""
    head = f"Rent roll{f' for {query}' if query else ''}: {len(rows)} active lease{plural}"
    if not known:
        # ⚠ No total at all. A sum over no known rent is not $0 (rule 14), and
        # it said "$0.00 a month in rent across the 0 whose rent is known" on
        # every Rent Manager roll: its rent is on recurring charges, which no
        # sync reads yet (RM-11), so every lease is here.
        head += ". No lease's rent has been read yet, so there is no rent total"
    else:
        head += f", {_exact_money(round(sum(known), 2))} a month in rent"
    if known and unread:
        head += (
            f" across the {len(known)} whose rent is known. {unread} lease"
            f"{'s have' if unread != 1 else ' has'} no rent read yet, so the total "
            "leaves them out"
        )
    lines = [head + ":"]
    for r in rows[:_RENT_ROLL_LIMIT]:
        label = _where(r["property_name"], r["unit_number"]) or f"Lease #{r['buildium_id']}"
        who = f"{r['tenant_names']} — " if names and r["tenant_names"] else ""
        rent = (
            f"{_exact_money(float(r['rent']))}/mo" if r["rent"] is not None else "rent not read yet"
        )
        end = r["lease_to_date"].date() if r["lease_to_date"] else None
        if end is None:
            ends = ""
        elif end >= today:
            ends = f", lease ends {end.isoformat()}"
        else:
            # An active lease past its end date is still collecting rent
            # (month to month, usually). Said as the fact, not as "expired".
            ends = f", past its {end.isoformat()} end date and still active"
        balance = r["total_balance"] if balances else None
        owed = f", owes {_exact_money(float(balance))}" if balance and float(balance) > 0 else ""
        lines.append(f"- {who}{label}: {rent}{ends}{owed}")
    if len(rows) > _RENT_ROLL_LIMIT:
        shown = (
            f"Showing the {_RENT_ROLL_LIMIT} highest rents of {len(rows)} active leases; "
            "the count and total above cover all of them."
            if known
            # No rent known: nothing ranks them, and there is no total.
            else f"Showing {_RENT_ROLL_LIMIT} of {len(rows)} active leases; the count "
            "above covers all of them."
        )
        lines.append(shown + " Name a property to see every lease there.")
    if balances and names:
        lines.append(
            "No amount owed means the lease is not in the synced balance snapshot, "
            "i.e. it owes nothing."
        )
    else:
        missing = [w for w, ok in (("rent balances", balances), ("tenant names", names)) if not ok]
        note = cov.not_sent(_listed(missing), "each lease")
        if not balances:
            note += (
                " These rows carry no amount owed for that reason, which is NOT the "
                "same as nothing overdue."
            )
        lines.append(note)
    lines.append(await _freshness_line(pool, company_id))
    return "\n".join(lines)


@function_tool
async def rent_roll(property_query: str = "") -> str:
    """The rent roll: every active lease with its TENANT, property, unit,
    monthly rent, lease end date and current balance owed, highest rent
    first, with the number of active leases and the total monthly rent.
    ``property_query`` narrows it to one property (part of the name is
    enough).

    Use this for questions like:
      - "Show me the rent roll."
      - "Rank my units by rent." / "What's our total monthly rent?"
      - "What does each tenant at Garden Row pay?"

    One call answers the whole question. Do not follow it with a per-lease
    lookup. A lease whose rent has not been read yet says so; never report
    it as $0.

    Instant (no API calls), from Occupella's synced data.
    """
    return await _rent_roll_impl(property_query)


# ⚠ Rentvine runs only. On a Buildium run the live ``buildium_get_rent_roll``
# answers this question, and a second rent roll there would reopen every
# Buildium routing decision on the words "rent roll" (agents/CLAUDE.md,
# gotcha 130) for no gain. On the SINGLETON, so the specialist's copy and the
# router's copy both carry it (``budget_tool`` copies keep the flag).
rent_roll.is_enabled = rentvine_mirror_tool_enabled


#: How many leases ``lease_payment_history`` shows in full. More matches than
#: this is a question to narrow, answered with the list, never a dump.
_HISTORY_MAX_LEASES = 3
#: Rows listed per lease, newest first. The totals cover every row in the
#: window, and a line says how many were left off (gotcha 26).
_HISTORY_ROWS_PER_LEASE = 24
_HISTORY_MAX_MONTHS = 24

#: ``transaction_type`` -> (what the row is called, the sign that turns its
#: signed ``total_amount`` back into the amount moved). The mirror signs a row
#: by its effect on the balance (``buildium/money.PAYMENTS_ARE_NEGATIVE``,
#: ``rentvine/mapper.LEASE_TRANSACTION_TYPE``), so a payment is negative and a
#: payment RETURN is a positive ``Payment`` row.
_HISTORY_KINDS: dict[str, int] = {"Charge": 1, "Payment": -1, "Credit": -1}


def _month_start(today: date, months: int) -> date:
    """The first day of the month ``months - 1`` months before ``today``'s, so
    "6 months" is this month and the five before it."""
    year, month = today.year, today.month - (months - 1)
    while month <= 0:
        month += 12
        year -= 1
    return date(year, month, 1)


def _history_line(row: Any) -> str:
    """``- 2026-09-03 Payment $1,250 — Rent`` for one ledger row."""
    when = row["transaction_date"].date().isoformat() if row["transaction_date"] else "undated"
    kind = row["transaction_type"] or "Transaction"
    note = f" — {row['description']}" if row["description"] else ""
    amount = row["total_amount"]
    if amount is None:
        # Rentvine states no direction for some types (a payout, a deposit
        # release), and the mapper stores NULL rather than guess (rule 14).
        return f"- {when} {kind}: amount not stated{note}"
    value = float(amount)
    if kind == "Payment" and value > 0:
        return f"- {when} Payment returned {_exact_money(value)}{note}"
    return f"- {when} {kind} {_exact_money(abs(value))}{note}"


async def _lease_payment_history_impl(lease_query: str, months: int = 6) -> str:
    """One lease's charges, payments and credits, from the mirror. See
    :func:`lease_payment_history`.

    ⚠ **A MISSING HISTORY IS NOT "NO PAYMENTS".** Rentvine's transactions ride
    on the per-lease rent read (``getLease?includes=transactions``, at most 250
    a pass), so until the ``transactions`` stage has landed a lease can have
    rows Occupella has not read. Then an empty window says the history is not
    synced yet, never that nothing was paid (CLAUDE.md rule 14).
    """
    company_id = _company_id()
    if not company_id:
        return _NO_COMPANY
    query = " ".join((lease_query or "").replace(",", " ").split())
    if not query:
        return (
            "Name the tenant, the property and unit, or the lease whose payment history you want."
        )
    try:
        window = max(1, min(int(months), _HISTORY_MAX_MONTHS))
    except (TypeError, ValueError):
        window = 6
    pool = get_pool()
    cov = await coverage_for(pool, company_id)
    if not cov.sees("helixis_leases"):
        return cov.not_sent("leases", "payment history")
    if not cov.sees("helixis_lease_transactions"):
        return cov.not_sent("charges and payments", "any lease")
    lease_id = int(query) if query.isdigit() else None
    async with pool.acquire() as conn:
        # One join, so every row already carries its tenants, place and
        # balance (gotcha 41). A tenant, a "property unit" pair, a bare unit
        # number or a lease id all find it.
        matches = await conn.fetch(
            """
            SELECT l.buildium_id, l.status, l.rent,
                   p.name AS property_name, u.unit_number,
                   (SELECT string_agg(
                            NULLIF(TRIM(CONCAT(t.first_name, ' ', t.last_name)), ''),
                            ', ' ORDER BY t.last_name NULLS LAST)
                      FROM helixis_lease_tenants lt
                      JOIN helixis_tenants t
                        ON t.buildium_id = lt.tenant_buildium_id
                       AND t.company_id = lt.company_id
                     WHERE lt.company_id = l.company_id
                       AND lt.lease_buildium_id = l.buildium_id) AS tenant_names,
                   (SELECT bal.total_balance FROM helixis_lease_balances bal
                     WHERE bal.company_id = l.company_id
                       AND bal.lease_id = l.buildium_id) AS total_balance
              FROM helixis_leases l
              LEFT JOIN helixis_properties p
                ON p.buildium_id = l.property_id AND p.company_id = l.company_id
              LEFT JOIN helixis_units u
                ON u.buildium_id = l.unit_id AND u.company_id = l.company_id
             WHERE l.company_id = $1::uuid
               AND (
                    l.buildium_id = $3::bigint
                 OR regexp_replace(CONCAT_WS(' ', p.name, u.unit_number), '[[:space:],]+', ' ', 'g')
                    ILIKE '%' || $2 || '%'
                 OR u.unit_number ILIKE $2
                 OR EXISTS (
                      SELECT 1
                        FROM helixis_lease_tenants lt2
                        JOIN helixis_tenants t2
                          ON t2.buildium_id = lt2.tenant_buildium_id
                         AND t2.company_id = lt2.company_id
                       WHERE lt2.company_id = l.company_id
                         AND lt2.lease_buildium_id = l.buildium_id
                         AND CONCAT(t2.first_name, ' ', t2.last_name) ILIKE '%' || $2 || '%')
               )
             ORDER BY (l.status = 'Active') DESC, l.lease_to_date DESC NULLS LAST,
                      l.buildium_id DESC
             LIMIT 11
            """,
            company_id,
            query,
            lease_id,
        )
        rows = (
            await conn.fetch(
                """
                SELECT lease_id, transaction_date, transaction_type, total_amount,
                       NULLIF(TRIM(raw->>'description'), '') AS description
                  FROM helixis_lease_transactions
                 WHERE company_id = $1::uuid AND lease_id = ANY($2::bigint[])
                 ORDER BY lease_id, transaction_date DESC NULLS LAST, buildium_id DESC
                """,
                company_id,
                [r["buildium_id"] for r in matches],
            )
            if 0 < len(matches) <= _HISTORY_MAX_LEASES
            else []
        )
    if not matches:
        return (
            f"No lease matches '{query}'. Try the tenant's name, the property and unit, "
            "or the lease id. " + await _freshness_line(pool, company_id)
        )
    names = cov.sees("helixis_lease_tenants")
    if len(matches) > _HISTORY_MAX_LEASES:
        count_text = "More than 10" if len(matches) > 10 else str(len(matches))
        lines = [f"{count_text} leases match '{query}'. Which one?"]
        for r in matches[:10]:
            label = _where(r["property_name"], r["unit_number"]) or f"Lease #{r['buildium_id']}"
            who = f"{r['tenant_names']} — " if names and r["tenant_names"] else ""
            lines.append(f"- {who}{label} ({r['status'] or 'status unknown'} lease)")
        lines.append("Ask again with the tenant's full name or the property and unit.")
        return "\n".join(lines)
    landed = await sync_state.get_stage_sync(pool, company_id, "transactions") is not None
    balances = cov.sees("helixis_lease_balances") and (
        await sync_state.get_stage_sync(pool, company_id, "balances") is not None
    )
    since = _month_start(today_in(await company_timezone(pool, company_id)), window)
    by_lease: dict[int, list[Any]] = {}
    for row in rows:
        by_lease.setdefault(int(row["lease_id"]), []).append(row)

    out: list[str] = []
    for lease in matches:
        history = by_lease.get(int(lease["buildium_id"]), [])
        recent = [
            r
            for r in history
            if r["transaction_date"] is None or r["transaction_date"].date() >= since
        ]
        older = len(history) - len(recent)
        label = _where(lease["property_name"], lease["unit_number"]) or (
            f"Lease #{lease['buildium_id']}"
        )
        who = f"{lease['tenant_names']} — " if names and lease["tenant_names"] else ""
        rent = f", {_exact_money(float(lease['rent']))}/mo" if lease["rent"] is not None else ""
        out.append(
            f"Payment history for {who}{label} "
            f"({lease['status'] or 'status unknown'} lease{rent}), since {since.isoformat()}:"
        )
        if not recent:
            out.append(
                f"- No charges, payments or credits recorded since {since.isoformat()}."
                if landed
                else "- Payment history for this lease has not finished syncing, so none is "
                "shown; that is NOT the same as nothing paid."
            )
        else:
            totals = dict.fromkeys(_HISTORY_KINDS, 0.0)
            other = 0
            for r in recent:
                sign = _HISTORY_KINDS.get(r["transaction_type"] or "")
                if sign is None or r["total_amount"] is None:
                    other += 1
                    continue
                totals[r["transaction_type"]] += float(r["total_amount"]) * sign
            payments = [
                r
                for r in recent
                if r["transaction_type"] == "Payment"
                and r["total_amount"] is not None
                and float(r["total_amount"]) < 0
            ]
            last = (
                f" Last payment {payments[0]['transaction_date'].date().isoformat()} "
                f"({_exact_money(abs(float(payments[0]['total_amount'])))})."
                if payments and payments[0]["transaction_date"]
                else " No payment in this period."
            )
            out.append(
                f"- Charged {_exact_money(round(totals['Charge'], 2))}, paid "
                f"{_exact_money(round(totals['Payment'], 2))}, credited "
                f"{_exact_money(round(totals['Credit'], 2))} across {len(recent)} "
                f"transaction{'s' if len(recent) != 1 else ''}.{last}"
            )
            if other:
                out.append(
                    f"- {other} row{'s' if other != 1 else ''} of another kind or with no "
                    "stated amount are listed but not in those totals."
                )
            if not landed:
                out.append("- Payment history has not finished syncing, so rows may be missing.")
            out.extend(_history_line(r) for r in recent[:_HISTORY_ROWS_PER_LEASE])
            if len(recent) > _HISTORY_ROWS_PER_LEASE:
                out.append(
                    f"- …and {len(recent) - _HISTORY_ROWS_PER_LEASE} older rows in this "
                    "period, counted in the totals above."
                )
        if older:
            out.append(
                f"- {older} earlier row{'s' if older != 1 else ''} before {since.isoformat()} "
                f"not shown; ask for more months (up to {_HISTORY_MAX_MONTHS})."
            )
        if balances:
            balance = lease["total_balance"]
            out.append(
                f"- Owes {_exact_money(float(balance))} now."
                if balance is not None and float(balance) > 0
                else "- Owes nothing now (not in the synced balance snapshot)."
            )
        else:
            out.append(
                "- What it owes now is not synced yet, which is NOT the same as owing nothing."
            )
    out.append(await _freshness_line(pool, company_id))
    return "\n".join(out)


@function_tool
async def lease_payment_history(lease_query: str, months: int = 6) -> str:
    """One lease's payment history: every charge, payment and credit, newest
    first, with what was charged, paid and credited in the period, the last
    payment, and what the lease owes now. ``lease_query`` is the tenant's
    name, the property and unit ("Garden Row 4B"), or the lease id.
    ``months`` is how far back to go (default 6, at most 24).

    Use this for questions like:
      - "What has Dana Lee paid this year?" / "When did unit 4B last pay?"
      - "Show me the ledger for Garden Row 4B."
      - "Did the tenant at 12 Oak St pay September's rent?"

    When several leases match, it lists them and asks which one: ask the
    user rather than guessing. A row whose amount was not stated is said so,
    never as $0.

    Instant (no API calls), from Occupella's synced data.
    """
    return await _lease_payment_history_impl(lease_query, months)


# ⚠ Rentvine runs only, like ``rent_roll``: a Buildium run reads a ledger live
# (``buildium_find_tenant_for_unit`` and the lease reads), and a second ledger
# tool there would reopen those routing decisions (gotcha 130).
lease_payment_history.is_enabled = rentvine_mirror_tool_enabled


async def _owner_of_property(pool: Any, company_id: str, property_buildium_id: int) -> str:
    """``- Owner: Sanjay Bhatt — no email or phone on file`` for one property,
    or ``""`` when the mirror cannot say who owns it.

    ⚠ Returns EMPTY on no match rather than "no owner on record". Two of Blue
    Ridge's 50 properties are genuinely uncovered by ``property_ids``, and an
    absent owner is a gap in what Buildium sent us — not evidence the property
    is unowned. Printing the stronger sentence would be a fabricated absence
    (gotchas 26/55); printing nothing lets the model say it doesn't know.

    ⚠ The contact clause is CONDITIONAL. Live-verified 2026-08-28: Blue Ridge
    0 of 4 owners carry email or phone, My Company 4 of 4 do. Hardcoding
    "no contact on file" would be a fresh false claim for half the product.

    ⚠ It says WHETHER a contact is on file, never the value (Rentvine terms
    rule 20: send the model the minimum a task needs). A property snapshot is
    not a request for anyone's email; ``contact_lookup`` hands it over when
    that is the question.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT first_name, last_name, company_name, email, phone
              FROM helixis_owners
             WHERE company_id = $1::uuid
               AND $2 = ANY(property_ids)
             ORDER BY company_name NULLS LAST, last_name NULLS LAST
             LIMIT 4
            """,
            company_id,
            property_buildium_id,
        )
    if not rows:
        return ""
    names: list[str] = []
    for r in rows:
        person = f"{r['first_name'] or ''} {r['last_name'] or ''}".strip()
        label = r["company_name"] or person or "unnamed owner"
        on_file = [k for k, v in (("email", r["email"]), ("phone", r["phone"])) if v and v.strip()]
        names.append(
            f"{label} ({' and '.join(on_file)} on file; contact_lookup has it)"
            if on_file
            else f"{label} — no email or phone on file"
        )
    joined = "; ".join(names)
    return f"- Owner{'s' if len(names) > 1 else ''}: {joined}"


def _occupancy_rest(s: Any) -> list[str]:
    """What follows "X/Y occupied": the vacant count and, separately, the
    units whose occupancy is unknown. Unknown is never folded into vacant, and
    an empty list (the only case a caller may call "full") requires both to be
    zero (rule 14; sandbox 2026-10-02, "fully vacant" with 100 unknown)."""
    rest = [f"{s.vacant_count} vacant"] if s.vacant_count else []
    if s.occupancy_unknown_count:
        rest.append(f"{s.occupancy_unknown_count} occupancy unknown")
    return rest


def _rent_roll_line(detail: Any) -> str:
    """The snapshot's rent-roll line. An unknown roll says so, never "$0"
    (rule 14): Active leases with no known rent are counted, not summed as 0."""
    unknown = detail.rent_unknown_leases
    plural = "s" if unknown != 1 else ""
    if detail.rent_scheduled is None:
        return f"- Rent roll: rent not known for {unknown} active lease{plural}"
    line = f"- Rent roll: {_money(detail.rent_scheduled)}/mo scheduled"
    if unknown:
        line += f" (rent not known for {unknown} lease{plural})"
    return line


async def _property_snapshot_impl(property_query: str) -> str:
    company_id = _company_id()
    if not company_id:
        return _NO_COMPANY
    pool = get_pool()
    summaries = await cockpit.list_property_summaries(pool, company_id)
    if not summaries:
        return await _not_synced(company_id)

    query = (property_query or "").strip().lower()
    match = None
    if query.isdigit():
        match = next((s for s in summaries if s.buildium_id == int(query)), None)
    if match is None and query:
        candidates = [s for s in summaries if query in (s.name or "").lower()]
        if len(candidates) == 1:
            match = candidates[0]
        elif len(candidates) > 1:
            names = ", ".join(s.name or f"#{s.buildium_id}" for s in candidates)
            return f"Multiple properties match '{property_query}': {names}. Which one?"
    if match is None:
        names = ", ".join(s.name or f"#{s.buildium_id}" for s in summaries)
        return f"No property matches '{property_query}'. Properties on file: {names}."

    detail = await cockpit.get_property_detail(pool, company_id, match.buildium_id)
    if detail is None:
        return await _not_synced(company_id)
    s = detail.summary
    # ⚠ The ADDRESS is on the row and was being dropped. "Tell me about
    # AZ — 1700 W Washington St" is an address-phrased question, and a header
    # carrying only the property NAME leaves the reader unable to confirm the
    # lookup landed on the right building — which matters most when it did not.
    where = ", ".join(p for p in (s.address_line1, s.city, s.state) if p and p.strip())
    header = s.name or f"#{s.buildium_id}"
    lines = [
        f"{header}{f' — {where}' if where and where not in header else ''} — snapshot:",
        f"- Occupancy: {s.occupied_count}/{s.unit_count}"
        + (f" ({', '.join(rest)})" if (rest := _occupancy_rest(s)) else " (full)"),
        _rent_roll_line(detail)
        + (
            f", {_money(detail.rent_collected_mtd)} collected this month"
            if detail.rent_collected_mtd is not None
            else ""
        ),
        f"- Open work orders: {s.open_work_orders}",
    ]
    # ``None`` = this PMS sends no balances / ledger / lease links
    # (``cockpit.py``). ``_money(None)`` prints "$0", so each is checked here.
    if s.delinquent_total is not None:
        lines.append(
            f"- Delinquent: {_money(s.delinquent_total)}"
            + (f" across {s.delinquent_leases} leases" if s.delinquent_leases else "")
        )
    unsent = [
        what
        for what, value in (
            ("rent balances", s.delinquent_total),
            ("payments", detail.rent_collected_mtd),
            ("who lives in which unit", detail.tenants),
        )
        if value is None
    ]
    if unsent:
        cov = await coverage_for(pool, company_id)
        lines.append("- " + cov.not_sent(_listed(unsent), "this property"))
    # ⚠ WHO OWNS IT. `helixis_owners.property_ids` covers 48 of Blue Ridge's 50
    # properties and nothing joined it, so "who owns MA — 24 Beacon St?" had no
    # local answer at all: one measured run spent 176.7s across five Buildium
    # lookups and concluded "I could not determine which person or vendor owns
    # 24 Beacon St." The mirror knew. Same shape as gotchas 41/50/51/53 — the
    # association was synced and never read.
    owner_line = await _owner_of_property(pool, company_id, s.buildium_id)
    if owner_line:
        lines.append(owner_line)
    # ⚠ FETCHED AND THROWN AWAY. ``get_property_detail`` already builds
    # ``open_work_order_list`` and ``delinquency_list`` — this tool rendered
    # only their COUNT and TOTAL, so "what's open at this property" and "who
    # owes what here" each cost a separate lookup. Battery v10-100 q4 ("tell
    # me about AZ — 1700 W Washington St") spent 182s across FOUR specialist
    # consults and still admitted "the searches on this property didn't fully
    # complete". The items were one render away. Fourth instance of the shape
    # in gotchas 41 / 50 / 51 / 53.
    if detail.open_work_order_list:
        shown = detail.open_work_order_list[:5]
        bits = [
            str(w.get("title") or w.get("description") or "untitled").strip()[:70] for w in shown
        ]
        more = len(detail.open_work_order_list) - len(shown)
        lines.append(
            "- Open maintenance here: " + "; ".join(bits) + (f" (+{more} more)" if more > 0 else "")
        )
    if detail.delinquency_list:
        shown = detail.delinquency_list[:5]
        bits = []
        for d in shown:
            who = str(d.get("tenant_names") or d.get("unit_number") or "a lease").strip()
            bits.append(f"{who} {_money(float(d.get('total_balance') or 0))}")
        more = len(detail.delinquency_list) - len(shown)
        lines.append(
            "- Who is behind: " + "; ".join(bits) + (f" (+{more} more)" if more > 0 else "")
        )
    # Unit roster from the mirror — "what units are at X?" must never need
    # a live API call (properties + units are always mirrored).
    units = await cockpit.list_units_for_property(pool, company_id, match.buildium_id)
    if units:
        shown = units[:30]
        unit_bits = []
        for u in shown:
            label = str(u.get("unit_number") or f"#{u.get('buildium_id')}")
            # None is UNKNOWN (a Rentvine unit from the units export that carried
            # no `isVacant` flag, gotcha 187), never "vacant" (rule 14).
            occupied = u.get("is_occupied")
            occ = "occupancy unknown" if occupied is None else "occupied" if occupied else "vacant"
            rent = u.get("market_rent")
            unit_bits.append(f"{label} ({occ}{f', market {_money(rent)}' if rent else ''})")
        suffix = f" … and {len(units) - 30} more" if len(units) > 30 else ""
        lines.append(f"- Units ({len(units)}): " + "; ".join(unit_bits) + suffix)
    if detail.tenants:
        roster = ", ".join(
            f"{(t.get('first_name') or '')} {(t.get('last_name') or '')}".strip()
            + (f" (Unit {t['unit_number']})" if t.get("unit_number") else "")
            for t in detail.tenants[:12]
        )
        lines.append(f"- Tenants: {roster}")
    if detail.expiring_lease_list:
        nxt = detail.expiring_lease_list[0]
        when = nxt["lease_to_date"].date().isoformat() if nxt.get("lease_to_date") else "?"
        lines.append(f"- Next lease expiration: Unit {nxt.get('unit_number') or '?'} on {when}")
    lines.append(await _freshness_line(pool, company_id))
    return "\n".join(lines)


@function_tool
async def property_snapshot(property_query: str) -> str:
    """The operational picture for ONE property from Occupella's live-synced
    Buildium data: occupancy, rent roll (scheduled vs collected this month),
    open work orders, delinquency, tenant roster, next lease expiration.

    ``property_query`` is a property name fragment ("Garden Row") or a
    Buildium property id.

    Use this for "how is <property> doing?" / "who lives at <property>?" /
    "what's going on at <property>?" / "what units are at <property>?"
    questions — it includes the full unit roster (unit numbers, occupancy,
    market rent). Instant (no Buildium API calls); never follow it with
    live property/unit list tools for the same property. For the current
    truth about one specific record, or before any write, use the live
    buildium_* tools.
    """
    return await _property_snapshot_impl(property_query)


#: Stated on the payables rollup when the mirror carries NO paid bills.
#:
#: ⚠ THIS IS A GAP NOTICE, NOT AN ANSWER, and the distinction is why battery
#: v13 q06 cost 174 seconds. "When did we last pay Volt & Vine Electric?" sent
#: the router to ``buildium_agent`` FIVE times plus ``gmail_agent`` once, and
#: came back with nothing usable — because there is nothing to find. Live-
#: verified 2026-08-28: **0 of Blue Ridge's 67 mirrored bills are marked paid.**
#: The sync only ever lands unpaid ones, which fits gotcha 14 — the live
#: ``GET /v1/bills`` refuses any query whose result could include paid bills
#: unless BOTH paid-date bounds are present, so an unbounded sync gets exactly
#: this.
#:
#: The wrong fix is a ``vendor_payments`` rollup. Built on this data it would
#: answer "no payments on record" for every vendor — a FABRICATED ABSENCE, and
#: this product's worst failure mode (gotchas 26/55). The right fix is to say
#: the mirror cannot answer it, so the model stops paying for five consults to
#: rediscover that.
_NO_PAYMENT_HISTORY = (
    "⚠ PAYMENT HISTORY IS NOT MIRRORED. Every bill on file is unpaid — there "
    "are zero paid bills in this company's mirror, so questions like \"when "
    'did we last pay <vendor>" CANNOT be answered from here, and an empty '
    "result is NOT evidence that a vendor was never paid. Say that plainly "
    "rather than reporting nothing found, and do not consult a specialist to "
    "confirm it — the gap is in what we sync, not in what you looked up."
)


async def _paid_bill_count(pool: Any, company_id: str) -> int | None:
    """How many mirrored bills are marked paid. Zero is the interesting case —
    see ``_NO_PAYMENT_HISTORY``. Computed rather than assumed so the notice
    disappears by itself the day the sync starts landing paid bills.

    ⚠ Returns ``None`` for "could not tell", which is NOT the same as zero and
    must not collapse into it. Both claims this count drives — "payment history
    is not mirrored" and "everything is paid" — are assertions about the world,
    and an unreadable count is grounds for neither (gotcha 55: a guard that
    cannot see must decline to act, never report emptiness).
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT COUNT(*) AS paid
              FROM helixis_bills
             WHERE company_id = $1::uuid AND is_paid IS TRUE
            """,
            company_id,
        )
    if not row:
        return None
    paid = row.get("paid") if hasattr(row, "get") else row["paid"]
    return int(paid) if paid is not None else None


async def _unpaid_bills_impl() -> str:
    company_id = _company_id()
    if not company_id:
        return _NO_COMPANY
    pool = get_pool()
    cov = await coverage_for(pool, company_id)
    if not cov.sees("helixis_bills"):
        return cov.not_sent("bills", "your payables")
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            -- COALESCE to the line items, not to zero: a bill whose
            -- top-level total is NULL (which is EVERY production bill —
            -- Buildium doesn't send TotalAmount) still has its money in the
            -- lines. Ingest now derives this too, but reading it here as well
            -- means a future ingest regression can't turn the payables book
            -- into $0.00 again. Stays NULL when there is genuinely no amount.
            SELECT b.buildium_id, b.due_date, b.reference_number,
                   COALESCE(b.total_amount,
                            (SELECT SUM(bl2.amount)
                               FROM helixis_bill_lines bl2
                              WHERE bl2.company_id = b.company_id
                                AND bl2.bill_id = b.buildium_id)) AS total_amount,
                   v.name AS vendor_name,
                   -- ⚠ The memo is what the bill is FOR, and it is the half a
                   -- payables question usually wants. q27 ("what's the oldest
                   -- unpaid bill?") answered "Redbud General Maintenance —
                   -- $265, due July 21" and scored a factual miss, because the
                   -- expected answer is gutter cleaning. It was one column away.
                   -- A Rentvine bill has no line rows (ADR 0010 Decision
                   -- (b)); what it is FOR is the bill's own description. No
                   -- Buildium raw carries a lower-case "description" key.
                   COALESCE(
                       (SELECT string_agg(DISTINCT bl3.memo, '; ')
                          FROM helixis_bill_lines bl3
                         WHERE bl3.company_id = b.company_id
                           AND bl3.bill_id = b.buildium_id
                           AND NULLIF(TRIM(bl3.memo), '') IS NOT NULL),
                       NULLIF(TRIM(b.raw->>'description'), '')) AS memo,
                   (SELECT string_agg(DISTINCT p.name, ', ')
                      FROM helixis_bill_lines bl
                      JOIN helixis_properties p
                        ON p.buildium_id = bl.property_id AND p.company_id = bl.company_id
                     WHERE bl.company_id = b.company_id
                       AND bl.bill_id = b.buildium_id) AS properties
              FROM helixis_bills b
              LEFT JOIN helixis_vendors v
                ON v.buildium_id = b.vendor_id AND v.company_id = b.company_id
             WHERE b.company_id = $1::uuid AND b.is_paid IS NOT TRUE
             ORDER BY b.due_date NULLS LAST, b.total_amount DESC NULLS LAST
            """,
            company_id,
        )
    paid_on_file = await _paid_bill_count(pool, company_id)
    if not rows:
        summaries = await cockpit.list_property_summaries(pool, company_id)
        if not summaries:
            return await _not_synced(company_id)
        # ⚠ "everything is paid" is a claim about the PAID set, and this tool
        # only ever reads the unpaid one. Live-verified 2026-08-28: 0 of Blue
        # Ridge's 67 mirrored bills carry is_paid — so an empty result here
        # means "nothing unpaid is on file", which is NOT the same sentence.
        # Saying the stronger one would be gotcha 41's confident false claim
        # pointed at money.
        # ⚠ The weaker sentence fires ONLY on a definite zero. Unknown
        # (``None``) keeps the ordinary wording rather than manufacturing a
        # caveat out of a failed count — the same decline-to-assert direction
        # the count itself uses, pointed the other way.
        settled = (
            "no bills are marked paid in the mirror either, so this is not "
            "evidence that anything was settled"
            if paid_on_file == 0
            else "everything is paid"
        )
        return f"No unpaid bills on file — {settled}. " + await _freshness_line(pool, company_id)
    lines = ["Unpaid bills (soonest due first):"]
    total = 0.0
    unpriced = 0
    today = datetime.now(UTC).date()
    dupe_keys = Counter(_dupe_key(r) for r in rows)
    for r in rows:
        due = r["due_date"]
        overdue = " ⚠ OVERDUE" if due and due < today else ""
        due_part = f"due {due.isoformat()}" if due else "no due date"
        ref = f", ref {r['reference_number']}" if r["reference_number"] else ""
        at = f" at {r['properties']}" if r["properties"] else ""
        vendor = r["vendor_name"] or f"Bill #{r['buildium_id']}"
        amount = r["total_amount"]
        # An unknown amount says so. Rendering NULL as $0.00 is how a whole
        # payables book reported as zero and read like a fact (battery v2).
        if amount is None:
            unpriced += 1
            amount_part = "amount not recorded"
        else:
            total += float(amount)
            amount_part = _money(float(amount))
        what = f" — {r['memo']}" if r["memo"] else ""
        dupe = " ⚠ DUPLICATE-SHAPED" if dupe_keys[_dupe_key(r)] > 1 else ""
        # ⚠ The bill id (the PMS's own), which was fetched and used only as a
        # fallback label when a vendor name was missing. An accountant
        # reconciling "Apex — $450, gutter cleaning" cannot open that record
        # without it, and it is the atom of the traceability spine.
        #
        # It is here for the EXPORT, not for prose: every spelling of a bare
        # identifier is removed from the reply by design (a leaked record
        # number in chat is the thing reply_hygiene exists to stop), so an id
        # quoted into a sentence simply vanishes. ``export_table`` writes
        # bytes that never pass through that scrubber, which is where a
        # traceable figure can actually reach a person.
        lines.append(
            f"- {vendor}: {amount_part}{what}, {due_part}{at}{ref}{overdue}{dupe} "
            f"[bill {r['buildium_id']}]"
        )
    priced = len(rows) - unpriced
    tail = f" ({unpriced} with no amount recorded, not counted)" if unpriced else ""
    lines.append(f"Total unpaid: {_money(total)} across {priced} bills{tail}.")
    if paid_on_file == 0:
        lines.append(_NO_PAYMENT_HISTORY)
    # ⚠ Say what DUPLICATE-SHAPED means, and say what it does NOT mean. "Same
    # vendor, same amount, same due date" is a real pattern worth a human look
    # and is ALSO exactly what a legitimate per-property recurring invoice
    # looks like — 16 identical gutter-cleaning bills across 16 properties are
    # 16 real bills, not one billed 16 times. Calling them duplicates outright
    # would be the confident false claim of gotcha 41; saying nothing is how
    # q28 ("what should I NOT pay yet?") missed the duplicate kit entirely.
    if dupe_groups := sum(1 for n in dupe_keys.values() if n > 1):
        lines.append(
            f"{dupe_groups} group(s) above share a vendor, amount AND due date. That is "
            f"what a double-billed invoice looks like — and also what a per-property "
            f"recurring charge looks like. Check the properties on each before paying or "
            f"withholding; do NOT report them as duplicates on this signal alone."
        )
    lines.append(
        "Each row ends with its bill id. Those ids are for EXPORTS and "
        "lookups, not for prose — identifiers are stripped from chat replies, so "
        "quoting one into a sentence drops it. If the user wants to reconcile or "
        "hand this to an accountant, call export_table and include the id column."
    )
    lines.append(await _freshness_line(pool, company_id))
    return "\n".join(lines)


@function_tool
async def unpaid_bills() -> str:
    """Every unpaid vendor bill from Occupella's live-synced Buildium data:
    vendor, amount, due date (overdue flagged), the properties its lines
    charge, and the total owed.

    Use this for accounts-payable questions like:
      - "What bills are unpaid?" / "What do we owe vendors?"
      - "Anything overdue to pay?"
      - "How much is due this week?"

    Instant (no Buildium API calls). Before recording a payment or editing
    a bill, confirm current truth with the live buildium_* tools.
    """
    return await _unpaid_bills_impl()


async def _spending_report_impl(days: int, property_query: str) -> str:
    company_id = _company_id()
    if not company_id:
        return _NO_COMPANY
    days = max(1, min(int(days or 90), 365))
    query = (property_query or "").strip()
    pool = get_pool()
    cov = await coverage_for(pool, company_id)
    # ⚠ ALL THREE, not just the bills. Spend is summed from bill LINES and
    # categorised by GL account; a PMS that sends bills but not their lines
    # (Rentvine since 2026-10-01: ADR 0010 Decision (b) holds the lines back)
    # would otherwise read "No billed spend" from an empty join, a false
    # all-clear about money (rule 14).
    if not cov.sees("helixis_bills"):
        return cov.not_sent("bills", "your payables")
    if not (cov.sees("helixis_bill_lines") and cov.sees("helixis_gl_accounts")):
        return cov.not_sent("spending by category or property", "your expense reports")
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT COALESCE(g.name, 'Uncategorized') AS gl_name,
                   g.type AS gl_type,
                   SUM(bl.amount)                    AS total,
                   COUNT(DISTINCT b.buildium_id)     AS bill_count
              FROM helixis_bill_lines bl
              JOIN helixis_bills b
                ON b.buildium_id = bl.bill_id AND b.company_id = bl.company_id
              LEFT JOIN helixis_gl_accounts g
                ON g.buildium_id = bl.gl_account_id AND g.company_id = bl.company_id
             WHERE bl.company_id = $1::uuid
               AND b.bill_date >= current_date - make_interval(days => $2)
               AND ($3 = '' OR bl.property_id IN (
                       SELECT buildium_id FROM helixis_properties
                        WHERE company_id = $1::uuid AND name ILIKE '%' || $3 || '%'))
             GROUP BY 1, 2
             ORDER BY total DESC NULLS LAST
            """,
            company_id,
            days,
            query,
        )
        # ⚠ BY PROPERTY, the breakdown the GL grouping above cannot give.
        # "Which property is costing me the most in repairs?" spent 177s across
        # six tool calls and concluded "all of it is filed under
        # 'Uncategorized' GL category and the bills aren't linked to specific
        # property addresses" (battery v10-100 q48, still failing in v11).
        # ``bl.property_id`` was already in the query above — used to FILTER
        # and never to GROUP. Fifth instance this week of a field being in
        # hand and not surfaced (gotchas 41/50/51/53).
        by_property = await conn.fetch(
            """
            SELECT p.name                        AS property_name,
                   SUM(bl.amount)                AS total,
                   COUNT(DISTINCT b.buildium_id) AS bill_count
              FROM helixis_bill_lines bl
              JOIN helixis_bills b
                ON b.buildium_id = bl.bill_id AND b.company_id = bl.company_id
              JOIN helixis_properties p
                ON p.buildium_id = bl.property_id AND p.company_id = bl.company_id
             WHERE bl.company_id = $1::uuid
               AND b.bill_date >= current_date - make_interval(days => $2)
               AND ($3 = '' OR p.name ILIKE '%' || $3 || '%')
             GROUP BY p.name
             ORDER BY total DESC NULLS LAST
            """,
            company_id,
            days,
            query,
        )
        # ⚠ ADR 0010 Decision (b): a Rentvine line charged to a PORTFOLIO or
        # MANAGER ledger belongs to no single property, so the by-property
        # rollup below leaves it out. It must SAY so, with the amount, rather
        # than present property totals that silently miss part of the book.
        # Buildium lines carry no ledger_type, so this is 0 for them. Asked
        # only unscoped: a property filter already excludes these lines from
        # every total above, so "It IS in the category totals" would be false.
        unattributed = None
        if not query:
            unattributed = await conn.fetchval(
                """
                SELECT SUM(bl.amount)
                  FROM helixis_bill_lines bl
                  JOIN helixis_bills b
                    ON b.buildium_id = bl.bill_id AND b.company_id = bl.company_id
                 WHERE bl.company_id = $1::uuid
                   AND bl.ledger_type IN (1, 2)
                   AND b.bill_date >= current_date - make_interval(days => $2)
                """,
                company_id,
                days,
            )
    scope = f" at properties matching '{query}'" if query else ""
    if not rows:
        summaries = await cockpit.list_property_summaries(pool, company_id)
        if not summaries:
            return await _not_synced(company_id)
        return f"No billed spend in the last {days} days{scope}. " + await _freshness_line(
            pool, company_id
        )
    lines = [f"Billed spend by GL category, last {days} days{scope}:"]
    total = 0.0
    for r in rows:
        kind = f" ({r['gl_type']})" if r["gl_type"] else ""
        lines.append(
            f"- {r['gl_name']}{kind}: {_money(float(r['total'] or 0))} "
            f"across {r['bill_count']} bill(s)"
        )
        total += float(r["total"] or 0)
    lines.append(f"Total: {_money(total)}.")
    if by_property:
        lines.append("")
        lines.append(f"By property, highest first (last {days} days):")
        top = float(by_property[0]["total"] or 0)
        # ⚠ REPORT THE TIE. Blue Ridge has SEVENTEEN properties at exactly
        # $900 — the duplicate kit is two $450 bills per property — so "which
        # property is costing the most" has no single answer. Naming one would
        # be the right number attached to an invented winner, which is gotcha
        # 41's confident false claim pointed at money. The model reads this
        # line and writes prose from it, so the tie has to be stated here
        # rather than left to be inferred from a list.
        tied = [r for r in by_property if float(r["total"] or 0) == top]
        if len(tied) > 1:
            lines.append(
                f"⚠ {len(tied)} properties are TIED at the top on {_money(top)} each — "
                f"there is no single highest. Name the tie, do not pick one."
            )
        for r in by_property[:15]:
            lines.append(
                f"- {r['property_name']}: {_money(float(r['total'] or 0))} "
                f"across {r['bill_count']} bill(s)"
            )
        if len(by_property) > 15:
            lines.append(f"… and {len(by_property) - 15} more properties below these.")
    if unattributed:
        lines.append(
            f"Not counted by property: {_money(float(unattributed))} of spend charged to a "
            "portfolio or management ledger, which belongs to no single property. It IS "
            "in the category totals above. Do NOT split it across properties."
        )
    lines.append(await _freshness_line(pool, company_id))
    return "\n".join(lines)


@function_tool
async def spending_report(days: int = 90, property_query: str = "") -> str:
    """Billed vendor spend, grouped BOTH by GL category and BY PROPERTY, from
    Occupella's live-synced Buildium data, over the last N days (default 90),
    optionally filtered to properties whose name matches ``property_query``.

    Use this for spend-analysis questions like:
      - "How much did we spend on plumbing?"
      - "What's our maintenance spend at Garden Row this quarter?"
      - "Where is the money going?"

    Instant (no Buildium API calls).
    """
    return await _spending_report_impl(days, property_query)


async def _open_tasks_impl() -> str:
    company_id = _company_id()
    if not company_id:
        return _NO_COMPANY
    pool = get_pool()
    cov = await coverage_for(pool, company_id)
    if not cov.sees("helixis_tasks"):
        return cov.not_sent("tasks", "your task list")
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT t.buildium_id, t.title, t.task_type, t.status, t.priority,
                   t.due_date, t.category_name, p.name AS property_name,
                   NULLIF(TRIM(CONCAT(s.first_name, ' ', s.last_name)), '') AS assignee
              FROM helixis_tasks t
              LEFT JOIN helixis_properties p
                ON p.buildium_id = t.property_id AND p.company_id = t.company_id
              LEFT JOIN buildium_staff s
                ON s.buildium_user_id = t.assigned_to_user_id AND s.company_id = t.company_id
             WHERE t.company_id = $1::uuid
               AND {open_status_sql("t.status")}
             ORDER BY CASE WHEN t.due_date < current_date THEN 0 ELSE 1 END,
                      t.due_date NULLS LAST, t.priority
            """,
            company_id,
        )
    if not rows:
        summaries = await cockpit.list_property_summaries(pool, company_id)
        if not summaries:
            return await _not_synced(company_id)
        return "No open tasks — the board is clear. " + await _freshness_line(pool, company_id)
    lines = ["Open tasks (overdue first):"]
    today = datetime.now(UTC).date()
    for r in rows:
        due = r["due_date"]
        overdue = " ⚠ OVERDUE" if due and due < today else ""
        bits = [b for b in [r["status"], r["priority"], r["property_name"]] if b]
        if due:
            bits.append(f"due {due.isoformat()}")
        if r["assignee"]:
            bits.append(f"assigned to {r['assignee']}")
        title = r["title"] or f"Task #{r['buildium_id']}"
        lines.append(f"- {title} ({', '.join(bits)}){overdue}")
    lines.append(f"{len(rows)} open tasks in total.")
    lines.append(await _freshness_line(pool, company_id))
    return "\n".join(lines)


@function_tool
async def open_tasks() -> str:
    """Every open task (New / InProgress / Deferred) from Occupella's
    live-synced Buildium data — title, status, priority, property, due
    date (overdue flagged), and assignee — overdue first.

    Use this for workload questions like:
      - "What's on the task board?" / "What's overdue?"
      - "What is <staff member> working on?"
      - "Any open requests at <property>?"

    Instant (no Buildium API calls). For one specific task's current
    truth, or before updating a task, use the live buildium_* tools.
    """
    return await _open_tasks_impl()


#: How many work orders one call lists before it says how many more there are.
#: A cap with the remainder named, never a silent cut (gotchas 26/55).
_WORK_ORDER_LIST_CAP = 40

#: ⚠ ``status IS NULL`` is OPEN. A work order whose status did not arrive is
#: unknown, not finished (``maintenance_status.is_open``); ``NOT IN`` alone
#: would drop it, because ``NULL NOT IN (...)`` is not true.
_OPEN_WORK_ORDERS_SQL = f"""
SELECT w.buildium_id, w.title, w.status, w.priority,
       p.name AS property_name, u.unit_number,
       {operations_repo.vendor_key_present_sql("w")} AS has_vendor_key,
       {operations_repo.vendor_ref_sql("w")} AS vendor_ref,
       v.name AS vendor_name
  FROM helixis_work_orders w
  LEFT JOIN helixis_properties p
    ON p.company_id = w.company_id AND p.buildium_id = w.property_id
  LEFT JOIN helixis_units u
    ON u.company_id = w.company_id AND u.buildium_id = w.unit_id
  LEFT JOIN helixis_vendors v
    ON v.company_id = w.company_id
   AND v.buildium_id::text = {operations_repo.vendor_ref_sql("w")}
 WHERE w.company_id = $1::uuid
   AND {open_status_sql("w.status")}
 ORDER BY p.name NULLS LAST, w.buildium_id
"""  # noqa: S608 — module constants only, never caller input


def _vendor_phrase(row: Any) -> str | None:
    """Who is assigned, or ``None`` when the row cannot say (rule 14: an
    absent key is unknown, never "no vendor")."""
    if row["vendor_name"]:
        return f"vendor: {row['vendor_name']}"
    if row["vendor_ref"]:
        return f"vendor #{row['vendor_ref']} (not among the synced vendors)"
    if row["has_vendor_key"]:
        return "no vendor assigned"
    return None


def _work_order_line(row: Any) -> str:
    title = " ".join(str(row["title"] or "untitled").split())[:120]
    where = row["property_name"] or "property not on file"
    if row["unit_number"]:
        where += f", unit {row['unit_number']}"
    status = "status unknown" if row["status"] is None else str(row["status"])
    bits = [status.replace("InProgress", "In progress")]
    if row["priority"]:
        bits.append(f"{row['priority']} priority")
    if (vendor := _vendor_phrase(row)) is not None:
        bits.append(vendor)
    return f"- #{row['buildium_id']} {title} — {where} ({', '.join(bits)})"


async def _open_work_orders_impl(search: str = "", property_query: str = "") -> str:
    company_id = _company_id()
    if not company_id:
        return _NO_COMPANY
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(_OPEN_WORK_ORDERS_SQL, company_id)
    word = (search or "").strip().lower()
    place = (property_query or "").strip().lower()
    matched = [
        r
        for r in rows
        if (not word or word in f"{r['title'] or ''} {r['property_name'] or ''}".lower())
        and (not place or place in (r["property_name"] or "").lower())
    ]
    filtered = bool(word or place)
    if not rows:
        summaries = await cockpit.list_property_summaries(pool, company_id)
        if not summaries:
            return await _not_synced(company_id)
    if not matched:
        what = "No open work orders" + (" match that" if filtered else " on file")
        return f"{what}. " + await _freshness_line(pool, company_id)
    shown = matched[:_WORK_ORDER_LIST_CAP]
    listing = "\n".join(_work_order_line(r) for r in shown)
    lines = [
        f"Open work orders{' matching the filter' if filtered else ''} ({len(matched)}):",
        # ⚠ The titles are written by tenants and staff, and a Rentvine title
        # IS the free-text description. Fenced, so an instruction inside one
        # is data (the injection eval case), and the run is marked tainted.
        wrap_untrusted(listing, label="work-order descriptions, written by tenants and staff"),
    ]
    if len(matched) > len(shown):
        lines.append(
            f"(+{len(matched) - len(shown)} more not listed. Pass search or "
            "property_query to narrow it; never conclude one is absent from this list.)"
        )
    lines.append("The #number is the work order's id, which a status update needs.")
    lines.append(await _freshness_line(pool, company_id))
    return "\n".join(lines)


@function_tool
async def open_work_orders(search: str = "", property_query: str = "") -> str:
    """Every OPEN work order from Occupella's synced property-management data
    (Buildium or Rentvine): id, description, property, unit, status, priority
    and assigned vendor. Portfolio-wide in one call.

    Use this for "what's open in maintenance?", "list the open work orders",
    "who is assigned to the sink leak?", "anything open at <property>?". On a
    Rentvine workspace it is the ONLY way to read work orders. Pass ``search``
    to match a keyword in the description, ``property_query`` to scope to one
    property.

    Instant (no API calls). The id it prints is what a status update needs.
    """
    return await _open_work_orders_impl(search, property_query)


#: The sections a "full portfolio review" is made of, in reading order, each
#: paired with the bulk impl that answers it. Composed here rather than left to
#: the model: every one of these tools already existed and was already
#: discoverable, and the question STILL died at the deadline every time it was
#: asked, because answering it meant five separate decisions to reach for the
#: bulk tool instead of one more specialist consult. Gotchas 25/32/34 say the
#: same thing three times — only a structural stop works — and the structural
#: form of "call these five" is one tool that calls these five.
_PORTFOLIO_REVIEW_SECTIONS: tuple[tuple[str, Any], ...] = (
    ("Portfolio", _portfolio_overview_impl),
    ("Delinquency", _delinquency_report_impl),
    ("Unpaid vendor bills", _unpaid_bills_impl),
    ("Open work orders and tasks", _open_tasks_impl),
    ("Leases expiring within 90 days", lambda: _expiring_leases_impl(90)),
)


async def _portfolio_review_impl() -> str:
    """Run every review section CONCURRENTLY and compose one answer.

    Concurrency matters more than it looks: run sequentially these are five
    round trips on the critical path of a question that already has a 240s
    budget it cannot meet. Gathered together they are one.

    A failing section degrades to a named line rather than taking the answer
    down. A portfolio review that returns four of five sections is useful; one
    that returns an exception is the NULL reply this exists to eliminate. The
    failure is stated in the output — never silently omitted — because a
    missing section that reads as "nothing to report" is exactly the
    truncated-list-as-absence failure of gotcha 26.
    """
    results = await asyncio.gather(
        *(impl() for _, impl in _PORTFOLIO_REVIEW_SECTIONS),
        return_exceptions=True,
    )

    blocks: list[str] = []
    for (heading, _), result in zip(_PORTFOLIO_REVIEW_SECTIONS, results, strict=True):
        if isinstance(result, BaseException):
            logger.warning("portfolio review section %r failed", heading, exc_info=result)
            blocks.append(
                f"## {heading}\nThis section could not be loaded, so it is MISSING "
                "from this review — do not report it as empty or as nothing to "
                "report. Offer to retry just this part."
            )
            continue
        blocks.append(f"## {heading}\n{result}")

    return (
        "FULL PORTFOLIO REVIEW — every section below is already loaded.\n"
        "This is the complete answer to a portfolio-wide question. Do NOT call "
        "further lookups to expand any section; summarize what is here.\n\n" + "\n\n".join(blocks)
    )


@function_tool
async def portfolio_review() -> str:
    """THE tool for a broad, multi-part question about the whole portfolio.

    Returns every portfolio rollup at once, in ONE call: property and unit
    counts with occupancy, total delinquency with the largest debtors, unpaid
    vendor bills, open work orders and tasks with overdue flagged, and leases
    expiring within 90 days.

    Use this whenever the question asks for more than one of those at once:
      - "Give me a full portfolio review"
      - "How are we doing overall?" / "Brief me on everything"
      - "What needs attention across all properties?"

    This REPLACES calling portfolio_overview, delinquency_report,
    unpaid_bills, open_tasks and expiring_leases separately — it runs all of
    them for you. After this returns you have everything; write the answer.

    Instant (no Buildium API calls). For one specific record's current truth,
    or before any write, use the live buildium_* tools.
    """
    return await _portfolio_review_impl()


#: What the model must do when a name matches more than one person.
#:
#: ⚠ Deliberately the SAME instruction `buildium_tools` gives on its own
#: ambiguity list (gotcha 41). Two wordings for one rule drift, and the drift
#: here would be one surface telling the model to ask and the other letting it
#: choose — pinned across both by `test_contact_ambiguity_note.py`.
_MULTIPLE_PEOPLE_NOTE = (
    "Each line above is one person, complete. Do NOT combine a name from one "
    "line with a property or contact detail from another. Until the user says "
    "which one they mean, name ALL of them and ask; stating one confidently "
    "would be a guess presented as a fact."
)


#: Shown instead of contact details when a lookup matched more than one person.
_CONTACTS_WITHHELD_NOTE = (
    "Emails and phone numbers are shared only once the match is ONE person. Ask "
    "the user which of these they mean, then look that person up by full name."
)


def _contacts_allowed(matches: int) -> bool:
    """Contact details go to the model only for a single, exact match.

    Rentvine terms rule 20 (``design/rentvine-terms-rules.md``): send the model
    provider the minimum fields a task needs. Several matches means the user has
    not said who they mean yet, and eight people's emails and phones are not
    needed to ask them. Applies to every PMS; the mirror is shared.
    """
    return matches == 1


async def _contact_lookup_impl(name_query: str) -> str:
    company_id = _company_id()
    if not company_id:
        return _NO_COMPANY
    query = (name_query or "").strip()
    if not query:
        return "Give me a name (or part of one) to look up."
    pool = get_pool()

    # User-confirmed aliases FIRST (migration 0057): "Mike the plumber" was
    # saved once via remember_contact_alias — exact match beats any fuzzy
    # search, and the saved LABEL drives the detail lookup below so the
    # contact info still surfaces.
    alias_note = ""
    alias_row = await entity_aliases.lookup_alias(pool, company_id=company_id, alias=query)
    if alias_row is not None:
        alias_note = (
            f"'{alias_row.alias}' is your saved name for {alias_row.label or 'this contact'} "
            f"({alias_row.entity_type} #{alias_row.entity_id}).\n"
        )
        if alias_row.label:
            query = alias_row.label
    like = f"%{query}%"
    async with pool.acquire() as conn:
        # ⚠ ``lease_status`` and ``ended_on`` are the point of this query, not
        # decoration. The join already reached the lease to get the property
        # name and threw the STATUS away, so a tenant whose lease ended five
        # months ago rendered byte-identical to a current one and the reply
        # said "Harlan Archuleta is a tenant living at 120 4th St" (battery
        # v10-100, q10 — judged a safety violation). The model was not
        # hallucinating; the tool told it that. Same shape as gotchas 41 and
        # 50: the field was already in the row and was fetched and dropped.
        #
        # Ordered so a CURRENT lease wins when a person has both — an Active
        # lease they still live under first, then any Active lease, then the
        # most recent end date. Reporting someone's oldest tenancy would
        # recreate the bug pointing the other way. ``IS TRUE`` because a lease
        # with no status makes the first key NULL, and DESC sorts NULL first.
        tenants = await conn.fetch(
            f"""
            SELECT t.first_name, t.last_name, t.email, t.phone,
                   lease.properties, lease.lease_status, lease.ended_on,
                   lease.link_status
              FROM helixis_tenants t
              LEFT JOIN LATERAL (
                    SELECT COALESCE(p.name, '')          AS properties,
                           l.status                      AS lease_status,
                           l.lease_to_date::date         AS ended_on,
                           lt.status                     AS link_status
                      FROM helixis_lease_tenants lt
                      JOIN helixis_leases l
                        ON l.buildium_id = lt.lease_buildium_id
                       AND l.company_id = lt.company_id
                      LEFT JOIN helixis_properties p
                        ON p.buildium_id = l.property_id AND p.company_id = l.company_id
                     WHERE lt.company_id = t.company_id
                       AND lt.tenant_buildium_id = t.buildium_id
                     ORDER BY (l.status = 'Active' AND {current_link_sql()}) IS TRUE DESC,
                              (l.status = 'Active') DESC, l.lease_to_date DESC NULLS LAST
                     LIMIT 1
              ) lease ON TRUE
             WHERE t.company_id = $1::uuid
               AND CONCAT(t.first_name, ' ', t.last_name) ILIKE $2
             LIMIT 8
            """,
            company_id,
            like,
        )
        owners = await conn.fetch(
            """
            SELECT first_name, last_name, company_name, email, phone
              FROM helixis_owners
             WHERE company_id = $1::uuid
               AND CONCAT(first_name, ' ', last_name, ' ', COALESCE(company_name, ''))
                   ILIKE $2
             LIMIT 8
            """,
            company_id,
            like,
        )
        vendors = await conn.fetch(
            """
            SELECT name, company_name, category_name, email, phone
              FROM helixis_vendors
             WHERE company_id = $1::uuid
               AND CONCAT(COALESCE(name, ''), ' ', COALESCE(company_name, '')) ILIKE $2
             LIMIT 8
            """,
            company_id,
            like,
        )
    if not tenants and not owners and not vendors:
        summaries = await cockpit.list_property_summaries(pool, company_id)
        if not summaries:
            # Mirror not synced yet — search live Buildium HERE, inside this
            # one tool call, instead of returning "not synced" and watching
            # the agent fan out to list_tenants + list_owners + list_vendors
            # as three more steps (the 15-step "Brandon Oscar" transcript).
            # ⚠ Never for a Rentvine company: it has no Buildium to search,
            # and #1063 hid every other live Buildium path from its runs
            # (audit 2026-09-29). The same for every mirror-only system.
            if await _mirror_pms(company_id) is None:
                live = await _live_contact_fallback(query)
                if live:
                    return alias_note + live
            return await _not_synced(company_id)
        fuzzy = await _fuzzy_contact_fallback(pool, company_id, query)
        if fuzzy:
            return alias_note + fuzzy
        return alias_note + f"No tenant, owner, or vendor matches '{query}'."

    share = _contacts_allowed(len(tenants) + len(owners) + len(vendors))

    def _contact_bits(email: str | None, phone: str | None) -> str:
        if not share:
            return ""
        bits = [b for b in [email, phone] if b]
        return f" — {', '.join(bits)}" if bits else ""

    # A PMS that sends no lease-tenant links (Rentvine) leaves every tenant
    # without a lease row here, and "no lease on file" would be a false
    # statement about each of them. Asked only when there are tenants.
    cov = await coverage_for(pool, company_id) if tenants else None
    unlinked = cov is not None and not cov.sees("helixis_lease_tenants")

    def _tenancy(r: Any) -> str:
        """``tenant at Redbud Commons`` / ``FORMER tenant — lease at Redbud
        Commons ended 2026-03-13``.

        ⚠ The tense is the whole job. Without it the model writes "X is a
        tenant living at Y" about someone who moved out, which is a false
        statement about a real person and — because this tool also hands over
        their email — a former resident's contact details released on the
        strength of it.

        ⚠ FORMER is uppercase deliberately. The reply is prose written by a
        model reading this line, and a lowercase qualifier mid-row is exactly
        the kind of thing that gets smoothed away in summary. Same reasoning
        as the truncation footer in gotcha 26: when the consequence of missing
        a word is a confident false claim, make the word hard to miss.

        A tenant with no lease row at all reads "tenant, no lease on file" —
        NOT plain "tenant". Unknown must not render as current (gotcha 26).
        """
        # ⚠ The tense judgement comes from ``agents/tenancy.py``, not from a
        # comparison written here. It used to be an inline
        # ``status.lower() == "active"``, and the identical judgement written
        # separately in ``buildium_tools._format_tenant_profile`` was the bug
        # battery v13 q08 found: this surface said FORMER and that one said
        # "Past" at the bottom of a contact card. One definition now, so they
        # cannot disagree — the ``buildium/money.py::bill_amount`` precedent.
        status = (r["lease_status"] or "").strip()
        where = f" at {r['properties']}" if r["properties"] else ""
        if unlinked:
            source = (cov.name if cov else None) or "your property management system"
            return f"tenant, current or former unknown: lease links aren't synced from {source}"
        if tenancy_is_unknown(status):
            return f"tenant, {tenancy_label(status)}{where}"
        if is_current_tenancy(status):
            # ⚠ An active LEASE is not an active TENANCY: a co-tenant who moved
            # out keeps a MovedOut link on the lease the others still hold
            # (``db.tenant_links``). That person read as "tenant at Redbud
            # Commons". A link with no status keeps the lease's answer, the
            # same reading every link filter gives it.
            link = r["link_status"]
            if is_current_link(link):
                return f"tenant{where}"
            if link == MOVED_OUT_LINK_STATUS:
                return f"{FORMER} tenant — moved out; the lease{where} continues for the others"
            if link == FUTURE_LINK_STATUS:
                return f"{INCOMING} tenant{where} — not moved in yet"
            return f"tenant{where}, current or former unknown"
        if is_upcoming_tenancy(status):
            # Not FORMER: the lease has not started, and its end date is ahead.
            return f"{INCOMING} tenant{where} — lease {status}, not active yet"
        ended = r["ended_on"]
        when = f" ended {ended.isoformat()}" if ended else f" is {status}"
        lease_where = f" at {r['properties']}" if r["properties"] else ""
        return f"{FORMER} tenant — lease{lease_where}{when}"

    lines = [
        alias_note + f"People matching '{query}':" if alias_note else f"People matching '{query}':"
    ]
    for r in tenants:
        full = f"{r['first_name'] or ''} {r['last_name'] or ''}".strip()
        lines.append(f"- {full} ({_tenancy(r)}){_contact_bits(r['email'], r['phone'])}")
    for r in owners:
        full = f"{r['first_name'] or ''} {r['last_name'] or ''}".strip() or r["company_name"]
        lines.append(f"- {full} (owner){_contact_bits(r['email'], r['phone'])}")
    for r in vendors:
        label = r["name"] or r["company_name"]
        cat = f", {r['category_name']}" if r["category_name"] else ""
        lines.append(f"- {label} (vendor{cat}){_contact_bits(r['email'], r['phone'])}")
    # ⚠ The closing line only exists when MORE THAN ONE person matched.
    #
    # `buildium_get_tenant` got this in gotcha 41's fix — its ambiguity list
    # ends by telling the model to name all of them and ask. `contact_lookup`
    # is the MIRROR tool, the one the model reaches for first and the one
    # gotcha 51 was found in, and it never got the same line: it printed a
    # list and a freshness footer, which is an invitation to pick. Two tenants
    # sharing a surname is precisely the shape (checklist #104), and stating
    # one of them confidently is a guess presented as a fact about a real
    # person.
    #
    # ⚠ Silent on a single match, deliberately. A disambiguation instruction on
    # every lookup is one the model learns to skip, which is gotcha 52 pointed
    # at a prompt instead of a regex.
    if len(tenants) + len(owners) + len(vendors) > 1:
        lines.append(_MULTIPLE_PEOPLE_NOTE)
        lines.append(_CONTACTS_WITHHELD_NOTE)
    lines.append(await _freshness_line(pool, company_id))
    return "\n".join(lines)


async def _mirror_pms(company_id: str) -> registry.PmsSpec | None:
    """The mirror-only system this company is positively on (Rentvine, Rent
    Manager, Propertyware): this run's PMS binding (``run_pms``), else the
    same coverage lookup ``_not_synced`` makes. Buildium or unknown is None,
    which keeps the Buildium behaviour every company had before."""
    from helixis.agents.run_pms import run_pms_spec

    bound = run_pms_spec()
    if bound is not None and not bound.live_tools:
        return bound
    try:
        found = registry.spec((await coverage_for(get_pool(), company_id)).pms)
    except Exception:  # noqa: BLE001 — a routing refinement must never fail a read
        logger.warning("could not ask which PMS this company uses", exc_info=True)
        return None
    return found if found is not None and not found.live_tools else None


async def _live_contact_fallback(query: str) -> str | None:
    """Mirror empty → live-search Buildium tenants, owners, AND vendors in
    this single tool call (three HTTP fetches run concurrently; families
    are independent so one failing never discards the others — the
    gotcha-14 lesson). Returns None when Buildium isn't connected or every
    fetch failed, letting the caller fall back to _NOT_SYNCED."""
    import asyncio

    # Local import: buildium_tools does not import mirror_tools, so this
    # direction is cycle-safe; kept lazy to preserve that.
    from helixis.agents import buildium_tools as bt

    try:
        client, api, err = await bt._open_api()
    except Exception:
        return None
    if err or api is None or client is None:
        return None

    async def _safe(coro: Any) -> list[Any]:
        try:
            return list(await coro)
        except Exception:
            return []

    try:
        async with client:
            tenants, owners, vendors = await asyncio.gather(
                _safe(api.rentals.list_tenants()),
                _safe(api.rentals.list_owners()),
                _safe(api.vendors.list()),
            )
    except Exception:
        return None

    q = query.lower()

    def _hit(*parts: Any) -> bool:
        return q in " ".join(str(p) for p in parts if p).lower()

    # (label, contact) per match; contacts are attached only for ONE match.
    found: list[tuple[str, str]] = []
    for t in tenants:
        full = bt._tenant_full_name(t)
        if _hit(full, getattr(t, "email", None)):
            email, phone = getattr(t, "email", None), bt._first_phone(t)
            found.append((f"- {full} (tenant)", ", ".join(b for b in [email, phone] if b)))
    for o in owners:
        person = f"{getattr(o, 'first_name', '') or ''} {getattr(o, 'last_name', '') or ''}".strip()
        company = getattr(o, "company_name", None)
        if _hit(person, company, getattr(o, "email", None)):
            name = company or person or "Unnamed owner"
            found.append((f"- {name} (owner)", getattr(o, "email", None) or ""))
    for v in vendors:
        label = bt._vendor_label(v)
        if _hit(label, getattr(v, "email", None)):
            cat = bt._vendor_category_name(v)
            found.append((f"- {label} (vendor{f', {cat}' if cat else ''})", ""))

    share = _contacts_allowed(len(found))
    lines = [f"{label} — {contact}" if share and contact else label for label, contact in found]
    if len(found) > 1:
        lines.append(_CONTACTS_WITHHELD_NOTE)

    if not lines:
        return (
            f"No tenant, owner, or vendor matches '{query}' — searched live "
            "Buildium (all three contact types) because the local mirror "
            "hasn't synced yet. This is authoritative; no further lookups "
            "needed."
        )
    return "\n".join(
        [f"People matching '{query}' (live Buildium — mirror not synced yet):", *lines]
    )


async def _fuzzy_contact_fallback(pool: Any, company_id: str, query: str) -> str:
    """Typo-tolerant last resort (pg_trgm, migration 0057): when exact
    substring finds nothing, try similarity matching so "Jhon Smtih" still
    finds John Smith. Returns "" when the extension/indexes aren't applied
    yet (gotcha 8) or nothing clears the similarity threshold — the caller
    falls through to the honest no-match reply."""
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT full_name, kind, email, phone, sim FROM (
                    SELECT CONCAT(first_name, ' ', last_name) AS full_name,
                           'tenant' AS kind, email, phone,
                           similarity(CONCAT(first_name, ' ', last_name), $2) AS sim
                      FROM helixis_tenants WHERE company_id = $1::uuid
                    UNION ALL
                    SELECT COALESCE(NULLIF(CONCAT(first_name, ' ', last_name), ' '),
                                    company_name),
                           'owner', email, phone,
                           similarity(
                               CONCAT(first_name, ' ', last_name, ' ',
                                      COALESCE(company_name, '')), $2)
                      FROM helixis_owners WHERE company_id = $1::uuid
                    UNION ALL
                    SELECT COALESCE(name, company_name), 'vendor', email, phone,
                           similarity(
                               CONCAT(COALESCE(name, ''), ' ',
                                      COALESCE(company_name, '')), $2)
                      FROM helixis_vendors WHERE company_id = $1::uuid
                ) candidates
                WHERE sim > 0.3
                ORDER BY sim DESC
                LIMIT 5
                """,
                company_id,
                query,
            )
    except Exception:  # noqa: BLE001 — extension missing / pre-migration → no fallback
        logger.debug("trgm contact fallback unavailable (non-fatal)", exc_info=True)
        return ""
    if not rows:
        return ""
    # ⚠ No emails or phones, ever: a fuzzy match is a guess about who the user
    # meant, and a guessed person's contact details are exactly what rule 20
    # says not to send. Once confirmed, the exact lookup hands them over.
    lines = [f"No exact match for '{query}' — closest names in your synced data:"]
    for r in rows:
        lines.append(f"- {r['full_name']} ({r['kind']})")
    lines.append(
        "(fuzzy match — confirm which one the user meant, then look them up by "
        "full name for their contact details)"
    )
    return "\n".join(lines)


async def _remember_alias_impl(
    alias: str, entity_type: str, entity_id: int, label: str = ""
) -> str:
    company_id = _company_id()
    if not company_id:
        return _NO_COMPANY
    ctx = current_memory_context()
    ok = await entity_aliases.upsert_alias(
        get_pool(),
        company_id=company_id,
        alias=alias,
        entity_type=(entity_type or "").strip().lower(),
        entity_id=entity_id,
        label=(label or "").strip() or None,
        created_by=(ctx.user_id if ctx else None),
    )
    if not ok:
        return (
            "I couldn't save that shortcut — I need the alias, a valid type "
            "(tenant/owner/vendor/property/unit), and the Buildium id."
        )
    shown = label or f"{entity_type} #{entity_id}"
    return f"Saved — from now on '{alias}' means {shown}."


@function_tool
async def contact_lookup(name_query: str) -> str:
    """Find a person or company across the WHOLE Buildium contact graph —
    tenants (with their property), owners, and vendors (with category) —
    by name fragment, from Occupella's live-synced data. Checks the user's
    saved shortcuts first (see remember_contact_alias) and falls back to
    typo-tolerant fuzzy matching when nothing matches exactly.

    Use this for "who is X?" / "what's X's number/email?" / "do we work
    with a vendor called X?" questions when you don't know which kind of
    contact the name belongs to. Instant (no Buildium API calls).
    """
    return await _contact_lookup_impl(name_query)


@function_tool
async def remember_contact_alias(
    alias: str, entity_type: str, entity_id: int, label: str = ""
) -> str:
    """Save the user's own name for an entity ("Mike the plumber" → vendor
    4821) so future lookups resolve it instantly. Call this ONLY after the
    user has confirmed which entity they meant (e.g. they picked one from a
    disambiguation list, or explicitly asked you to remember a nickname).

    Args:
        alias: The user's shorthand, exactly as they say it.
        entity_type: One of tenant / owner / vendor / property / unit.
        entity_id: The Buildium id of the confirmed entity.
        label: The entity's display name (shown back on future lookups).
    """
    return await _remember_alias_impl(alias, entity_type, entity_id, label)


async def _bank_balances_impl() -> str:
    company_id = _company_id()
    if not company_id:
        return _NO_COMPANY
    pool = get_pool()
    cov = await coverage_for(pool, company_id)
    if not cov.sees("helixis_bank_accounts"):
        return cov.not_sent("bank accounts", "your bank accounts")
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT buildium_id, name, description, bank_account_type,
                   is_active, balance
              FROM helixis_bank_accounts
             WHERE company_id = $1::uuid
             ORDER BY is_active DESC NULLS LAST, balance DESC NULLS LAST
            """,
            company_id,
        )
    if not rows:
        summaries = await cockpit.list_property_summaries(pool, company_id)
        if not summaries:
            return await _not_synced(company_id)
        return (
            "No bank accounts in the mirror yet — run Sync data on the "
            "Buildium connector, or use the live buildium_* tools."
        )
    lines = ["Bank accounts:"]
    total = 0.0
    unknown = 0
    for r in rows:
        kind = f" ({r['bank_account_type']})" if r["bank_account_type"] else ""
        inactive = " — INACTIVE" if r["is_active"] is False else ""
        label = r["name"] or f"Account #{r['buildium_id']}"
        # ⚠ A NULL balance is "not on file", never $0 (rule 14): it was
        # `float(balance or 0)`, which printed an unknown balance as empty and
        # added nothing to the total while the total read as complete.
        if r["balance"] is None:
            lines.append(f"- {label}{kind}: balance not on file{inactive}")
            if r["is_active"] is not False:
                unknown += 1
            continue
        balance = float(r["balance"])
        lines.append(f"- {label}{kind}: {_money(balance)}{inactive}")
        if r["is_active"] is not False:
            total += balance
    tail = (
        f" ({unknown} active account(s) have no balance on file and are NOT counted)"
        if unknown
        else ""
    )
    lines.append(f"Total across active accounts: {_money(total)}{tail}.")
    lines.append(await _freshness_line(pool, company_id))
    return "\n".join(lines)


@function_tool
async def bank_balances() -> str:
    """Every bank account and its balance from Occupella's live-synced
    Buildium data — name, type (Checking/Savings), balance, inactive
    flags, and the total across active accounts. Balances refresh on
    every bank-transaction webhook.

    Use this for cash-position questions like:
      - "What's in our operating account?" / "How much cash do we have?"
      - "List our bank accounts."

    Instant (no Buildium API calls). Account/routing numbers are never
    stored or shown — direct the user to Buildium itself for those.
    """
    return await _bank_balances_impl()


async def _properties_near_impl(near_address: str, radius_miles: float) -> str:
    company_id = _company_id()
    if not company_id:
        return _NO_COMPANY
    address = (near_address or "").strip()
    if not address:
        return "Give me a place to search around (an address, landmark, or city)."
    settings = get_settings()
    if not geocoding_supported(settings):
        return _MAPS_UNCONFIGURED

    point = await geocode(address, settings=settings)
    if point is None:
        return (
            f"I couldn't find a location for '{near_address}'. Try a fuller "
            "address (street, city, state) or a well-known landmark."
        )

    radius_miles = max(0.1, min(float(radius_miles or 5.0), 100.0))
    radius_meters = radius_miles * _MILE_M
    pool = get_pool()
    try:
        nearby = await geo_repo.properties_near(
            pool,
            company_id=company_id,
            lat=point.lat,
            lng=point.lng,
            radius_meters=radius_meters,
            limit=25,
        )
    except Exception:  # noqa: BLE001 — missing 0053 migration / no geog column
        logger.warning("properties_near failed", exc_info=True)
        return _GEO_NOT_READY

    where = point.formatted_address or address
    if not nearby:
        return (
            f"None of your properties are within {radius_miles:g} miles of {where}. "
            "(Only geocoded properties are searchable — newly synced ones may not "
            "have coordinates yet.)"
        )
    lines = [f"Your properties within {radius_miles:g} miles of {where} (closest first):"]
    for p in nearby:
        miles = p.distance_meters / _MILE_M
        addr_bits = [b for b in [p.address_line1, p.city, p.state] if b]
        addr = f" — {', '.join(addr_bits)}" if addr_bits else ""
        label = p.name or f"#{p.buildium_id}"
        lines.append(f"- {label}{addr} ({miles:.1f} mi)")
    # Render a static map inline: the search center + each matched property.
    queue_map(
        [(point.lat, point.lng)] + [(p.lat, p.lng) for p in nearby],
        caption=f"Your properties near {where}",
    )
    lines.append(await _freshness_line(pool, company_id))
    return "\n".join(lines)


@function_tool
async def properties_near(near_address: str, radius_miles: float = 5.0) -> str:
    """Which of the company's OWN properties sit near a place, from Occupella's
    geocoded property mirror — each property's address and its distance in
    miles, nearest first.

    ``near_address`` is any place to search around: an address, a landmark,
    a neighborhood, or a city ("downtown Austin", "123 Main St, Dallas TX").
    ``radius_miles`` defaults to 5.

    Use this for proximity questions about the portfolio like:
      - "Which of my properties are near downtown?"
      - "Do we manage anything within 10 miles of the airport?"
      - "What's my closest property to <address>?"

    Instant local spatial query (one geocode of the search address, then a
    PostGIS read — no per-property Buildium API calls). For vendors/services
    near a property (not your own properties), that's a different lookup.
    """
    return await _properties_near_impl(near_address, radius_miles)


_INTEL_NO_ROWS = (
    "No property intelligence on file for this property yet — the data is "
    "gathered in the background after a property is geocoded (daily sync). "
    "Check back after the next sync."
)

# Display order + honest per-source "no data yet" lines.
_INTEL_SOURCES: tuple[tuple[str, str, str], ...] = (
    ("flood", "Flood zone (FEMA)", "no data yet"),
    ("wildfire", "Wildfire (USFS)", "no data yet"),
    ("radon", "Radon (EPA)", "no data yet (county not in the EPA zone table, or not fetched)"),
    ("superfund", "Superfund (EPA NPL)", "no data yet"),
    ("fmr", "Fair Market Rent (HUD)", "no data yet (needs the free HUD API key, or not fetched)"),
)


def _intel_as_of_line(fetched: dict[str, datetime | None], *, now: datetime | None = None) -> str:
    """Freshness footer for property intel — NOT ``_as_of_line``.

    ⚠ **This used to call `_as_of_line`, and both halves of what it said were
    false.** Measured against the live table on 2026-09-10:

        (from Occupella's synced Buildium data, updated 552h ago)

    on a FEMA flood zone. Buildium has never seen this data — it is FEMA, USFS,
    EPA and HUD — and `_as_of_line` renders everything past two hours as
    `f"{minutes // 60}h ago"`, which is right for a mirror that updates within
    seconds and wrong here by three orders of magnitude: a year-old row reads
    "9600h ago". The number was true and functionally a way of not saying it.
    That footer's own docstring promises "the mirror updates within seconds via
    webhooks and at least daily", which is a guarantee this caller does not
    have (gotcha 48's shape — a comment describing somebody else's property).

    ⚠ **Past-due is stated, never used to withhold the answer.** A flood zone
    from fourteen months ago is still the best answer available; suppressing it
    would be a fabricated absence, this product's worst failure mode
    (gotchas 26/55/57). The reader gets the finding AND its age.
    """
    dated = [at for at in fetched.values() if at is not None]
    age = describe_age(max(dated) if dated else None, now=now)
    line = (
        "(from public FEMA / USFS / EPA / HUD data Occupella gathered for this "
        f"property's coordinates, last checked {age})"
    )
    if overdue := stale_sources(fetched, now=now):
        # Named individually: "some of this is old" is not actionable, and the
        # sources age on very different schedules — HUD republishes Fair Market
        # Rent every October while the EPA radon map is a static 1993 product.
        line += (
            f" ⚠ Past its refresh window: {', '.join(overdue)}. Treat as a starting point "
            "and confirm against the source before relying on it."
        )
    return line


def _intel_detail(value: Any) -> dict[str, Any]:
    """jsonb comes back as str or dict depending on the pool's codec."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            import json

            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return {}


async def _unit_mix(pool: Any, company_id: str, buildium_property_id: int) -> list[str]:
    """Best-effort bedroom mix from the unit mirror's raw Buildium payload
    (``UnitBedrooms``: Studio / OneBed / TwoBed / ...). Empty when unknown."""
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT COALESCE(raw->>'UnitBedrooms', 'unknown') AS bedrooms,
                       COUNT(*) AS n
                  FROM helixis_units
                 WHERE company_id = $1::uuid AND property_id = $2
                 GROUP BY 1
                 ORDER BY 2 DESC
                """,
                company_id,
                buildium_property_id,
            )
    except Exception:  # noqa: BLE001 — cosmetic enhancement only
        return []
    return [f"{r['n']}× {r['bedrooms']}" for r in rows if r["bedrooms"] != "unknown"]


def _fmr_lines(detail: dict[str, Any], mix: list[str]) -> list[str]:
    order = (
        ("efficiency", "Efficiency"),
        ("one_bedroom", "1BR"),
        ("two_bedroom", "2BR"),
        ("three_bedroom", "3BR"),
        ("four_bedroom", "4BR"),
    )
    rents = [f"{label} ${detail[key]:,}" for key, label in order if detail.get(key) is not None]
    lines = []
    if rents:
        lines.append("  " + " · ".join(rents) + "/mo")
    if mix:
        lines.append(f"  Unit mix here: {', '.join(mix)}")
    return lines


async def _property_hazards_impl(property_query: str) -> str:
    company_id = _company_id()
    if not company_id:
        return _NO_COMPANY
    query = (property_query or "").strip()
    if not query:
        return "Give me a property name (or Buildium property id) to look up."
    pool = get_pool()
    try:
        matches = await geo_repo.resolve_property(pool, company_id=company_id, query=query)
    except Exception:  # noqa: BLE001 — missing 0053 migration / no geog column
        logger.warning("property_hazards: resolve failed", exc_info=True)
        return _GEO_NOT_READY
    if not matches:
        summaries = await cockpit.list_property_summaries(pool, company_id)
        if not summaries:
            return await _not_synced(company_id)
        names = ", ".join(s.name or f"#{s.buildium_id}" for s in summaries)
        return f"No property matches '{property_query}'. Properties on file: {names}."
    if len(matches) > 1:
        names = ", ".join(m.name or f"#{m.buildium_id}" for m in matches)
        return f"Multiple properties match '{property_query}': {names}. Which one?"
    prop = matches[0]

    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT source, code, label, detail, fetched_at
                  FROM helixis_property_intel
                 WHERE company_id = $1::uuid AND buildium_property_id = $2
                """,
                company_id,
                prop.buildium_id,
            )
    except Exception:  # noqa: BLE001 — migration 0066 not applied yet
        logger.warning("property_hazards: intel query failed (migration 0066?)", exc_info=True)
        return _INTEL_NO_ROWS
    by_source = {r["source"]: r for r in rows}
    if not by_source:
        return f"{prop.name or f'#{prop.buildium_id}'}: {_INTEL_NO_ROWS}"

    lines = [f"{prop.name or f'#{prop.buildium_id}'} — property intelligence:"]
    for source, heading, missing in _INTEL_SOURCES:
        row = by_source.get(source)
        if row is None:
            lines.append(f"- {heading}: {missing}")
            continue
        lines.append(f"- {heading}: {row['label'] or row['code'] or 'on file'}")
        detail = _intel_detail(row["detail"])
        if source == "superfund":
            for site in (detail.get("sites") or [])[:5]:
                if isinstance(site, dict):
                    lines.append(
                        f"  · {site.get('name', 'Unnamed site')} "
                        f"({site.get('distance_km', '?')} km, "
                        f"{site.get('npl_status', 'NPL status unknown')})"
                    )
        elif source == "fmr":
            lines.extend(_fmr_lines(detail, await _unit_mix(pool, company_id, prop.buildium_id)))
    lines.append(_intel_as_of_line({r["source"]: r["fetched_at"] for r in rows}))
    return "\n".join(lines)


@function_tool
async def property_hazards(property_query: str) -> str:
    """Hazard and pricing intelligence about ONE property (the physical
    parcel and its market), from public government data Occupella gathered for
    its stored coordinates: FEMA flood zone, USFS wildfire hazard potential,
    EPA county radon zone, nearby EPA Superfund (NPL) sites, and the HUD
    Fair Market Rents for its ZIP alongside the property's unit mix.

    ``property_query`` is a property name fragment ("Garden Row") or a
    Buildium property id.

    Use this for hazard / environment / pricing questions like:
      - "Is <property> in a flood zone?" / "What's the wildfire risk there?"
      - "Any environmental concerns near <property>?"
      - "How does our rent compare to the HUD fair market rent?"

    This is data about the PROPERTY — its parcel, environment, and market
    pricing — never about the people or demographics of an area. It must
    not be used to characterize neighborhoods or their residents, or to
    steer anyone toward or away from an area (fair-housing rule). Sources
    with no cached data are reported honestly as "no data yet". Instant
    (local read of previously gathered data; no external API calls).
    """
    return await _property_hazards_impl(property_query)


_JURISDICTION_NOT_RESOLVED = (
    "Jurisdiction not yet resolved for this property — resolution runs "
    "automatically after geocoding; ask again after the next sync."
)


def _jsonb_scalar(value: Any) -> Any:
    """``jurisdiction_notes.value`` is jsonb — asyncpg returns str or a
    decoded object depending on the pool's codec. Decode strings; pass
    decoded values (numbers, strings, null, dicts) through untouched."""
    if isinstance(value, str):
        try:
            import json

            return json.loads(value)
        except ValueError:
            return value
    return value


async def _jurisdiction_overrides(pool: Any, keys: list[str]) -> dict[str, Any]:
    """The config-table override layer (migration 0067): any
    ``jurisdiction_notes`` row for the property's stack WINS over the bundled
    JSON. ``keys`` is ordered general → specific ('US', state, county, place)
    so a more specific layer's value replaces a broader one. A pre-0067 DB
    degrades to no overrides."""
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT jurisdiction_key, param, value FROM public.jurisdiction_notes "
                "WHERE jurisdiction_key = ANY($1::text[])",
                keys,
            )
    except Exception:  # noqa: BLE001 — migration 0067 not applied yet
        logger.debug("jurisdiction_notes unavailable (migration 0067?)", exc_info=True)
        return {}
    by_key: dict[str, dict[str, Any]] = {}
    for r in rows:
        by_key.setdefault(r["jurisdiction_key"], {})[r["param"]] = _jsonb_scalar(r["value"])
    merged: dict[str, Any] = {}
    for key in keys:
        merged.update(by_key.get(key, {}))
    return merged


_DEADLINE_KINDS = ("deposit_return", "nonpay_notice", "mtm_termination")
_NOT_ENCODED_DEADLINE = "apply the most tenant-protective assumption and verify the statute"


def _deadline_flag_lines(flags: list[str]) -> list[str]:
    return [f"- ⚠ {flag}" for flag in flags]


def _deposit_return_lines(
    answer: jurisdiction.JurisdictionAnswer, state_abbr: str, trigger: date
) -> list[str]:
    group = answer.state.get("deposit")
    days = group.get("return_deadline_days") if isinstance(group, dict) else None
    if not isinstance(days, int | float) or isinstance(days, bool):
        return [
            "- Deposit return deadline: not encoded for this state — " + _NOT_ENCODED_DEADLINE + "."
        ]
    unit = (group.get("return_deadline_unit") or "calendar") if isinstance(group, dict) else None
    computed = deadline_math.compute_deadline(trigger, int(days), unit, state=state_abbr)
    completion = deadline_math.compliance_completion_date(computed.due_date, state=state_abbr)
    lines = [
        f"- Deposit return (tenancy ended {trigger.isoformat()}): return due by "
        f"{computed.due_date.isoformat()}; schedule completion by "
        f"{completion.isoformat()} — never rely on weekend roll-forward."
    ]
    lines.extend(_deadline_flag_lines(computed.flags))
    return lines


def _nonpay_notice_lines(
    answer: jurisdiction.JurisdictionAnswer, state_abbr: str, trigger: date
) -> list[str]:
    group = answer.state.get("notice")
    nonpay = group.get("nonpay_days") if isinstance(group, dict) else None
    if isinstance(nonpay, int | float) and not isinstance(nonpay, bool):
        unit = group.get("nonpay_unit") if isinstance(group, dict) else None
        computed = deadline_math.compute_deadline(trigger, int(nonpay), unit, state=state_abbr)
        lines = [
            f"- Nonpayment notice (served {trigger.isoformat()}): the notice period runs "
            f"through {computed.due_date.isoformat()} ({computed.unit_used} days)."
        ]
        lines.extend(_deadline_flag_lines(computed.flags))
        return lines
    if nonpay is not None:
        return [
            f"- Nonpayment notice: no fixed statutory period applies here — "
            f"the regime is '{nonpay}'."
        ]
    return ["- Nonpayment notice period: not encoded — " + _NOT_ENCODED_DEADLINE + "."]


def _mtm_termination_lines(
    answer: jurisdiction.JurisdictionAnswer, state_abbr: str, trigger: date
) -> list[str]:
    group = answer.state.get("notice")
    mtm = group.get("mtm_termination_days") if isinstance(group, dict) else None
    if isinstance(mtm, int | float) and not isinstance(mtm, bool):
        computed = deadline_math.compute_deadline(trigger, int(mtm), "calendar", state=state_abbr)
        lines = [
            f"- Month-to-month termination (notice given {trigger.isoformat()}): effective "
            f"no earlier than {computed.due_date.isoformat()}."
        ]
        lines.extend(_deadline_flag_lines(computed.flags))
        return lines
    if mtm is not None:
        return [f"- Month-to-month termination: no fixed statutory day count — {mtm}."]
    return ["- Month-to-month termination period: not encoded — " + _NOT_ENCODED_DEADLINE + "."]


def _deadline_lines(
    answer: jurisdiction.JurisdictionAnswer,
    state_abbr: str,
    deadline_kind: str,
    trigger_date: str,
) -> list[str]:
    """Computed-deadline lines for the snapshot, or honest error text. Never
    raises — an invalid input degrades to a plain explanation line."""
    kind = (deadline_kind or "").strip().lower()
    raw = (trigger_date or "").strip()
    if not kind and not raw:
        return []
    if not kind or not raw:
        return [
            "- Deadline not computed — both deadline_kind and trigger_date (YYYY-MM-DD) are needed."
        ]
    if kind not in _DEADLINE_KINDS:
        return [
            f"- Deadline not computed — unknown deadline_kind '{deadline_kind}'. "
            f"Valid kinds are {', '.join(_DEADLINE_KINDS)}."
        ]
    try:
        trigger = date.fromisoformat(raw)
    except ValueError:
        return [
            f"- Deadline not computed — trigger_date '{trigger_date}' is not a "
            "valid YYYY-MM-DD date."
        ]
    try:
        if kind == "deposit_return":
            return _deposit_return_lines(answer, state_abbr, trigger)
        if kind == "nonpay_notice":
            return _nonpay_notice_lines(answer, state_abbr, trigger)
        return _mtm_termination_lines(answer, state_abbr, trigger)
    except Exception:  # noqa: BLE001 — deadline math is an enhancement; degrade
        logger.warning("deadline computation failed (kind=%s)", kind, exc_info=True)
        return ["- Deadline not computed — the computation failed; verify the statute directly."]


def _capture_jurisdiction_lookup(
    *,
    company_id: str,
    state: str | None,
    county_fips: str | None,
    place_fips: str | None,
    resolved: bool,
    deadline_kind: str,
    rules_version: str | None,
) -> None:
    """Best-effort telemetry — jurisdiction identifiers only (state/FIPS),
    never property names, addresses, or tenant data. Must never affect the
    answer, so everything is swallowed."""
    try:
        from helixis.analytics.posthog_client import capture

        ctx = current_memory_context()
        distinct_id = (ctx.user_id if ctx and ctx.user_id else company_id) or "server"
        capture(
            distinct_id,
            "jurisdiction_lookup",
            {
                "state": state,
                "county_fips": county_fips,
                "place_fips": place_fips,
                "resolved": resolved,
                "deadline_kind": (deadline_kind or "").strip().lower() or None,
                "rules_version": rules_version,
                "company_id": company_id,
            },
            company_id=company_id,
        )
    except Exception:  # noqa: BLE001, S110 — analytics never affects the answer
        pass


async def _jurisdiction_lookup_impl(
    property_query: str, deadline_kind: str = "", trigger_date: str = ""
) -> str:
    company_id = _company_id()
    if not company_id:
        return _NO_COMPANY
    query = (property_query or "").strip()
    if not query:
        return "Give me a property name (or Buildium property id) to look up."
    pool = get_pool()
    try:
        matches = await geo_repo.resolve_property(pool, company_id=company_id, query=query)
    except Exception:  # noqa: BLE001 — missing 0053 migration / no geog column
        logger.warning("jurisdiction_lookup: resolve failed", exc_info=True)
        return _GEO_NOT_READY
    if not matches:
        summaries = await cockpit.list_property_summaries(pool, company_id)
        if not summaries:
            return await _not_synced(company_id)
        names = ", ".join(s.name or f"#{s.buildium_id}" for s in summaries)
        return f"No property matches '{property_query}'. Properties on file: {names}."
    if len(matches) > 1:
        names = ", ".join(m.name or f"#{m.buildium_id}" for m in matches)
        return f"Multiple properties match '{property_query}': {names}. Which one?"
    prop = matches[0]
    label = prop.name or f"#{prop.buildium_id}"

    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT state_fips, county_fips, place_fips, "
                "       county_name, place_name "
                "  FROM public.helixis_properties "
                " WHERE company_id = $1::uuid AND buildium_id = $2",
                company_id,
                prop.buildium_id,
            )
    # ⚠ This SELECT now names 0099's `county_name`/`place_name` as well as
    # 0067's FIPS columns, so a database missing EITHER migration lands here and
    # the tool answers "not resolved yet" instead of a snapshot. That is loud,
    # honest and self-correcting the moment the migration applies —
    # migration_check.py CAN see 0099 (it adds columns), unlike a data-only one.
    except Exception:  # noqa: BLE001 — migration 0067 or 0099 not applied yet
        logger.warning(
            "jurisdiction_lookup: jurisdiction column read failed (migration 0067/0099?)",
            exc_info=True,
        )
        return f"{label}: {_JURISDICTION_NOT_RESOLVED}"
    state_fips = (row["state_fips"] if row else None) or ""
    if not state_fips:
        _capture_jurisdiction_lookup(
            company_id=company_id,
            state=None,
            county_fips=None,
            place_fips=None,
            resolved=False,
            deadline_kind=deadline_kind,
            rules_version=None,
        )
        return f"{label}: {_JURISDICTION_NOT_RESOLVED}"
    state_abbr = jurisdiction.STATE_FIPS_TO_ABBR.get(state_fips)
    if not state_abbr:
        return (
            f"{label}: resolved to an unrecognized state FIPS code ({state_fips}) — "
            "I can't map it to a jurisdiction snapshot."
        )
    county_fips = row["county_fips"]
    place_fips = row["place_fips"]

    keys = ["US", f"state:{state_abbr}"]
    if county_fips:
        keys.append(f"county:{county_fips}")
    if place_fips:
        keys.append(f"place:{place_fips}")
    overrides = await _jurisdiction_overrides(pool, keys)
    # ⚠ `today` decides which DATED pending change of a landlord-tenant statute
    # is in force, so it must be the company's date and not the container's.
    # Left uninjected, `jurisdiction.lookup` falls back to `date.today()` — UTC,
    # which is not the manager's calendar for 4 hours a day in New York and 10
    # in Honolulu (migration 0098).
    answer = jurisdiction.lookup(
        state_abbr,
        county_fips=county_fips,
        place_fips=place_fips,
        # ⚠ Rendered in the snapshot header so a wrong county or place is
        # VISIBLE. The rules below it come from the state block plus whichever
        # overlay these select, and naming only the state made a boundary
        # mis-geocode indistinguishable from a correct answer (item 196).
        county_name=row["county_name"],
        place_name=row["place_name"],
        overrides=overrides or None,
        today=today_in(await company_timezone(pool, company_id)),
    )
    text = jurisdiction.format_compact(answer, property_label=label)
    extra = _deadline_lines(answer, state_abbr, deadline_kind, trigger_date)
    if extra:
        # Computed deadlines slot in BEFORE the mandatory footer — the
        # footer stays the last line (test-pinned contract).
        lines = text.splitlines()
        text = "\n".join(lines[:-1] + extra + [lines[-1]])
    _capture_jurisdiction_lookup(
        company_id=company_id,
        state=state_abbr,
        county_fips=county_fips,
        place_fips=place_fips,
        resolved=True,
        deadline_kind=deadline_kind,
        rules_version=answer.version,
    )
    return text


@function_tool
async def jurisdiction_lookup(
    property_query: str, deadline_kind: str = "", trigger_date: str = ""
) -> str:
    """Which landlord-tenant rules govern ONE property — the operational
    parameters of its state (and, where applicable, its local overlay
    jurisdiction) with statute citations. Covers security-deposit caps and
    return deadlines, late-fee rules, required notice periods (nonpayment,
    month-to-month termination, entry), source-of-income screening
    protections, property-management licensing, eviction speed, and
    state-specific quirks.

    ``property_query`` is a property name fragment ("Garden Row") or a
    Buildium property id.

    When a concrete trigger date is known, pass BOTH ``deadline_kind`` (one
    of deposit_return, nonpay_notice, mtm_termination) and ``trigger_date``
    (YYYY-MM-DD — the move-out date, notice-service date, or notice-given
    date respectively) and the snapshot appends the computed calendar due
    date, using the state's day-count rules (calendar vs business days,
    state holidays). deposit_return also computes the compliance completion
    date (the last business day strictly before the due date) — schedule to
    that, never to the statutory last minute.

    Use this whenever a question touches deposits, notices, evictions,
    late fees, rent increases, or tenant screening for a property.

    These are operational parameters with citations, NOT legal advice.
    Deposit dispositions, termination notices, rent increases, and
    screening denials are high-liability actions that require human
    confirmation and verification against the current controlling statute
    before executing — never act from this snapshot alone. This tool
    never assists refusing a housing voucher or any other source-of-income
    discrimination, even where state law is silent. Instant (local read of
    the property's previously resolved jurisdiction; no external API
    calls).
    """
    return await _jurisdiction_lookup_impl(property_query, deadline_kind, trigger_date)


#: ⚠ "Spend" is the wrong word for this number and the tool must not use it.
#: Live-verified 2026-08-29: 0 of Blue Ridge's 67 mirrored bills are marked
#: paid, because the sync can only fetch UNPAID ones (gotcha 14/57). So the
#: 12-month figure is what a vendor has INVOICED, not what we have paid them —
#: and "we spent $15,300 with Apex" would be a confident false claim about
#: money, in the exact shape gotchas 41/51/53 keep producing.
_BILLED_NOT_PAID = (
    "⚠ Amounts are BILLED, not PAID. These are invoice totals, paid and "
    "unpaid alike — do NOT describe them as money spent or vendors as having "
    "been paid."
)

#: Completion speed needs ``helixis_work_order_status_history``, which is
#: written only by live status-change webhooks. Live-verified 2026-08-29: 0
#: rows for Blue Ridge, 3 across all companies — so the average is NULL for
#: essentially every vendor. Rendering a blank invites "this vendor is slow";
#: saying nothing invites the model to infer it from work-order counts.
_NO_COMPLETION_TIMES = (
    "⚠ No completion times are on file (they come from live work-order status "
    "webhooks, and none have been recorded yet), so this cannot rank vendors "
    "by speed. Absence of a time is NOT evidence a vendor is slow."
)


async def _vendor_performance_impl() -> str:
    company_id = _company_id()
    if not company_id:
        return _NO_COMPANY
    pool = get_pool()
    from helixis.db.repositories.operations import list_vendor_ops

    try:
        rows = await list_vendor_ops(pool, company_id)
    except Exception:  # noqa: BLE001 — a rollup must degrade, never raise
        logger.warning("vendor_performance failed", exc_info=True)
        return (
            "The vendor rollup could not be read just now (an error on OUR side). "
            "Do NOT report the company as having no vendors."
        )
    if not rows:
        name = (await coverage_for(pool, company_id)).name
        return (
            "No vendors are mirrored for this company. That means none have "
            f"synced from {name or 'its property management system'} — it is not "
            "evidence the company uses none."
        )

    billed = all(r.spend_12m is not None for r in rows)
    lines = ["Vendors, by amount billed over the last 12 months:" if billed else "Vendors:"]
    for r in rows:
        bits = [f"- {r.name or f'vendor #{r.buildium_id}'}"]
        if r.category_name:
            bits.append(f" ({r.category_name})")
        if not r.is_active:
            # ⚠ INACTIVE is uppercase for gotcha 51's reason: a lowercase
            # mid-row qualifier is what gets smoothed away when a model
            # summarises, and recommending a retired vendor is a real error.
            bits.append(" — INACTIVE")
        # ``None`` = the PMS sends no bills; "$0 billed" would rank every
        # vendor as unused.
        # Same for work orders when they can't be attributed to a vendor.
        facts = []
        if r.spend_12m is not None:
            facts.append(f"{_money(r.spend_12m)} billed across {r.bill_count_12m} bills")
        if r.total_work_orders is not None:
            facts.append(f"{r.total_work_orders} work orders ({r.open_work_orders} open)")
        if facts:
            bits.append(": " + ", ".join(facts))
        if r.jobs_90d:
            bits.append(f", {r.jobs_90d} in the last 90 days")
        if r.avg_days_to_complete is not None:
            bits.append(f", avg {r.avg_days_to_complete:.1f} days to complete")
        lines.append("".join(bits))

    unsent = [
        what
        for what, missing in (
            ("bills", not billed),
            ("which vendor did each work order", any(r.total_work_orders is None for r in rows)),
        )
        if missing
    ]
    if unsent:
        cov = await coverage_for(pool, company_id)
        lines.append(cov.not_sent(_listed(unsent), "each vendor"))
    if billed:
        lines.append(_BILLED_NOT_PAID)
    # Computed, never hardcoded (gotcha 57): the day status webhooks start
    # landing, this caveat becomes false and must disappear by itself.
    if all(r.avg_days_to_complete is None for r in rows):
        lines.append(_NO_COMPLETION_TIMES)
    lines.append(
        "Every row already carries billing, work-order volume and activity — do "
        "NOT look these up per vendor."
    )
    lines.append(await _freshness_line(pool, company_id))
    return "\n".join(lines)


@function_tool
async def vendor_performance() -> str:
    """Every vendor with their 12-month billing, work-order volume, recent
    activity and completion speed — the whole book in ONE call.

    Use this for any question comparing or ranking vendors: "who do we use
    most", "which vendor bills us the most", "who handles plumbing", "is this
    vendor still active", "who should I call for X".

    Amounts are BILLED, not paid. Instant (no Buildium API calls).
    """
    return await _vendor_performance_impl()


#: ⚠ An empty leasing pipeline has TWO causes and they call for opposite
#: actions. Live-verified 2026-08-29: 28 applicants exist across the three
#: companies and 0 leads carry ``source='buildium'`` — which looks exactly
#: like a broken sync and is not one. ``sync_lead_from_applicant`` takes
#: ``allow_create=False`` from the BACKFILL by design ("a full historical
#: import stays link/move-only and can't mass-create a lead for every past
#: applicant"); only the live applicant webhook creates. So a company whose
#: applicants were imported historically has a legitimately empty board, and
#: reporting that as "you have no prospects" would be a false claim about
#: somebody's business.
_EMPTY_PIPELINE_WITH_APPLICANTS = (
    "⚠ No leads are on the pipeline board, but {n} applicant(s) ARE mirrored "
    "from Buildium. That is expected rather than broken: leads are created "
    "from LIVE applicant webhooks, and applicants imported by the historical "
    "backfill are deliberately linked rather than created. Say that — do not "
    "report the company as having no leasing activity."
)

#: The same empty board on any other system has another cause: nothing turns
#: its applications into leads (``sync_lead_from_applicant`` runs on Buildium's
#: webhook and backfill only), so "linked by the backfill" would be false and
#: none of them will ever reach the board.
_EMPTY_PIPELINE_WITH_SYNCED_APPLICANTS = (
    "⚠ No leads are on the pipeline board, but {n} applicant(s) ARE synced from "
    "{system}. Occupella does not turn {system} applications into pipeline "
    "leads, so they are not on the board. Say that — do not report the company "
    "as having no leasing activity."
)


async def _leasing_funnel_impl() -> str:
    company_id = _company_id()
    if not company_id:
        return _NO_COMPANY
    pool = get_pool()
    from helixis.db.repositories import leads as leads_repo

    try:
        by_stage = await leads_repo.count_leads_by_stage(pool, company_id=company_id)
        metrics = await leads_repo.lead_metrics(pool, company_id=company_id)
        cold = await leads_repo.cold_leads(pool, company_id=company_id, limit=10)
    except Exception:  # noqa: BLE001 — a rollup must degrade, never raise
        logger.warning("leasing_funnel failed", exc_info=True)
        return (
            "The leasing pipeline could not be read just now (an error on OUR "
            "side). Do NOT report the company as having no leads."
        )

    if metrics.total == 0:
        async with pool.acquire() as conn:
            applicants = await conn.fetchval(
                "SELECT count(*) FROM helixis_applicants WHERE company_id = $1::uuid",
                company_id,
            )
        cov = await coverage_for(pool, company_id)
        if applicants:
            if cov.pms is not None and cov.pms != registry.BUILDIUM:
                return _EMPTY_PIPELINE_WITH_SYNCED_APPLICANTS.format(
                    n=applicants, system=cov.name or "the property management system"
                )
            return _EMPTY_PIPELINE_WITH_APPLICANTS.format(n=applicants)
        if not cov.sees("helixis_applicants"):
            return "No leads on file in Occupella. " + cov.not_sent(
                "rental applications", "your applicants"
            )
        # ⚠ An empty applicants table is only "no pipeline" once the
        # applicants stage has landed: a read the key's role refused leaves it
        # empty too (2026-10-02, Rentvine applications).
        if await sync_state.get_stage_sync(pool, company_id, "applicants") is None:
            return (
                "No leads on file in Occupella, and applications haven't synced for this "
                "workspace yet, so I can't say whether anyone has applied. Check the "
                f"applications in {cov.name or 'your property management system'}."
            )
        return (
            "No leads and no mirrored applicants for this company — there is "
            "genuinely no leasing pipeline on file yet."
        )

    lines = ["Leasing pipeline (active leads by stage):"]
    for stage in leads_repo.LEAD_STAGES:
        lines.append(f"- {stage.capitalize()}: {by_stage.get(stage, 0)}")
    lines.append(
        f"New this month: {metrics.new_this_month} (previous month "
        f"{metrics.new_prev_month}); {metrics.active} active, "
        f"{metrics.leased} leased, {metrics.total} lifetime."
    )
    if cold:
        lines.append(f"\n{len(cold)} going cold (longest-quiet first):")
        for lead in cold:
            who = lead.name or lead.email or lead.phone or "unnamed lead"
            lines.append(f"- {who} — {lead.stage}")
    lines.append(
        "Counts are ACTIVE leads; lost leads are excluded from the stage "
        "breakdown but counted in the lifetime total."
    )
    lines.append(await _freshness_line(pool, company_id))
    return "\n".join(lines)


@function_tool
async def leasing_funnel() -> str:
    """The leasing pipeline in ONE call: active leads by stage, this month vs
    last, how many are leased, and which are going cold.

    Use this for any prospect or leasing-pipeline question — "how's leasing
    going", "how many applications this month", "who needs a follow-up",
    "what's in the pipeline".

    Instant (no Buildium API calls).
    """
    return await _leasing_funnel_impl()


mirror_tools = [
    portfolio_review,
    portfolio_overview,
    delinquency_report,
    expiring_leases,
    rent_roll,
    lease_payment_history,
    property_snapshot,
    unpaid_bills,
    spending_report,
    vendor_performance,
    leasing_funnel,
    open_tasks,
    open_work_orders,
    contact_lookup,
    remember_contact_alias,
    bank_balances,
    properties_near,
    property_hazards,
    jurisdiction_lookup,
]

__all__ = [
    "bank_balances",
    "contact_lookup",
    "delinquency_report",
    "expiring_leases",
    "jurisdiction_lookup",
    "lease_payment_history",
    "mirror_tools",
    "open_tasks",
    "open_work_orders",
    "portfolio_overview",
    "portfolio_review",
    "properties_near",
    "property_hazards",
    "property_snapshot",
    "rent_roll",
    "spending_report",
    "unpaid_bills",
]
