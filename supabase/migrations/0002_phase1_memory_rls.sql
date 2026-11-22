-- =============================================================================
-- HISTORICAL MIGRATION — DO NOT RE-RUN
-- =============================================================================
--
-- This migration was the original Phase 1 RLS setup for helixis_leases,
-- helixis_work_orders, helixis_activity_log, and buildium_events, plus a
-- bootstrap insert into the companies table. It was never applied to
-- production (verified: supabase_migrations.schema_migrations contains zero
-- rows for version 0002).
--
-- WHY IT WAS NEVER APPLIED
-- The transaction would fail at line 37 (`select distinct account_id from
-- public.helixis_work_orders`) because that table was never created — its
-- declaration lives in src/helixis/db/schema/helixis_entities.sql which was
-- never applied to prod.
--
-- WHY IT CAN NEVER BE RE-RUN AGAINST CURRENT PROD
-- - Lines 34-46 bootstrap companies via `account_id::text` into companies(id),
--   but companies.id is now uuid (per 0011_baseline_uuid_alignment.sql).
-- - Lines 52-53, 104-105, 156-157, 208-209 add `company_id text` columns
--   referencing companies(id), but companies.id is uuid.
-- - The helixis_entities.sql schema (post-PR #204) declares company_id as
--   uuid, not text.
--
-- WHAT REPLACES IT
-- - 0011_baseline_uuid_alignment.sql: companies(id) uuid alignment + retro
--   correction of 0001/0007 column types.
-- - PR #204 (helixis_entities.sql + entities.py): same retro correction
--   extended to the helixis_entities subtree and its repository code.
-- - 0020_rls_on_work_orders_and_leases.sql: RLS for the two tables that PR
--   #204's schema actually creates.
--
-- OUT OF SCOPE FOR THIS REPLACEMENT CHAIN
-- - helixis_activity_log exists in prod (created by an earlier migration);
--   its RLS state is not addressed by 0020 and is captured as a separate
--   observation in PR B's body.
-- - buildium_events RLS likewise out of scope for 0020.
-- - memory_audit table (lines 260-300) — out of scope; tracked separately.
--
-- WHY KEPT
-- Preserved as historical record so the migration sequence is intact and any
-- future archaeology has the original intent visible. If you are reading this
-- because you are debugging a failure: do NOT manually run this file. Read
-- 0011 and 0020 instead.
-- =============================================================================


-- Phase 1: company_id + RLS lockdown on Buildium-mirror tables, plus memory_audit.
--
-- Apply once against a database that already has 0001_init.sql.
--   psql "$HELIXIS_SUPABASE__DB_URL" -f supabase/migrations/0002_phase1_memory_rls.sql
--
-- Idempotent: re-runs are safe. Backfill clauses are guarded by IS NULL so a
-- second run does nothing.
--
-- What this does:
--   1. Adds company_id (text, FK -> companies.id) to four Buildium-mirror
--      tables: helixis_leases, helixis_work_orders, helixis_activity_log,
--      buildium_events.
--   2. Backfills company_id from the existing account_id (the convention
--      already enforced in worker.py: company_id = str(account_id)).
--   3. Sets company_id NOT NULL once backfill is in place.
--   4. Adds an index on company_id for membership-join performance.
--   5. Enables RLS on all four tables and adds select/insert/update/delete
--      policies that mirror the membership-join pattern from 0001_init.sql.
--   6. Creates the memory_audit table with the same RLS pattern.
--
-- This migration trusts main's existing convention (company_id = str(account_id))
-- per the Phase 1 brief's "trust main" rule. If Helixis later supports multiple
-- companies per Buildium account (or vice versa), this convention will need
-- to be replaced by a real mapping table, with a follow-up backfill.

begin;

-- ─────────────────────────────────────────────────────────────────────
-- Helper: ensure a companies row exists for every account_id in the
-- Buildium tables before we add the FK. Without this, the ALTER TABLE
-- below would fail on any unmapped account_id.
-- ─────────────────────────────────────────────────────────────────────

insert into public.companies (id, name)
select distinct account_id::text, 'Buildium account ' || account_id::text
  from (
        select account_id from public.helixis_leases       where account_id is not null
        union
        select account_id from public.helixis_work_orders  where account_id is not null
        union
        select account_id from public.helixis_activity_log where account_id is not null
        union
        select account_id from public.buildium_events      where account_id is not null
       ) src
 where account_id::text not in (select id from public.companies)
on conflict (id) do nothing;

-- ─────────────────────────────────────────────────────────────────────
-- helixis_leases
-- ─────────────────────────────────────────────────────────────────────

alter table public.helixis_leases
    add column if not exists company_id text references public.companies(id) on delete cascade;

update public.helixis_leases
   set company_id = account_id::text
 where company_id is null;

alter table public.helixis_leases
    alter column company_id set not null;

create index if not exists helixis_leases_company_id_idx
    on public.helixis_leases (company_id);

alter table public.helixis_leases enable row level security;

drop policy if exists "members can read helixis_leases"   on public.helixis_leases;
drop policy if exists "members can write helixis_leases"  on public.helixis_leases;

create policy "members can read helixis_leases"
    on public.helixis_leases for select
    using (
        exists (
            select 1
              from public.company_users cu
             where cu.company_id = helixis_leases.company_id
               and cu.user_id    = auth.uid()
        )
    );

create policy "members can write helixis_leases"
    on public.helixis_leases for all
    using (
        exists (
            select 1
              from public.company_users cu
             where cu.company_id = helixis_leases.company_id
               and cu.user_id    = auth.uid()
        )
    )
    with check (
        exists (
            select 1
              from public.company_users cu
             where cu.company_id = helixis_leases.company_id
               and cu.user_id    = auth.uid()
        )
    );

-- ─────────────────────────────────────────────────────────────────────
-- helixis_work_orders
-- ─────────────────────────────────────────────────────────────────────

alter table public.helixis_work_orders
    add column if not exists company_id text references public.companies(id) on delete cascade;

update public.helixis_work_orders
   set company_id = account_id::text
 where company_id is null;

alter table public.helixis_work_orders
    alter column company_id set not null;

create index if not exists helixis_work_orders_company_id_idx
    on public.helixis_work_orders (company_id);

alter table public.helixis_work_orders enable row level security;

drop policy if exists "members can read helixis_work_orders"  on public.helixis_work_orders;
drop policy if exists "members can write helixis_work_orders" on public.helixis_work_orders;

create policy "members can read helixis_work_orders"
    on public.helixis_work_orders for select
    using (
        exists (
            select 1
              from public.company_users cu
             where cu.company_id = helixis_work_orders.company_id
               and cu.user_id    = auth.uid()
        )
    );

create policy "members can write helixis_work_orders"
    on public.helixis_work_orders for all
    using (
        exists (
            select 1
              from public.company_users cu
             where cu.company_id = helixis_work_orders.company_id
               and cu.user_id    = auth.uid()
        )
    )
    with check (
        exists (
            select 1
              from public.company_users cu
             where cu.company_id = helixis_work_orders.company_id
               and cu.user_id    = auth.uid()
        )
    );

-- ─────────────────────────────────────────────────────────────────────
-- helixis_activity_log
-- ─────────────────────────────────────────────────────────────────────

alter table public.helixis_activity_log
    add column if not exists company_id text references public.companies(id) on delete cascade;

update public.helixis_activity_log
   set company_id = account_id::text
 where company_id is null;

alter table public.helixis_activity_log
    alter column company_id set not null;

create index if not exists helixis_activity_log_company_id_idx
    on public.helixis_activity_log (company_id);

alter table public.helixis_activity_log enable row level security;

drop policy if exists "members can read helixis_activity_log"  on public.helixis_activity_log;
drop policy if exists "members can write helixis_activity_log" on public.helixis_activity_log;

create policy "members can read helixis_activity_log"
    on public.helixis_activity_log for select
    using (
        exists (
            select 1
              from public.company_users cu
             where cu.company_id = helixis_activity_log.company_id
               and cu.user_id    = auth.uid()
        )
    );

create policy "members can write helixis_activity_log"
    on public.helixis_activity_log for all
    using (
        exists (
            select 1
              from public.company_users cu
             where cu.company_id = helixis_activity_log.company_id
               and cu.user_id    = auth.uid()
        )
    )
    with check (
        exists (
            select 1
              from public.company_users cu
             where cu.company_id = helixis_activity_log.company_id
               and cu.user_id    = auth.uid()
        )
    );

-- ─────────────────────────────────────────────────────────────────────
-- buildium_events
-- ─────────────────────────────────────────────────────────────────────

alter table public.buildium_events
    add column if not exists company_id text references public.companies(id) on delete cascade;

update public.buildium_events
   set company_id = account_id::text
 where company_id is null;

alter table public.buildium_events
    alter column company_id set not null;

create index if not exists buildium_events_company_id_idx
    on public.buildium_events (company_id);

alter table public.buildium_events enable row level security;

drop policy if exists "members can read buildium_events"  on public.buildium_events;
drop policy if exists "members can write buildium_events" on public.buildium_events;

create policy "members can read buildium_events"
    on public.buildium_events for select
    using (
        exists (
            select 1
              from public.company_users cu
             where cu.company_id = buildium_events.company_id
               and cu.user_id    = auth.uid()
        )
    );

create policy "members can write buildium_events"
    on public.buildium_events for all
    using (
        exists (
            select 1
              from public.company_users cu
             where cu.company_id = buildium_events.company_id
               and cu.user_id    = auth.uid()
        )
    )
    with check (
        exists (
            select 1
              from public.company_users cu
             where cu.company_id = buildium_events.company_id
               and cu.user_id    = auth.uid()
        )
    );

-- ─────────────────────────────────────────────────────────────────────
-- memory_audit — every Mem0 add/search/delete writes an audit row.
-- ─────────────────────────────────────────────────────────────────────

create table if not exists public.memory_audit (
    id          uuid        primary key default gen_random_uuid(),
    company_id  text        not null references public.companies(id) on delete cascade,
    user_id     uuid        not null references auth.users(id),
    action      text        not null check (action in ('add', 'search', 'delete', 'clear')),
    memory_id   text,
    query       text,
    content     text,
    metadata    jsonb,
    created_at  timestamptz not null default now()
);

create index if not exists memory_audit_company_created_idx
    on public.memory_audit (company_id, created_at desc);

alter table public.memory_audit enable row level security;

drop policy if exists "members can read memory_audit"   on public.memory_audit;
drop policy if exists "members can insert memory_audit" on public.memory_audit;

create policy "members can read memory_audit"
    on public.memory_audit for select
    using (
        exists (
            select 1
              from public.company_users cu
             where cu.company_id = memory_audit.company_id
               and cu.user_id    = auth.uid()
        )
    );

create policy "members can insert memory_audit"
    on public.memory_audit for insert
    with check (
        exists (
            select 1
              from public.company_users cu
             where cu.company_id = memory_audit.company_id
               and cu.user_id    = auth.uid()
        )
    );

commit;
