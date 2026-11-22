-- 0015_buildium_staff.sql
--
-- PR #81 — local mirror of Buildium staff (``UserMessage``) so the
-- Properties × Roles grid + Inbox reassign dropdowns don't hit
-- Buildium on every render.
--
-- Source of truth is still Buildium — this table is a *cache*. The
-- ``last_synced_at`` column lets us age rows; the on-demand
-- "Re-sync" button + (future) cron job upsert here.
--
-- Field shapes mirror the verified Buildium ``UserMessage`` spec
-- (PR #81 brief). Columns NOT in the spec (``Title``, ``Department``,
-- ``IsAdmin``, etc.) are intentionally absent — PR #78's
-- OutstandingBalance episode is the precedent.

create table if not exists public.buildium_staff (
    id                   uuid        primary key default gen_random_uuid(),
    company_id           uuid        not null
        references public.companies(id) on delete cascade,
    buildium_user_id     integer     not null,
    -- Display fields (PascalCase on the wire → snake_case here).
    first_name           text,
    last_name            text,
    email                text,
    -- ``user_types`` is an array because a Buildium user can carry
    -- multiple types (Staff + Tenant Center, etc.). Routing
    -- dropdowns filter to rows where ``'Staff' = ANY(user_types)``.
    user_types           text[]      not null default '{}',
    user_role_id         integer,
    user_role_name       text,
    -- ``is_active=false`` is the soft-delete tombstone for users
    -- that disappeared from a sync. Historical ``property_assignments``
    -- rows still resolve a name via this column.
    is_active            boolean     not null default true,
    last_synced_at       timestamptz not null default now(),
    unique (company_id, buildium_user_id)
);

create index if not exists buildium_staff_company_idx
    on public.buildium_staff (company_id);

create index if not exists buildium_staff_active_idx
    on public.buildium_staff (company_id, is_active);

alter table public.buildium_staff enable row level security;

drop policy if exists "buildium_staff_company_read"   on public.buildium_staff;
drop policy if exists "buildium_staff_service_write" on public.buildium_staff;

-- Members of the company can READ their staff cache. Writes only
-- happen via the service role (the sync job runs there), so
-- end-user PostgREST cannot mutate the cache directly.
create policy "buildium_staff_company_read"
    on public.buildium_staff for select
    using (
        exists (
            select 1 from public.company_users cu
             where cu.company_id = buildium_staff.company_id
               and cu.user_id    = auth.uid()
        )
    );
