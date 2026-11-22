-- 0007_buildium_events.sql
--
-- Promotes the standalone ``src/helixis/db/schema/buildium_events.sql``
-- into the numbered migration pipeline so it lands in the same apply
-- sequence as 0001-0006 and never gets forgotten on a fresh environment.
-- The schema file is left in place with a deprecation comment for
-- historical reference.
--
-- Apply order on prod (current state — 0001 through 0006 already applied,
-- buildium_events table missing):
--   psql "$HELIXIS_SUPABASE__DB_URL" -f supabase/migrations/0007_buildium_events.sql
--
-- Note on 0002: migration ``0002_phase1_memory_rls.sql`` references this
-- table directly (alter table public.buildium_events ...). On a clean
-- database 0002 fails because buildium_events does not exist yet —
-- 0002 has no IF EXISTS / DO $$ EXCEPTION guards. Production is
-- mid-pipeline (0002 applied against a database where the schema file
-- had been run separately, then later prod environments missed the
-- schema-file step), so this migration on its own fixes prod without
-- needing to re-run 0002. New clean environments will need the apply
-- order to be 0001 -> 0007 -> 0002 -> 0003 -> ...; see PR description.

begin;

create extension if not exists pgcrypto;

create table if not exists buildium_events (
    id               uuid        primary key default gen_random_uuid(),
    dedup_key        text        not null,
    event_name       text        not null,
    event_time       timestamptz not null,
    account_id       bigint      not null,
    -- Helixis tenancy boundary. uuid to match companies(id) post 0001 correction. See RLS in 0002.
    company_id       uuid        not null references public.companies(id) on delete cascade,
    entity_ids       jsonb       not null default '{}'::jsonb,
    payload          jsonb       not null,

    -- Queue state
    status           text        not null default 'pending',   -- pending | running | done | failed
    attempts         int         not null default 0,
    last_error       text,
    available_at     timestamptz not null default now(),
    claimed_at       timestamptz,
    claimed_by       text,

    received_at      timestamptz not null default now(),
    processed_at     timestamptz,

    constraint buildium_events_status_check
        check (status in ('pending', 'running', 'done', 'failed'))
);

create unique index if not exists buildium_events_dedup_key_uidx
    on buildium_events (dedup_key);

create index if not exists buildium_events_queue_idx
    on buildium_events (status, available_at)
    where status = 'pending';

create index if not exists buildium_events_event_time_idx
    on buildium_events (event_time desc);

create index if not exists buildium_events_event_name_idx
    on buildium_events (event_name);

create index if not exists buildium_events_company_id_idx
    on buildium_events (company_id);

commit;
