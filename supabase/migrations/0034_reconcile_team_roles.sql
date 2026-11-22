-- 0034_reconcile_team_roles.sql
--
-- ADR 0005 — collapse the drifted team-role vocabulary into the reconciled set
-- Admin / Manager / Member (no "Owner" — reserved for rental Owners).
--
-- Verified against helixis-test before writing (gotcha #8): all live rows are
-- role='owner', each company has a single member, the live CHECK allows
-- ('owner','admin','member'), the write-RLS gates on ('owner','admin'), and the
-- signup trigger inserts 'owner'.
--
-- ⚠ ORDERING: apply this ONLY AFTER the backend that normalizes legacy
-- 'owner'→'admin' is deployed. The currently-deployed backend's admin gate
-- accepts 'owner'/'manager'; reconciling data to 'admin' before that code ships
-- would 403 admins on rename/invite. The new code reads both, so once it's live
-- this is safe to apply (and required for the new 'manager' role to pass the
-- tightened CHECK).

begin;

-- 1. Reconcile existing rows to the new vocabulary.
update public.company_users set role = 'admin'  where role = 'owner';
update public.company_users set role = 'member' where role = 'employee';

-- 2. Tighten the CHECK to the reconciled set (drops 'owner'/'employee').
alter table public.company_users drop constraint if exists company_users_role_check;
alter table public.company_users
    add constraint company_users_role_check
    check (role in ('admin', 'manager', 'member'));

-- 3. Signup trigger now assigns 'admin' (the reconciled top role). Without this
--    the new CHECK would reject every new signup (the trigger wrote 'owner').
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path to 'public', 'auth'
as $function$
declare
    display_name text;
begin
    display_name := coalesce(nullif(split_part(NEW.email, '@', 1), ''), 'Personal Workspace');

    insert into public.companies (id, name)
    values (NEW.id, display_name)
    on conflict (id) do nothing;

    insert into public.company_users (user_id, company_id, role)
    values (NEW.id, NEW.id, 'admin')
    on conflict (user_id, company_id) do nothing;

    return NEW;
end;
$function$;

-- 4. Write-RLS gates on 'admin' only (was owner/admin). SELECT policy (any
--    member reads the roster) is unchanged. Defense-in-depth — the API's
--    require_role is the primary gate (ADR 0005).
drop policy if exists "owners can add company members"    on public.company_users;
drop policy if exists "owners can update company members" on public.company_users;
drop policy if exists "owners can remove company members" on public.company_users;

create policy "admins can add company members"
    on public.company_users for insert
    with check (company_id in (select user_company_ids_with_role(array['admin'::text])));

create policy "admins can update company members"
    on public.company_users for update
    using      (company_id in (select user_company_ids_with_role(array['admin'::text])))
    with check (company_id in (select user_company_ids_with_role(array['admin'::text])));

create policy "admins can remove company members"
    on public.company_users for delete
    using      (company_id in (select user_company_ids_with_role(array['admin'::text])));

commit;
