-- 0105_work_order_pms_updated_at.sql — when the PMS last changed a work order.
--
-- "Stalled" (first_scan.STALLED_AFTER_DAYS, read by the first scan and the
-- morning brief) was `updated_at < now() - 7 days`, and `updated_at` is the
-- MIRROR's write time. Every Rentvine pass rewrites every work order, so the
-- count could never pass 0: measured 2026-10-03, all 8 sandbox work orders
-- open, Rentvine's dateTimeModified as old as 2026-09-24, mirror updated_at
-- today on all 8. This column holds the PMS's own last change; the readers use
-- COALESCE(pms_updated_at, updated_at). Buildium rows leave it NULL (its
-- /workorders exposes no created or updated date), so Buildium is unchanged.
--
-- Additive and nullable: no backfill, no default, no rewrite of existing rows.
-- The next Rentvine pass fills it.
--
-- Standing check (run after applying; expect 1 row, timestamp with time zone):
--     select column_name, data_type, is_nullable
--       from information_schema.columns
--      where table_schema = 'public'
--        and table_name = 'helixis_work_orders'
--        and column_name = 'pms_updated_at';
--
-- NOTE: migrations have no runner (supabase/migrations/CLAUDE.md) — apply by
-- hand and VERIFY by effect.

begin;

alter table public.helixis_work_orders
    add column if not exists pms_updated_at timestamptz;

comment on column public.helixis_work_orders.pms_updated_at is
    'When the PMS last changed this work order (Rentvine dateTimeModified). NULL '
    'on Buildium rows. Stalled counts read COALESCE(pms_updated_at, updated_at).';

commit;
