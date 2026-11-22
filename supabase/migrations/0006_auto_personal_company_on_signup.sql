-- Phase 6 (slice): auto-create a personal company + membership on
-- every new ``auth.users`` insert.
--
-- Apply once to a database that already has 0001_init.sql and
-- 0002_phase1_memory_rls.sql:
--   psql "$HELIXIS_SUPABASE__DB_URL" -f supabase/migrations/0006_auto_personal_company_on_signup.sql
--
-- Why this exists
-- ----------------
-- Helixis uses a multi-tenant boundary: every API call goes through
-- ``require_company_access(user, company_id)``, which queries
-- ``company_users`` to verify membership. A user with zero memberships
-- can sign in but every subsequent API call 403s.
--
-- For self-service signup (the LoginForm's "Create account" tab), we
-- need new users to land with at least one company they own. This
-- migration adds an AFTER-INSERT trigger on ``auth.users`` that
-- creates a personal company keyed by ``user-<uuid>`` and grants the
-- new user the 'owner' role on it. Existing users are unaffected.
--
-- Idempotent: re-running the migration drops the prior trigger /
-- function and recreates them, no row changes.
--
-- Trust-boundary note: the trigger runs as ``SECURITY DEFINER`` so it
-- can write to ``public.companies`` and ``public.company_users``
-- regardless of the role inserting the user. RLS is bypassed for these
-- two specific writes — same shape as Supabase's own ``handle_new_user``
-- examples in their docs.

begin;

create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = public, auth
as $$
declare
    new_company_id text;
    display_name   text;
begin
    -- Personal company id is deterministic per-user so re-running the
    -- trigger (e.g. via a manual auth.users INSERT during testing)
    -- collapses on the same row.
    new_company_id := 'user-' || NEW.id::text;
    display_name := coalesce(
        nullif(split_part(NEW.email, '@', 1), ''),
        'Personal Workspace'
    );

    insert into public.companies (id, name)
    values (new_company_id, display_name)
    on conflict (id) do nothing;

    insert into public.company_users (user_id, company_id, role)
    values (NEW.id, new_company_id, 'owner')
    on conflict (user_id, company_id) do nothing;

    return NEW;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
    after insert on auth.users
    for each row execute function public.handle_new_user();

commit;
