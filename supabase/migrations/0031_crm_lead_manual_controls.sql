-- 0031_crm_lead_manual_controls.sql
--
-- CRM manual controls — the storage half of "run my pipeline by hand".
--
-- The CRM board is self-updating (a reply sent → Contacted, a tour on
-- Calendar → Scheduled, an application in Buildium → Applied/Leased). But a
-- PM still needs to override: move a lead by hand, drop a lead that went
-- nowhere, or type in a lead that came by a channel Helixis doesn't watch (a
-- walk-in, a phone call). This migration lands the two columns those
-- controls need. The move-by-hand control reuses the existing ``stage``
-- column — no schema change for it.
--
-- Two changes:
-- * ``status`` — 'active' (in the pipeline) | 'lost' (won't-pursue). A lost
--   lead drops off the board and the stage counts but is KEPT for records
--   (never hard-deleted by the user). Defaults 'active' so every existing
--   row stays on the board. Board-facing queries filter ``status='active'``;
--   ``get_lead`` still returns a lost lead so its record is reachable.
-- * ``source`` gains 'manual' — a lead typed in by the PM rather than
--   ingested from SMS/email/a listing site. The CHECK is rebuilt to add it.
--
-- Apply order: after 0030. Idempotent (``add column if not exists`` +
-- ``drop constraint if exists``), so a partial re-run is safe.
--
-- NOTE: migrations have no runner (CLAUDE.md §4 / gotcha #8). This file
-- being committed is NOT proof it's live — apply it manually to
-- helixis-test.

begin;

-- Pipeline membership. 'lost' leaves the board but survives for records.
alter table public.crm_leads
    add column if not exists status text not null default 'active'
        check (status in ('active', 'lost'));

-- Allow a hand-entered lead. Rebuild the (auto-named) source CHECK to add
-- 'manual'. The inline check from 0028 is named ``crm_leads_source_check``.
alter table public.crm_leads
    drop constraint if exists crm_leads_source_check;
alter table public.crm_leads
    add constraint crm_leads_source_check
        check (source in ('zillow', 'apartments', 'referral', 'web_form', 'email', 'sms', 'manual'));

-- The board/stage-list path filters (company, status, stage); index it.
create index if not exists crm_leads_company_status_stage_idx
    on public.crm_leads (company_id, status, stage);

commit;
