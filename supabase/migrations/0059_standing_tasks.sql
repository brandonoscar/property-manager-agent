-- 0059_standing_tasks.sql
--
-- Per-person standing agents v1 (multiplayer phase 2, upgrade 3). One row
-- per scheduled prompt a user's agent runs on their behalf — v1 cadence is
-- daily at ``hour_utc``; the worker's poll (webhooks/standing_agents.py,
-- dark behind HELIXIS_STANDING_AGENTS_ENABLED) claims due rows by stamping
-- ``last_run_at`` BEFORE the LLM run (crash = day skipped, never a double
-- bill), runs the prompt through the orchestrator scoped to (company, user),
-- and delivers the output to the owner's Inbox via the Helixis.TaskRequest
-- pipeline. No external sends ever originate here.
--
-- No API writes to this table exist yet (v1 rows are inserted manually /
-- by a future settings UI), so no RLS policies are added beyond the
-- service-role default — the worker reads via the service connection.
--
-- Apply on prod (after 0058):
--   psql "$HELIXIS_SUPABASE__DB_URL" -f supabase/migrations/0059_standing_tasks.sql

begin;

create table if not exists public.standing_tasks (
    id           bigint generated always as identity primary key,
    company_id   uuid        not null,
    user_id      uuid        not null,
    title        text        not null,
    prompt       text        not null,
    cadence      text        not null default 'daily'
                 check (cadence in ('daily')),          -- v1: daily only
    hour_utc     smallint    not null default 12
                 check (hour_utc between 0 and 23),
    enabled      boolean     not null default true,
    last_run_at  timestamptz,
    created_at   timestamptz not null default now()
);

create index if not exists standing_tasks_due_idx
    on public.standing_tasks (enabled, last_run_at)
 where enabled;

-- RLS on, no policies: deny-all for anon/authenticated (no API path reads
-- this table yet — v1 rows are inserted manually and consumed by the
-- worker over the service connection, which bypasses RLS). The future
-- settings-UI slice adds owner-scoped policies alongside its routes.
-- Same posture as 0058's history table.
alter table public.standing_tasks enable row level security;

commit;
