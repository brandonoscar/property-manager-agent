-- 0043: bank accounts join the money mirror (ontology v2, slice 3).
--
-- "What's in our operating account?" becomes a local SQL question. Balance
-- freshness rides BankAccount.Transaction.* webhooks (the handler refetches
-- the account) + backfill/daily resync. BankAccount.* (table) events stay
-- acknowledged-only — their envelope id key is undocumented; the backfill
-- prune keeps the set exact instead.
--
-- PII policy (same class as tenant TaxId): AccountNumber,
-- AccountNumberUnmasked, RoutingNumber, CheckPrintingInfo and
-- ElectronicPayments are stripped from ``raw`` BEFORE persist — no bank
-- credentials at rest, ever.

create table if not exists public.helixis_bank_accounts (
    id                uuid        primary key default gen_random_uuid(),
    buildium_id       bigint      unique not null,
    account_id        bigint      not null,
    company_id        uuid        not null
        references public.companies(id) on delete cascade,
    name              text,
    description       text,
    bank_account_type text,
    country           text,
    is_active         boolean,
    balance           numeric(14, 2),
    gl_account_id     bigint,
    raw               jsonb       not null,
    created_at        timestamptz not null default now(),
    updated_at        timestamptz not null default now()
);

create index if not exists helixis_bank_accounts_company_idx
    on public.helixis_bank_accounts (company_id);

alter table public.helixis_bank_accounts enable row level security;

-- Members read their company's rows; writes are service-role only (the
-- backfill + webhook worker), same policy shape as 0038/0041/0042.
drop policy if exists helixis_bank_accounts_member_select
    on public.helixis_bank_accounts;
create policy helixis_bank_accounts_member_select
    on public.helixis_bank_accounts for select
    using (
        exists (
            select 1
              from public.company_users cu
             where cu.company_id = helixis_bank_accounts.company_id
               and cu.user_id = auth.uid()
        )
    );
