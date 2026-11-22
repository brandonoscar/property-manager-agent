-- 0028_crm_leads.sql
--
-- CRM / lead pipeline (foundation) — scaffolded behind HELIXIS_CRM__ENABLED.
--
-- Background. Helixis is adding a "CRM" section: a self-updating pipeline
-- of leads (renters inquiring on a listing, owners shopping for a PM).
-- Helixis moves each lead between stages when a *verifiable fact* occurs —
-- a reply is sent, a tour lands on Google Calendar, an application / lease
-- shows up in Buildium. This migration lands the storage; the ingestion +
-- move logic ships later. The feature flag defaults OFF, so nothing reads
-- or writes this table in production yet.
--
-- Shape notes:
-- * ``kind`` splits renters (Fair-Housing regulated — drafts only, never
--   scored/ranked) from owners (B2B sales — may be prioritized). The
--   distinction changes what Helixis is *allowed* to do with the lead, so
--   it's a first-class column, CHECK-guarded.
-- * ``stage`` is the pipeline position, CHECK-guarded against a closed set.
--   "scheduled" (not "toured") because a card moves there the moment a tour
--   hits the calendar. Future stages extend the CHECK, not the table.
-- * ``source`` records where the lead came in (zillow / apartments /
--   referral / web_form / email), CHECK-guarded but wide.
-- * Contact identity is ``email`` + ``phone`` — a lead is keyed on email
--   first, phone second (a masked listing-relay address lives in ``email``).
--   Both nullable: a phone-only lead has no email and vice-versa.
-- * ``unit_label`` is free text (the property/unit of interest, or an owner
--   doors summary) — properties live in Buildium, mirrored on demand only.
-- * ``raw`` jsonb carries the originating payload (the parsed inbound email,
--   the listing-site notification) for debugging + future re-parse.
--
-- Apply order: after 0027. Service-role writers (the API routes + the
-- future ingest worker) bypass RLS; the policies are the per-company
-- read/write gate for the authenticated PostgREST session (defense in
-- depth), identical in shape to 0014 / 0025.
--
-- NOTE: migrations have no runner (CLAUDE.md §4 / gotcha #8). This file
-- being committed is NOT proof it's live — apply it manually to
-- helixis-test before the flag is flipped on.

begin;

create table if not exists public.crm_leads (
    id           uuid        primary key default gen_random_uuid(),
    company_id   uuid        not null
        references public.companies(id) on delete cascade,
    -- 'renter' (regulated) | 'owner' (B2B). Drives allowed AI behavior.
    kind         text        not null default 'renter'
        check (kind in ('renter', 'owner')),
    -- Pipeline position. Closed set; extend the CHECK to add stages.
    stage        text        not null default 'new'
        check (stage in ('new', 'contacted', 'scheduled', 'applied', 'leased')),
    -- Where the lead arrived from.
    source       text        not null default 'email'
        check (source in ('zillow', 'apartments', 'referral', 'web_form', 'email', 'sms')),
    name         text        not null default '',
    -- Contact identity. Lead is matched on email first, phone second.
    email        text,
    phone        text,
    -- Property/unit of interest (renters) or a short doors summary (owners).
    unit_label   text,
    -- One-line context shown on the card.
    note         text,
    -- Originating payload (parsed email / listing notification) for
    -- debugging + future re-parse. Never load-bearing for the pipeline.
    raw          jsonb       not null default '{}'::jsonb,
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now()
);

-- Company-scoped listing + the two match keys the ingest path dedups on.
create index if not exists crm_leads_company_stage_idx
    on public.crm_leads (company_id, stage);

create index if not exists crm_leads_company_email_idx
    on public.crm_leads (company_id, email)
    where email is not null;

create index if not exists crm_leads_company_phone_idx
    on public.crm_leads (company_id, phone)
    where phone is not null;

alter table public.crm_leads enable row level security;

drop policy if exists "crm_leads_company_read"  on public.crm_leads;
drop policy if exists "crm_leads_company_write" on public.crm_leads;

create policy "crm_leads_company_read"
    on public.crm_leads for select
    using (
        exists (
            select 1 from public.company_users cu
             where cu.company_id = crm_leads.company_id
               and cu.user_id    = auth.uid()
        )
    );

create policy "crm_leads_company_write"
    on public.crm_leads for all
    using (
        exists (
            select 1 from public.company_users cu
             where cu.company_id = crm_leads.company_id
               and cu.user_id    = auth.uid()
        )
    )
    with check (
        exists (
            select 1 from public.company_users cu
             where cu.company_id = crm_leads.company_id
               and cu.user_id    = auth.uid()
        )
    );

commit;
