-- 0056_confirmation_resolution_detail.sql
--
-- Approval-surface honesty (approval-pipeline PR, Act 3): record WHO resolved
-- a confirmation and WHY it was rejected. Before this, resolve() wrote only
-- status + resolved_at — the creating user was captured but the resolving
-- user was not, and a decline carried no reason, so there was nothing to
-- learn from a month of rejections when tuning proposals.
--
-- Both columns nullable: reasons are OPTIONAL in the UI (never force friction
-- into a decline), and resolver attribution is best-effort on legacy paths.
-- The repository falls back to the legacy UPDATE when these columns are
-- missing, so deploy order vs. this migration is safe either way (gotcha 8).
--
-- Apply on prod (after 0055):
--   psql "$HELIXIS_SUPABASE__DB_URL" -f supabase/migrations/0056_confirmation_resolution_detail.sql

begin;

alter table public.helixis_pending_confirmations
    add column if not exists resolution_reason text,
    add column if not exists resolved_by text;

commit;
