-- 0011_baseline_uuid_alignment.sql
--
-- AUDIT-TRAIL MIGRATION — does not change any data or schema by default.
--
-- Background. Production Supabase has run with uuid as the data type
-- for every ``id`` and ``company_id`` column since the project's first
-- signup (likely auto-created by Supabase Studio's table builder
-- before the migration files were ever applied via psql). Migrations
-- 0001 and 0007 originally declared ``text``. The drift was caught
-- 2026-05-15 while debugging audit Finding #3 (the 0006 trigger was
-- inserting ``'user-' || NEW.id::text`` into a uuid column and would
-- have 500'd every signup).
--
-- 0001 and 0007 have been retroactively corrected in the same PR that
-- introduces this file. The corrected files are byte-equivalent in
-- behaviour to what prod has been running.
--
-- This migration's job: make a defensible no-op pass over the schema
-- to confirm that any DB built from 0001..0011 ends up with the
-- uuid types we want. If somehow a fresh DB ends up with text
-- columns, this migration will fail loudly with a clear message
-- rather than silently letting drift accumulate.

do $$
declare
    expected_columns text[] := array[
        'companies.id',
        'company_users.id',
        'company_users.company_id',
        'company_buildium_credentials.company_id',
        'buildium_events.id',
        'buildium_events.company_id',
        'helixis_activity_log.id',
        'helixis_activity_log.company_id'
    ];
    col text;
    parts text[];
    t text;
    c text;
    actual_type text;
begin
    foreach col in array expected_columns loop
        parts := string_to_array(col, '.');
        t := parts[1];
        c := parts[2];
        select data_type into actual_type
          from information_schema.columns
         where table_schema = 'public'
           and table_name   = t
           and column_name  = c;
        if actual_type is null then
            -- Column doesn't exist yet (e.g. running this migration
            -- before its host table was created in another migration
            -- — should not happen in 0001..0011 order, but be lenient).
            continue;
        end if;
        if actual_type <> 'uuid' then
            raise exception
                'Schema drift detected: public.%.% is type %, expected uuid. '
                'See migration 0011 for context — prod has always been uuid. '
                'Do not "fix" this by reverting to text without understanding '
                'why uuid is correct (auth.users.id is uuid, the 0006 trigger '
                'depends on it).',
                t, c, actual_type;
        end if;
    end loop;
end$$;
