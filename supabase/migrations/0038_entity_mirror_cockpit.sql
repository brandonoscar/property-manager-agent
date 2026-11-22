-- 0038_entity_mirror_cockpit.sql — the property-cockpit entity mirror.
--
-- Workstream A (road-to-launch §A, master #5 — ⭐ COMMITTED): extend the thin
-- Buildium mirror (helixis_leases / helixis_work_orders) with the entities the
-- cockpit's starter-six metrics need:
--
--   vacancy                → helixis_properties + helixis_units (+ active leases)
--   open work orders       → helixis_work_orders (exists)
--   lease expirations 90d  → helixis_leases (exists)
--   delinquency            → helixis_lease_balances (outstanding-balance snapshot)
--   rent roll              → helixis_leases.rent (scheduled) +
--                            helixis_lease_transactions (collected)
--   tenants + basics       → helixis_tenants + helixis_lease_tenants join
--
-- ONTOLOGY STANCE (per the settled architecture decision): a faithful MIRROR
-- in plain Postgres, not a formal ontology. Buildium is the source of truth
-- and the referential authority. Cross-entity references (property_id,
-- unit_id, lease_id, tenant_id) are SOFT bigint Buildium ids + indexes, NOT
-- enforced FK constraints — webhook and backfill upserts arrive out of order
-- (a lease event can land before its property is mirrored) and a hard FK
-- would reject valid rows. company_id keeps its hard FK: that's OUR tenancy
-- boundary, not Buildium's graph.
--
-- Conventions mirror helixis_entities.sql + 0028: buildium_id as the upsert
-- conflict key, typed hot columns for query paths, ``raw`` jsonb keeping the
-- full payload so new fields need no migration, RLS = SELECT-only for company
-- members (0020's pattern); the backend writes via the service role.
--
-- NOTE: migrations have no runner (CLAUDE.md §4 / gotcha #8) — apply manually
-- to helixis-test and VERIFY, in order, after 0037.

begin;

-- ── Properties ────────────────────────────────────────────────────────

create table if not exists public.helixis_properties (
    id              uuid        primary key default gen_random_uuid(),
    buildium_id     bigint      unique not null,
    account_id      bigint      not null,
    company_id      uuid        not null
        references public.companies(id) on delete cascade,
    name            text,
    rental_sub_type text,
    address_line1   text,
    city            text,
    state           text,
    postal_code     text,
    raw             jsonb       not null,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);

create index if not exists helixis_properties_company_idx
    on public.helixis_properties (company_id);
create index if not exists helixis_properties_account_idx
    on public.helixis_properties (account_id);

-- ── Units ─────────────────────────────────────────────────────────────

create table if not exists public.helixis_units (
    id              uuid        primary key default gen_random_uuid(),
    buildium_id     bigint      unique not null,
    account_id      bigint      not null,
    company_id      uuid        not null
        references public.companies(id) on delete cascade,
    -- Soft Buildium reference (see header) — join on
    -- helixis_properties.buildium_id.
    property_id     bigint,
    unit_number     text,
    market_rent     numeric(12, 2),
    raw             jsonb       not null,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);

create index if not exists helixis_units_company_idx
    on public.helixis_units (company_id);
create index if not exists helixis_units_property_idx
    on public.helixis_units (property_id);

-- ── Tenants ───────────────────────────────────────────────────────────
-- PII lives here (names / email / phone). Reads are RLS-scoped to company
-- members below; anything leaving the backend for logs or telemetry goes
-- through security.pii.redact as usual.

create table if not exists public.helixis_tenants (
    id              uuid        primary key default gen_random_uuid(),
    buildium_id     bigint      unique not null,
    account_id      bigint      not null,
    company_id      uuid        not null
        references public.companies(id) on delete cascade,
    first_name      text,
    last_name       text,
    email           text,
    phone           text,
    raw             jsonb       not null,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);

create index if not exists helixis_tenants_company_idx
    on public.helixis_tenants (company_id);

-- ── Lease ↔ tenant join ───────────────────────────────────────────────
-- Buildium models tenancy many-to-many (Lease.Tenants[] / Tenant.Leases[]).
-- One row per (lease, tenant) pair, refreshed wholesale per lease on sync.

create table if not exists public.helixis_lease_tenants (
    company_id          uuid        not null
        references public.companies(id) on delete cascade,
    lease_buildium_id   bigint      not null,
    tenant_buildium_id  bigint      not null,
    status              text,
    move_in_date        timestamptz,
    updated_at          timestamptz not null default now(),
    primary key (company_id, lease_buildium_id, tenant_buildium_id)
);

create index if not exists helixis_lease_tenants_tenant_idx
    on public.helixis_lease_tenants (tenant_buildium_id);

-- ── Lease transactions (rent-roll "collected" side) ──────────────────

create table if not exists public.helixis_lease_transactions (
    id                  uuid        primary key default gen_random_uuid(),
    buildium_id         bigint      unique not null,
    account_id          bigint      not null,
    company_id          uuid        not null
        references public.companies(id) on delete cascade,
    lease_id            bigint,
    transaction_date    timestamptz,
    transaction_type    text,
    total_amount        numeric(12, 2),
    raw                 jsonb       not null,
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now()
);

create index if not exists helixis_lease_transactions_company_idx
    on public.helixis_lease_transactions (company_id);
create index if not exists helixis_lease_transactions_lease_idx
    on public.helixis_lease_transactions (lease_id);
create index if not exists helixis_lease_transactions_date_idx
    on public.helixis_lease_transactions (company_id, transaction_date);

-- ── Outstanding-balance snapshot (delinquency) ────────────────────────
-- /v1/leases/outstandingbalances is a point-in-time view, so the mirror is a
-- snapshot table: one row per delinquent lease, wholesale-replaced per
-- company on each backfill/refresh cycle. snapshot_at tells readers how
-- fresh the delinquency picture is.

create table if not exists public.helixis_lease_balances (
    company_id      uuid        not null
        references public.companies(id) on delete cascade,
    lease_id        bigint      not null,
    account_id      bigint      not null,
    property_id     bigint,
    unit_id         bigint,
    total_balance   numeric(12, 2),
    snapshot_at     timestamptz not null default now(),
    raw             jsonb       not null,
    primary key (company_id, lease_id)
);

create index if not exists helixis_lease_balances_property_idx
    on public.helixis_lease_balances (company_id, property_id);

-- ── RLS: SELECT-only for company members (0020's pattern) ─────────────
-- The backend writes via the service role (bypasses RLS); these policies
-- gate user-scoped JWT reads.

alter table public.helixis_properties enable row level security;
drop policy if exists "members can read helixis properties" on public.helixis_properties;
create policy "members can read helixis properties"
    on public.helixis_properties for select
    using (
        exists (
            select 1
              from public.company_users cu
             where cu.company_id = helixis_properties.company_id
               and cu.user_id    = auth.uid()
        )
    );

alter table public.helixis_units enable row level security;
drop policy if exists "members can read helixis units" on public.helixis_units;
create policy "members can read helixis units"
    on public.helixis_units for select
    using (
        exists (
            select 1
              from public.company_users cu
             where cu.company_id = helixis_units.company_id
               and cu.user_id    = auth.uid()
        )
    );

alter table public.helixis_tenants enable row level security;
drop policy if exists "members can read helixis tenants" on public.helixis_tenants;
create policy "members can read helixis tenants"
    on public.helixis_tenants for select
    using (
        exists (
            select 1
              from public.company_users cu
             where cu.company_id = helixis_tenants.company_id
               and cu.user_id    = auth.uid()
        )
    );

alter table public.helixis_lease_tenants enable row level security;
drop policy if exists "members can read helixis lease tenants" on public.helixis_lease_tenants;
create policy "members can read helixis lease tenants"
    on public.helixis_lease_tenants for select
    using (
        exists (
            select 1
              from public.company_users cu
             where cu.company_id = helixis_lease_tenants.company_id
               and cu.user_id    = auth.uid()
        )
    );

alter table public.helixis_lease_transactions enable row level security;
drop policy if exists "members can read helixis lease transactions" on public.helixis_lease_transactions;
create policy "members can read helixis lease transactions"
    on public.helixis_lease_transactions for select
    using (
        exists (
            select 1
              from public.company_users cu
             where cu.company_id = helixis_lease_transactions.company_id
               and cu.user_id    = auth.uid()
        )
    );

alter table public.helixis_lease_balances enable row level security;
drop policy if exists "members can read helixis lease balances" on public.helixis_lease_balances;
create policy "members can read helixis lease balances"
    on public.helixis_lease_balances for select
    using (
        exists (
            select 1
              from public.company_users cu
             where cu.company_id = helixis_lease_balances.company_id
               and cu.user_id    = auth.uid()
        )
    );

commit;
