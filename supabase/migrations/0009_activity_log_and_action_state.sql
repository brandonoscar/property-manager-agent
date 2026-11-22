-- 0009_activity_log_and_action_state.sql
--
-- Phase A.2: create the human-action audit log + extend
-- ``buildium_events`` with execution-tracking columns.
--
-- Two things land here:
--   1. ``public.helixis_activity_log`` — written one row per
--      successful action execution (user confirms an action surface
--      button → Helixis calls Buildium → activity row recorded).
--      Older code in ``repositories/activity.py`` already referenced
--      the table but it was never created — current prod logs show
--      ``UndefinedTableError``. This migration makes that path real.
--   2. Four execution columns on ``public.buildium_events`` so the
--      Inbox can hide rows that have already been actioned and the
--      audit can answer "which action did the user pick, when, and
--      what was the Buildium response?".
--
-- Apply order on prod (current state — 0001-0007 applied, 0008
-- applied with the Phase A.1 enrichment columns):
--   psql "$HELIXIS_SUPABASE__DB_URL" -f supabase/migrations/0009_activity_log_and_action_state.sql
--
-- Idempotent: ``create table if not exists``, ``add column if not
-- exists``, ``drop policy if exists`` before each ``create policy``.

begin;

-- ── helixis_activity_log ───────────────────────────────────────────

create table if not exists public.helixis_activity_log (
    id              uuid        primary key default gen_random_uuid(),
    account_id      bigint,
    company_id      uuid        not null
        references public.companies(id) on delete cascade,
    user_id         uuid,
    source          text        not null,   -- 'webhook' | 'chat' | 'scheduled' | 'manual'
    source_label    text,
    action_type     text        not null,
    description     text        not null,
    property        text,
    tenant          text,
    entity_type     text,
    entity_id       bigint,
    raw             jsonb,
    created_at      timestamptz not null default now()
);

create index if not exists helixis_activity_log_company_id_idx
    on public.helixis_activity_log (company_id, created_at desc);

alter table public.helixis_activity_log enable row level security;

drop policy if exists "members read activity"  on public.helixis_activity_log;
drop policy if exists "members write activity" on public.helixis_activity_log;

create policy "members read activity"
    on public.helixis_activity_log for select
    using (
        exists (
            select 1
              from public.company_users cu
             where cu.company_id = helixis_activity_log.company_id
               and cu.user_id    = auth.uid()
        )
    );

create policy "members write activity"
    on public.helixis_activity_log for insert
    with check (
        exists (
            select 1
              from public.company_users cu
             where cu.company_id = helixis_activity_log.company_id
               and cu.user_id    = auth.uid()
        )
    );


-- ── buildium_events execution columns ──────────────────────────────
--
-- The existing 0007 RLS policies already gate reads + writes by
-- company_users membership, so the new columns inherit the same
-- access boundary — no extra policy work needed here.

alter table public.buildium_events
    add column if not exists executed_action_index integer,
    add column if not exists executed_at           timestamptz,
    add column if not exists executed_by_user_id   uuid,
    add column if not exists execution_result      jsonb;

commit;
