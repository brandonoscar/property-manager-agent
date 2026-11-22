-- 0026_security_advisor_hardening.sql
--
-- P2 security-advisor hardening (WARN-level findings from the live Supabase
-- security advisor, surfaced by the risk #1 RLS audit). None of these are a
-- leak on their own; they are defense-in-depth.
--
--   A. function_search_path_mutable — SECURITY DEFINER / trigger functions with
--      no pinned search_path can be hijacked via a mutable search_path. Pin it.
--   B. SECURITY DEFINER helpers callable via PostgREST RPC by the anon role:
--      ``user_company_ids_with_role`` can return ANY user's company memberships
--      (it takes p_user_id), so anon must not be able to call it. The RLS
--      policies that use these helpers run as the ``authenticated`` role, so we
--      revoke from ``anon`` ONLY — authenticated keeps EXECUTE so policy
--      evaluation is unaffected. ``handle_new_user`` is a trigger function that
--      fires as its owner regardless of caller grants, so it can be revoked
--      from both anon and authenticated.
--
-- Defensive: each statement is guarded by ``to_regprocedure`` so the migration
-- applies cleanly on a fresh database (where a function may not yet exist) AND
-- on live (where ``user_belongs_to_company`` exists but was created out-of-band
-- and is not otherwise in this migration tree). Idempotent — safe to re-run.
--
-- NOTE: this migration is code-only until applied. It does NOT touch the leak
-- surface (RLS) — that landed in 0025. Apply manually per §4 after review.

begin;

do $$
begin
    -- ── A. Pin search_path on the flagged functions ──────────────────
    if to_regprocedure('public.set_updated_at_company_buildium_credentials()') is not null then
        alter function public.set_updated_at_company_buildium_credentials()
            set search_path = public, pg_temp;
    end if;

    if to_regprocedure('public.user_belongs_to_company(uuid)') is not null then
        alter function public.user_belongs_to_company(uuid)
            set search_path = public, pg_temp;
    end if;

    -- ── B. Revoke anon EXECUTE on the SECURITY DEFINER RPC helpers ────
    -- (authenticated keeps EXECUTE — the company_users RLS policies call these
    -- during policy evaluation as the authenticated role).
    if to_regprocedure('public.user_company_ids_with_role(text[], uuid)') is not null then
        revoke execute on function public.user_company_ids_with_role(text[], uuid) from anon;
    end if;

    if to_regprocedure('public.user_belongs_to_company(uuid)') is not null then
        revoke execute on function public.user_belongs_to_company(uuid) from anon;
    end if;

    -- handle_new_user is a SECURITY DEFINER trigger fn — fires as its owner via
    -- the auth.users trigger, never legitimately called via RPC. Revoke both.
    if to_regprocedure('public.handle_new_user()') is not null then
        revoke execute on function public.handle_new_user() from anon, authenticated;
    end if;
end
$$;

commit;
