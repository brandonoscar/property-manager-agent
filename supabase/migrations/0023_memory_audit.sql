-- memory_audit — every Mem0 add/search/delete/clear/delete_company writes one
-- audit row (best-effort; the backend swallows failures so audit can never
-- regress a memory op). This is the SOC2 trail for the company-scoped memory
-- subsystem.
--
-- HISTORY / WHY THIS IS A FRESH MIGRATION (not the 0002 block):
--   The table was originally declared in 0002_phase1_memory_rls.sql, but 0002
--   is a HISTORICAL migration that was never applied (its earlier statements
--   reference tables/types that no longer match prod). The memory_audit table
--   was therefore never created on helixis-test, so every memory op logged
--   `relation "memory_audit" does not exist` and the audit trail silently
--   dropped (gotcha #8). This migration recreates it standalone, reconciled
--   with the CURRENT schema and code:
--     * company_id is UUID (companies.id became uuid in 0011; the app passes
--       company_users.company_id, also uuid). The 0002 block declared it as
--       `text`, which would fail the FK against the uuid companies.id.
--     * the action CHECK includes 'delete_company' (added with the
--       right-to-erasure path in PR #327); without it, erasure audit rows
--       would be rejected.
--
-- Idempotent: guarded with IF NOT EXISTS / DROP POLICY IF EXISTS.
--   psql "$HELIXIS_SUPABASE__DB_URL" -f supabase/migrations/0023_memory_audit.sql

begin;

create table if not exists public.memory_audit (
    id          uuid        primary key default gen_random_uuid(),
    company_id  uuid        not null references public.companies(id) on delete cascade,
    user_id     uuid        not null references auth.users(id),
    action      text        not null
                check (action in ('add', 'search', 'delete', 'clear', 'delete_company')),
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

-- Client (PostgREST) access is membership-scoped. The backend writes via the
-- service/asyncpg connection, which bypasses RLS, so these policies only gate
-- direct client reads/inserts — they never block the backend audit write.
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
