"""The detector runner — crash-isolated execution + persistence.

``run_detectors_for_company`` runs a set of detectors against one company,
isolating each: a detector that raises is logged + reported to Sentry and
its findings are skipped, but sibling detectors and other companies keep
running (the plan's "a failure in one detector must not take down the run").

``run_detector_cycle`` sweeps every company with mirrored books — the
scheduled entry point the webhook worker calls (via
``reconciliation.run_reconciliation_cycle``). It runs the ``default_enabled``
state-rule detectors plus any allow-listed in ``HELIXIS_DETECTORS__ENABLED_IDS``
(comma-separated ids), so newly-added dark detectors can be switched on per-id
without a deploy that flips them all at once.
"""

from __future__ import annotations

import logging
import os

import asyncpg

# Importing the built-in detector modules registers them into the registry.
import helixis.detectors.bridge_rules  # noqa: F401
import helixis.detectors.mirror_state_rules  # noqa: F401
import helixis.detectors.state_rules  # noqa: F401
import helixis.detectors.weather_rules  # noqa: F401
from helixis.db.company_pms import company_pms_strict
from helixis.db.pms_coverage import BUILDIUM, blind_tables, unlanded_tables
from helixis.db.repositories.mirror_sync_state import get_freshness
from helixis.detectors.base import Detector
from helixis.detectors.inbox_bridge import bridge_new_findings
from helixis.detectors.registry import dark_state_rule_detector_ids, enabled_state_rule_detectors
from helixis.detectors.store import persist_findings
from helixis.observability.sentry_setup import capture_swallowed

logger = logging.getLogger(__name__)

_ENABLED_IDS_ENV = "HELIXIS_DETECTORS__ENABLED_IDS"


def _extra_enabled_ids() -> frozenset[str]:
    """Detector ids switched on beyond the default-enabled set, from a
    comma-separated env allowlist. Empty when unset."""
    raw = os.getenv(_ENABLED_IDS_ENV, "")
    return frozenset(p.strip() for p in raw.split(",") if p.strip())


async def run_detectors_for_company(
    pool: asyncpg.Pool,
    company_id: str,
    detectors: list[Detector],
    *,
    failed: set[str] | None = None,
) -> dict[str, int]:
    """Run ``detectors`` against one company; upsert findings, resolve healed.

    Returns per-detector current-detection counts (a failed detector is
    counted 0 and never aborts its siblings).

    ⚠ **A crashed detector counts 0, which is the same number a healthy quiet
    one reports** — so the returned mapping cannot, on its own, tell "found
    nothing" from "threw before it could look". That is not a cosmetic
    distinction: a detector that starts raising on every company goes on
    contributing a clean-looking zero to every cycle forever.

    It is deliberately still counted 0 rather than omitted or negative,
    because callers SUM these values and a sentinel would corrupt the total.
    Instead, pass a ``failed`` set and the ids that raised are added to it —
    a mutated object beside the return value, so the failure channel can be
    added without changing a shape two callers already depend on. Passing
    nothing preserves the old behaviour exactly.
    """
    counts: dict[str, int] = {}
    async with pool.acquire() as conn:
        for det in detectors:
            try:
                findings = await det.scan(conn, company_id)
                await persist_findings(conn, company_id, det, findings)
                counts[det.detector_id] = len(findings)
            except (Exception, SystemExit) as exc:  # noqa: BLE001 — isolate each detector
                counts[det.detector_id] = 0
                if failed is not None:
                    failed.add(det.detector_id)
                logger.exception("detector %s failed for company %s", det.detector_id, company_id)
                capture_swallowed(exc, op=f"detector.{det.detector_id}")
    return counts


