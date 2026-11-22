"""End-to-end RLS isolation test for the 4 Buildium-mirror tables.

Skipped automatically when ``HELIXIS_SUPABASE__DB_URL`` is unset (the
CI environment does not have a Postgres instance to test against). When
a DB is available, this proves cross-company isolation by:

  1. Creating two companies (A, B) and a member user for each.
  2. Inserting a row scoped to company A in each of the 4 RLS-protected
     tables.
  3. Setting the JWT claim to user B and querying — expecting 0 rows
     for every table.

The same harness can be run locally:
    HELIXIS_SUPABASE__DB_URL=postgresql://… pytest tests/unit/test_buildium_rls.py
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest

# Skip the whole module unless a DB is configured.
DB_URL = os.environ.get("HELIXIS_SUPABASE__DB_URL", "")

pytestmark = pytest.mark.skipif(
    not DB_URL,
    reason="HELIXIS_SUPABASE__DB_URL not set; RLS test requires a real Postgres",
)


PROTECTED_TABLES = (
    "helixis_leases",
    "helixis_work_orders",
    "helixis_activity_log",
    "buildium_events",
)

# Every table that must have RLS ENABLED. Membership-scoped tables carry per-
# tenant policies; deny-all tables (service-role only) have RLS on with NO
# policy. Either way, ``pg_tables.rowsecurity`` must be true — a false here is
# the risk #1 leak (Supabase lint 0013).
MEMBERSHIP_SCOPED_TABLES = (
    "companies",
    "company_users",
    "helixis_leases",
    "helixis_work_orders",
    "helixis_activity_log",
    "buildium_events",
    "helixis_chat_messages",
    "property_assignments",
    "buildium_staff",
    "orchestrator_runs",
    "memory_audit",
    "helixis_action_idempotency",
    "helixis_pending_confirmations",
    "helixis_property_intel",
)

# RLS on, NO permissive policy — reachable only via the service-role pool, which
# bypasses RLS. anon/authenticated must see zero rows even when data exists.
# (helixis_memories* + mem0migrations are created by Mem0 at runtime; locked by
# migration 0025 + memory/manager.py.)
DENY_ALL_TABLES = (
    "helixis_gmail_seen",
    "helixis_memories",
    "helixis_memories_entities",
    "mem0migrations",
    "jurisdiction_notes",
    # Every PMS's credentials (migration 0110): company_buildium_credentials
    # was membership-scoped until then, and any member could rewrite it past
    # the API's admin check (gotcha 212).
    "company_buildium_credentials",
    "company_rentvine_credentials",
    "company_rentmanager_credentials",
    "company_propertyware_credentials",
    "company_doorloop_credentials",
)


@pytest.fixture
async def pool() -> AsyncIterator:
    """Per-test asyncpg pool. Caller closes it."""
    import asyncpg

    p = await asyncpg.create_pool(DB_URL, min_size=1, max_size=2)
    yield p
    await p.close()


@pytest.fixture
async def two_companies(pool) -> tuple[tuple[str, str], tuple[str, str]]:
    """Create two companies and a member auth user each. Returns:
    ((company_a, user_a_uuid), (company_b, user_b_uuid))
    """
    # companies.id is uuid — a slug like "test_co_a_<hex>" fails asyncpg's
    # uuid encoding on the FIRST insert, erroring the fixture and both
    # behavioral tests. That's exactly what kept every scheduled rls-guard
    # run red since 2026-07-13: locally this file always SKIPS (no DB URL),
    # so the Monday cron was the only host that ever executed it.
    company_a = str(uuid.uuid4())
    company_b = str(uuid.uuid4())
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO companies (id, name) VALUES ($1, $2), ($3, $4)",
                company_a,
                "rls-guard test co A",
                company_b,
                "rls-guard test co B",
            )
            # auth.users(id) requires a real Supabase row; use service role
            # to insert minimal auth.users entries for the test.
            await conn.execute(
                "INSERT INTO auth.users (id, instance_id, aud, role, email) "
                "VALUES ($1, $2, 'authenticated', 'authenticated', $3), "
                "       ($4, $2, 'authenticated', 'authenticated', $5) "
                "ON CONFLICT (id) DO NOTHING",
                user_a,
                uuid.UUID("00000000-0000-0000-0000-000000000000"),
                f"a-{user_a}@test.local",
                user_b,
                f"b-{user_b}@test.local",
            )
            await conn.execute(
                "INSERT INTO company_users (user_id, company_id, role) "
                "VALUES ($1, $2, 'member'), ($3, $4, 'member')",
                user_a,
                company_a,
                user_b,
                company_b,
            )
    yield ((company_a, str(user_a)), (company_b, str(user_b)))
    # Teardown — explicitly remove EVERYTHING this fixture or its tests could
    # have created (not just the companies), children before parents, so the
    # scheduled live run (rls-guard.yml) never accumulates junk in the prod DB.
    # (The prior version deleted only `companies` and relied on FK cascades +
    # left the auth.users rows behind — fine for an ad-hoc local run, not for a
    # weekly cron.)
    async with pool.acquire() as conn:
        async with conn.transaction():
            for table in (
                "buildium_events",
                "helixis_leases",
                "helixis_work_orders",
                "helixis_activity_log",
                "helixis_gmail_seen",
            ):
                await conn.execute(
                    f"DELETE FROM {table} WHERE company_id IN ($1, $2)", company_a, company_b
                )
            # A trigger on auth.users auto-provisions a company PER NEW USER
            # (id = the user's uuid, name from the email prefix) plus its
            # membership row — discovered live 2026-08-17. Without deleting
            # those too, the weekly cron accumulates two junk companies per
            # run even when every test passes.
            await conn.execute(
                "DELETE FROM company_users WHERE company_id::text IN ($1, $2, $3, $4)",
                company_a,
                company_b,
                str(user_a),
                str(user_b),
            )
            await conn.execute(
                "DELETE FROM companies WHERE id::text IN ($1, $2, $3, $4)",
                company_a,
                company_b,
                str(user_a),
                str(user_b),
            )
            await conn.execute("DELETE FROM auth.users WHERE id IN ($1, $2)", user_a, user_b)


@pytest.mark.asyncio
async def test_user_b_cannot_read_company_a_rows(pool, two_companies) -> None:
    """Insert into all 4 tables under company A; query as user B; expect 0."""
    (company_a, _user_a), (_company_b, user_b) = two_companies

    # Seed one row per protected table for company A. Service role bypasses
    # RLS so we can write directly without a JWT context.
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO buildium_events "
            "  (dedup_key, event_name, event_time, account_id, company_id, payload) "
            "VALUES ($1, 'test.event', now(), $2, $3, '{}'::jsonb)",
            f"test-{uuid.uuid4()}",
            int(company_a[-8:], 16) % 1_000_000,
            company_a,
        )
        await conn.execute(
            "INSERT INTO helixis_leases "
            "  (buildium_id, account_id, company_id, raw) "
            "VALUES ($1, $2, $3, '{}'::jsonb)",
            int(uuid.uuid4().int % 1_000_000_000),
            int(company_a[-8:], 16) % 1_000_000,
            company_a,
        )
        await conn.execute(
            "INSERT INTO helixis_work_orders "
            "  (buildium_id, account_id, company_id, raw) "
            "VALUES ($1, $2, $3, '{}'::jsonb)",
            int(uuid.uuid4().int % 1_000_000_000),
            int(company_a[-8:], 16) % 1_000_000,
            company_a,
        )
        await conn.execute(
            "INSERT INTO helixis_activity_log "
            "  (account_id, company_id, source, action_type, description) "
            "VALUES ($1, $2, 'manual', 'test', 'rls test row')",
            int(company_a[-8:], 16) % 1_000_000,
            company_a,
        )

    # Read as user B — RLS must hide every row from every table.
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SET LOCAL ROLE authenticated; "
                f'SET LOCAL "request.jwt.claims" = \'{{"sub":"{user_b}"}}\';'
            )
            for table in PROTECTED_TABLES:
                rows = await conn.fetch(f"SELECT * FROM {table} WHERE company_id = $1", company_a)
                assert rows == [], (
                    f"RLS did not block: user B read {len(rows)} rows from {table} for company A"
                )


@pytest.mark.asyncio
async def test_all_tenant_tables_have_rls_enabled(pool) -> None:
    """Structural guard: every tenant + service-only table has rowsecurity on.

    Cheaper than row-level seeding for each table, and catches the exact regression
    behind risk #1 — a public table shipped with RLS disabled (Supabase lint 0013).
    """
    expected = set(MEMBERSHIP_SCOPED_TABLES) | set(DENY_ALL_TABLES)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT tablename, rowsecurity FROM pg_tables "
            "WHERE schemaname = 'public' AND tablename = ANY($1::text[])",
            list(expected),
        )
    state = {r["tablename"]: r["rowsecurity"] for r in rows}
    off = sorted(t for t in expected if state.get(t) is False)
    assert off == [], f"RLS disabled on public table(s): {', '.join(off)}"


@pytest.mark.asyncio
async def test_deny_all_tables_block_authenticated_reads(pool, two_companies) -> None:
    """A deny-all table (RLS on, no policy) must return 0 rows to authenticated,
    even when a row exists. Seed gmail_seen via service role, read as user B."""
    (company_a, user_a), (_company_b, user_b) = two_companies

    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO helixis_gmail_seen (user_id, message_id, company_id) "
            "VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
            uuid.UUID(user_a),
            f"msg-{uuid.uuid4()}",
            company_a,
        )

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SET LOCAL ROLE authenticated; "
                f'SET LOCAL "request.jwt.claims" = \'{{"sub":"{user_b}"}}\';'
            )
            rows = await conn.fetch("SELECT * FROM helixis_gmail_seen")
            assert rows == [], (
                f"deny-all RLS leaked: authenticated read {len(rows)} gmail_seen rows"
            )
