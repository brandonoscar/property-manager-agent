-- 0052_clamp_rls_helper_to_caller.sql
--
-- Closes the p_user_id hole in user_company_ids_with_role. The function is
-- SECURITY DEFINER and (correctly, per 0027) executable by `authenticated`
-- because the company_users / companies RLS policies call it during policy
-- evaluation. But its p_user_id parameter defaulted to auth.uid() while
-- still honouring an explicit argument — so any signed-in user could call
--   POST /rest/v1/rpc/user_company_ids_with_role {"p_user_id": "<victim>"}
-- and enumerate ANOTHER user's company memberships (surfaced by the
-- Supabase security advisor, 2026-07-06).
--
-- Fix: when the caller has a JWT identity (auth.uid() is not null), the
-- function now ALWAYS uses auth.uid() and ignores p_user_id. The parameter
-- is kept so the (text[], uuid) signature — referenced by the RLS policies
-- in 0004/0034 and the grants in 0027 — stays intact, and so service-role /
-- direct-connection callers (auth.uid() is null there) can still pass an
-- explicit user id. Grants are untouched by CREATE OR REPLACE.
--
-- Idempotent: CREATE OR REPLACE, same signature, same volatility.

begin;

create or replace function public.user_company_ids_with_role(
    p_roles text[] default null::text[],
    p_user_id uuid default auth.uid()
)
returns setof uuid
language sql
stable
security definer
set search_path to 'public'
as $$
    select company_id
      from public.company_users
     where user_id = coalesce(auth.uid(), p_user_id)
       and (p_roles is null or role = any (p_roles));
$$;

commit;
