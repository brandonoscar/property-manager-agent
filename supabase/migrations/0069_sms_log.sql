-- SMS message log — one row per outbound send and inbound reply.
--
-- APPLIED to helixis-test 2026-08-18 via the Supabase MCP (verified by effect).
-- gotcha 8) — apply manually via psql, then verify by effect.
--
-- Outbound rows are written best-effort by ``comms/sms.py::send_sms`` after
-- Twilio accepts the message (status 'queued'); delivery receipts
-- (``POST /api/v1/twilio/status``) advance status → sent | delivered |
-- undelivered | failed (+ error_code). Inbound rows are written by
-- ``POST /api/v1/twilio/inbound`` (status 'received', body kept).
--
-- ``company_id`` is NULLABLE: an inbound message may come from a number
-- with no consent row in any company (e.g. a wrong-number text) — we still
-- log it, unattributed.
--
-- ``body`` is INBOUND ONLY. Outbound rows must never store a body: outbound
-- content is reconstructible from ``template_id`` (the four registered
-- campaign shapes are templates-only), and storing rendered bodies would
-- duplicate the recipient's PII at rest for zero information gain.

create table if not exists public.helixis_sms_log (
    id          uuid        primary key default gen_random_uuid(),
    company_id  uuid
        references public.companies(id) on delete cascade,
    direction   text        not null
        check (direction in ('outbound', 'inbound')),
    phone       text        not null,  -- E.164 counterparty number
    template_id text,                  -- outbound only (one of comms/sms.py TEMPLATES)
    twilio_sid  text        unique,    -- Twilio MessageSid (delivery receipts key on it)
    status      text        not null default 'queued'
        check (status in ('queued', 'sent', 'delivered', 'undelivered', 'failed', 'received')),
    error_code  text,                  -- Twilio ErrorCode on undelivered/failed
    body        text,                  -- INBOUND only — see header comment
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);

comment on table public.helixis_sms_log is
    'Per-message SMS log (outbound sends + inbound replies). Outbound rows '
    'never store a body — content is reconstructible from template_id; '
    'storing rendered bodies would duplicate PII.';
comment on column public.helixis_sms_log.company_id is
    'Nullable: an inbound message may not match any consent row.';
comment on column public.helixis_sms_log.body is
    'Inbound message text only. Outbound rows must leave this null.';

create index if not exists helixis_sms_log_company_created_idx
    on public.helixis_sms_log (company_id, created_at desc);
create index if not exists helixis_sms_log_twilio_sid_idx
    on public.helixis_sms_log (twilio_sid);

-- ── RLS: SELECT-only for company members (0066/0068's pattern) ────────
-- The backend writes via the service role (bypasses RLS); the policy
-- gates user-scoped JWT reads. Rows with a null company_id are invisible
-- to members by construction (the EXISTS never matches) — service-role
-- tooling reads those.

alter table public.helixis_sms_log enable row level security;
drop policy if exists "members can read helixis sms log" on public.helixis_sms_log;
create policy "members can read helixis sms log"
    on public.helixis_sms_log for select
    using (
        exists (
            select 1
              from public.company_users cu
             where cu.company_id = helixis_sms_log.company_id
               and cu.user_id    = auth.uid()
        )
    );