async def run_state_rule_detectors(
    pool: asyncpg.Pool,
    company_id: str,
    extra_ids: frozenset[str] = frozenset(),
    *,
    failed: set[str] | None = None,
    blind: set[str] | None = None,
) -> dict[str, int]:
    """Run the enabled state-rule detectors for one company (default-enabled
    ones plus ``extra_ids``).

    ⚠ **A detector reading a table this company's PMS never fills is SKIPPED,
    not run.** Before Rentvine mirrored lease transactions,
    ``rent_not_collected`` would have reported every active Rentvine lease as
    unpaid and ``rent_under_charged`` as never charged; the same holds for a
    Rentvine table whose stage has never landed. Both are false claims about a
    tenant's money, and the Inbox bridge would draft a message about each one.
    The skipped ids go into ``blind`` (same out-parameter shape as ``failed``),
    so "could not see" never reads as the clean zero a quiet run reports.

    The PMS lookup is the STRICT one: a failed read raises and the company is
    skipped for this cycle. Failing open to Buildium would run every money rule
    blind, which is the case this exists to stop.
    """
    pms = await company_pms_strict(pool, company_id)
    # A Rentvine table whose stage never landed is skipped the same way: it is
    # not yet known to be complete (``pms_coverage.unlanded_tables``). Every
    # system but Buildium lands its tables this way (Rent Manager, Propertyware).
    landed = await get_freshness(pool, company_id) if pms != BUILDIUM else {}
    runnable: list[Detector] = []
    for det in enabled_state_rule_detectors(extra_ids):
        reads = det.reads_tables or ()
        if blind_tables(pms, reads) or unlanded_tables(pms, reads, landed):
            if blind is not None:
                blind.add(det.detector_id)
            continue
        runnable.append(det)
    return await run_detectors_for_company(pool, company_id, runnable, failed=failed)


# DELIBERATELY CROSS-TENANT (gotcha 47): this enumerates the companies to
# sweep, so a company_id filter would defeat its purpose. Every query the
# detectors themselves run is scoped to one company_id from this list.
_COMPANIES_SQL = """
SELECT DISTINCT company_id FROM helixis_leases
UNION
SELECT DISTINCT company_id FROM helixis_bills
"""


async def run_detector_cycle(pool: asyncpg.Pool) -> None:
    """One sweep of the enabled state-rule detectors over every company with
    mirrored books. Cheap (pure SQL), idempotent, per-company isolated."""
    extra_ids = _extra_enabled_ids()
    async with pool.acquire() as conn:
        rows = await conn.fetch(_COMPANIES_SQL)
    for row in rows:
        company_id = str(row["company_id"])
        try:
            failed: set[str] = set()
            blind: set[str] = set()
            counts = await run_state_rule_detectors(
                pool, company_id, extra_ids, failed=failed, blind=blind
            )
            # ⚠ Logged on EVERY cycle, not only when something was found. This
            # was `if total:`, so a sweep that detected nothing wrote no line —
            # which made "the detectors ran and the book is clean" identical to
            # "the detectors are dead", and left the only interesting question
            # unanswerable from the outside. `duplicate_vendor_bill` is
            # default-enabled and has produced zero findings in its lifetime;
            # nothing in this process could say whether that is a clean
            # portfolio or a detector broken since it merged.
            #
            # `dark` names the registered detectors that never ran (9 of 12 as
            # of 2026-08-29) and `failed` the ones that raised — the second is
            # what separates a real zero from a crash, since a crashed
            # detector still reports a healthy-looking 0 in `counts`.
            # `blind` names the ones skipped because this company's PMS never
            # fills a table they read (db/pms_coverage.py).
            logger.info(
                "detectors: company=%s findings=%d counts=%s dark=%s failed=%s blind=%s",
                company_id,
                sum(counts.values()),
                counts,
                dark_state_rule_detector_ids(extra_ids) or "-",
                sorted(failed) or "-",
                sorted(blind) or "-",
            )
        except (Exception, SystemExit) as exc:  # noqa: BLE001 — sweep survives one bad company
            logger.exception("detector cycle failed for company %s", company_id)
            capture_swallowed(exc, op="detector.cycle")
        # Project unbridged open findings into the Inbox pipeline (one
        # Risk.FlagRaised event per rule group → enrichment → planner →
        # the full message card). Runs even when THIS sweep found
        # nothing new: it also picks up analyst-written flags and any
        # groups a previous pass failed to bridge. Own try so a bridge
        # failure never masks detector results (and vice versa).
        try:
            await bridge_new_findings(pool, company_id)
        except (Exception, SystemExit) as exc:  # noqa: BLE001 — same isolation as above
            logger.exception("risk bridge failed for company %s", company_id)
            capture_swallowed(exc, op="detector.inbox_bridge")


__all__ = [
    "run_detector_cycle",
    "run_detectors_for_company",
    "run_state_rule_detectors",
]
