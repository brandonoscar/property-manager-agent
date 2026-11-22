-- Helixis initial schema.
--
-- Note: this file was retroactively corrected on 2026-05-16 to reflect
-- production's actual uuid-based schema (see migration 0011 for the
-- audit trail). Production has run with uuid ids since day one — the
-- earlier `text` declarations in this file were never applied. Do not
-- "fix" this back to text without understanding why uuid is correct
-- (see 0006 trigger that depends on auth.users.id being a uuid).
--
-- Run once against a fresh Supabase / Postgres database before starting the backend.
--
-- Applies:
--   1. pgvector extension (used by Mem0 for embedding storage)
--   2. companies + company_users tables (multi-tenancy)
--   3. RLS policies scoped to auth.uid()

begin;

-- ---------------------------------------------------------------------------
-- Extensions
-- ---------------------------------------------------------------------------
create extension if not exists "uuid-ossp";
create extension if not exists "vector";

-- ---------------------------------------------------------------------------
-- Tenancy
-- ---------------------------------------------------------------------------
create table if not exists public.companies (
    id          uuid primary key default gen_random_uuid(),
    name        text not null,
    created_at  timestamptz not null default now()
);

create table if not exists public.company_users (
    id          uuid not null primary key default gen_random_uuid(),
    user_id     uuid not null references auth.users(id) on delete cascade,
    company_id  uuid not null references public.companies(id) on delete cascade,
    role        text not null default 'member',
    created_at  timestamptz not null default now(),
    unique (company_id, user_id)
);

create index if not exists company_users_user_id_idx
    on public.company_users (user_id);
create index if not exists company_users_company_id_idx
    on public.company_users (company_id);

-- ---------------------------------------------------------------------------
-- Row Level Security
-- ---------------------------------------------------------------------------
alter table public.companies      enable row level security;
alter table public.company_users  enable row level security;

-- Users can see companies they belong to.
drop policy if exists "members can read companies" on public.companies;
create policy "members can read companies"
    on public.companies for select
    using (
        exists (
            select 1
              from public.company_users cu
             where cu.company_id = companies.id
               and cu.user_id    = auth.uid()
        )
    );

-- Users can see their own memberships.
drop policy if exists "users can read own memberships" on public.company_users;
create policy "users can read own memberships"
    on public.company_users for select
    using (user_id = auth.uid());

-- ---------------------------------------------------------------------------
-- Mem0 collection (created lazily by Mem0 at first write, but keeping the
-- extension enabled above is the prerequisite).
-- ---------------------------------------------------------------------------

commit;
