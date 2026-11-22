"""Per-table mirror sync recency (migration 0057).

Before this table, "how stale is the mirror?" had one answer for the whole
portfolio: a GREATEST() over three tables' ``updated_at``, so stale bills or
work orders were invisible behind a fresh lease row, and the worker's
daily-resync guard lived in process memory. Writers: the backfill (one row
per completed stage) and the webhook dispatcher (per event family). Readers:
the chat "as of" footer, the connectors card's ``lastSyncAt``, and any
future per-workflow staleness limit.

Every function here is BEST-EFFORT and deploy-window safe (gotcha 8): if the
0057 table isn't applied yet, writes log-and-continue and reads return empty
— freshness is an honesty feature, never load-bearing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import asyncpg

logger = logging.getLogger(__name__)

#: How far behind a core table may fall before it is worth saying so.
#:
#: 26 rather than 24: a nightly sync that runs at 03:00 and slips an hour is
#: not a problem, and a threshold that fires on ordinary jitter is one people
#: learn to ignore (gotcha 52 — a guard that cries wolf gets switched off).
STALE_AFTER_HOURS = 26

# The tables whose staleness a PM would actually care about in a chat answer.
# (Reference data — GL accounts, bank accounts — refreshes on its own cadence
# and would only add noise to the footer.)
CORE_TABLES = (
    "properties",
    "units",
    "tenants",
    "leases",
    "balances",
    "work_orders",
    "tasks",
    "bills",
    "vendors",
    "owners",
)


async def record_sync(
    pool: asyncpg.Pool,
    *,
    company_id: str,
    table_name: str,
    row_count: int | None = None,
    source: str = "backfill",
) -> None:
    """Upsert one table's sync timestamp to now(). Never raises."""
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO helixis_mirror_sync_state
                    (company_id, table_name, synced_at, row_count, source)
                VALUES ($1::uuid, $2, now(), $3, $4)
                ON CONFLICT (company_id, table_name)
                DO UPDATE SET synced_at = now(),
                              row_count = COALESCE($3, helixis_mirror_sync_state.row_count),
                              source = $4
                """,
                company_id,
                table_name,
                row_count,
                source,
            )
    except Exception:  # noqa: BLE001 — freshness bookkeeping must never break a sync
        logger.debug("mirror sync-state record failed (non-fatal)", exc_info=True)


async def get_freshness(pool: asyncpg.Pool, company_id: str) -> dict[str, datetime]:
    """table_name → synced_at for the company. Empty on any failure
    (including the pre-migration deploy window)."""
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT table_name, synced_at
                  FROM helixis_mirror_sync_state
                 WHERE company_id = $1::uuid
                """,
                company_id,
            )
        return {r["table_name"]: r["synced_at"] for r in rows}
    except Exception:  # noqa: BLE001
        logger.debug("mirror sync-state read failed (non-fatal)", exc_info=True)
        return {}


@dataclass(frozen=True)
class StageSync:
    """One stage's last completed sync: when, and how many rows came back."""

    synced_at: datetime
    row_count: int | None


async def get_stage_sync(pool: asyncpg.Pool, company_id: str, table_name: str) -> StageSync | None:
    """One stage's row, or ``None`` when there isn't one we can read.

    ⚠ **``row_count`` is why this exists.** ``record_sync`` has written it on
    every stage since 0057 and NOTHING has ever read it — ``get_freshness``
    selects ``table_name, synced_at`` and drops it. It is the one fact that
    separates *"we never asked Buildium"* from *"we asked and your account has
    none"*, and without it the product told a customer whose sync had worked
    perfectly that their data had not synced yet (gotcha 41's family: the
    field was already stored and thrown away).

    ⚠ **The stamp is only evidence for a stage that calls its own endpoint
    inside its own ``try``.** ``backfill._STAGE_WORK_LIST_FROM`` names the one
    stage where it is not (``transactions``), and that stage is deliberately
    left unstamped when its work-list failed. ``properties`` — the caller this
    was written for — has its own try and its own fetch, so a stamped row
    genuinely means Buildium answered.

    ⚠ **``None`` is NOT "zero rows".** Never-synced and unreadable both return
    ``None``, and collapsing them is safe ONLY because every caller must treat
    ``None`` as "not enough evidence to claim the account is empty" — the
    conservative direction (gotchas 55/57). A caller that reads ``None`` as an
    empty portfolio would state a fresh false claim about somebody's books
    during an outage.
    """
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT synced_at, row_count
                  FROM helixis_mirror_sync_state
                 WHERE company_id = $1::uuid
                   AND table_name = $2
                """,
                company_id,
                table_name,
            )
    except Exception:  # noqa: BLE001 — freshness is never load-bearing
        logger.debug("mirror sync-state stage read failed (non-fatal)", exc_info=True)
        return None
    if row is None:
        return None
    return StageSync(synced_at=row["synced_at"], row_count=row["row_count"])


def stalest_core(freshness: dict[str, datetime]) -> tuple[str, datetime] | None:
    """The oldest CORE table's (name, synced_at), or None when no core table
    has a recorded sync. Pure — unit-testable without a pool."""
    core = [(name, ts) for name, ts in freshness.items() if name in CORE_TABLES]
    if not core:
        return None
    return min(core, key=lambda item: item[1])


@dataclass(frozen=True)
class FreshnessVerdict:
    """How current the mirror is, decided once and worded per audience.

    ⚠ The DECISION lives here; the SENTENCE does not. The chat footer writes
    for a model that will paraphrase it into an answer, and a page header
    writes for a person glancing at a grid — gotchas 39 and 42 are both this
    product shipping one message to a reader it was not written for. Sharing
    the verdict keeps the two surfaces from disagreeing about whether the data
    is stale; letting each word its own output keeps either from sounding
    absurd.
    """

    as_of: datetime | None
    """Newest core sync — what "current as of" means. `None` = never synced."""
    stalest_table: str | None
    stalest_at: datetime | None
    stale_hours: int | None
    """Age of the stalest core table, whole hours. `None` when unknown."""

    @property
    def is_stale(self) -> bool:
        return self.stale_hours is not None and self.stale_hours >= STALE_AFTER_HOURS

    @property
    def has_data(self) -> bool:
        return self.as_of is not None


def assess(freshness: dict[str, datetime], now: datetime | None = None) -> FreshnessVerdict:
    """Turn a per-table map into one verdict. Pure.

    ⚠ `as_of` is the NEWEST core table and `stalest_*` is the oldest, and both
    are reported because either alone misleads. The newest is the flattering
    number — a lease row synced a minute ago hides day-old bills behind it,
    which is the exact honesty gap migration 0057 was added to close. The
    oldest alone would say "yesterday" about an account whose books are
    current apart from one reference table nobody asked about.
    """
    core = {name: ts for name, ts in freshness.items() if name in CORE_TABLES}
    # Fall back to non-core rows rather than reporting nothing: an account
    # mid-first-sync has reference tables and no core ones yet, and "never
    # synced" would be a false claim about a sync visibly in progress.
    values = core or freshness
    if not values:
        return FreshnessVerdict(None, None, None, None)

    stalest = stalest_core(freshness)
    stale_hours: int | None = None
    if stalest is not None:
        ts = stalest[1]
        ts = ts if ts.tzinfo else ts.replace(tzinfo=UTC)
        stale_hours = int(((now or datetime.now(UTC)) - ts).total_seconds() // 3600)

    return FreshnessVerdict(
        as_of=max(values.values()),
        stalest_table=stalest[0] if stalest else None,
        stalest_at=stalest[1] if stalest else None,
        stale_hours=stale_hours,
    )
