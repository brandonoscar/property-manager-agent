-- Incremental-sync watermarks for the connectors built on `helixis.pms`
-- (Rent Manager, Propertyware). Phase 0b of design/connector-shared-layer.md.
--
-- One row per (company, resource): "everything modified before this time has
-- been read cleanly". `pms.watermarks.Watermark.advance` only ever moves it
-- forward, and only after a clean pass of that resource.
--
-- WHY A TABLE AND NOT A COLUMN on helixis_mirror_sync_state: that table is in
-- the Data Analyst's ALLOWED_TABLES and the analyst reads its columns live
-- (`describe_mirror_schema_impl`), so a new column there would change every
-- Buildium customer's analyst schema (ADR 0010 F-4). This table is in
-- `sql_guard.EXCLUDED_TABLES` with its reason, and in `buildium/purge.py`
-- PURGE_TABLES: it is sync state, and a disconnect purges it with the mirror.
--
-- RLS on with no policy, like 0106 and 0107: only the backend's privileged
-- pool reads or writes it.

begin;

create table if not exists public.helixis_pms_sync_watermarks (
    company_id  uuid not null
        references public.companies(id) on delete cascade,
    -- Named by the connector: "Tenants", "leases/charges".
    resource    text not null,
    watermark   timestamptz not null,
    updated_at  timestamptz not null default now(),
    primary key (company_id, resource)
);

alter table public.helixis_pms_sync_watermarks enable row level security;

comment on table public.helixis_pms_sync_watermarks is
    'Per-company incremental-sync watermarks for the helixis.pms connectors '
    '(pms/watermarks.py). Advanced only after a clean pass; purged on disconnect.';

commit;

-- ---------------------------------------------------------------------------
-- STANDING CHECK (migration_check.py sees the table; this proves its shape):
--
--     select relrowsecurity,
--            (select count(*) from pg_policies
--              where tablename = 'helixis_pms_sync_watermarks') as policies
--       from pg_class where oid = 'public.helixis_pms_sync_watermarks'::regclass;
--
-- Expect true, 0. The behavioural probe advances one watermark, advances it
-- again to an EARLIER time, reads it back (it must not move backwards), and
-- raises at the end so it rolls back. Recorded in docs/migrations/applied.md.
-- ---------------------------------------------------------------------------
