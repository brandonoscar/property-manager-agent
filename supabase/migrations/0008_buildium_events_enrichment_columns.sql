-- 0008_buildium_events_enrichment_columns.sql
--
-- Phase A.1: persist the synchronous-path enrichment + planner output
-- alongside the raw inbound webhook on ``buildium_events``.
--
-- ``enrichment``     — JSON serialisation of ``EnrichedEvent`` (full
--                       task body + unit + active lease + 90d history
--                       at unit and property). Source of truth for the
--                       frontend's webhook detail card.
-- ``action_surface`` — JSON serialisation of ``ActionSurface`` (1-2
--                       sentence summary + 1-4 proposed actions with
--                       parameter values). The planner's output;
--                       consumed verbatim by the Phase A.2 frontend.
--
-- Both columns are nullable so events that don't have a registered
-- enrichment handler (everything except ``Task.Created`` today) still
-- INSERT cleanly through ``enqueue``. The router's
-- :func:`store_enrichment` is also idempotent against a database
-- where this migration hasn't been applied yet — it catches
-- ``UndefinedColumnError`` and falls through — so prod stays healthy
-- between merging the code and running this script.
--
-- Apply order on prod (current state — 0001 through 0007 already
-- applied with the production-safe uuid scripts):
--   psql "$HELIXIS_SUPABASE__DB_URL" -f supabase/migrations/0008_buildium_events_enrichment_columns.sql
--
-- Idempotent: ``add column if not exists``. Re-running is a no-op.

begin;

alter table public.buildium_events
    add column if not exists enrichment     jsonb,
    add column if not exists action_surface jsonb;

-- No indexes today. The frontend reads enrichment + action_surface
-- via the same row id as the inbound row; the existing primary key
-- + ``buildium_events_event_time_idx`` cover the access path.

commit;
