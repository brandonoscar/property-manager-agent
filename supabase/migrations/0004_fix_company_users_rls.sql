-- 0004_fix_company_users_rls.sql
--
-- Tighten RLS on ``public.company_users`` so:
--   1. Members can SEE every row for any company they belong to (so the
--      UI can render a teammates list and require_company_access keeps
--      working).
--   2. Only owners and admins can INSERT new memberships into a company
--      they belong to.
--   3. Only owners and admins can UPDATE existing memberships in their
--      company (e.g., promoting a member → admin).
--   4. Only owners and admins can DELETE memberships from their company
--      (no last-owner safeguard yet — that's v2).
--
-- Pre-state (from 0001_init.sql):
--   - SELECT policy: ``user_id = auth.uid()`` (own rows only)
--   - No INSERT / UPDATE / DELETE policies → blocked from any
--     user-context connection. Only paths that wrote to the table:
--       * the auto-personal-company trigger from 0006 (SECURITY DEFINER,
--         owned by ``postgres`` which has BYPASSRLS — so unaffected by
--         RLS regardless of policies) and
--       * scripts/create_user.py (uses the service-role key, also
--         bypasses RLS).
--   These two paths continue to work without change.
--
-- Recursion note: a policy on ``company_users`` that queries
-- ``company_users`` would re-trigger RLS on the inner read, leading to
-- infinite recursion in some Postgres versions and confusingly slow
-- plans in others. We push the membership lookup into a SECURITY
-- DEFINER helper (``public.user_company_ids_with_role``) which runs
-- with BYPASSRLS as ``postgres``, breaking the loop cleanly.
--
-- Idempotent: drops existing policies / helper before recreating.

begin;

-- ---------------------------------------------------------------------------
-- Helper: return the company_ids a user is in, optionally filtered by role.
-- ---------------------------------------------------------------------------
drop function if exists public.user_company_ids_with_role(text[], uuid);

create or replace function public.user_company_ids_with_role(
    p_roles text[] default null,
    p_user_id uuid default auth.uid()
)
-- uuid, matching companies.id since 0001 (0052 states the same signature).
returns setof uuid
language sql
security definer
set search_path = public
stable
as $$
    select company_id
      from public.company_users
     where user_id = p_user_id
       and (p_roles is null or role = any (p_roles));
$$;

-- ---------------------------------------------------------------------------
-- SELECT: members of the same company can see each other's memberships.
-- ---------------------------------------------------------------------------
drop policy if exists "users can read own memberships"        on public.company_users;
drop policy if exists "members can read company memberships"  on public.company_users;
create policy "members can read company memberships"
    on public.company_users for select
    using (
        company_id in (select public.user_company_ids_with_role(null))
    );

-- ---------------------------------------------------------------------------
-- INSERT: only owners / admins of the target company can add memberships.
-- ---------------------------------------------------------------------------
drop policy if exists "owners can add company members" on public.company_users;
create policy "owners can add company members"
    on public.company_users for insert
    with check (
        company_id in (
            select public.user_company_ids_with_role(array['owner', 'admin'])
        )
    );

-- ---------------------------------------------------------------------------
-- UPDATE: same gate, both USING and WITH CHECK so a privileged member
-- can't move a row to a company they don't admin.
-- ---------------------------------------------------------------------------
drop policy if exists "owners can update company members" on public.company_users;
create policy "owners can update company members"
    on public.company_users for update
    using (
        company_id in (
            select public.user_company_ids_with_role(array['owner', 'admin'])
        )
    )
    with check (
        company_id in (
            select public.user_company_ids_with_role(array['owner', 'admin'])
        )
    );

-- ---------------------------------------------------------------------------
-- DELETE: same gate.
-- ---------------------------------------------------------------------------
drop policy if exists "owners can remove company members" on public.company_users;
create policy "owners can remove company members"
    on public.company_users for delete
    using (
        company_id in (
            select public.user_company_ids_with_role(array['owner', 'admin'])
        )
    );

commit;
