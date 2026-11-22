-- 0057_mirror_freshness_and_aliases.sql
--
-- Mirror data layer (approval-pipeline batch, PR 3). Two gaps from the
-- research investigation (§1.11, §1.8):
--
-- 1) PER-TABLE FRESHNESS. Nothing persisted per-table sync recency — the
--    chat footer's "as of" was a portfolio-global max over three tables, so
--    stale bills/work-orders were invisible, and the worker's re-sync guard
--    was in-memory only. ``helixis_mirror_sync_state`` is written by the
--    backfill (per stage) and the webhook dispatcher (per event family);
--    read by the chat footer, the connectors card, and any future
--    staleness limit.
--
-- 2) ENTITY ALIASES + TYPO TOLERANCE. "Mike the plumber" → vendor 4821 had
--    nowhere to live, and contact lookup was exact-substring only (a typo'd
--    name found nothing). ``helixis_entity_aliases`` stores user-confirmed
--    nicknames (exact-match, checked FIRST); pg_trgm GIN indexes give the
--    existing ILIKE lookups an index AND enable a similarity fallback for
--    typos.
--
-- Apply on prod (after 0056):
--   psql "$HELIXIS_SUPABASE__DB_URL" -f supabase/migrations/0057_mirror_freshness_and_aliases.sql

begin;

-- ── per-table sync recency ───────────────────────────────────────────

create table if not exists public.helixis_mirror_sync_state (
    company_id  uuid        not null,
    table_name  text        not null,
    synced_at   timestamptz not null default now(),
    row_count   integer,
    source      text,                 -- 'backfill' | 'webhook' | 'manual'
    primary key (company_id, table_name)
);

alter table public.helixis_mirror_sync_state enable row level security;

drop policy if exists "members read mirror sync state" on public.helixis_mirror_sync_state;
create policy "members read mirror sync state"
    on public.helixis_mirror_sync_state for select
    using (
        exists (
            select 1
              from public.company_users cu
             where cu.company_id = helixis_mirror_sync_state.company_id
               and cu.user_id    = auth.uid()
        )
    );

-- ── user-confirmed entity aliases ────────────────────────────────────

create table if not exists public.helixis_entity_aliases (
    id          uuid        primary key default gen_random_uuid(),
    company_id  uuid        not null,
    alias       text        not null,
    entity_type text        not null
        check (entity_type in ('tenant', 'owner', 'vendor', 'property', 'unit')),
    entity_id   bigint      not null,   -- Buildium id (the entity spine)
    label       text,                   -- display name at confirmation time
    created_by  text,
    created_at  timestamptz not null default now()
);

create unique index if not exists helixis_entity_aliases_company_alias_idx
    on public.helixis_entity_aliases (company_id, lower(alias));

alter table public.helixis_entity_aliases enable row level security;

drop policy if exists "members read entity aliases"  on public.helixis_entity_aliases;
drop policy if exists "members write entity aliases" on public.helixis_entity_aliases;
create policy "members read entity aliases"
    on public.helixis_entity_aliases for select
    using (
        exists (
            select 1
              from public.company_users cu
             where cu.company_id = helixis_entity_aliases.company_id
               and cu.user_id    = auth.uid()
        )
    );
create policy "members write entity aliases"
    on public.helixis_entity_aliases for insert
    with check (
        exists (
            select 1
              from public.company_users cu
             where cu.company_id = helixis_entity_aliases.company_id
               and cu.user_id    = auth.uid()
        )
    );

-- ── typo tolerance for contact lookup ────────────────────────────────
-- pg_trgm GIN indexes serve BOTH the existing ILIKE '%q%' lookups (which
-- currently seq-scan) and the new similarity (%) fallback for typos.

create extension if not exists pg_trgm;

create index if not exists helixis_tenants_name_trgm_idx
    on public.helixis_tenants
    using gin ((coalesce(first_name, '') || ' ' || coalesce(last_name, '')) gin_trgm_ops);

create index if not exists helixis_owners_name_trgm_idx
    on public.helixis_owners
    using gin (
        (coalesce(first_name, '') || ' ' || coalesce(last_name, '')
         || ' ' || coalesce(company_name, '')) gin_trgm_ops
    );

create index if not exists helixis_vendors_name_trgm_idx
    on public.helixis_vendors
    using gin (
        (coalesce(name, '') || ' ' || coalesce(company_name, '')) gin_trgm_ops
    );

commit;
