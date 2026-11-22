-- 0005_buildium_account_id_nullable.sql
--
-- Buildium's API auth uses only ``x-buildium-client-id`` and
-- ``x-buildium-client-secret`` headers — there is no account_id
-- parameter in any auth flow per their developer docs. We were
-- collecting it from the user out of an abundance of caution; it's
-- unused at the call site, so the field is being removed from the
-- Save form.
--
-- 0003 declared the column NOT NULL. Drop the constraint so existing
-- code paths and rows keep working without forcing a migration to
-- delete the column entirely. Existing rows keep their value;
-- new rows write NULL.

begin;

alter table public.company_buildium_credentials
    alter column account_id drop not null;

commit;
