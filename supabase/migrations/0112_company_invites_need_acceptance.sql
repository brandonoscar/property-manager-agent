-- An invite to someone who already has an account waits for them to accept it,
-- and company_users is written by the backend alone.
--
-- WHY (a read-only audit, confirmed on helixis-test, abcdefghijklmnopqrst,
-- 2026-11-11):
--   * Every account is the admin of its own sign-up company (handle_new_user,
--     0034), so anyone who signs up can invite.
--   * POST /api/v1/company/invite resolved an email to an existing auth user and
--     inserted their company_users row at once, with no step on their side.
--   * The backend and the web app make a user's companies active in the order
--     "the sign-up company last" (_resolve_company_ids, orderCompanyIds), so the
--     inviter's company became the victim's active one: their chat, their books
--     session, their uploads.
--   * The same insert worked straight through PostgREST: "admins can add company
--     members" checks only that company_id is one the caller administers, and
--     anon and authenticated hold INSERT, UPDATE and DELETE on company_users
--     (Supabase's defaults). Measured before this migration:
--       select policyname, cmd from pg_policies
--        where schemaname = 'public' and tablename = 'company_users';
--         -> one SELECT, one INSERT, one UPDATE, one DELETE policy
--       has_table_privilege('authenticated', 'public.company_users', 'INSERT')
--         -> true (and UPDATE, DELETE; anon the same)
--
-- WHAT THIS DOES
--   1. company_member_invites holds an invite to an existing account until that
--      person accepts or declines it in the app. It is the backend's alone (RLS on,
--      no policy, anon and authenticated revoked), like the credentials tables
--      (0110). Nothing that grants anything reads it: user_company_ids_with_role
--      and user_belongs_to_company read company_users, as does every backend
--      query, the web app's company list and the books' sign-in
--      (src/helixis/books_sign_in.py). A pending invite is therefore not a
--      membership anywhere, by construction rather than by a filter each reader
--      must remember.
--   2. company_users loses its three write policies and anon and authenticated
--      lose every write on it, so a membership is written only by the backend
--      (its pool connects as postgres, which owns the table and bypasses RLS) and
--      by handle_new_user (SECURITY DEFINER, the user's own company). The SELECT
--      policy stays: the web app reads the caller's companies with their own
--      token (frontend/src/services/auth.ts fetchCompanyIds).
--
-- SAFE BEFORE AND AFTER THE DEPLOY. The backend that reads this table refuses to
-- add an existing account while the table is missing (503, nothing written), so
-- it may ship first. Nothing in the web app, helixis-onboarding or the backend
-- writes company_users through PostgREST (searched 2026-11-11: the backend
-- writes through its own pool, the web app only reads). NOT VERIFIED: the
-- sidebar extension and the desktop app, which are not in this session; they go
-- through the API.
--
-- STANDING CHECK, after applying (each must return what it says):
--   select policyname, cmd from pg_policies
--    where schemaname = 'public' and tablename = 'company_users';
--     -> exactly one row: "members can read company memberships", SELECT
--   select policyname from pg_policies
--    where schemaname = 'public' and tablename = 'company_member_invites';
--     -> no rows
--   select r.rolname, t.tbl
--     from (values ('anon'), ('authenticated')) r(rolname),
--          (values ('public.company_users'), ('public.company_member_invites')) t(tbl)
--    where has_table_privilege(r.rolname, t.tbl, 'INSERT')
--       or has_table_privilege(r.rolname, t.tbl, 'UPDATE')
--       or has_table_privilege(r.rolname, t.tbl, 'DELETE')
--       or (t.tbl = 'public.company_member_invites'
--           and has_table_privilege(r.rolname, t.tbl, 'SELECT'));
--     -> no rows
--   select relrowsecurity from pg_class where oid = 'public.company_member_invites'::regclass;
--     -> true
-- The behavioural probe (a signed-in admin of one company inserts another user
-- into it through PostgREST and is refused; the web app still reads its own
-- companies) goes in docs/migrations/applied.md. Before applying, it is played on
-- a throwaway Postgres by tests/unit/test_company_users_capture_db.py, and
-- tests/unit/test_invites_need_acceptance.py walks every migration and fails if
-- company_users ends with a write policy or a write grant to either role.

begin;

create table if not exists public.company_member_invites (
    id          uuid primary key default gen_random_uuid(),
    company_id  uuid not null references public.companies (id) on delete cascade,
    user_id     uuid not null references auth.users (id) on delete cascade,
    role        text not null check (role in ('admin', 'manager', 'member')),
    -- Attribution that outlives the inviter (test_fk_delete_rules).
    invited_by  uuid references auth.users (id) on delete set null,
    created_at  timestamptz not null default now(),
    expires_at  timestamptz not null default (now() + interval '14 days'),
    unique (company_id, user_id)
);

create index if not exists company_member_invites_user_idx
    on public.company_member_invites (user_id);

alter table public.company_member_invites enable row level security;
revoke all on table public.company_member_invites from anon, authenticated;

drop policy if exists "admins can add company members"    on public.company_users;
drop policy if exists "admins can update company members" on public.company_users;
drop policy if exists "admins can remove company members" on public.company_users;

revoke insert, update, delete, truncate on table public.company_users from anon, authenticated;
-- anon has no session, so the SELECT policy already shows it nothing; take the
-- grant back too, so a policy added by mistake later still opens nothing to it.
revoke all on table public.company_users from anon;

commit;
