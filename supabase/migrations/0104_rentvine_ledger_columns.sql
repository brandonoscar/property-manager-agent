-- 0104_rentvine_ledger_columns.sql — ADR 0010 Decision (b): money keys to a LEDGER.
--
-- A Rentvine money row is charged to a ledger: 1 Manager, 2 Portfolio,
-- 3 Property, 4 Unit (the spec's `ledger_types` enum). Only 3 and 4 belong to
-- one property; 1 and 2 are mirrored in full and attributed to NO property, and
-- property rollups say how much they leave out. These two columns are what lets
-- a reader tell the difference. Buildium rows leave both NULL (Buildium money
-- is property-grained), so nothing on the Buildium path changes.
--
-- Additive and nullable: no backfill, no default, no rewrite of existing rows.
--
-- Standing check (run after applying; expect 4 rows, 2 per table):
--     select table_name, column_name, data_type
--       from information_schema.columns
--      where table_schema = 'public'
--        and table_name in ('helixis_bill_lines', 'helixis_lease_transactions')
--        and column_name in ('ledger_id', 'ledger_type');
-- Behavioural probe (rolls back by construction): see the DO block in
-- docs/migrations/applied.md for 0104; a ledger_type of 5 must be refused.
--
-- NOTE: migrations have no runner (supabase/migrations/CLAUDE.md) — apply by
-- hand and VERIFY by effect.

begin;

alter table public.helixis_bill_lines
    add column if not exists ledger_id   bigint,
    add column if not exists ledger_type smallint;

alter table public.helixis_lease_transactions
    add column if not exists ledger_id   bigint,
    add column if not exists ledger_type smallint;

do $$
begin
    if not exists (
        select 1 from pg_constraint where conname = 'helixis_bill_lines_ledger_type_chk'
    ) then
        alter table public.helixis_bill_lines
            add constraint helixis_bill_lines_ledger_type_chk
            check (ledger_type is null or ledger_type between 1 and 4);
    end if;
    if not exists (
        select 1 from pg_constraint where conname = 'helixis_lease_transactions_ledger_type_chk'
    ) then
        alter table public.helixis_lease_transactions
            add constraint helixis_lease_transactions_ledger_type_chk
            check (ledger_type is null or ledger_type between 1 and 4);
    end if;
end $$;

comment on column public.helixis_bill_lines.ledger_type is
    'Rentvine ledger type (1 Manager, 2 Portfolio, 3 Property, 4 Unit). Only 3 and 4 '
    'attribute to property_id; NULL on Buildium rows. ADR 0010 Decision (b).';
comment on column public.helixis_lease_transactions.ledger_type is
    'Rentvine ledger type (1 Manager, 2 Portfolio, 3 Property, 4 Unit); NULL on '
    'Buildium rows. ADR 0010 Decision (b).';

commit;
