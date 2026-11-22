-- Per-company Buildium credentials.
--
-- All four secrets (client_id, client_secret, webhook_secret) are stored
-- ciphertext-only — encrypted with the project-wide HELIXIS_DATA_KEY (Fernet).
-- The plaintext for each field never leaves the request that submitted it,
-- and the webhook secret is shown back to the user exactly once at
-- generation time. ``last4`` is stored to power masked-display in the UI
-- without ever round-tripping plaintext through the API.
--
-- ``company_id`` is uuid to match the existing ``public.companies(id)``
-- uuid column set up in 0001_init.sql.
--
-- RLS: any member of the company can read and write their own row. We
-- intentionally do not gate on owner/admin role here because the existing
-- 0001 migration only ships ``role default 'member'``; tightening to
-- owner-only would lock everyone out today. Tighten this later when a
-- real role taxonomy exists.

begin;

create table if not exists public.company_buildium_credentials (
    company_id                  uuid primary key
        references public.companies(id) on delete cascade,
    encrypted_client_id         text not null,
    encrypted_client_secret     text not null,
    client_id_last4             text not null,
    encrypted_webhook_secret    text,
    webhook_secret_last4        text,
    account_id                  bigint not null,
    base_url                    text not null
        default 'https://apisandbox.buildium.com',
    last_tested_at              timestamptz,
    created_at                  timestamptz not null default now(),
    updated_at                  timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- updated_at trigger
-- ---------------------------------------------------------------------------
create or replace function public.set_updated_at_company_buildium_credentials()
returns trigger
language plpgsql
as $$
begin
    new.updated_at := now();
    return new;
end;
$$;

drop trigger if exists trg_set_updated_at_buildium_creds
    on public.company_buildium_credentials;

create trigger trg_set_updated_at_buildium_creds
    before update on public.company_buildium_credentials
    for each row execute function public.set_updated_at_company_buildium_credentials();

-- ---------------------------------------------------------------------------
-- Row Level Security
-- ---------------------------------------------------------------------------
alter table public.company_buildium_credentials enable row level security;

drop policy if exists "members can read buildium creds"
    on public.company_buildium_credentials;
create policy "members can read buildium creds"
    on public.company_buildium_credentials for select
    using (
        exists (
            select 1 from public.company_users cu
            where cu.company_id = public.company_buildium_credentials.company_id
              and cu.user_id = auth.uid()
        )
    );

drop policy if exists "members can write buildium creds"
    on public.company_buildium_credentials;
create policy "members can write buildium creds"
    on public.company_buildium_credentials for all
    using (
        exists (
            select 1 from public.company_users cu
            where cu.company_id = public.company_buildium_credentials.company_id
              and cu.user_id = auth.uid()
        )
    )
    with check (
        exists (
            select 1 from public.company_users cu
            where cu.company_id = public.company_buildium_credentials.company_id
              and cu.user_id = auth.uid()
        )
    );

commit;
