-- 0042_money_mirror_bills_gl.sql — ontology v2, slice 2: the money ledger.
-- Bills (with per-line GL/property detail) + the GL chart of accounts join
-- the mirror, closing the last entity-awareness gap: "what did we spend on
-- plumbing at Garden Row this quarter?" becomes a local SQL question and
-- every GlAccountId resolves to a name without a live call.
--
-- Freshness: Bill.* webhooks are handled (the handler refetches the bill —
-- the mirror upsert rides that fetch), and Bill.Payment.* now refreshes the
-- paid status of every bill the payment covers. GLAccount.* events are in
-- the acknowledged-only set (reference data that changes rarely) — the
-- chart of accounts refreshes on backfill/daily resync.
--
-- Conventions per 0038: buildium_id upsert key, soft Buildium references,
-- company_id hard FK, ``raw`` jsonb, RLS SELECT-only for company members;
-- service-role writes. Bill lines are replace-on-upsert (delete + insert)
-- keyed by the parent bill.
--
-- NOTE: migrations have no runner (CLAUDE.md §4 / gotcha #8) — apply
-- manually to helixis-test and VERIFY, in order, after 0041.

begin;

-- ── GL chart of accounts ──────────────────────────────────────────────

create table if not exists public.helixis_gl_accounts (
    id                   uuid        primary key default gen_random_uuid(),
    buildium_id          bigint      unique not null,
    account_id           bigint      not null,
    company_id           uuid        not null
        references public.companies(id) on delete cascade,
    account_number       text,
    name                 text,
    -- Asset / Liability / Equity / Income / Expense
    type                 text,
    sub_type             text,
    is_active            boolean,
    is_bank_account      boolean,
    parent_gl_account_id bigint,
    raw                  jsonb       not null,
    created_at           timestamptz not null default now(),
    updated_at           timestamptz not null default now()
);

create index if not exists helixis_gl_accounts_company_idx
    on public.helixis_gl_accounts (company_id);

-- ── Bills ─────────────────────────────────────────────────────────────

create table if not exists public.helixis_bills (
    id               uuid        primary key default gen_random_uuid(),
    buildium_id      bigint      unique not null,
    account_id       bigint      not null,
    company_id       uuid        not null
        references public.companies(id) on delete cascade,
    vendor_id        bigint,
    bill_date        date,
    due_date         date,
    total_amount     numeric(12, 2),
    is_paid          boolean,
    reference_number text,
    -- The WO→bill link (CreateBillRequest.WorkOrderId; read off raw).
    work_order_id    bigint,
    raw              jsonb       not null,
    created_at       timestamptz not null default now(),
    updated_at       timestamptz not null default now()
);

create index if not exists helixis_bills_company_idx
    on public.helixis_bills (company_id);
create index if not exists helixis_bills_vendor_idx
    on public.helixis_bills (company_id, vendor_id);
create index if not exists helixis_bills_unpaid_idx
    on public.helixis_bills (company_id, is_paid);

-- ── Bill lines (per-line GL + property attribution) ───────────────────

create table if not exists public.helixis_bill_lines (
    company_id    uuid        not null
        references public.companies(id) on delete cascade,
    -- Soft parent reference: join on helixis_bills.buildium_id.
    bill_id       bigint      not null,
    line_index    int         not null,
    property_id   bigint,
    unit_id       bigint,
    gl_account_id bigint,
    amount        numeric(12, 2),
    memo          text,
    primary key (company_id, bill_id, line_index)
);

create index if not exists helixis_bill_lines_property_idx
    on public.helixis_bill_lines (company_id, property_id);
create index if not exists helixis_bill_lines_gl_idx
    on public.helixis_bill_lines (company_id, gl_account_id);

-- ── RLS: SELECT-only for company members (0020's pattern) ─────────────

alter table public.helixis_gl_accounts enable row level security;
drop policy if exists "members can read helixis gl accounts" on public.helixis_gl_accounts;
create policy "members can read helixis gl accounts"
    on public.helixis_gl_accounts for select
    using (
        exists (
            select 1
              from public.company_users cu
             where cu.company_id = helixis_gl_accounts.company_id
               and cu.user_id    = auth.uid()
        )
    );

alter table public.helixis_bills enable row level security;
drop policy if exists "members can read helixis bills" on public.helixis_bills;
create policy "members can read helixis bills"
    on public.helixis_bills for select
    using (
        exists (
            select 1
              from public.company_users cu
             where cu.company_id = helixis_bills.company_id
               and cu.user_id    = auth.uid()
        )
    );

alter table public.helixis_bill_lines enable row level security;
drop policy if exists "members can read helixis bill lines" on public.helixis_bill_lines;
create policy "members can read helixis bill lines"
    on public.helixis_bill_lines for select
    using (
        exists (
            select 1
              from public.company_users cu
             where cu.company_id = helixis_bill_lines.company_id
               and cu.user_id    = auth.uid()
        )
    );

commit;
