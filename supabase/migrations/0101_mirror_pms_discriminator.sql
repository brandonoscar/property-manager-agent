-- The `pms` discriminator on the 21 entity mirror tables. ADR 0010 Decision (a),
-- revision 3.1.
--
-- ONE COLUMN PER TABLE. That is the whole migration, and it took three ADR
-- revisions and three review rounds to get down to it:
--
--   * revision 1 said two columns (`pms` + `external_id text`);
--   * revision 2 said four statements per table, after review round 1 found
--     `buildium_id` is NOT NULL;
--   * round 2 failed that because the template named `buildium_id` on 8 tables
--     that have no column by that name, 3 of them inside composite PRIMARY KEYs
--     where DROP NOT NULL is refused outright;
--   * revision 3 removed `external_id` entirely, because the mirror holds 65
--     `bigint` relationship columns across 20 of the 21 tables and a text
--     identity column BREAKS EVERY ONE OF THOSE JOINS — probed: the Rentvine
--     join runs and returns 0 rows, with no error. A fabricated absence.
--
-- So: the PMS's record id goes in the identity column each table ALREADY has,
-- and `pms` says which system issued it. Nothing is dropped, no constraint is
-- weakened, no PRIMARY KEY is touched, and every existing upsert and join works
-- unchanged for both systems.
--
-- ⚠ FOR A BUILDIUM CUSTOMER THIS CHANGES NOTHING, and that claim is narrow and
-- earned rather than general. No predicate reads `pms`. The one thing that DOES
-- read the column LIST is the Data Analyst (`agents/analysis_tools.py` queries
-- information_schema live, by design), and the hidden-column set in
-- `analysis/sql_guard.py` is what keeps its view byte-identical. That set is not
-- optional — see ADR 0010 Decision (a).
--
-- ---------------------------------------------------------------------------
-- ⚠ `scripts/migration_check.py` CANNOT VERIFY THIS FILE, AND SAYS SO.
--
-- The table names are supplied at RUNTIME by `format()` inside the loop below,
-- so a static parser cannot know them — it prints this file under "NOT
-- VERIFIABLE HERE" rather than guessing. (Before that was handled it did
-- guess, and reported a missing column `public.pms` forever: a canary that
-- could not pass, with zero real coverage of the 21 tables behind it.)
--
-- THE STANDING CHECK IS THIS, and 21 is the whole pass criterion:
--
--     select count(*) as tables_with_pms
--       from information_schema.columns
--      where table_schema = 'public' and column_name = 'pms';
--     -- expect 21
--
-- ⚠ COUNT THE COLUMN, NEVER TRUST THE APPLY. This is a `DO` block over an
-- array: a typo'd table name inside it raises and rolls the whole transaction
-- back (so 21 or 0, never a partial) — but a name that is merely ABSENT from
-- the array applies cleanly and leaves that table behind, which is the failure
-- a count catches and a successful `psql` run does not (CLAUDE.md's rule:
-- verify by EFFECT, not by the tool reporting success).
--
-- ⚠ And the count alone cannot see the DEFAULT, which is the load-bearing
-- half — every pre-0101 row is Buildium's and `default 'buildium'` is what
-- encodes that. A column added with no default reads as 21 here and leaves
-- every existing row NULL:
--
--     select count(*) as tables_defaulting_to_buildium
--       from information_schema.columns
--      where table_schema = 'public' and column_name = 'pms'
--        and column_default like '%buildium%';
--     -- expect 21
-- ---------------------------------------------------------------------------

begin;

do $$
declare
    t text;
begin
    foreach t in array array[
        -- family 1 — own `buildium_id` (13)
        'helixis_properties', 'helixis_units', 'helixis_tenants',
        'helixis_leases', 'helixis_lease_transactions', 'helixis_work_orders',
        'helixis_applicants', 'helixis_vendors', 'helixis_owners',
        'helixis_tasks', 'helixis_gl_accounts', 'helixis_bills',
        'helixis_bank_accounts',
        -- family 2 — keyed by PARENT ids (6)
        'helixis_work_order_status_history', 'helixis_lease_tenants',
        'helixis_turnovers', 'helixis_renewal_offers', 'helixis_property_intel',
        'buildium_staff',
        -- family 3 — no Buildium id column at all (2)
        'helixis_bill_lines', 'helixis_lease_balances'
    ]
    loop
        execute format(
            'alter table public.%I add column if not exists pms text not null '
            'default ''buildium''', t);
        execute format(
            'comment on column public.%I.pms is %L', t,
            'Which property-management system issued this row''s record id. '
            'Every row written before migration 0101 is Buildium''s, which is '
            'what the default encodes. ADR 0010 Decision (a).');
    end loop;
end $$;

-- ---------------------------------------------------------------------------
-- The identity columns now hold whichever system's id, so SAY SO where a reader
-- will actually look. ADR 0010 records this naming debt explicitly: the column
-- is called `buildium_id` and a Rentvine row's value is a Rentvine id. A rename
-- would touch 13 column names plus the 65 relationship columns' referents plus
-- every mirror query, so it is deliberately deferred — but an un-commented
-- misnomer is how the next reader gets it wrong (gotcha 48).
-- ---------------------------------------------------------------------------
do $$
declare
    r record;
begin
    for r in
        select table_name, column_name
        from information_schema.columns
        where table_schema = 'public'
          and column_name in (
              'buildium_id', 'lease_buildium_id', 'tenant_buildium_id',
              'work_order_buildium_id', 'buildium_property_id', 'buildium_user_id')
          and table_name in (
              'helixis_properties', 'helixis_units', 'helixis_tenants',
              'helixis_leases', 'helixis_lease_transactions', 'helixis_work_orders',
              'helixis_applicants', 'helixis_vendors', 'helixis_owners',
              'helixis_tasks', 'helixis_gl_accounts', 'helixis_bills',
              'helixis_bank_accounts', 'helixis_work_order_status_history',
              'helixis_lease_tenants', 'helixis_turnovers',
              'helixis_renewal_offers', 'helixis_property_intel', 'buildium_staff')
    loop
        execute format(
            'comment on column public.%I.%I is %L', r.table_name, r.column_name,
            'The record id in whichever PMS this row''s `pms` column names — '
            'Buildium for every row written before migration 0101. The column '
            'name is historical; see ADR 0010 Decision (a) on why a rename is '
            'deferred rather than forgotten.');
    end loop;
end $$;

commit;
