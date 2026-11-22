-- 0062: Buildium initial-backfill progress (initial-sync feature).
--
-- One row per company tracking the onboarding bulk pull (buildium/backfill.py
-- :: run_initial_sync), so the frontend can show a "Buildium data is still
-- syncing…" banner that clears when the backfill finishes, and the agent can
-- hedge on a known-stale mirror. Read via GET /buildium/sync-status.
--
-- Apply manually (no runner — §4 / gotcha 8):
--   psql "$HELIXIS_SUPABASE__DB_URL" -f supabase/migrations/0062_buildium_sync_state.sql

create table if not exists public.buildium_sync_state (
    company_id   uuid primary key,
    status       text not null default 'pending'
                 check (status in ('pending', 'syncing', 'done', 'failed')),
    started_at   timestamptz,
    finished_at  timestamptz,
    counts       jsonb not null default '{}'::jsonb,   -- {properties: 12, leases: 40, ...}
    last_error   text,
    updated_at   timestamptz not null default now()
);

alter table public.buildium_sync_state enable row level security;

-- Reads go through the service-key backend (GET /buildium/sync-status), which
-- bypasses RLS; this policy is defense-in-depth so a member can only ever see
-- their own company's row via PostgREST.
drop policy if exists buildium_sync_state_member_select on public.buildium_sync_state;
create policy buildium_sync_state_member_select on public.buildium_sync_state
    for select
    using (
        company_id in (
            select company_id from public.company_users where user_id = auth.uid()
        )
    );
