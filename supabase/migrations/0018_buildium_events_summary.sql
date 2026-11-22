-- 0018_buildium_events_summary.sql
--
-- PR #92 — add an agent-written narrative summary to each event so
-- the Inbox detail pane can render plain-English "what happened"
-- text above the field grid. The Inbox today shows ``Title +
-- Property + Reporter + Category + Priority + Due`` plus suggested
-- actions, but nowhere does it say "Tenant Jimmy reported a mirror
-- fell off the wall and broke. He sounded alarmed in the message."
-- That narrative is what this column adds.
--
-- The summarizer runs in the worker (NOT the receiver — Buildium's
-- 10s SLA is sacred), following the PR #89 planner pattern. Each
-- row's lifecycle:
--
--   pending → succeeded   (LLM call returned valid text)
--           → retrying    (transient error, attempts < 5)
--           → failed      (attempts >= 5 OR non-retryable error)
--
-- ``summary_attempts`` and ``summary_last_attempted_at`` gate the
-- claim query so a SIGKILL'd worker doesn't leave a row stuck and
-- so a deterministically-failing row eventually stops eating LLM
-- budget. ``summary_prompt_version`` is the in-code tag the
-- summarizer stamps with each successful run so we can detect rows
-- written against an old prompt and re-summarize them in a future PR
-- without rebuilding the whole pipeline.
--
-- Backfill: every existing row starts at ``summary_status='pending'``
-- via the column default and the worker sweeps them naturally. No
-- one-shot script. Cost on first sweep: ~$0.001 per row at Gemini
-- 2.5 Flash; the production row count is ~13 today (see PR #90
-- verification), so first sweep is ~$0.013 — negligible. If the
-- count grows past 500 before this lands, throttle the worker's
-- first pass via a feature flag (see brief).
--
-- Idempotent (``add column if not exists`` / ``do $$ if not exists``)
-- — re-applying on an already-patched environment is a no-op.
-- Forward-only per repo convention.

alter table public.buildium_events
    add column if not exists summary_text              text,
    add column if not exists summary_status            text        not null default 'pending',
    add column if not exists summary_error             text,
    add column if not exists summary_attempts          int         not null default 0,
    add column if not exists summary_last_attempted_at timestamptz,
    add column if not exists summary_prompt_version    text;

-- Enum constraint added separately so re-runs don't choke on
-- duplicate constraint names; same pg11+ idiom PR #89 uses for the
-- planner_status check.
do $$
begin
    if not exists (
        select 1 from pg_constraint
         where conname = 'buildium_events_summary_status_check'
    ) then
        alter table public.buildium_events
            add constraint buildium_events_summary_status_check
            check (summary_status in ('pending', 'succeeded', 'failed', 'retrying'));
    end if;
end$$;

-- Partial index for the worker's claim query. Only rows actively
-- waiting for the summarizer are indexed — mirrors PR #89's
-- planner-pending index strategy so the index stays small as
-- ``buildium_events`` grows.
create index if not exists buildium_events_summary_pending_idx
    on public.buildium_events (received_at)
    where summary_status in ('pending', 'retrying');
