-- Every PMS credentials table is the backend's alone: no member policy on any
-- of them, and no table grant to anon or authenticated.
--
-- WHY (measured on helixis-test, abcdefghijklmnopqrst, 2026-10-05 and again
-- 2026-10-07):
--   * 0003 and 0100 gave company members a SELECT policy and a FOR ALL policy
--     on company_buildium_credentials and company_rentvine_credentials, and
--     `anon` and `authenticated` hold every table privilege (SELECT, INSERT,
--     UPDATE, DELETE) on all five credentials tables, Supabase's defaults. So
--     any signed-in member, not only an admin, could read and rewrite their
--     company's row through PostgREST with their own token, skipping the API's
--     admin check (`_require_admin` in api/routes/buildium.py and rentvine.py).
--   * Buildium's client used the row's base_url as its host, so a member could
--     point it at a server they control and be sent the company's client id
--     and secret on the next sync. buildium/credentials.py now refuses any
--     address but Buildium's own two, so a row edited some other way still
--     sends nothing.
--   * 0107 named this hole when it made the Rent Manager and Propertyware
--     tables backend-only, and 0109 did the same for DoorLoop. This finishes
--     it for the two tables that came first.
--
-- WHAT STILL WORKS (checked 2026-10-07):
--   * The backend. Its pool connects as `postgres`, which owns these tables
--     and has BYPASSRLS, so neither policies nor these grants apply to it.
--   * Nothing else reads or writes these tables. The web app, onboarding,
--     sidebar and desktop app all save and read keys through the API (all four
--     repos searched for the table names).
--   * The one-PMS trigger function (assert_one_pms_per_company, security
--     invoker) runs as whoever inserts, which is now only the backend.
--
-- REVOKE as well as dropping the policies. With RLS on and no policy, anon and
-- authenticated already get nothing; without the grants, a policy added by
-- mistake later still opens nothing.
--
-- STANDING CHECK, after applying (both must return no rows):
--   select tablename, policyname from pg_policies
--    where schemaname = 'public' and tablename like 'company\_%\_credentials';
--   select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace
--    where n.nspname = 'public' and c.relname like 'company\_%\_credentials'
--      and (has_table_privilege('authenticated', c.oid, 'SELECT,INSERT,UPDATE,DELETE')
--        or has_table_privilege('anon', c.oid, 'SELECT,INSERT,UPDATE,DELETE'));
-- The behavioural probe (a member's token reads and writes nothing, the
-- backend still reads every row) is recorded in docs/migrations/applied.md.
-- tests/unit/test_pms_credentials_backend_only.py walks every migration and
-- fails if any credentials table ends with a policy or a grant to either role.

begin;

drop policy if exists "members can read buildium creds"
    on public.company_buildium_credentials;
drop policy if exists "members can write buildium creds"
    on public.company_buildium_credentials;
drop policy if exists "members can read rentvine creds"
    on public.company_rentvine_credentials;
drop policy if exists "members can write rentvine creds"
    on public.company_rentvine_credentials;

revoke all on table public.company_buildium_credentials from anon, authenticated;
revoke all on table public.company_rentvine_credentials from anon, authenticated;
revoke all on table public.company_rentmanager_credentials from anon, authenticated;
revoke all on table public.company_propertyware_credentials from anon, authenticated;
revoke all on table public.company_doorloop_credentials from anon, authenticated;

commit;
