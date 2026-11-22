-- 0017_buildium_events_planner_status.sql
--
-- PR #89 — move the LLM planner out of the synchronous Buildium
-- webhook receiver into the worker. The receiver previously called
-- ``planner.plan(...)`` inline at ``webhooks/router.py:279`` before
-- returning 200 to Buildium; that pinned the planner timeout at 8s
-- because Buildium suspends subscriptions after 20 consecutive 10s+
-- responses. Gemini 2.5 Flash p99 for structured JSON output
-- exceeds 8s, so two production rows
-- (0fd4e984-a80b-4628-8505-df97e23be213,
--  fcb56b89-3c7c-43cf-802f-a054d89a654e) consistently silently
-- fell back to the deterministic floor without any error surfacing
-- in the inbox.
--
-- This migration adds the bookkeeping the worker uses to claim
-- planner-pending rows, plus the columns the inbox reads to render
-- honest state on success / timeout / failure.
--
-- Columns:
--   planner_status            — pending | in_progress | succeeded
--                                | timed_out | failed. NOT NULL with
--                                default 'pending' so the existing
--                                ``enqueue`` INSERT (which doesn't
--                                touch this column) starts new rows
--                                in the right state.
--   planner_error             — last error text (truncated by the
--                                worker). NULL on succeeded / pending.
--   planner_attempts          — worker increments on each claim,
--                                claim refuses past 5.
--   planner_last_attempted_at — set by the worker on claim; the claim
--                                query uses a 30s cooldown to keep
--                                stuck ``in_progress`` rows from
--                                blocking siblings forever.
--
-- The PR #82-style ``status`` / ``attempts`` / ``claimed_at`` /
-- ``claimed_by`` / ``available_at`` columns added in 0007 are NOT
-- touched; they belong to the legacy per-event dispatch worker
-- (``handlers/__init__.py`` bridge → ``dispatch_event``), which
-- continues to claim and run independently.
--
-- Backfill rule:
--   - Rows with a non-null ``action_surface`` carrying more than one
--     action are assumed to have come from a successful planner run
--     and are marked ``succeeded``.
--   - Rows with ``action_surface IS NULL`` OR an action_surface whose
--     ``actions`` array has 0 or 1 entries (the deterministic-floor
--     shape) are marked ``pending`` so the worker replans them on
--     its next pass. The two production stuck rows fall in this
--     bucket.
--
-- Idempotent — re-applying on an already-patched environment is a
-- no-op (``IF NOT EXISTS``, default values match the seed values).
-- Forward-only per repo convention (no down script).

alter table public.buildium_events
    add column if not exists planner_status            text        not null default 'pending',
    add column if not exists planner_error             text,
    add column if not exists planner_attempts          int         not null default 0,
    add column if not exists planner_last_attempted_at timestamptz;

-- Enum constraint added separately so re-runs don't choke on
-- duplicate constraint names; pg11+ supports IF NOT EXISTS on
-- ``add constraint`` via a DO block.
do $$
begin
    if not exists (
        select 1 from pg_constraint
         where conname = 'buildium_events_planner_status_check'
    ) then
        alter table public.buildium_events
            add constraint buildium_events_planner_status_check
            check (planner_status in (
                'pending', 'in_progress', 'succeeded', 'timed_out', 'failed'
            ));
    end if;
end$$;

-- Partial index for the worker's claim query. Only rows actively
-- waiting for the planner are indexed, so the index stays small even
-- as ``buildium_events`` grows.
create index if not exists buildium_events_planner_pending_idx
    on public.buildium_events (received_at)
    where planner_status = 'pending';

-- Backfill: classify the historical rows so the worker picks up the
-- stuck floor-only rows on its first pass. ``jsonb_typeof`` guards
-- against rows where ``action_surface->'actions'`` isn't a JSONB
-- array (malformed payloads from earlier phases).
update public.buildium_events
   set planner_status = case
       when action_surface is not null
            and jsonb_typeof(action_surface -> 'actions') = 'array'
            and jsonb_array_length(action_surface -> 'actions') > 1
       then 'succeeded'
       else 'pending'
   end
 where planner_status = 'pending';  -- only touch defaults, not anything
                                    -- a future re-run might have set
