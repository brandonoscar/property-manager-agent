-- 0041_entity_graph_vendors_owners_tasks.sql — ontology v2, slice 1:
-- vendors, rental owners, and tasks join the entity mirror, completing the
-- rentals entity graph (every id Helixis mentions now resolves to a local
-- name; the money/people entities stop being live-read-only).
--
-- Freshness: vendors + tasks have live webhook events (Vendor.* / Task.*,
-- already handled — the handlers refetch the entity, so the mirror upsert
-- rides that fetch). Rental owners have NO webhook events in Buildium's
-- catalog, so the owners mirror is kept fresh by the daily resync +
-- manual Sync only.
--
-- Conventions per 0038: buildium_id upsert key, soft Buildium references,
-- company_id hard FK as the tenancy boundary, ``raw`` jsonb payload, RLS
-- SELECT-only for company members (0020's pattern); service-role writes.
-- PII: vendor TaxId/AccountNumber and owner TaxId are STRIPPED from raw
-- before persist (same policy as tenants, migration 0038 era).
--
-- Wire shapes verified 2026-07-05 against the LeadSimple SDK
-- (AllTasksMessage attribute_map: TaskType/TaskStatus/Priority/DueDate/
-- Property/UnitId/AssignedToUserId/Category; VendorMessage;
-- RentalOwnerMessage).
--
-- NOTE: migrations have no runner (CLAUDE.md §4 / gotcha #8) — apply
-- manually to helixis-test and VERIFY, in order, after 0040.

begin;

-- ── Vendors ───────────────────────────────────────────────────────────

create table if not exists public.helixis_vendors (
    id            uuid        primary key default gen_random_uuid(),
    buildium_id   bigint      unique not null,
    account_id    bigint      not null,
    company_id    uuid        not null
        references public.companies(id) on delete cascade,
    name          text,
    is_company    boolean,
    first_name    text,
    last_name     text,
    company_name  text,
    is_active     boolean,
    category_id   bigint,
    category_name text,
    phone         text,
    email         text,
    raw           jsonb       not null,
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now()
);

create index if not exists helixis_vendors_company_idx
    on public.helixis_vendors (company_id);

-- ── Rental owners ─────────────────────────────────────────────────────

create table if not exists public.helixis_owners (
    id           uuid        primary key default gen_random_uuid(),
    buildium_id  bigint      unique not null,
    account_id   bigint      not null,
    company_id   uuid        not null
        references public.companies(id) on delete cascade,
    first_name   text,
    last_name    text,
    company_name text,
    email        text,
    phone        text,
    -- Soft Buildium property references (RentalOwnerMessage.PropertyIds).
    property_ids bigint[],
    raw          jsonb       not null,
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now()
);

create index if not exists helixis_owners_company_idx
    on public.helixis_owners (company_id);

-- ── Tasks (all four Buildium task subtypes, generic view) ─────────────

create table if not exists public.helixis_tasks (
    id                  uuid        primary key default gen_random_uuid(),
    buildium_id         bigint      unique not null,
    account_id          bigint      not null,
    company_id          uuid        not null
        references public.companies(id) on delete cascade,
    -- ContactRequest | ResidentRequest | Todo | RentalOwnerRequest
    task_type           text,
    title               text,
    status              text,
    priority            text,
    due_date            date,
    property_id         bigint,
    unit_id             bigint,
    assigned_to_user_id bigint,
    category_name       text,
    raw                 jsonb       not null,
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now()
);

create index if not exists helixis_tasks_company_idx
    on public.helixis_tasks (company_id);
create index if not exists helixis_tasks_property_idx
    on public.helixis_tasks (property_id);
create index if not exists helixis_tasks_status_idx
    on public.helixis_tasks (company_id, status);

-- ── RLS: SELECT-only for company members (0020's pattern) ─────────────

alter table public.helixis_vendors enable row level security;
drop policy if exists "members can read helixis vendors" on public.helixis_vendors;
create policy "members can read helixis vendors"
    on public.helixis_vendors for select
    using (
        exists (
            select 1
              from public.company_users cu
             where cu.company_id = helixis_vendors.company_id
               and cu.user_id    = auth.uid()
        )
    );

alter table public.helixis_owners enable row level security;
drop policy if exists "members can read helixis owners" on public.helixis_owners;
create policy "members can read helixis owners"
    on public.helixis_owners for select
    using (
        exists (
            select 1
              from public.company_users cu
             where cu.company_id = helixis_owners.company_id
               and cu.user_id    = auth.uid()
        )
    );

alter table public.helixis_tasks enable row level security;
drop policy if exists "members can read helixis tasks" on public.helixis_tasks;
create policy "members can read helixis tasks"
    on public.helixis_tasks for select
    using (
        exists (
            select 1
              from public.company_users cu
             where cu.company_id = helixis_tasks.company_id
               and cu.user_id    = auth.uid()
        )
    );

commit;
