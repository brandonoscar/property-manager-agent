-- 0020_rls_on_work_orders_and_leases.sql — RLS for the helixis_entities subtree.
--
-- WHY THIS EXISTS
-- src/helixis/db/schema/helixis_entities.sql declares helixis_leases,
-- helixis_work_orders, and helixis_activity_log. The first two have never
-- been applied to prod. Once PR #204's corrected schema is applied, those
-- tables will exist but be RLS-unprotected. 0020 closes that gap by
-- installing SELECT-only RLS that mirrors 0019_orchestrator_runs.sql's
-- auth pattern.
--
-- WHY NOT JUST RE-RUN 0002_phase1_memory_rls.sql
-- 0002 was the original Phase 1 RLS installer for the same family of
-- tables. It was never applied to prod (verified:
-- supabase_migrations.schema_migrations contains zero rows for version
-- 0002) and can never re-run against the current uuid-aligned schema:
--   * Lines 34-46 bootstrap insert ``account_id::text`` into
--     ``companies(id)``, but ``companies.id`` is uuid (per 0011).
--   * Lines 52-53 and 104-105 add ``company_id text`` columns referencing
--     ``companies(id)``, but ``companies.id`` is uuid.
-- See the HISTORICAL header on 0002 for the full archaeology.
--
-- WRITE PATH
-- The backend writes to these tables through the service role (see
-- ``src/helixis/db/repositories/entities.py`` against the pool returned
-- by ``get_pool()``), which bypasses RLS. No INSERT/UPDATE/DELETE policy
-- is required for production traffic — same stance 0019 took for
-- orchestrator_runs.
--
-- APPLY ORDER
-- helixis_entities.sql (post-PR #204) MUST be applied before 0020. 0020's
-- ``alter table`` statements reference public.helixis_work_orders and
-- public.helixis_leases; without those tables, the migration errors on
-- the first statement.

begin;

-- ── helixis_work_orders ───────────────────────────────────────────────

alter table public.helixis_work_orders enable row level security;

-- Read scoped to the authenticated user's company memberships. Mirrors
-- the pattern from migration 0019's "members can read orchestrator runs"
-- policy — same join through public.company_users, same auth.uid() check.
drop policy if exists "members can read helixis work orders" on public.helixis_work_orders;
create policy "members can read helixis work orders"
    on public.helixis_work_orders for select
    using (
        exists (
            select 1
              from public.company_users cu
             where cu.company_id = helixis_work_orders.company_id
               and cu.user_id    = auth.uid()
        )
    );

-- ── helixis_leases ────────────────────────────────────────────────────

alter table public.helixis_leases enable row level security;

drop policy if exists "members can read helixis leases" on public.helixis_leases;
create policy "members can read helixis leases"
    on public.helixis_leases for select
    using (
        exists (
            select 1
              from public.company_users cu
             where cu.company_id = helixis_leases.company_id
               and cu.user_id    = auth.uid()
        )
    );

-- The backend writes via the service role (bypasses RLS), so no
-- INSERT/UPDATE policy is required for production traffic. The SELECT
-- policies above are the ones that matter when the API reads back
-- through a user-scoped JWT.

commit;
