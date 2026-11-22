-- 0040_applicants_mirror_crm_link.sql — applicants join the entity mirror,
-- and CRM leads gain a soft link to their Buildium applicant.
--
-- CRM ↔ Buildium enrichment: when Buildium fires Applicant.Created/Updated,
-- the webhook worker mirrors the applicant here and auto-moves the matching
-- CRM lead (matched by email/phone) to 'applied' — or 'leased' when the
-- applicant's Status is AddedToLease. The link column makes that match
-- sticky: once linked, later webhooks find the lead by applicant id even if
-- the email on either side is edited.
--
-- Conventions per 0038: buildium_id as the upsert conflict key, soft bigint
-- Buildium references (NOT hard FKs — webhook/backfill upserts arrive out of
-- order), company_id keeps its hard FK as OUR tenancy boundary, ``raw`` jsonb
-- keeps the full payload, RLS = SELECT-only for company members (0020's
-- pattern); the backend writes via the service role.
--
-- Wire shape verified against the Buildium ApplicantMessage spec
-- (LeadSimple/buildium applicant_message.rb, checked 2026-07-05): Id,
-- ApplicantGroupId, PropertyId, UnitId, TenantId, FirstName, LastName,
-- Email, PhoneNumbers[{Number,Type}], Status (Unknown|Undecided|Approved|
-- Rejected|AddedToLease|Cancelled|Deferred|New|Draft|AddedToDraftLease),
-- Applications, LastUpdatedDateTime.
--
-- NOTE: migrations have no runner (CLAUDE.md §4 / gotcha #8) — apply manually
-- to helixis-test and VERIFY, in order, after 0039.

begin;

-- ── Applicants mirror ─────────────────────────────────────────────────

create table if not exists public.helixis_applicants (
    id                 uuid        primary key default gen_random_uuid(),
    buildium_id        bigint      unique not null,
    account_id         bigint      not null,
    company_id         uuid        not null
        references public.companies(id) on delete cascade,
    first_name         text,
    last_name          text,
    email              text,
    phone              text,
    -- Buildium applicant Status verbatim (enum above) — the auto-move input.
    status             text,
    -- Soft Buildium references (see header).
    applicant_group_id bigint,
    property_id        bigint,
    unit_id            bigint,
    -- Set by Buildium once the applicant transitions into a tenant.
    tenant_id          bigint,
    raw                jsonb       not null,
    created_at         timestamptz not null default now(),
    updated_at         timestamptz not null default now()
);

create index if not exists helixis_applicants_company_idx
    on public.helixis_applicants (company_id);
create index if not exists helixis_applicants_property_idx
    on public.helixis_applicants (property_id);
create index if not exists helixis_applicants_email_idx
    on public.helixis_applicants (company_id, lower(email));

-- ── CRM lead → applicant link ─────────────────────────────────────────
-- Soft Buildium id (join on helixis_applicants.buildium_id). NULL until an
-- applicant webhook (or backfill sweep) matches the lead.

alter table public.crm_leads
    add column if not exists buildium_applicant_id bigint;

create index if not exists crm_leads_buildium_applicant_idx
    on public.crm_leads (company_id, buildium_applicant_id);

-- ── RLS: SELECT-only for company members (0020's pattern) ─────────────

alter table public.helixis_applicants enable row level security;
drop policy if exists "members can read helixis applicants" on public.helixis_applicants;
create policy "members can read helixis applicants"
    on public.helixis_applicants for select
    using (
        exists (
            select 1
              from public.company_users cu
             where cu.company_id = helixis_applicants.company_id
               and cu.user_id    = auth.uid()
        )
    );

commit;
