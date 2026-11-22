-- DoorLoop credentials, and the one-PMS rule across all five systems.
-- design/doorloop-connector.md ("After Brandon's go", step 2).
--
-- 0107's shape, for 0107's reasons, which apply here unchanged:
--   * ONE FILE: the trigger function names every credentials table, and
--     plpgsql resolves a table when the statement runs, not when the function
--     is created. A function naming a table that does not exist yet would fail
--     EVERY connect to every system (fail-closed, but an outage). The table is
--     created before the function is replaced, in one transaction.
--   * WHY THE TRIGGER MUST KNOW THE NEW TABLE: every system's record ids share
--     the `buildium_id` column under `UNIQUE (company_id, buildium_id)` (ADR
--     0010 Decision (a)). A company holding DoorLoop keys and any other
--     system's keys would have colliding records overwrite each other with no
--     error and no log.
--
-- WHAT DOES NOT CHANGE, and tests/unit/test_one_pms_race.py and
-- tests/unit/test_one_pms_four_way.py pin it:
--   * The advisory lock is still taken FIRST, with the same key.
--   * Each existing branch keeps its checks in their order; DoorLoop's check is
--     added last. The Rentvine and Buildium branches still start with each
--     other's table (0103).
--   * The error: same errcode, same message, with the other system's name.
--     The connect routes read the name from it (`db.company_pms.connected_to`).
--   * The `else` still refuses a binding to a table the function does not know.
-- New: one bare `if exists (` on company_doorloop_credentials in every branch,
-- and a DoorLoop branch that checks the four others.
--
-- THE NEW TABLE copies 0107's: company primary key with a cascading foreign
-- key, Fernet ciphertext only, a last4 for masked display, base_url (never read:
-- the client's host is a constant), capabilities, last_tested_at, and RLS on
-- with NO policy, so only the backend's privileged pool reads or writes it.
-- DoorLoop authenticates with one bearer API key (design, "Can a vendor get
-- access?"), so there is one secret column. Its last4 is kept, unlike
-- Rentvine's secret (0100, note 2): DoorLoop lists a company's keys under
-- "Zapier & API Keys", so four characters help someone tell which one is saved.
--
-- `create or replace trigger` (PostgreSQL 14+), as 0107: safe to re-run, no DROP.

begin;

-- ---------------------------------------------------------------------------
-- DoorLoop
-- ---------------------------------------------------------------------------
create table if not exists public.company_doorloop_credentials (
    company_id                  uuid primary key
        references public.companies(id) on delete cascade,
    -- The API key a DoorLoop user makes under Settings → "Zapier & API Keys",
    -- sent as a bearer token on every request. Fernet ciphertext only.
    encrypted_api_key           text not null,
    api_key_last4               text not null,
    base_url                    text not null
        default 'https://app.doorloop.com/api',
    capabilities                jsonb not null default '{}'::jsonb,
    last_tested_at              timestamptz,
    created_at                  timestamptz not null default now(),
    updated_at                  timestamptz not null default now()
);

create or replace function public.set_updated_at_company_doorloop_credentials()
returns trigger
language plpgsql
as $$
begin
    new.updated_at := now();
    return new;
end;
$$;

create or replace trigger trg_set_updated_at_doorloop_creds
    before update on public.company_doorloop_credentials
    for each row execute function public.set_updated_at_company_doorloop_credentials();

alter table public.company_doorloop_credentials enable row level security;

-- ---------------------------------------------------------------------------
-- One PMS per company, five ways
-- ---------------------------------------------------------------------------
create or replace function public.assert_one_pms_per_company()
returns trigger
language plpgsql
as $$
declare
    other_pms text;
begin
    -- ⚠ FIRST. Without it two concurrent connects each read a snapshot that
    -- cannot see the other's uncommitted row, and both commit (0103).
    perform pg_advisory_xact_lock(hashtext('one_pms:' || new.company_id::text));

    -- Which tables are the OTHER ones depends on which table fired us, so the
    -- function is written once and bound to all five.
    if tg_table_name = 'company_rentvine_credentials' then
        if exists (
            select 1 from public.company_buildium_credentials
            where company_id = new.company_id
        ) then
            other_pms := 'Buildium';
        end if;
        if exists (
            select 1 from public.company_rentmanager_credentials
            where company_id = new.company_id
        ) then
            other_pms := 'Rent Manager';
        end if;
        if exists (
            select 1 from public.company_propertyware_credentials
            where company_id = new.company_id
        ) then
            other_pms := 'Propertyware';
        end if;
        if exists (
            select 1 from public.company_doorloop_credentials
            where company_id = new.company_id
        ) then
            other_pms := 'DoorLoop';
        end if;
    elsif tg_table_name = 'company_buildium_credentials' then
        if exists (
            select 1 from public.company_rentvine_credentials
            where company_id = new.company_id
        ) then
            other_pms := 'Rentvine';
        end if;
        if exists (
            select 1 from public.company_rentmanager_credentials
            where company_id = new.company_id
        ) then
            other_pms := 'Rent Manager';
        end if;
        if exists (
            select 1 from public.company_propertyware_credentials
            where company_id = new.company_id
        ) then
            other_pms := 'Propertyware';
        end if;
        if exists (
            select 1 from public.company_doorloop_credentials
            where company_id = new.company_id
        ) then
            other_pms := 'DoorLoop';
        end if;
    elsif tg_table_name = 'company_rentmanager_credentials' then
        if exists (
            select 1 from public.company_buildium_credentials
            where company_id = new.company_id
        ) then
            other_pms := 'Buildium';
        end if;
        if exists (
            select 1 from public.company_rentvine_credentials
            where company_id = new.company_id
        ) then
            other_pms := 'Rentvine';
        end if;
        if exists (
            select 1 from public.company_propertyware_credentials
            where company_id = new.company_id
        ) then
            other_pms := 'Propertyware';
        end if;
        if exists (
            select 1 from public.company_doorloop_credentials
            where company_id = new.company_id
        ) then
            other_pms := 'DoorLoop';
        end if;
    elsif tg_table_name = 'company_propertyware_credentials' then
        if exists (
            select 1 from public.company_buildium_credentials
            where company_id = new.company_id
        ) then
            other_pms := 'Buildium';
        end if;
        if exists (
            select 1 from public.company_rentvine_credentials
            where company_id = new.company_id
        ) then
            other_pms := 'Rentvine';
        end if;
        if exists (
            select 1 from public.company_rentmanager_credentials
            where company_id = new.company_id
        ) then
            other_pms := 'Rent Manager';
        end if;
        if exists (
            select 1 from public.company_doorloop_credentials
            where company_id = new.company_id
        ) then
            other_pms := 'DoorLoop';
        end if;
    elsif tg_table_name = 'company_doorloop_credentials' then
        if exists (
            select 1 from public.company_buildium_credentials
            where company_id = new.company_id
        ) then
            other_pms := 'Buildium';
        end if;
        if exists (
            select 1 from public.company_rentvine_credentials
            where company_id = new.company_id
        ) then
            other_pms := 'Rentvine';
        end if;
        if exists (
            select 1 from public.company_rentmanager_credentials
            where company_id = new.company_id
        ) then
            other_pms := 'Rent Manager';
        end if;
        if exists (
            select 1 from public.company_propertyware_credentials
            where company_id = new.company_id
        ) then
            other_pms := 'Propertyware';
        end if;
    else
        -- Bound to a table this function does not know: refuse rather than
        -- let the insert through unchecked.
        raise exception using
            errcode = 'raise_exception',
            message = format(
                'assert_one_pms_per_company is bound to %s, which is not a PMS '
                'credentials table it knows', tg_table_name
            );
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

create or replace trigger trg_one_pms_doorloop
    before insert or update of company_id on public.company_doorloop_credentials
    for each row execute function public.assert_one_pms_per_company();

comment on table public.company_doorloop_credentials is
    'Per-company DoorLoop credentials (design/doorloop-connector.md). '
    'Ciphertext only; RLS on with no policy, so only the backend reads it.';

commit;

-- ---------------------------------------------------------------------------
-- STANDING CHECK, as 0107's: migration_check.py sees the table and cannot see
-- the function body. Read it here:
--
--     select count(*) as overloads,
--            bool_and(prosrc like '%pg_advisory_xact_lock%') as locks,
--            bool_and(prosrc like '%company_doorloop_credentials%') as five_way
--       from pg_proc where proname = 'assert_one_pms_per_company';
--
-- Expect 1, true, true. Then `select count(*) from pg_trigger where tgfoid =
-- 'public.assert_one_pms_per_company'::regproc` expects 5. The behavioural
-- proof is 0107's DO block with a DoorLoop row for a company holding Buildium
-- keys; it raises its own sentinel at the end, so it rolls back by
-- construction, and the expected error is the trigger's. Record it in
-- docs/migrations/applied.md.
--
-- ROLLBACK, if the new function misbehaves: re-run 0107's function and trigger
-- statements (`create or replace`, safe to re-run). That puts back the
-- four-way function; DoorLoop's trigger then matches no branch and its `else`
-- refuses every DoorLoop insert, which is harmless while the connector is
-- switched off. The empty table can stay; `db.company_pms` reads it, and falls
-- back to the four-way query while it does not exist.
-- ---------------------------------------------------------------------------
