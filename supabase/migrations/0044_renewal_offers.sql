-- 0044: renewal offers — the property-page renewals flow.
--
-- One row per (company, lease) renewal conversation. Drafted by the LLM on
-- the property cockpit, edited by the PM, sent via the PM's Gmail
-- (Composio) — NEVER auto-sent. The actual lease renewal is executed in
-- Buildium by the PM; when the mirror's lease end date moves out past the
-- expiring window, the offer reads as renewed (computed at query time, no
-- webhook coupling needed).

create table if not exists public.helixis_renewal_offers (
    id                 uuid        primary key default gen_random_uuid(),
    company_id         uuid        not null
        references public.companies(id) on delete cascade,
    lease_buildium_id  bigint      not null,
    property_id        bigint,
    unit_id            bigint,
    -- draft → sent → (computed: renewed) | dismissed
    status             text        not null default 'draft'
        check (status in ('draft', 'sent', 'dismissed')),
    proposed_rent      numeric(12, 2),
    term_months        integer,
    subject            text,
    body               text,
    recipient_email    text,
    sent_at            timestamptz,
    sent_message_id    text,
    created_by         uuid,
    created_at         timestamptz not null default now(),
    updated_at         timestamptz not null default now(),
    unique (company_id, lease_buildium_id)
);

create index if not exists helixis_renewal_offers_company_idx
    on public.helixis_renewal_offers (company_id);
create index if not exists helixis_renewal_offers_property_idx
    on public.helixis_renewal_offers (company_id, property_id);

alter table public.helixis_renewal_offers enable row level security;

drop policy if exists helixis_renewal_offers_member_select
    on public.helixis_renewal_offers;
create policy helixis_renewal_offers_member_select
    on public.helixis_renewal_offers for select
    using (
        exists (
            select 1
              from public.company_users cu
             where cu.company_id = helixis_renewal_offers.company_id
               and cu.user_id = auth.uid()
        )
    );
