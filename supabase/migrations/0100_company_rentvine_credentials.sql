-- Per-company Rentvine credentials. ADR 0010 Decision (e), revision 3.1.
--
-- Mirrors 0003_company_buildium_credentials.sql: secrets are ciphertext-only,
-- encrypted with the project-wide HELIXIS_DATA_KEY (Fernet), and `last4` powers
-- masked display without round-tripping plaintext through the API.
--
-- THREE DELIBERATE DIFFERENCES FROM 0003, each one a decision rather than an
-- oversight:
--
--   1. `account_code` is stored PLAINTEXT. It is the subdomain in
--      https://{account_code}.rentvine.com/api/manager — it is in every URL we
--      emit and every log line. Encrypting a value that travels in clear on the
--      wire buys nothing and costs a decrypt on every request.
--
--   2. `encrypted_api_secret` has NO `last4`. Rentvine shows the secret once at
--      creation; the customer pastes it and never sees it again. A last4 exists
--      to help somebody recognise a key they can still look up. There is no
--      such surface for this one, so storing four characters of a secret is
--      exposure with no use.
--
--   3. NO `encrypted_webhook_secret` COLUMN — deliberately, and this is the
--      one most likely to be "helpfully" added later. Third-party prose says a
--      per-account signing key exists; the vendor's own OpenAPI spec contains
--      ZERO occurrences of `webhook`, `signature` or `hmac` (measured, twice,
--      by two separate review rounds), so the ALGORITHM that consumes such a
--      key is unknown. A column holding a value nothing can validate is
--      CLAUDE.md gotcha 79's shape: a slot that reads as configured. Phase 5
--      adds it when a captured delivery says what it is.
--
-- ⚠ THE EXCLUSIVITY TRIGGER BELOW IS NOT BELT-AND-BRACES. See its own comment.

begin;

create table if not exists public.company_rentvine_credentials (
    company_id                  uuid primary key
        references public.companies(id) on delete cascade,
    -- The subdomain. Not secret (see note 1 above).
    account_code                text not null,
    encrypted_api_key           text not null,
    api_key_last4               text not null,
    -- No last4 (see note 2 above).
    encrypted_api_secret        text not null,
    base_url                    text not null
        default 'https://{account}.rentvine.com/api/manager',
    last_tested_at              timestamptz,
    created_at                  timestamptz not null default now(),
    updated_at                  timestamptz not null default now()
);

comment on column public.company_rentvine_credentials.account_code is
    'Rentvine subdomain. Stored plaintext: it appears in every request URL, so '
    'encrypting it buys nothing and costs a decrypt per call.';

-- ---------------------------------------------------------------------------
-- updated_at trigger  (mirrors 0003)
-- ---------------------------------------------------------------------------
create or replace function public.set_updated_at_company_rentvine_credentials()
returns trigger
language plpgsql
as $$
begin
    new.updated_at := now();
    return new;
end;
$$;

drop trigger if exists trg_set_updated_at_rentvine_creds
    on public.company_rentvine_credentials;

create trigger trg_set_updated_at_rentvine_creds
    before update on public.company_rentvine_credentials
    for each row execute function public.set_updated_at_company_rentvine_credentials();

-- ---------------------------------------------------------------------------
-- ONE PMS PER COMPANY — ADR 0010 `[needs founder decision]` a1, and finding
-- R3-7 from the Phase 2.5 round-3 review.
-- ---------------------------------------------------------------------------
-- ⚠ THIS IS NOT A TIDINESS RULE. Decision (a) stores BOTH systems' record ids
-- in the SAME column (`buildium_id`), keyed `UNIQUE (company_id, buildium_id)`
-- from migration 0049. That is safe ONLY while a company has one PMS. If a
-- company ever held both, a Rentvine record and a Buildium record that happen
-- to share an id would collide on that key and the mirror's upsert
-- (`ON CONFLICT ... DO UPDATE`) would SILENTLY OVERWRITE one with the other —
-- no error, no log, one row where there were two. Probed on PostgreSQL 16
-- during the round-3 review, with the alternative schema as a control.
--
-- Both systems issue small integers from independent sequences (the largest id
-- anywhere in Rentvine's spec is 901,735), so an overlap is LIKELY, not exotic.
--
-- ⚠ It cannot be an index. Exclusivity spans TWO tables and no index does. The
-- ADR said "a partial unique index" for one revision; that named a mechanism
-- which cannot exist, which is how an invariant ends up enforced by nothing
-- (gotcha 62's `comms_triage` shape).
--
-- FAILS CLOSED: raises, so the INSERT is refused and the connect route surfaces
-- the error. It does not "prefer" one PMS or quietly delete the other row.
create or replace function public.assert_one_pms_per_company()
returns trigger
language plpgsql
as $$
declare
    other_pms text;
begin
    -- Which table is the OTHER one depends on which table fired us, so the
    -- function is written once and bound to both.
    if tg_table_name = 'company_rentvine_credentials' then
        if exists (
            select 1 from public.company_buildium_credentials
            where company_id = new.company_id
        ) then
            other_pms := 'Buildium';
        end if;
    elsif tg_table_name = 'company_buildium_credentials' then
        if exists (
            select 1 from public.company_rentvine_credentials
            where company_id = new.company_id
        ) then
            other_pms := 'Rentvine';
        end if;
    end if;

    if other_pms is not null then
        raise exception using
            errcode = 'unique_violation',
            message = format(
                'company %s is already connected to %s; one PMS per company '
                '(ADR 0010 a1). Disconnect the existing PMS first.',
                new.company_id, other_pms
            );
    end if;
    return new;
end;
$$;

drop trigger if exists trg_one_pms_rentvine on public.company_rentvine_credentials;
create trigger trg_one_pms_rentvine
    before insert or update of company_id on public.company_rentvine_credentials
    for each row execute function public.assert_one_pms_per_company();

-- ⚠ BOTH DIRECTIONS. Guarding only the new table would leave the invariant
-- open to whichever PMS is connected second, which is the ordering nobody
-- tests.
drop trigger if exists trg_one_pms_buildium on public.company_buildium_credentials;
create trigger trg_one_pms_buildium
    before insert or update of company_id on public.company_buildium_credentials
    for each row execute function public.assert_one_pms_per_company();

-- ---------------------------------------------------------------------------
-- Row Level Security  (mirrors 0003 exactly)
-- ---------------------------------------------------------------------------
alter table public.company_rentvine_credentials enable row level security;

drop policy if exists "members can read rentvine creds"
    on public.company_rentvine_credentials;
create policy "members can read rentvine creds"
    on public.company_rentvine_credentials for select
    using (
        exists (
            select 1 from public.company_users cu
            where cu.company_id = public.company_rentvine_credentials.company_id
              and cu.user_id = auth.uid()
        )
    );

drop policy if exists "members can write rentvine creds"
    on public.company_rentvine_credentials;
create policy "members can write rentvine creds"
    on public.company_rentvine_credentials for all
    using (
        exists (
            select 1 from public.company_users cu
            where cu.company_id = public.company_rentvine_credentials.company_id
              and cu.user_id = auth.uid()
        )
    )
    with check (
        exists (
            select 1 from public.company_users cu
            where cu.company_id = public.company_rentvine_credentials.company_id
              and cu.user_id = auth.uid()
        )
    );

commit;
