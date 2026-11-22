-- DEPRECATED: this schema is now applied via
-- supabase/migrations/0007_buildium_events.sql.
-- Kept here for historical reference; do not edit. New changes to the
-- buildium_events shape go in a follow-up numbered migration.
--
-- Buildium webhook event log + Postgres-backed queue.
--
-- Buildium can redeliver; the unique index on dedup_key prevents duplicate
-- processing without requiring handler-side idempotency. The `status` column
-- doubles as queue state — workers claim rows via SELECT ... FOR UPDATE
-- SKIP LOCKED.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS buildium_events (
    id               uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    dedup_key        text        NOT NULL,
    event_name       text        NOT NULL,
    event_time       timestamptz NOT NULL,
    account_id       bigint      NOT NULL,
    -- Helixis tenancy boundary. See migration 0002 for RLS policies.
    company_id       text        NOT NULL REFERENCES public.companies(id) ON DELETE CASCADE,
    entity_ids       jsonb       NOT NULL DEFAULT '{}'::jsonb,
    payload          jsonb       NOT NULL,

    -- Queue state
    status           text        NOT NULL DEFAULT 'pending',   -- pending | running | done | failed
    attempts         int         NOT NULL DEFAULT 0,
    last_error       text,
    available_at     timestamptz NOT NULL DEFAULT now(),
    claimed_at       timestamptz,
    claimed_by       text,

    received_at      timestamptz NOT NULL DEFAULT now(),
    processed_at     timestamptz,

    CONSTRAINT buildium_events_status_check
        CHECK (status IN ('pending', 'running', 'done', 'failed'))
);

CREATE UNIQUE INDEX IF NOT EXISTS buildium_events_dedup_key_uidx
    ON buildium_events (dedup_key);

CREATE INDEX IF NOT EXISTS buildium_events_queue_idx
    ON buildium_events (status, available_at)
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS buildium_events_event_time_idx
    ON buildium_events (event_time DESC);

CREATE INDEX IF NOT EXISTS buildium_events_event_name_idx
    ON buildium_events (event_name);

CREATE INDEX IF NOT EXISTS buildium_events_company_id_idx
    ON buildium_events (company_id);
