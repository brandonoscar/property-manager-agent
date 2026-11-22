#!/usr/bin/env python3
"""Demo data seeder for the Helixis Supabase database.

Creates one demo company ("Helixis Demo Properties") with two users,
realistic property-management entities, activity history, webhook events,
and Mem0 memories. Designed for the Vercel preview / local-dev story:
"open the app, sign in as the demo user, see a working portfolio."

Idempotent — safe to re-run. Wipes and re-seeds with ``--reset``. Refuses
to run unless ``HELIXIS_SEED=1`` is set, and refuses to run against a
database that already has > 5 ``companies`` rows unless ``--force`` is
passed (production-safety check).

Usage:
    HELIXIS_SUPABASE__DB_URL=postgresql://… HELIXIS_SEED=1 \\
      python3 scripts/seed.py [--reset] [--force]

The Mem0 portion is best-effort: if Mem0 cannot initialise (missing API
key, missing pgvector, etc.) the SQL portion still seeds and the script
exits 0 with a logged warning. Seeding always uses
``MemoryManager.add()`` so the memory_audit trail is exercised.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger("seed")

# Fixed UUIDs so re-runs deterministically resolve the same rows.
# ⚠ Must be a REAL uuid string: companies.id is uuid (0001_init header comment
# — production ran uuid from day one). The previous "demo-company" literal
# could never insert (invalid uuid cast), and the uuid-typed known-state
# tables (helixis_payment_promises / helixis_reminders, dogfood-prep step 3)
# could never hold rows for it.
DEMO_COMPANY_ID = "00000000-0000-0000-0000-0000000c0001"
DEMO_OWNER_ID = uuid.UUID("00000000-0000-0000-0000-0000000d000a")
DEMO_PM_ID = uuid.UUID("00000000-0000-0000-0000-0000000d000b")
SUPABASE_INSTANCE_ID = uuid.UUID("00000000-0000-0000-0000-000000000000")

# Buildium account_id we pretend to mirror. Fixed so re-seeds collapse on
# the unique buildium_id index.
DEMO_ACCOUNT_ID = 990001


def _now() -> datetime:
    return datetime.now(UTC)


# ─────────────────────────────────────────────────────────────────────
# Guards
# ─────────────────────────────────────────────────────────────────────


def _require_env_guard() -> None:
    if os.environ.get("HELIXIS_SEED") != "1":
        sys.stderr.write(
            "seed: refusing to run without HELIXIS_SEED=1.\n"
            "  Re-run with HELIXIS_SEED=1 if you really mean it.\n"
        )
        sys.exit(2)


async def _production_safety_check(conn: Any, *, force: bool) -> None:
    count = await conn.fetchval("SELECT count(*) FROM public.companies")
    if count > 5 and not force:
        sys.stderr.write(
            f"seed: refusing — companies table has {count} rows (looks "
            "like production). Pass --force to override.\n"
        )
        sys.exit(3)
    if count > 5:
        sys.stderr.write(
            f"seed: WARNING — running against a DB with {count} companies (--force given).\n"
        )


# ─────────────────────────────────────────────────────────────────────
# SQL seeders
# ─────────────────────────────────────────────────────────────────────


async def seed_company_and_users(conn: Any) -> None:
    await conn.execute(
        "INSERT INTO public.companies (id, name) VALUES ($1, $2) ON CONFLICT (id) DO NOTHING",
        DEMO_COMPANY_ID,
        "Occupella Demo Properties",
    )
    # auth.users — Supabase's user table. Direct insert works because
    # the seed script runs with service-role connection privileges. The
    # production sign-in flow goes through the Supabase Auth API (which
    # is what the demo emails would resolve to once a magic-link is sent).
    await conn.execute(
        "INSERT INTO auth.users (id, instance_id, aud, role, email) "
        "VALUES ($1, $2, 'authenticated', 'authenticated', $3), "
        "       ($4, $2, 'authenticated', 'authenticated', $5) "
        "ON CONFLICT (id) DO NOTHING",
        DEMO_OWNER_ID,
        SUPABASE_INSTANCE_ID,
        "demo-owner@occupella.app",
        DEMO_PM_ID,
        "demo-pm@occupella.app",
    )
    await conn.execute(
        "INSERT INTO public.company_users (user_id, company_id, role) "
        "VALUES ($1, $2, 'owner'), ($3, $2, 'member') "
        "ON CONFLICT (user_id, company_id) DO NOTHING",
        DEMO_OWNER_ID,
        DEMO_COMPANY_ID,
        DEMO_PM_ID,
    )
    logger.info("seeded company + 2 users + memberships")


async def seed_leases(conn: Any) -> int:
    """Two leases: one current, one ending in 30 days."""
    leases = [
        {
            "buildium_id": 70001,
            "property_id": 41001,
            "unit_id": 51001,
            "status": "Active",
            "lease_from_date": _now() - timedelta(days=180),
            "lease_to_date": _now() + timedelta(days=185),
            "rent": 1850.00,
            "raw": {"unit": "4B", "tenant": "Sarah Chen"},
        },
        {
            "buildium_id": 70002,
            "property_id": 41001,
            "unit_id": 51002,
            "status": "Active",
            "lease_from_date": _now() - timedelta(days=335),
            "lease_to_date": _now() + timedelta(days=30),
            "rent": 2100.00,
            "raw": {"unit": "7C", "tenant": "Marcus Williams"},
        },
    ]
    for r in leases:
        await conn.execute(
            "INSERT INTO public.helixis_leases "
            "  (buildium_id, account_id, company_id, property_id, unit_id, "
            "   status, lease_from_date, lease_to_date, rent, raw) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb) "
            "ON CONFLICT (buildium_id) DO NOTHING",
            r["buildium_id"],
            DEMO_ACCOUNT_ID,
            DEMO_COMPANY_ID,
            r["property_id"],
            r["unit_id"],
            r["status"],
            r["lease_from_date"],
            r["lease_to_date"],
            r["rent"],
            json.dumps(r["raw"]),
        )
    return len(leases)


async def seed_work_orders(conn: Any) -> int:
    """5 work orders, mix of statuses, two >7 days old."""
    base_id = 80000
    wos = [
        # Two >7 days old so the demo "open work orders >7 days" prompt has hits.
        ("New", "High", "HVAC no-cool, Unit 4B", _now() - timedelta(days=12)),
        ("InProgress", "Normal", "Leaky faucet, Unit 7C", _now() - timedelta(days=9)),
        ("Completed", "Normal", "Replaced smoke detector batteries", _now() - timedelta(days=4)),
        (
            "OnHold",
            "Low",
            "Repaint Unit 2A — tenant requested colour swap",
            _now() - timedelta(days=2),
        ),
        ("Cancelled", "Normal", "Pest control — false alarm", _now() - timedelta(days=1)),
    ]
    for i, (status, priority, title, _created) in enumerate(wos):
        await conn.execute(
            "INSERT INTO public.helixis_work_orders "
            "  (buildium_id, account_id, company_id, property_id, unit_id, "
            "   title, status, priority, raw) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb) "
            "ON CONFLICT (buildium_id) DO NOTHING",
            base_id + i,
            DEMO_ACCOUNT_ID,
            DEMO_COMPANY_ID,
            41001,
            51001 + i,
            title,
            status,
            priority,
            json.dumps({"title": title, "status": status}),
        )
    return len(wos)


async def seed_activity(conn: Any) -> int:
    """10 activity rows over the last 14 days, varied tools."""
    samples = [
        ("buildium.list_work_orders", "Listed 7 open work orders", "manual"),
        ("email.send", "Sent reminder to Sarah Chen for rent renewal", "chat"),
        ("memory.search", "Searched company memory: 'plumber preferences'", "chat"),
        ("confirm.request", "Approved: send 3 collection emails", "chat"),
        ("buildium.list_leases", "Pulled active leases (45)", "scheduled"),
        ("email.send", "Notified vendor about HVAC repair", "chat"),
        ("memory.search", "Searched: 'units expiring this quarter'", "chat"),
        ("webhook.received", "Buildium webhook: lease.renewed", "webhook"),
        ("buildium.list_payments", "MTD collected: $92,180", "scheduled"),
        ("confirm.request", "Denied: bulk eviction notice (operator declined)", "chat"),
    ]
    for i, (action_type, description, source) in enumerate(samples):
        await conn.execute(
            "INSERT INTO public.helixis_activity_log "
            "  (account_id, company_id, source, action_type, description, "
            "   user_id, raw, created_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8)",
            DEMO_ACCOUNT_ID,
            DEMO_COMPANY_ID,
            source,
            action_type,
            description,
            str(DEMO_OWNER_ID) if i % 2 == 0 else str(DEMO_PM_ID),
            json.dumps({}),
            _now() - timedelta(days=i * 1.4),
        )
    return len(samples)


async def seed_buildium_events(conn: Any) -> int:
    """3 webhook events covering work_order.created / lease.renewed / payment.received."""
    events = [
        ("work_order.created", {"work_order_id": 80000}),
        ("lease.renewed", {"lease_id": 70001}),
        ("payment.received", {"amount": 1850.00, "lease_id": 70001}),
    ]
    for event_name, entity_ids in events:
        # Idempotency on dedup_key — re-runs collapse on the unique index.
        dedup = f"demo-{event_name}-{DEMO_ACCOUNT_ID}"
        await conn.execute(
            "INSERT INTO public.buildium_events "
            "  (dedup_key, event_name, event_time, account_id, company_id, "
            "   entity_ids, payload, status) "
            "VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb, 'done') "
            "ON CONFLICT (dedup_key) DO NOTHING",
            dedup,
            event_name,
            _now() - timedelta(hours=2),
            DEMO_ACCOUNT_ID,
            DEMO_COMPANY_ID,
            json.dumps(entity_ids),
            json.dumps({"event": event_name, "demo": True}),
        )
    return len(events)


# ─────────────────────────────────────────────────────────────────────
# Mem0 seeder
# ─────────────────────────────────────────────────────────────────────


DEMO_MEMORIES = [
    ("ABC Plumbing prefers email over phone — call only for urgent leaks", "vendor"),
    (
        "Tenant in Unit 4B (Sarah Chen) has been late 2 months in a row; auto-pay still failing",
        "tenant",
    ),
    (
        "Quarterly HVAC inspection vendor: NorthStar Mechanical, $250 per unit, "
        "schedule 2 weeks ahead",
        "maintenance",
    ),
    (
        "Owner draws are processed on the 25th of each month — never push past "
        "the 27th, the trust account guidance requires it",
        "preference",
    ),
    (
        "Late-fee policy: 5% after day 5, 10% after day 10. Waivable once per "
        "tenant with owner approval.",
        "financial",
    ),
]


async def seed_known_state(conn: Any) -> int:
    """Known-state rows for the reasoning loop's ``check_known_state`` channel
    (dogfood-prep step 3): one payment promise + two reminder cards, so a
    seeded dev DB answers the F10 continuity turns instead of reporting
    "Nothing on record"."""
    await conn.execute(
        """
        INSERT INTO public.helixis_payment_promises
            (company_id, email, promised_date, source, source_message_id)
        VALUES ($1::uuid, $2, (now() + interval '5 days')::date, 'manual', 'seed')
        ON CONFLICT (company_id, email) DO UPDATE SET
            promised_date = EXCLUDED.promised_date, updated_at = now()
        """,
        DEMO_COMPANY_ID,
        "marcus.williams@example.com",
    )
    reminders = [
        (
            "rent_missing",
            "unit-7C",
            "high",
            "1 lease at Demo Court has no rent payment this month",
            "Past the 10th with nothing posted: Unit 7C. No recent outreach on file.",
        ),
        (
            "task_overdue",
            "wo-hvac-4b",
            "normal",
            "HVAC no-cool in Unit 4B is 12 days old",
            "Opened 12 days ago, still New. Tenant: Sarah Chen.",
        ),
    ]
    for producer, entity_key, severity, title, body in reminders:
        await conn.execute(
            """
            INSERT INTO public.helixis_reminders
                (company_id, producer, entity_key, status, severity, title, body,
                 payload, escalation)
            VALUES ($1::uuid, $2, $3, 'open', $4, $5, $6, '{}'::jsonb, 0)
            ON CONFLICT (company_id, producer, entity_key) DO UPDATE SET
                severity = EXCLUDED.severity, title = EXCLUDED.title,
                body = EXCLUDED.body, last_fired_at = now(), updated_at = now()
            """,
            DEMO_COMPANY_ID,
            producer,
            entity_key,
            severity,
            title,
            body,
        )
    return 1 + len(reminders)


async def seed_memories() -> int:
    """Write 5 memories via MemoryManager.add() — exercises memory_audit."""
    try:
        from helixis.memory.manager import MemoryManager
        from helixis.settings import Settings
    except Exception as e:  # noqa: BLE001
        logger.warning("seed: skipping memories (import failed): %s", e)
        return 0

    settings = Settings()
    if not settings.memory.enabled:
        logger.info("seed: skipping memories (memory disabled in settings)")
        return 0

    mgr = MemoryManager(settings)
    await mgr.initialize()
    if not mgr.available:
        logger.warning(
            "seed: skipping memories (Mem0 unavailable — likely missing "
            "OPENAI_API_KEY or DB_URL for pgvector). The SQL portion still "
            "seeded successfully.",
        )
        return 0

    written = 0
    for content, category in DEMO_MEMORIES:
        result = await mgr.add(
            content,
            company_id=DEMO_COMPANY_ID,
            stored_by=str(DEMO_OWNER_ID),
            category=category,
        )
        if result is not None:
            written += 1
    return written


# ─────────────────────────────────────────────────────────────────────
# Reset
# ─────────────────────────────────────────────────────────────────────


async def reset_demo_data(conn: Any) -> None:
    """Wipe ONLY demo-company data. companies row deletion cascades to
    company_users + the 4 RLS-protected tables (FK ON DELETE CASCADE).
    """
    await conn.execute("DELETE FROM public.companies WHERE id = $1", DEMO_COMPANY_ID)
    # auth.users entries — only delete the two known demo UUIDs. Never
    # delete arbitrary auth.users rows.
    await conn.execute(
        "DELETE FROM auth.users WHERE id IN ($1, $2)",
        DEMO_OWNER_ID,
        DEMO_PM_ID,
    )
    logger.info("reset: demo-company + demo users wiped (cascade clean)")


# ─────────────────────────────────────────────────────────────────────
# Entrypoint
# ─────────────────────────────────────────────────────────────────────


async def _run(*, reset: bool, force: bool) -> int:
    import asyncpg

    dsn = os.environ.get("HELIXIS_SUPABASE__DB_URL", "").strip()
    if not dsn:
        sys.stderr.write("seed: HELIXIS_SUPABASE__DB_URL is required.\n")
        return 4

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            await _production_safety_check(conn, force=force)

            if reset:
                async with conn.transaction():
                    await reset_demo_data(conn)

            # Idempotent seed
            async with conn.transaction():
                await seed_company_and_users(conn)
                leases = await seed_leases(conn)
                wos = await seed_work_orders(conn)
                activity = await seed_activity(conn)
                events = await seed_buildium_events(conn)
                known = await seed_known_state(conn)

            logger.info("seeded known-state rows: %d", known)
            logger.info("seeded leases: %d", leases)
            logger.info("seeded work orders: %d", wos)
            logger.info("seeded activity: %d", activity)
            logger.info("seeded buildium_events: %d", events)
    finally:
        await pool.close()

    # Memories run on their own pool inside MemoryManager.
    memories = await seed_memories()
    logger.info("seeded memories: %d", memories)

    logger.info("seed complete: company=%s", DEMO_COMPANY_ID)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Helixis demo data seeder.")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Wipe demo-company data first, then re-seed.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Bypass the >5 companies production-safety check.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(name)s: %(message)s",
    )

    _require_env_guard()
    return asyncio.run(_run(reset=args.reset, force=args.force))


if __name__ == "__main__":
    sys.exit(main())
