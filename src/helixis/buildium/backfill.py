"""Full-account pull into the cockpit entity mirror (migration 0038).

The webhook-driven sync keeps the mirror fresh *incrementally*, but a company
that just connected Buildium has an empty mirror — this module does the
initial (and re-runnable) bulk pull: properties → units → tenants → leases
(+ tenant links) → outstanding balances → recent lease transactions.

Design notes:

* Stages are independent — a failure in one is recorded and the rest still
  run (a partial mirror beats an empty one). Every upsert is idempotent
  (buildium_id conflict keys), so re-running converges.
* The Buildium client already enforces the 10 req/s account-wide token
  bucket, so this module doesn't pace itself.
* Lease transactions are fetched per ACTIVE lease with the server-side
  ``transactiondatefrom`` filter (verified against the LeadSimple reference
  client, LeaseTransactionsApi ``get_lease_ledgers``) limited to the last
  ``TXN_LOOKBACK_DAYS``. A client-side date guard stays as belt-and-braces
  in case a Buildium API revision changes the param semantics.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg

from helixis.buildium.api import BuildiumApi
from helixis.buildium.client import BuildiumClient
from helixis.buildium.errors import BuildiumError
from helixis.db.repositories import entities
from helixis.settings import BuildiumSettings
from helixis.webhooks.crm_applicant_sync import sync_lead_from_applicant

logger = logging.getLogger(__name__)

# Window for mirrored lease transactions. A full year (not just the current
# collection window) so "how does July compare to January" is a local SQL
# question — the server-side date filter keeps the widened window cheap.
TXN_LOOKBACK_DAYS = 365


@dataclass
class BackfillResult:
    """Per-stage row counts + non-fatal stage errors."""

    properties: int = 0
    units: int = 0
    tenants: int = 0
    leases: int = 0
    balances: int = 0
    transactions: int = 0
    work_orders: int = 0
    applicants: int = 0
    vendors: int = 0
    owners: int = 0
    tasks: int = 0
    gl_accounts: int = 0
    bills: int = 0
    bank_accounts: int = 0
    errors: list[str] = field(default_factory=list)

    #: Bare stage names that failed, recorded ALONGSIDE the human-readable
    #: message rather than re-derived from it. See :meth:`record_failure`.
    failed_stages: set[str] = field(default_factory=set)

    #: The HTTP status Buildium answered each recorded failure with, in the
    #: order :attr:`errors` holds them; ``None`` for a failure Buildium never
    #: answered (a timeout, an exception of ours). Read by :func:`keys_refused`.
    failure_statuses: list[int | None] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def record_failure(self, stage: str, exc: object, *, detail: str | None = None) -> None:
        """Record one stage failure: the message a human reads, the stage
        name the freshness stamp checks, and the HTTP status Buildium answered
        with (:func:`keys_refused`).

        ⚠ **The stage name must be recorded, never parsed back out of the
        message.** ``_record_stage_freshness`` used to derive it with
        ``e.split(":", 1)[0]``, which is correct for ``"units: <exc>"`` and
        silently wrong for the three sites that qualify their prefix —
        ``bills(unpaid)``, ``bills(paid)`` and ``transactions(lease 123)``.
        Those never equal ``"bills"`` or ``"transactions"``, so a stage that
        had just FAILED was stamped fresh, and the mirror's own footer told
        the model its bills and ledger were current when the fetch had
        errored. That is a fabricated freshness claim on exactly the two
        stages already known to be incomplete (gotcha 57: zero paid bills on
        file), which is this product's worst failure mode pointed at the
        metadata instead of the data.

        The message format is unchanged, so every existing reader of
        ``errors`` sees what it saw before; the stage travels beside it
        instead of inside it.
        """
        label = f"{stage}({detail})" if detail else stage
        self.errors.append(f"{label}: {exc}")
        self.failed_stages.add(stage)
        status = getattr(exc, "status", None) if isinstance(exc, BuildiumError) else None
        self.failure_statuses.append(status if isinstance(status, int) else None)


async def backfill_company(
    bsettings: BuildiumSettings,
    pool: asyncpg.Pool,
    *,
    company_id: str,
    account_id: int,
) -> BackfillResult:
    """Pull the company's Buildium account into the entity mirror."""
    result = BackfillResult()

    async with BuildiumClient(bsettings, company_id=company_id) as client:
        api = BuildiumApi(client)

        try:
            for prop in await api.rentals.list_properties():
                await entities.upsert_property(
                    pool, prop, account_id=account_id, company_id=company_id
                )
                result.properties += 1
        except Exception as exc:  # noqa: BLE001 — stage isolation (see module doc)
            logger.warning("backfill properties failed for company=%s: %s", company_id, exc)
            result.record_failure("properties", exc)

        try:
            for unit in await api.rentals.list_units():
                await entities.upsert_unit(pool, unit, account_id=account_id, company_id=company_id)
                result.units += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("backfill units failed for company=%s: %s", company_id, exc)
            result.record_failure("units", exc)

        try:
            for tenant in await api.rentals.list_tenants():
                await entities.upsert_tenant(
                    pool, tenant, account_id=account_id, company_id=company_id
                )
                result.tenants += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("backfill tenants failed for company=%s: %s", company_id, exc)
            result.record_failure("tenants", exc)

        active_lease_ids: list[int] = []
        try:
            for lease in await api.leases.list():
                await entities.upsert_lease(
                    pool, lease, account_id=account_id, company_id=company_id
                )
                await entities.refresh_lease_tenant_links(pool, lease, company_id=company_id)
                if (lease.status or "").lower() == "active":
                    active_lease_ids.append(lease.id)
                result.leases += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("backfill leases failed for company=%s: %s", company_id, exc)
            result.record_failure("leases", exc)

        # CRM ↔ Buildium enrichment: applicants feed the CRM auto-move (an
        # existing sandbox applicant should link/move its lead on the first
        # sync, not only on the next webhook).
        try:
            for applicant in await api.applicants.list():
                await entities.upsert_applicant(
                    pool, applicant, account_id=account_id, company_id=company_id
                )
                await sync_lead_from_applicant(
                    pool, applicant, company_id=company_id, account_id=account_id
                )
                result.applicants += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("backfill applicants failed for company=%s: %s", company_id, exc)
            result.record_failure("applicants", exc)

        # Entity graph, slice 1 (0041): vendors + owners + tasks, so every
        # id in an answer resolves to a local name. Owners have NO webhook
        # events — this backfill/daily-resync path is their ONLY freshness.
        try:
            for vendor in await api.vendors.list():
                await entities.upsert_vendor(
                    pool, vendor, account_id=account_id, company_id=company_id
                )
                result.vendors += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("backfill vendors failed for company=%s: %s", company_id, exc)
            result.record_failure("vendors", exc)

        try:
            for owner in await api.rentals.list_owners():
                await entities.upsert_owner(
                    pool, owner, account_id=account_id, company_id=company_id
                )
                result.owners += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("backfill owners failed for company=%s: %s", company_id, exc)
            result.record_failure("owners", exc)

        try:
            for task in await api.tasks.list():
                await entities.upsert_task(pool, task, account_id=account_id, company_id=company_id)
                result.tasks += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("backfill tasks failed for company=%s: %s", company_id, exc)
            result.record_failure("tasks", exc)

        # Money mirror, slice 2 (0042): the chart of accounts (GLAccount.*
        # events are acknowledged-only, so this is its freshness path) and
        # bills with per-line GL/property attribution.
        try:
            for gl in await api.general_ledger.list_accounts():
                await entities.upsert_gl_account(
                    pool, gl, account_id=account_id, company_id=company_id
                )
                result.gl_accounts += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("backfill gl accounts failed for company=%s: %s", company_id, exc)
            result.record_failure("gl_accounts", exc)

        # Bills, live-probed 2026-07-06 (twice): GET /v1/bills demands BOTH
        # frompaiddate AND topaiddate whenever the result includes paid
        # bills — an unfiltered call 422s on FromPaidDate, and Paid+from-
        # only 422s on ToPaidDate. The unpaid and paid fetches sit in
        # SEPARATE try blocks so a paid-side failure can't discard the
        # already-fetched unpaid bills (the second half of the last FAIL).
        try:
            for bill in await api.bills.list(paidstatus="Unpaid"):
                await entities.upsert_bill(pool, bill, account_id=account_id, company_id=company_id)
                result.bills += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("backfill unpaid bills failed for company=%s: %s", company_id, exc)
            result.record_failure("bills", exc, detail="unpaid")

        try:
            today = datetime.now(UTC).date()
            paid_cutoff = today - timedelta(days=TXN_LOOKBACK_DAYS)
            for bill in await api.bills.list(
                paidstatus="Paid",
                frompaiddate=paid_cutoff.isoformat(),
                topaiddate=today.isoformat(),
            ):
                await entities.upsert_bill(pool, bill, account_id=account_id, company_id=company_id)
                result.bills += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("backfill paid bills failed for company=%s: %s", company_id, exc)
            result.record_failure("bills", exc, detail="paid")

        try:
            # Bank accounts (0043): upsert the live set, then prune vanished
            # rows — BankAccount.* table events are acknowledged-only, so
            # this reconcile is the delete path.
            bank_accounts = await api.bank_accounts.list()
            for bank in bank_accounts:
                await entities.upsert_bank_account(
                    pool, bank, account_id=account_id, company_id=company_id
                )
                result.bank_accounts += 1
            await entities.prune_bank_accounts(
                pool, [b.id for b in bank_accounts], company_id=company_id
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("backfill bank accounts failed for company=%s: %s", company_id, exc)
            result.record_failure("bank_accounts", exc)

        # Probe 2026-07-05 exposed that the mirror's work orders were
        # webhook-only — a fresh connect showed 0 open WOs in the cockpit.
        try:
            for wo in await api.work_orders.list():
                await entities.upsert_work_order(
                    pool, wo, account_id=account_id, company_id=company_id
                )
                result.work_orders += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("backfill work orders failed for company=%s: %s", company_id, exc)
            result.record_failure("work_orders", exc)

        try:
            balances = await api.leases.list_outstanding_balances()
            result.balances = await entities.replace_lease_balances(
                pool, balances, account_id=account_id, company_id=company_id
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("backfill balances failed for company=%s: %s", company_id, exc)
            result.record_failure("balances", exc)

        cutoff = datetime.now(UTC) - timedelta(days=TXN_LOOKBACK_DAYS)
        for lease_id in active_lease_ids:
            try:
                for txn in await api.leases.list_transactions(
                    lease_id, transactiondatefrom=cutoff.date().isoformat()
                ):
                    txn_date = txn.date
                    if txn_date is not None and txn_date.tzinfo is None:
                        txn_date = txn_date.replace(tzinfo=UTC)
                    if txn_date is not None and txn_date < cutoff:
                        continue
                    await entities.upsert_lease_transaction(
                        pool, txn, account_id=account_id, company_id=company_id
                    )
                    result.transactions += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "backfill transactions failed for lease=%s company=%s: %s",
                    lease_id,
                    company_id,
                    exc,
                )
                result.record_failure("transactions", exc, detail=f"lease {lease_id}")

    logger.info(
        "backfill company=%s: %d properties, %d units, %d tenants, %d leases, "
        "%d work orders, %d vendors, %d owners, %d tasks, %d gl accounts, "
        "%d bills, %d bank accounts, %d balances, %d transactions, %d errors",
        company_id,
        result.properties,
        result.units,
        result.tenants,
        result.leases,
        result.work_orders,
        result.vendors,
        result.owners,
        result.tasks,
        result.gl_accounts,
        result.bills,
        result.bank_accounts,
        result.balances,
        result.transactions,
        len(result.errors),
    )
    await _record_stage_freshness(pool, company_id=company_id, result=result)
    return result


#: A stage whose WORK-LIST is derived from another stage, rather than from its
#: own endpoint. ``transactions`` is the only one: it iterates
#: ``active_lease_ids``, which is built inside the ``leases`` try-block.
#:
#: ⚠ Without this, a failed upstream stage MANUFACTURES A CLEAN STAMP
#: downstream. If ``api.leases.list()`` raises, ``active_lease_ids`` is empty
#: or partial, the transactions loop body never executes, nothing raises,
#: ``transactions`` never enters ``failed_stages`` — and it is stamped fresh
#: with ``row_count 0``. "We fetched the transactions and there were none" and
#: "we never asked" are then indistinguishable, in the one direction that
#: matters: the zero looks like an answer.
#:
#: That falsifies this function's own guarantee below, and CLAUDE.md gotcha 57
#: leans on it by name — *"a freshness stamp is evidence a stage did not error
#: — check it before spending an API call."* True of every other stage,
#: because each calls its own endpoint inside its own try; false of this one.
#:
#: ⚠ The per-lease ``except`` DOES call ``record_failure("transactions", …)``
#: (a hypothesis worth disconfirming — it is not the hole). The hole is
#: strictly the zero-iteration case, where there is no exception to record.
_STAGE_WORK_LIST_FROM: dict[str, str] = {"transactions": "leases"}


async def _record_stage_freshness(
    pool: asyncpg.Pool, *, company_id: str, result: BackfillResult
) -> None:
    """Write per-table sync timestamps (migration 0057) for every stage that
    completed WITHOUT an error — and never for a stage whose work-list came
    from a stage that failed. Stage names double as table names; a stage that
    errored keeps its previous timestamp so the footer stays honest about what
    actually refreshed. Best-effort — never fails the backfill."""
    from helixis.db.repositories.mirror_sync_state import record_sync

    errored = result.failed_stages
    for stage in (
        "properties",
        "units",
        "tenants",
        "leases",
        "balances",
        "transactions",
        "work_orders",
        "applicants",
        "vendors",
        "owners",
        "tasks",
        "gl_accounts",
        "bills",
        "bank_accounts",
    ):
        if stage in errored:
            continue
        # A work-list derived from a failed stage was never attempted, so its
        # zero is not a measurement. Leave the previous timestamp standing.
        upstream = _STAGE_WORK_LIST_FROM.get(stage)
        if upstream is not None and upstream in errored:
            logger.warning(
                "backfill company=%s: not stamping %s — its work-list comes from "
                "%s, which failed, so a row_count of %s means 'never asked'",
                company_id,
                stage,
                upstream,
                getattr(result, stage, None),
            )
            continue
        await record_sync(
            pool,
            company_id=company_id,
            table_name=stage,
            row_count=getattr(result, stage, None),
            source="backfill",
        )


def fetched_nothing(result: BackfillResult) -> bool:
    """True when stages failed and not one row came back.

    That is a connection that does not work at all (keys Buildium rejects, an
    account it closed), not one flaky stage. An empty account with no errors is
    not it, and neither is a pull that failed one stage and synced the rest.
    Every count field is read, so a stage added later is counted without
    anyone remembering this function.
    """
    if not result.errors:
        return False
    counts = (getattr(result, f.name) for f in fields(result) if f.type in ("int", int))
    return not any(counts)


def keys_refused(result: BackfillResult) -> bool:
    """True when the pull fetched nothing and Buildium answered every failed
    request with a 401: it refuses the saved keys (revoked, rotated, or an
    account it closed). Entering new keys is the customer's to do, where any
    other pull that fetched nothing is an outage somebody has to chase.

    ⚠ Every failure, not most: a 500 among the 401s, or a failure Buildium
    never answered (a timeout, an exception of ours), is not the keys. A
    failure recorded without a status counts against it too, so ``all()`` of
    a list shorter than :attr:`BackfillResult.errors` can never pass.
    """
    statuses = result.failure_statuses
    return (
        fetched_nothing(result)
        and len(statuses) == len(result.errors)
        and all(status == 401 for status in statuses)
    )


def sync_counts(result: BackfillResult) -> dict[str, Any]:
    """The per-stage counts a completed pull records on ``buildium_sync_state``.

    ⚠ ONE HOME because the banner reads these labels back
    (``SyncProgressBanner.STAGE_LABELS``) and two writers picking different
    keys would render "0 work orders" beside "12 workOrders" with nothing
    failing. Every path that finishes a full mirror pull — the onboarding
    crawl and the worker's daily re-sync — states its progress through here.
    """
    return {
        "properties": result.properties,
        "units": result.units,
        "tenants": result.tenants,
        "leases": result.leases,
        "work_orders": result.work_orders,
        "tasks": result.tasks,
        "vendors": result.vendors,
        "owners": result.owners,
        "bills": result.bills,
    }


async def run_initial_sync(
    bsettings: BuildiumSettings,
    pool: asyncpg.Pool,
    *,
    company_id: str,
    account_id: int,
    reraise: bool = False,
) -> BackfillResult | None:
    """A full mirror pull wrapped in ``buildium_sync_state`` tracking.

    Fire-and-forget: kicked via ``asyncio.create_task`` from ``/buildium/test``
    once creds validate, so the frontend's "still syncing…" banner (which polls
    ``GET /buildium/sync-status``) reflects real progress. NEVER raises — a
    crash marks the row ``failed`` (banner shows a retry), and per-stage errors
    that ``backfill_company`` already tolerated still mark ``done`` (partial
    data is usable; the banner must not hang forever).

    ⚠ **IT IS NOT ONLY THE ONBOARDING PATH, AND THAT IS THE WHOLE POINT.**
    Three routes complete a full ``backfill_company`` — this one, the "Sync
    data" button (``POST /buildium/backfill``) and the worker's daily
    ``mirror_sync`` — and until 2026-09-16 only this one recorded that it had
    happened. So a customer whose first crawl was stranded by a deploy was told
    "your Buildium import stopped before it finished — re-run it from Settings
    → Connectors → Buildium", did exactly that, watched their data arrive, and
    the warning never went away. Any new caller that pulls the whole mirror
    goes through HERE rather than calling ``backfill_company`` and leaving the
    row describing a crawl that is no longer the truth.

    ``reraise`` is for a caller that reports the failure to a human: the
    fire-and-forget path has nobody to tell, so it swallows (default), while
    ``POST /buildium/backfill`` re-raises so it can put the provider's own
    words on screen instead of a generic "sync failed". The state is written
    identically either way — only who hears about it differs.

    Returns the :class:`BackfillResult` on success, or ``None`` when the pull
    crashed and ``reraise`` is False.
    """
    from helixis.db.repositories import sync_state

    try:
        await sync_state.mark_syncing(pool, company_id)
    except Exception:  # noqa: BLE001 — status is UX, never load-bearing
        logger.warning("sync_state mark_syncing failed for company=%s", company_id, exc_info=True)

    try:
        result = await backfill_company(
            bsettings, pool, company_id=company_id, account_id=account_id
        )
    except Exception as exc:  # noqa: BLE001 — a total crash → failed, not stuck
        logger.exception("initial Buildium sync crashed for company=%s", company_id)
        try:
            await sync_state.mark_failed(pool, company_id, str(exc))
        except Exception:  # noqa: BLE001
            logger.warning(
                "sync_state mark_failed failed for company=%s", company_id, exc_info=True
            )
        if reraise:
            raise
        return None

    if result.errors:
        # Non-fatal stage errors: data still landed, so we finish (banner
        # clears) but log the stages that hiccuped for triage.
        logger.warning(
            "initial Buildium sync for company=%s finished with stage errors: %s",
            company_id,
            "; ".join(result.errors[:5]),
        )
    try:
        await sync_state.mark_done(pool, company_id, sync_counts(result))
    except Exception:  # noqa: BLE001
        logger.warning("sync_state mark_done failed for company=%s", company_id, exc_info=True)
    return result


__all__ = [
    "BackfillResult",
    "TXN_LOOKBACK_DAYS",
    "backfill_company",
    "fetched_nothing",
    "keys_refused",
    "run_initial_sync",
    "sync_counts",
]
