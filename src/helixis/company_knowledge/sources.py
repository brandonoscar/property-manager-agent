"""Source rollups for the company knowledge profile (ADR 0006).

Each rollup is **best-effort and keyless**: it reads what Helixis already has in
Postgres (mirrored Buildium entities, webhook activity, uploaded documents) and
returns plain facts. Any source that's unavailable — a table not yet applied
(gotcha #8), or simply empty — contributes nothing rather than raising, so the
distiller degrades gracefully (ADR 0006 §2/§4). Richer sources (live Buildium API
snapshot, Drive titles) are added in the distiller-tuning pass once the live key
is flowing (~Jul 18); the scaffold runs on whatever's here now.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import asyncpg

from helixis.buildium.maintenance_status import open_status_sql
from helixis.db.pms_coverage import BUILDIUM, coverage_for
from helixis.webhooks.categorization import categorize

logger = logging.getLogger(__name__)


@dataclass
class CompanyFacts:
    """The raw, source-derived facts fed to the distiller. All optional — an
    empty field just means that source had nothing (or wasn't available)."""

    lease_count: int = 0
    work_order_count: int = 0
    open_work_order_count: int = 0
    # category -> count over the recent window (webhook activity).
    activity_by_category: dict[str, int] = field(default_factory=dict)
    activity_window_days: int = 30
    # Why the activity is UNKNOWN, when it is: never read as "none recorded"
    # (rule 14). Only Buildium's webhooks are read, so for any other system it
    # is not tracked at all; and a failed read is not an empty one.
    activity_unknown: str | None = None
    # Uploaded documents: (name, kind).
    documents: list[tuple[str, str]] = field(default_factory=list)
    # Uploaded documents WITH an extracted text digest: (name, digest). This is
    # the document *contents* — the grounded facts a lead reply / chat answer
    # needs. Empty for docs not yet digested (they still appear in ``documents``).
    document_digests: list[tuple[str, str]] = field(default_factory=list)
    # Connected Google Drive files, from the static index (#67 PR-2): (title,
    # folder-or-None). Titles only — the agent knows WHAT exists without a live
    # Drive call; fetching contents stays on-demand via the Drive specialist.
    drive_files: list[tuple[str, str | None]] = field(default_factory=list)

    def has_any_signal(self) -> bool:
        """True if at least one source produced something worth summarizing."""
        return bool(
            self.lease_count
            or self.work_order_count
            or self.activity_by_category
            or self.documents
            or self.document_digests
            or self.drive_files
        )


async def _scalar(pool: asyncpg.Pool, sql: str, *args: object) -> int:
    """Run a ``count(*)``-shaped query, returning 0 on any failure (missing
    table / transient error) so a single dead source never breaks the rollup."""
    try:
        val = await pool.fetchval(sql, *args)
    except Exception as exc:  # noqa: BLE001 — best-effort source
        logger.debug("company_knowledge source query failed: %s", exc)
        return 0
    return int(val or 0)


async def _portfolio(pool: asyncpg.Pool, company_id: str) -> tuple[int, int, int]:
    """Keyless portfolio signal from the mirror tables: lease count, work-order
    count, and open work-order count. (Properties/units/owners are not mirrored
    yet — those arrive in the live-Buildium tuning pass.)"""
    lease_count = await _scalar(
        pool,
        "SELECT count(*) FROM helixis_leases WHERE company_id = $1::uuid",
        company_id,
    )
    wo_count = await _scalar(
        pool,
        "SELECT count(*) FROM helixis_work_orders WHERE company_id = $1::uuid",
        company_id,
    )
    open_wo = await _scalar(
        pool,
        "SELECT count(*) FROM helixis_work_orders "
        f"WHERE company_id = $1::uuid AND {open_status_sql('status')}",
        company_id,
    )
    return lease_count, wo_count, open_wo


async def _activity_by_category(
    pool: asyncpg.Pool, company_id: str, *, window_days: int
) -> dict[str, int] | None:
    """Recent webhook activity rolled up to categories over ``window_days``.

    Groups ``buildium_events`` by ``event_name`` in the window, then folds each
    event name onto its category via :func:`categorize`. Returns {} when
    there's no recent activity and ``None`` when the read failed: a failure
    is not "none recorded".
    """
    try:
        rows = await pool.fetch(
            """
            SELECT event_name, count(*)::int AS n
            FROM buildium_events
            WHERE company_id = $1
              AND event_time >= now() - make_interval(days => $2)
            GROUP BY event_name
            """,
            company_id,
            window_days,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("company_knowledge activity rollup failed: %s", exc)
        return None

    by_category: dict[str, int] = {}
    for row in rows:
        category = categorize(row["event_name"])
        by_category[category] = by_category.get(category, 0) + int(row["n"])
    return by_category


async def _documents(pool: asyncpg.Pool, company_id: str) -> list[tuple[str, str]]:
    """Uploaded document (name, kind) pairs, newest first, capped at 25."""
    try:
        rows = await pool.fetch(
            """
            SELECT name, kind
            FROM public.company_documents
            WHERE company_id = $1::uuid
            ORDER BY created_at DESC
            LIMIT 25
            """,
            company_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("company_knowledge documents source failed: %s", exc)
        return []
    return [(row["name"], row["kind"]) for row in rows]


async def _document_digests(pool: asyncpg.Pool, company_id: str) -> list[tuple[str, str]]:
    """(name, digest) for the company's documents that have a cached text digest
    (migration 0037). Empty on any failure (table missing / not yet digested)."""
    try:
        from helixis.db.repositories import docs_links as docs_repo

        docs = await docs_repo.documents_for_knowledge(pool, company_id=company_id, limit=25)
    except Exception as exc:  # noqa: BLE001 — best-effort source (pre-0037 table)
        logger.debug("company_knowledge document-digests source failed: %s", exc)
        return []
    return [
        (d.name, d.text_digest.strip()) for d in docs if d.text_digest and d.text_digest.strip()
    ]


async def _drive_files(pool: asyncpg.Pool, company_id: str) -> list[tuple[str, str | None]]:
    """(title, folder) for the company's indexed Google Drive files (#67 PR-2).

    Reads the static ``drive_index`` — no live Drive call. Empty on a missing
    table (pre-0063) / no connected Drive / any error (best-effort source)."""
    try:
        from helixis.db.repositories import drive_index as drive_repo

        files = await drive_repo.list_for_company(pool, company_id=company_id, limit=200)
    except Exception as exc:  # noqa: BLE001 — best-effort source
        logger.debug("company_knowledge drive source failed: %s", exc)
        return []
    return [(f.title, f.path) for f in files]


async def gather_company_facts(
    pool: asyncpg.Pool, *, company_id: str, window_days: int = 30
) -> CompanyFacts:
    """Collect all keyless source signals for one company. Never raises."""
    lease_count, wo_count, open_wo = await _portfolio(pool, company_id)
    # ⚠ Only Buildium's webhooks are read (``buildium_events``). For any other
    # system there is nothing to count, and the distiller was told "none
    # recorded", which it could write into the profile as "no activity".
    cov = await coverage_for(pool, company_id)
    activity: dict[str, int] | None = None
    unknown: str | None
    if cov.pms == BUILDIUM:
        activity = await _activity_by_category(pool, company_id, window_days=window_days)
        unknown = None if activity is not None else "could not be read just now"
    elif cov.name is None:
        unknown = "not known (the company's property management system could not be read)"
    else:
        unknown = f"not tracked for {cov.name} (Occupella reads only Buildium's webhooks)"
    documents = await _documents(pool, company_id)
    document_digests = await _document_digests(pool, company_id)
    drive_files = await _drive_files(pool, company_id)
    return CompanyFacts(
        lease_count=lease_count,
        work_order_count=wo_count,
        open_work_order_count=open_wo,
        activity_by_category=activity or {},
        activity_window_days=window_days,
        activity_unknown=unknown,
        documents=documents,
        document_digests=document_digests,
        drive_files=drive_files,
    )


def facts_to_prompt_block(facts: CompanyFacts) -> str:
    """Render the gathered facts as a compact, human-readable block for the
    distiller prompt. Deterministic (sorted) so the same inputs yield a stable
    prompt."""
    lines: list[str] = []
    lines.append(f"- Leases on file (mirrored): {facts.lease_count}")
    lines.append(
        f"- Work orders (mirrored): {facts.work_order_count} ({facts.open_work_order_count} open)"
    )
    if facts.activity_by_category:
        rollup = ", ".join(
            f"{cat}: {n}"
            for cat, n in sorted(facts.activity_by_category.items(), key=lambda kv: (-kv[1], kv[0]))
        )
        lines.append(f"- Webhook activity, last {facts.activity_window_days}d — {rollup}")
    elif facts.activity_unknown:
        lines.append(
            f"- Webhook activity, last {facts.activity_window_days}d — UNKNOWN, "
            f"{facts.activity_unknown}. Do not describe the company's activity from it."
        )
    else:
        lines.append(f"- Webhook activity, last {facts.activity_window_days}d — none recorded")
    if facts.documents:
        docs = "; ".join(f"{name} ({kind})" for name, kind in facts.documents)
        lines.append(f"- Uploaded documents: {docs}")
    else:
        lines.append("- Uploaded documents: none")
    # Document CONTENTS — the grounded facts, each attributed to its source doc
    # so the distiller can cite them ("per Pet Policy.pdf").
    if facts.document_digests:
        lines.append("- Document contents (facts extracted from the uploads above):")
        for name, digest in facts.document_digests:
            lines.append(f"  • From “{name}”: {digest}")
    # Google Drive index — titles (+ folder) of the connected Drive, so the
    # profile records WHAT the company keeps in Drive (SOPs, templates, contracts).
    if facts.drive_files:
        lines.append("- Google Drive documents (connected Drive, titles indexed):")
        for title, folder in facts.drive_files:
            where = f" [{folder}]" if folder else ""
            lines.append(f"  • {title}{where}")
    return "\n".join(lines)


__all__ = ["CompanyFacts", "gather_company_facts", "facts_to_prompt_block"]
