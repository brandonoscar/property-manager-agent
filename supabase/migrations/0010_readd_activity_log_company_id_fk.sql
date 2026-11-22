-- 0010_readd_activity_log_company_id_fk.sql
--
-- Re-add the foreign key on ``public.helixis_activity_log.company_id``
-- that was declared in 0009_activity_log_and_action_state.sql but
-- failed to land in production Supabase (Studio rolled the FK out of
-- the BEGIN/COMMIT block during a partial-error replay). RLS already
-- enforced tenancy on this table so the missing FK was hygiene, not
-- security — but a forward-only migration history should leave the
-- prod schema reachable from `migrate from empty`.
--
-- Apply order: after 0009. Idempotent — checks pg_constraint before
-- ALTER so a fresh database (where 0009 created the FK as declared)
-- is a no-op.
--
-- Manual-apply equivalent (already executed against production
-- 2026-05-15):
--   alter table public.helixis_activity_log
--       add constraint helixis_activity_log_company_id_fkey
--       foreign key (company_id)
--       references public.companies(id)
--       on delete cascade;
--
-- Pre-add orphan check (run before applying, must be 0):
--   select count(*) from public.helixis_activity_log al
--    where al.company_id is not null
--      and not exists (select 1 from public.companies c
--                       where c.id = al.company_id);

do $$
begin
    if not exists (
        select 1
          from pg_constraint
         where conname  = 'helixis_activity_log_company_id_fkey'
           and conrelid = 'public.helixis_activity_log'::regclass
    ) then
        alter table public.helixis_activity_log
            add constraint helixis_activity_log_company_id_fkey
            foreign key (company_id)
            references public.companies(id)
            on delete cascade;
    end if;
end$$;
