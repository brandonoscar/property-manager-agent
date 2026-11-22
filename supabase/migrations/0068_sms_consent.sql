-- SMS consent ledger — the TCPA record of who agreed to receive texts.
--
-- APPLIED to helixis-test 2026-08-18 via the Supabase MCP; verified by
-- effect (10 columns, rowsecurity=true, 1 members-read policy). No runner
-- exists (CLAUDE.md §4 / gotcha 8) — future DBs apply manually via psql.
--
-- COMPLIANCE INVARIANT: a phone number existing in Buildium is NOT consent.
-- TCPA requires the recipient's own prior express consent before any
-- application-to-person text; a row in THIS table (with a null revoked_at)
-- is that consent. ``comms/sms.py`` refuses to send to any number without a
-- live row here — absence means "never texted", not "look it up elsewhere".
--
-- ``source`` records HOW consent was collected (the A2P campaign's declared
-- opt-in flows): 'app_settings' (a manager opting THEMSELVES in inside the
-- app), 'lease_packet' (a tenant's signed lease consent clause),
-- 'resident_portal' (portal opt-in checkbox). ``recipient_kind`` is who the
-- number belongs to (manager | tenant | owner | vendor). ``timezone`` is an
-- IANA zone used for the TCPA 08:00-21:00 recipient-local quiet-hours window.
--
-- Revocation (STOP) is recorded by setting ``revoked_at`` + a
-- ``revocation_source`` (e.g. 'twilio_stop_webhook', 'app_settings') — the
-- row is kept, never deleted, so the ledger proves both the grant AND the
-- revocation. Twilio Advanced Opt-Out blocks STOP'd numbers carrier-side in
-- v1; the inbound webhook that mirrors STOP into revoked_at is a follow-up.

create table if not exists public.helixis_sms_consent (
    id                uuid        primary key default gen_random_uuid(),
    company_id        uuid        not null
        references public.companies(id) on delete cascade,
    phone             text        not null,  -- E.164 (+1XXXXXXXXXX; comms/phone.py normalizes)
    display_name      text,
    recipient_kind    text        not null default 'manager'
        check (recipient_kind in ('manager', 'tenant', 'owner', 'vendor')),
    source            text        not null,  -- 'app_settings' | 'lease_packet' | 'resident_portal'
    timezone          text        not null default 'America/New_York',
    consented_at      timestamptz not null default now(),
    revoked_at        timestamptz,
    revocation_source text,
    unique (company_id, phone)
);

comment on table public.helixis_sms_consent is
    'TCPA consent ledger for outbound SMS. A phone number in Buildium is NOT '
    'consent — a row here (revoked_at is null) is. comms/sms.py fails closed '
    'without one.';
comment on column public.helixis_sms_consent.timezone is
    'IANA zone for the TCPA 08:00-21:00 recipient-local quiet-hours window.';

-- ── RLS: SELECT-only for company members (0066's pattern) ─────────────
-- The backend writes via the service role (bypasses RLS); the policy
-- gates user-scoped JWT reads.

alter table public.helixis_sms_consent enable row level security;
drop policy if exists "members can read helixis sms consent" on public.helixis_sms_consent;
create policy "members can read helixis sms consent"
    on public.helixis_sms_consent for select
    using (
        exists (
            select 1
              from public.company_users cu
             where cu.company_id = helixis_sms_consent.company_id
               and cu.user_id    = auth.uid()
        )
    );
