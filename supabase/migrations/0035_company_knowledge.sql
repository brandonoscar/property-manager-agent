-- 0035_company_knowledge.sql
--
-- ADR 0006 — Company knowledge MD: a short, standing markdown profile of each
-- company, learned in the background (Buildium API / webhooks / uploaded docs /
-- Drive) and refreshed at most once per day. DELIBERATELY SEPARATE from Mem0
-- memory (different shape, different source, different cadence) — this is a
-- single curated doc, not vector facts.
--
-- One row per company. Written ONLY by the daily background job (service key,
-- which bypasses RLS); nobody types into it. Read by company members (surfaced
-- read-only under Settings → AI & Memory) and injected as standing context each
-- agent turn. So RLS is a company-member SELECT policy + no client write policy
-- (the derived-artifact posture: clients read, the job writes via service key).
--
-- NOTE: migrations have no runner (CLAUDE.md §4 / gotcha #8). This file being
-- committed is NOT proof it's live — apply it manually to helixis-test.

begin;

create table if not exists public.company_knowledge (
    company_id   uuid        primary key
        references public.companies(id) on delete cascade,
    -- The short markdown profile (target ≤ ~1.5k tokens). May be empty on a
    -- fresh company until the first daily run produces something.
    content_md   text        not null default '',
    -- The date the current content was generated on. The daily job skips a
    -- company whose generated_on = today (idempotent, at-most-once-per-day).
    generated_on date,
    updated_at   timestamptz not null default now()
);

alter table public.company_knowledge enable row level security;

-- Read-only for company members. There is intentionally NO client write policy:
-- the content is a derived artifact written only by the daily job (service key,
-- bypasses RLS). A member can see their company's profile; no one can edit it.
drop policy if exists "company_knowledge_company_read" on public.company_knowledge;

create policy "company_knowledge_company_read"
    on public.company_knowledge for select
    using (
        exists (
            select 1 from public.company_users cu
             where cu.company_id = company_knowledge.company_id
               and cu.user_id    = auth.uid()
        )
    );

commit;
