-- Rent Manager and Propertyware credentials, and the one-PMS rule across all
-- four systems. Phase 0a of design/connector-shared-layer.md.
--
-- WHY ONE FILE
-- The trigger function below names all four credentials tables, and plpgsql
-- resolves a table when the statement runs, not when the function is created.
-- A function naming a table that does not exist yet would fail EVERY Buildium
-- and Rentvine connect (fail-closed, but an outage). Creating the tables and
-- replacing the function in one transaction means neither can exist without
-- the other.
--
-- WHY THE TRIGGER MUST KNOW THE NEW TABLES
-- ADR 0010 Decision (a) stores every system's record ids in the SAME
-- `buildium_id` column under `UNIQUE (company_id, buildium_id)`. Two systems'
-- small-integer ids collide, and the mirror's upsert then overwrites one
-- record with the other with no error and no log (0100's header, probed on
-- PostgreSQL 16). The two-way trigger from 0103 would let a company hold Rent
-- Manager keys AND Buildium keys.
--
-- WHAT DOES NOT CHANGE, and tests/unit/test_one_pms_race.py pins it unchanged:
--   * The advisory lock is still taken FIRST, with the same key, so every
--     connect for one company, to any of the four tables, takes turns. 0103's
--     header has the measurements and the reasons it is not a row lock.
--   * In the two existing branches the first check is still the one 0103 has
--     (Buildium in the Rentvine branch, Rentvine in the Buildium branch).
--   * The error: same errcode, same message, with the other system's name.
--     The connect routes read the name from it (`db.company_pms.connected_to`).
-- New: one bare `if exists (` per other table in every branch, and an `else`
-- that refuses a binding to a table this function does not know, because a
-- trigger that matched no branch would let every insert through.
--
-- `create or replace trigger` (PostgreSQL 14+) rather than 0100's drop-and-
-- create: the file stays safe to re-run, and it carries no DROP, which the
-- Supabase MCP holds for a human to confirm (gotcha 202's shape).
--
-- THE NEW TABLES copy 0100's shape (company primary key with a cascading
-- foreign key, Fernet ciphertext only, `*_last4` for masked display, base_url,
-- last_tested_at), with two deliberate differences:
--   1. RLS on with NO policy, like 0106: only the backend's privileged pool
--      reads or writes them. 0003 and 0100 also grant company members read and
--      write through PostgREST, which nothing uses (the app goes through the
--      API, which checks the admin role) and which would let any member edit a
--      row the API only lets an admin write.
--   2. `capabilities jsonb`: a write the vendor refused with a 403 is recorded
--      there, so the tool says why next time instead of trying again (each
--      connector's design doc).
-- Columns are in design/rentmanager-connector.md and
-- design/propertyware-connector.md, section "Credentials".

begin;

-- ---------------------------------------------------------------------------
-- Rent Manager
-- ---------------------------------------------------------------------------
create table if not exists public.company_rentmanager_credentials (
    company_id                  uuid primary key
        references public.companies(id) on delete cascade,
    -- The subdomain in https://{corp_id}.api.rentmanager.com. Plaintext: it is
    -- in every request URL. Label-guarded before it becomes a host.
    corp_id                     text not null,
    -- One location per workspace (design R2).
    location_id                 integer not null,
    encrypted_username          text not null,
    username_last4              text not null,
    -- No last4 for a password.
    encrypted_password          text not null,
    -- Filled only if Rent Manager's partner program issues one (design R1).
    encrypted_partner_token     text,
    base_url                    text not null,
    capabilities                jsonb not null default '{}'::jsonb,
    last_tested_at              timestamptz,
    created_at                  timestamptz not null default now(),
    updated_at                  timestamptz not null default now()
);

create or replace function public.set_updated_at_company_rentmanager_credentials()
returns trigger
language plpgsql
as $$
begin
    new.updated_at := now();
    return new;
end;
$$;

create or replace trigger trg_set_updated_at_rentmanager_creds
    before update on public.company_rentmanager_credentials
    for each row execute function public.set_updated_at_company_rentmanager_credentials();

alter table public.company_rentmanager_credentials enable row level security;

-- ---------------------------------------------------------------------------
-- Propertyware
-- ---------------------------------------------------------------------------
create table if not exists public.company_propertyware_credentials (
    company_id                  uuid primary key
        references public.companies(id) on delete cascade,
    -- The organization id, sent as a header. Plaintext: it identifies the
    -- account and is not a secret, like Rentvine's account code.
    system_id                   text not null,
    encrypted_client_id         text not null,
    client_id_last4             text not null,
    -- No last4: shown once by Propertyware, like Rentvine's secret.
    encrypted_client_secret     text not null,
    base_url                    text not null
        default 'https://api.propertyware.com/pw/api/rest/v1',
    capabilities                jsonb not null default '{}'::jsonb,
    last_tested_at              timestamptz,
    created_at                  timestamptz not null default now(),
    updated_at                  timestamptz not null default now()
);

create or replace function public.set_updated_at_company_propertyware_credentials()
returns trigger
language plpgsql
as $$
begin
    new.updated_at := now();
    return new;
end;
$$;

create or replace trigger trg_set_updated_at_propertyware_creds
    before update on public.company_propertyware_credentials
    for each row execute function public.set_updated_at_company_propertyware_credentials();

alter table public.company_propertyware_credentials enable row level security;

-- ---------------------------------------------------------------------------
-- One PMS per company, four ways
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
    -- function is written once and bound to all four.
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

create or replace trigger trg_one_pms_rentmanager
    before insert or update of company_id on public.company_rentmanager_credentials
    for each row execute function public.assert_one_pms_per_company();

create or replace trigger trg_one_pms_propertyware
    before insert or update of company_id on public.company_propertyware_credentials
    for each row execute function public.assert_one_pms_per_company();

comment on table public.company_rentmanager_credentials is
    'Per-company Rent Manager credentials (design/rentmanager-connector.md). '
    'Ciphertext only; RLS on with no policy, so only the backend reads it.';
comment on table public.company_propertyware_credentials is
    'Per-company Propertyware credentials (design/propertyware-connector.md). '
    'Ciphertext only; RLS on with no policy, so only the backend reads it.';

commit;

-- ---------------------------------------------------------------------------
-- STANDING CHECK. migration_check.py sees the two tables and cannot see the
-- function body, so a re-run of 0100 or 0103 (which silently put back the
-- two-way function) reads green there. Read it here:
--
--     select count(*) as overloads,
--            bool_and(prosrc like '%pg_advisory_xact_lock%') as locks,
--            bool_and(prosrc like '%company_rentmanager_credentials%'
--                 and prosrc like '%company_propertyware_credentials%') as four_way
--       from pg_proc where proname = 'assert_one_pms_per_company';
--
-- Expect 1, true, true. Then `select count(*) from pg_trigger where tgfoid =
-- 'public.assert_one_pms_per_company'::regproc` expects 4. The behavioural
-- proof is a DO block that inserts a Rent Manager row for a company holding
-- Buildium keys and raises its own sentinel at the end (rolls back by
-- construction); the expected error is the trigger's. Recorded in
-- docs/migrations/applied.md.
--
-- ROLLBACK, if the new function misbehaves: re-run 0103. It is `create or
-- replace`, so it is safe to run again, and it puts back the two-way function
-- (the two new triggers then match no branch and let inserts through, which is
-- harmless while nothing writes those tables). Never re-run 0100 for this: it
-- has no advisory lock. The two empty tables can stay; the code reads them
-- through `company_pms`, which needs them to exist or falls back.
-- ---------------------------------------------------------------------------
