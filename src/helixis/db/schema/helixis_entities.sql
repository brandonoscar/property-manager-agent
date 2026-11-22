-- Helixis-side mirror tables for entities received via Buildium webhooks.
--
-- Apply once to the Supabase Postgres database:
--   psql "$HELIXIS_SUPABASE__DB_URL" -f src/helixis/db/schema/helixis_entities.sql
--
-- Each table stores the Buildium ID as the conflict key so re-delivered
-- webhooks upsert instead of duplicating. The `raw` column keeps the full
-- pydantic-serialised payload so downstream features (analytics, memory
-- extraction, frontend rendering) can read any field without a migration.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ── Leases ────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS helixis_leases (
    id                  uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    buildium_id         bigint      NOT NULL,
    account_id          bigint      NOT NULL,
    -- Helixis tenancy boundary. Convention: company_id = account_id::text.
    -- See supabase/migrations/0002_phase1_memory_rls.sql for the RLS policies.
    company_id          uuid        NOT NULL REFERENCES public.companies(id) ON DELETE CASCADE,
    property_id         bigint,
    unit_id             bigint,
    status              text,
    lease_from_date     timestamptz,
    lease_to_date       timestamptz,
    rent                numeric(12, 2),
    raw                 jsonb       NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE helixis_leases
    ADD CONSTRAINT helixis_leases_company_buildium_key UNIQUE (company_id, buildium_id);

CREATE INDEX IF NOT EXISTS helixis_leases_account_idx
    ON helixis_leases (account_id);

CREATE INDEX IF NOT EXISTS helixis_leases_company_id_idx
    ON helixis_leases (company_id);

CREATE INDEX IF NOT EXISTS helixis_leases_unit_idx
    ON helixis_leases (unit_id);

-- ── Work orders ───────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS helixis_work_orders (
    id                      uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    buildium_id             bigint      NOT NULL,
    account_id              bigint      NOT NULL,
    -- Helixis tenancy boundary. See migration 0002 for RLS policies.
    company_id              uuid        NOT NULL REFERENCES public.companies(id) ON DELETE CASCADE,
    property_id             bigint,
    unit_id                 bigint,
    title                   text,
    status                  text,
    priority                text,
    category_id             bigint,
    assigned_to_user_id     bigint,
    raw                     jsonb       NOT NULL,
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE helixis_work_orders
    ADD CONSTRAINT helixis_work_orders_company_buildium_key UNIQUE (company_id, buildium_id);

CREATE INDEX IF NOT EXISTS helixis_work_orders_account_idx
    ON helixis_work_orders (account_id);

CREATE INDEX IF NOT EXISTS helixis_work_orders_company_id_idx
    ON helixis_work_orders (company_id);

CREATE INDEX IF NOT EXISTS helixis_work_orders_status_idx
    ON helixis_work_orders (account_id, status)
    WHERE status IN ('New', 'InProgress');

-- ── Activity log ──────────────────────────────────────────────────────
-- Feeds the Activity page. Each row is one action by an agent or a
-- processed webhook event. `source` + `source_label` mirror the demo
-- shape on the frontend so we can later flip production mode to read
-- from this table without schema work on the UI side.

CREATE TABLE IF NOT EXISTS helixis_activity_log (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id      bigint      NOT NULL,
    -- Helixis tenancy boundary. See migration 0002 for RLS policies.
    company_id      uuid        NOT NULL REFERENCES public.companies(id) ON DELETE CASCADE,
    source          text        NOT NULL,
    source_label    text,
    action_type     text        NOT NULL,
    description     text        NOT NULL,
    property        text,
    tenant          text,
    entity_type     text,
    entity_id       bigint,
    user_id         text,
    raw             jsonb,
    created_at      timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT helixis_activity_log_source_check
        CHECK (source IN ('webhook', 'chat', 'scheduled', 'manual'))
);

CREATE INDEX IF NOT EXISTS helixis_activity_log_account_time_idx
    ON helixis_activity_log (account_id, created_at DESC);

CREATE INDEX IF NOT EXISTS helixis_activity_log_company_id_idx
    ON helixis_activity_log (company_id);

CREATE INDEX IF NOT EXISTS helixis_activity_log_entity_idx
    ON helixis_activity_log (entity_type, entity_id)
    WHERE entity_id IS NOT NULL;
