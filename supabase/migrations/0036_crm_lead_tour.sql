-- 0036_crm_lead_tour.sql
--
-- "Book a tour" (CRM roadmap §Buildable-now #1): when a PM books a property
-- tour, Helixis creates a Google Calendar event (via the live Composio
-- connection) and moves the lead to Scheduled. Two columns hold the result so
-- the Scheduled stage can render as a week agenda:
--   * tour_at            — the scheduled tour datetime (drives the agenda grouping)
--   * calendar_event_id  — the Google Calendar event id (for a future update/cancel)
--
-- Apply order: after 0032 (the last crm_leads migration). Idempotent
-- (``add column if not exists``) so re-running is a no-op.
--
-- NOTE: migrations have no runner (CLAUDE.md §4 / gotcha #8). This file being
-- committed is NOT proof it's live — apply it manually to helixis-test.

begin;

alter table public.crm_leads
    add column if not exists tour_at timestamptz;

alter table public.crm_leads
    add column if not exists calendar_event_id text;

-- The Scheduled agenda reads leads by tour_at within the active board; a small
-- partial index keeps that ordering cheap once tours start landing.
create index if not exists crm_leads_tour_at_idx
    on public.crm_leads (company_id, tour_at)
    where tour_at is not null;

commit;
