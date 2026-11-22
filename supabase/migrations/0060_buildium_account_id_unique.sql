-- 0060_buildium_account_id_unique.sql
--
-- Cross-tenant safety (2026-07 Buildium release audit). Inbound webhooks are
-- routed to a company by ``SELECT ... FROM company_buildium_credentials WHERE
-- account_id = $1 LIMIT 1`` (webhooks/router.py, buildium/credentials.py). If
-- two companies ever held the same Buildium ``account_id``, that LIMIT 1 would
-- silently deliver one tenant's webhooks to the OTHER — a cross-tenant data
-- leak with no error. A Buildium account_id belongs to exactly one company, so
-- a unique index makes the invariant the DB enforces rather than an assumption.
--
-- PARTIAL: account_id is nullable (0005 — a company can save credentials before
-- POST /buildium/test backfills the id), and multiple NULLs must be allowed, so
-- the uniqueness applies only to non-null ids.
--
-- Apply on prod (after 0059). If this fails with a uniqueness violation, TWO
-- companies already share an account_id — that is the bug this prevents; resolve
-- the duplicate rows BEFORE the index will build.
--   psql "$HELIXIS_SUPABASE__DB_URL" -f supabase/migrations/0060_buildium_account_id_unique.sql

begin;

create unique index if not exists company_buildium_credentials_account_id_uidx
    on public.company_buildium_credentials (account_id)
 where account_id is not null;

commit;
