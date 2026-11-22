-- 0014_property_assignments.sql
--
-- PR #80 — Properties × Roles routing grid (foundation).
--
-- Background. Pre-PR-#80, Helixis had no model for "who handles what."
-- An inbound Buildium webhook (Task.Created, WorkOrder.Created, etc.)
-- wrote to Activity but had no concept of routing the event to the
-- right Helixis user. Brandon's design: each property × category cell
-- carries an *action* (assign to user, automate, escalate, ignore…).
-- v1 ships ``assign_to_user`` only; the schema is intentionally wider
-- than v1 needs so future PRs add new ``action_type`` values without
-- migrations.
--
-- Shape notes:
-- * ``buildium_property_id`` is INT, not a FK — properties live in
--   Buildium, mirrored locally only on demand. A NULL row is the
--   "(no property)" company-wide default — events that don't resolve
--   to a property (vendor created, general task, etc.) hit this row.
-- * ``action_type`` is checked against a closed enum at the SQL layer
--   so a typo in code surfaces immediately instead of silently
--   persisting a no-op. v1 enumerates one value; future PRs add to
--   the CHECK constraint.
-- * ``action_config`` is JSONB so handlers can carry arbitrary
--   payloads (e.g. ``{"buildium_user_id": 42}`` today;
--   ``{"automation_id": "...", "schedule": "..."}`` later).
--
-- Apply order: after 0013. Service-role writers (the API routes and
-- the webhook router) bypass RLS, so the policies are scoped to
-- per-company read/write for the authenticated session at PostgREST.

create table if not exists public.property_assignments (
    id                   uuid        primary key default gen_random_uuid(),
    company_id           uuid        not null
        references public.companies(id) on delete cascade,
    -- Buildium property id (or NULL for the "no property" default row).
    buildium_property_id integer,
    category             text        not null
        check (category in (
            'maintenance', 'leasing', 'accounting',
            'tenant_relations', 'other'
        )),
    -- PR #80 ships ONE action type. The CHECK constraint guards future
    -- PRs from typo'ing a value before they wire a handler.
    action_type          text        not null default 'assign_to_user'
        check (action_type in ('assign_to_user')),
    -- Free-form per-handler payload. ``assign_to_user`` reads
    -- ``buildium_user_id`` from here; future action types put their
    -- config in this column without altering the table.
    action_config        jsonb       not null default '{}'::jsonb,
    updated_at           timestamptz not null default now(),
    updated_by           uuid        references auth.users(id) on delete set null
);

-- One row per (company, property, category). NULL property is a single
-- row per (company, category) — Postgres treats NULL as distinct in
-- unique indexes by default, so we use a partial index plus a separate
-- index for the property-bound rows.
create unique index if not exists property_assignments_property_unique
    on public.property_assignments (company_id, buildium_property_id, category)
    where buildium_property_id is not null;

create unique index if not exists property_assignments_default_unique
    on public.property_assignments (company_id, category)
    where buildium_property_id is null;

create index if not exists property_assignments_company_idx
    on public.property_assignments (company_id, buildium_property_id);

alter table public.property_assignments enable row level security;

drop policy if exists "property_assignments_company_read"   on public.property_assignments;
drop policy if exists "property_assignments_company_write"  on public.property_assignments;

create policy "property_assignments_company_read"
    on public.property_assignments for select
    using (
        exists (
            select 1 from public.company_users cu
             where cu.company_id = property_assignments.company_id
               and cu.user_id    = auth.uid()
        )
    );

create policy "property_assignments_company_write"
    on public.property_assignments for all
    using (
        exists (
            select 1 from public.company_users cu
             where cu.company_id = property_assignments.company_id
               and cu.user_id    = auth.uid()
        )
    )
    with check (
        exists (
            select 1 from public.company_users cu
             where cu.company_id = property_assignments.company_id
               and cu.user_id    = auth.uid()
        )
    );
