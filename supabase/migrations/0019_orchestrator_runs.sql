-- Orchestrator-run telemetry (PR #141, Trust series #1).
--
-- One row per orchestrator invocation. The /dev/runs inspection page
-- reads from this table; future Trust-series work (narrative-truth
-- assertion, replay, weekly status doc) will consume the same shape.
--
-- Discipline:
--   * Telemetry is best-effort. The writer wraps inserts/updates in
--     try/except. A DB failure here MUST NOT take down the user's
--     streamed agent response.
--   * Multi-tenant from line one. ``company_id`` is NOT NULL, FK to
--     ``public.companies``. RLS scopes reads to the requester's
--     company_users membership — same pattern as the rest of the
--     schema (see migrations 0001, 0009).
--   * Operational, not analytical. PostHog stays the analytics layer.
--     This table holds per-run debug fidelity (tool args, outputs,
--     errors) that don't fit PostHog's event model.
--
-- Retention: no purge cron yet. Trust PR #145 will add one. Until
-- then, runs accumulate — manual delete-by-date is the workaround.

create table if not exists public.orchestrator_runs (
    id              uuid primary key default gen_random_uuid(),
    company_id      uuid not null references public.companies(id) on delete cascade,
    user_id         uuid,          -- nullable: system-initiated runs (cron, webhook) have no auth user
    session_id      text not null, -- matches the EventBus session id
    source          text not null, -- 'chat' | 'inbox_freeform' | 'system' (extend as new entry points land)
    user_query      text not null, -- the raw task text the orchestrator received
    started_at      timestamptz not null default now(),
    completed_at    timestamptz,
    latency_ms      integer,
    status          text not null default 'running',
        -- 'running' | 'succeeded' | 'failed' | 'cancelled'
    error_message   text,          -- short, user-safe; full trace lives in raw if needed
    error_type      text,          -- exception class name when status='failed'
    tool_calls      jsonb not null default '[]'::jsonb,
        -- [{name, args, output, started_at, duration_ms, error}]
    final_text      text,          -- assistant's final streamed text (truncated to 16k by writer)
    handoffs        jsonb not null default '[]'::jsonb,
        -- [{from_agent, to_agent, at}]
    raw             jsonb          -- full debug payload (truncated stream events, prompt fragments)
);

create index if not exists orchestrator_runs_company_started_idx
    on public.orchestrator_runs (company_id, started_at desc);
create index if not exists orchestrator_runs_session_idx
    on public.orchestrator_runs (session_id);
create index if not exists orchestrator_runs_status_idx
    on public.orchestrator_runs (status);

alter table public.orchestrator_runs enable row level security;

-- Read/write scoped to the authenticated user's company memberships.
-- Mirrors the pattern from migration 0001's "members can read companies"
-- policy — same join through public.company_users, same auth.uid() check.
drop policy if exists "members can read orchestrator runs" on public.orchestrator_runs;
create policy "members can read orchestrator runs"
    on public.orchestrator_runs for select
    using (
        exists (
            select 1
              from public.company_users cu
             where cu.company_id = orchestrator_runs.company_id
               and cu.user_id    = auth.uid()
        )
    );

-- The backend writes via the service role (bypasses RLS), so no
-- INSERT/UPDATE policy is required for production traffic. The SELECT
-- policy above is the one that matters when /dev/runs reads back
-- through a user-scoped JWT.
