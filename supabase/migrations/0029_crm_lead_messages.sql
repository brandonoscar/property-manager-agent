-- 0029_crm_lead_messages.sql
--
-- CRM lead conversation log — inbound/outbound messages per lead.
--
-- Background. The CRM pipeline (0028) stores the lead; this stores the
-- *conversation*. An inbound SMS (Twilio webhook) appends an 'inbound'
-- row; a user-approved reply appends an 'outbound' row. The lead board's
-- detail panel renders these as a thread. Email replies will reuse the
-- same table (channel='email') so there's one conversation log per lead
-- regardless of channel.
--
-- Shape notes:
-- * ``lead_id`` FKs ``crm_leads`` (cascade delete — a lead's messages die
--   with the lead). ``company_id`` is denormalised onto the row so RLS +
--   listing can scope without a join.
-- * ``direction`` is a closed enum: 'inbound' (from the lead) | 'outbound'
--   (from the PM/Helixis). ``channel`` is 'sms' | 'email'.
-- * ``external_id`` is the provider's message id (Twilio MessageSid) —
--   nullable, and UNIQUE-per-company when present so a re-delivered Twilio
--   webhook doesn't double-insert the same inbound text.
-- * ``body`` is the message text. ``raw`` jsonb carries the provider
--   payload for debugging.
--
-- Apply order: after 0028. Same company-scoped RLS shape as 0028 / 0014.
--
-- NOTE: migrations have no runner (gotcha #8) — apply manually before the
-- CRM flag is flipped on.

begin;

create table if not exists public.crm_lead_messages (
    id           uuid        primary key default gen_random_uuid(),
    company_id   uuid        not null
        references public.companies(id) on delete cascade,
    lead_id      uuid        not null
        references public.crm_leads(id) on delete cascade,
    direction    text        not null
        check (direction in ('inbound', 'outbound')),
    channel      text        not null default 'sms'
        check (channel in ('sms', 'email')),
    body         text        not null default '',
    -- Provider message id (Twilio MessageSid). Nullable; unique per company
    -- when set so a redelivered inbound webhook is idempotent.
    external_id  text,
    raw          jsonb       not null default '{}'::jsonb,
    created_at   timestamptz not null default now()
);

create index if not exists crm_lead_messages_lead_idx
    on public.crm_lead_messages (lead_id, created_at);

create unique index if not exists crm_lead_messages_external_unique
    on public.crm_lead_messages (company_id, external_id)
    where external_id is not null;

alter table public.crm_lead_messages enable row level security;

drop policy if exists "crm_lead_messages_company_read"  on public.crm_lead_messages;
drop policy if exists "crm_lead_messages_company_write" on public.crm_lead_messages;

create policy "crm_lead_messages_company_read"
    on public.crm_lead_messages for select
    using (
        exists (
            select 1 from public.company_users cu
             where cu.company_id = crm_lead_messages.company_id
               and cu.user_id    = auth.uid()
        )
    );

create policy "crm_lead_messages_company_write"
    on public.crm_lead_messages for all
    using (
        exists (
            select 1 from public.company_users cu
             where cu.company_id = crm_lead_messages.company_id
               and cu.user_id    = auth.uid()
        )
    )
    with check (
        exists (
            select 1 from public.company_users cu
             where cu.company_id = crm_lead_messages.company_id
               and cu.user_id    = auth.uid()
        )
    );

commit;
