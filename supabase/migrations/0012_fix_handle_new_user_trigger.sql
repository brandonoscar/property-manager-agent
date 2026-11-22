-- 0012_fix_handle_new_user_trigger.sql
--
-- Brings the repo definition of ``public.handle_new_user`` into line
-- with the trigger Brandon hand-patched in production on 2026-05-16
-- via Supabase SQL Editor.
--
-- Migration 0006 originally declared:
--     declare new_company_id text;
--     new_company_id := 'user-' || NEW.id::text;
--     insert into public.companies (id, name)
--     values (new_company_id, ...);
-- That inserts a ``text`` value like ``'user-<uuid>'`` into the
-- ``uuid`` column ``public.companies.id`` (uuid since day one in prod,
-- formally aligned on GitHub by Path B2 / PR #68). The trigger would
-- have 500'd every signup with ``invalid input syntax for type uuid``.
-- Prod was hand-fixed; this file is the source-of-truth version going
-- forward.
--
-- Apply order: after 0006 (which still exists for historical
-- accuracy). ``create or replace`` is idempotent, so re-running this
-- migration against a database that already has the corrected
-- function is a no-op. Re-running against a DB that still has the
-- broken 0006 version installs the correction.
--
-- New shape:
--   * The user's own uuid (NEW.id) doubles as the personal-company id
--     — `companies.id` is just a uuid now and there is no concept of
--     "personal company" beyond "the company that shares a row with
--     auth.users.id". One company per signup, deterministic, collapses
--     cleanly on re-trigger.
--   * `display_name` derives from the email's local-part, falling
--     back to "Personal Workspace" when the email is unset.
--   * Both inserts use ON CONFLICT DO NOTHING so re-triggers (e.g.
--     manual auth.users INSERT during testing) don't error.
--
-- Trust boundary unchanged: SECURITY DEFINER + search_path pinned to
-- public, auth so the function can insert into the multi-tenancy
-- tables regardless of the role that inserted the auth.users row.

create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path to 'public', 'auth'
as $function$
declare
    display_name text;
begin
    display_name := coalesce(
        nullif(split_part(NEW.email, '@', 1), ''),
        'Personal Workspace'
    );

    insert into public.companies (id, name)
    values (NEW.id, display_name)
    on conflict (id) do nothing;

    insert into public.company_users (user_id, company_id, role)
    values (NEW.id, NEW.id, 'owner')
    on conflict (user_id, company_id) do nothing;

    return NEW;
end;
$function$;
