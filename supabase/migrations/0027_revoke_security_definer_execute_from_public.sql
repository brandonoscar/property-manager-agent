-- 0027_revoke_security_definer_execute_from_public.sql
--
-- Corrects 0026. 0026 tried to stop the `anon` role from calling the SECURITY
-- DEFINER RPC helpers, but it did `REVOKE EXECUTE ... FROM anon` — which is a
-- NO-OP, because Postgres grants function EXECUTE to the built-in `PUBLIC`
-- role by default, and `anon` inherits it through `PUBLIC`. Verified against the
-- live advisor: after 0026 the anon `*_security_definer_function_executable`
-- warnings were unchanged.
--
-- The correct fix is to revoke from `PUBLIC` (and `anon` for good measure),
-- then GRANT EXECUTE back to `authenticated` for the two helpers the
-- `company_users` / `companies` RLS policies call during policy evaluation —
-- otherwise authenticated reads of those tables (login / bootstrap) break with
-- "permission denied for function". `handle_new_user` is a trigger function
-- (fires as its owner regardless of caller EXECUTE), so it needs no grant.
--
-- Each function is guarded by `to_regprocedure` (fresh DBs may not have created
-- it yet — `user_belongs_to_company` in particular was created out-of-band and
-- is not in this migration tree). The DO block then ASSERTS the end state
-- (authenticated keeps EXECUTE, anon/PUBLIC lost it) and RAISES — aborting the
-- whole transaction — if the grant math is wrong, so a bad apply can never
-- silently break login. Idempotent.

begin;

do $$
begin
    -- user_company_ids_with_role(text[], uuid) — used by the company_users RLS
    -- policy (0004); authenticated MUST keep EXECUTE.
    if to_regprocedure('public.user_company_ids_with_role(text[], uuid)') is not null then
        revoke execute on function public.user_company_ids_with_role(text[], uuid) from public, anon;
        grant  execute on function public.user_company_ids_with_role(text[], uuid) to authenticated;

        if not has_function_privilege(
            'authenticated', 'public.user_company_ids_with_role(text[], uuid)', 'EXECUTE'
        ) then
            raise exception 'ABORT: authenticated lost EXECUTE on user_company_ids_with_role';
        end if;
        if has_function_privilege(
            'anon', 'public.user_company_ids_with_role(text[], uuid)', 'EXECUTE'
        ) then
            raise exception 'ABORT: anon still has EXECUTE on user_company_ids_with_role';
        end if;
    end if;

    -- user_belongs_to_company(uuid) — keep authenticated (may back an RLS policy).
    if to_regprocedure('public.user_belongs_to_company(uuid)') is not null then
        revoke execute on function public.user_belongs_to_company(uuid) from public, anon;
        grant  execute on function public.user_belongs_to_company(uuid) to authenticated;

        if not has_function_privilege(
            'authenticated', 'public.user_belongs_to_company(uuid)', 'EXECUTE'
        ) then
            raise exception 'ABORT: authenticated lost EXECUTE on user_belongs_to_company';
        end if;
        if has_function_privilege('anon', 'public.user_belongs_to_company(uuid)', 'EXECUTE') then
            raise exception 'ABORT: anon still has EXECUTE on user_belongs_to_company';
        end if;
    end if;

    -- handle_new_user() — trigger fn; fires as owner, no caller needs EXECUTE.
    if to_regprocedure('public.handle_new_user()') is not null then
        revoke execute on function public.handle_new_user() from public, anon, authenticated;

        if has_function_privilege('anon', 'public.handle_new_user()', 'EXECUTE') then
            raise exception 'ABORT: anon still has EXECUTE on handle_new_user';
        end if;
    end if;
end
$$;

commit;
