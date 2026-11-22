-- 0048: In-app feedback (BR #19) — durable rows + PostHog for triage.
--
-- One row per submission. PostHog gets a mirrored "feedback submitted"
-- event for dashboards/triage; this table is the durable record (survives
-- PostHog sampling/outage). Insert-only from the API; member select.

create table if not exists public.helixis_feedback (
    id          uuid        primary key default gen_random_uuid(),
    company_id  uuid        not null
        references public.companies(id) on delete cascade,
    user_id     uuid        not null,
    category    text        not null default 'other'
        check (category in ('bug', 'idea', 'other')),
    message     text        not null,
    view        text,
    created_at  timestamptz not null default now()
);

alter table public.helixis_feedback enable row level security;

drop policy if exists helixis_feedback_member_select on public.helixis_feedback;
create policy helixis_feedback_member_select
    on public.helixis_feedback for select
    using (
        exists (
            select 1
              from public.company_users cu
             where cu.company_id = helixis_feedback.company_id
               and cu.user_id = auth.uid()
        )
    );
