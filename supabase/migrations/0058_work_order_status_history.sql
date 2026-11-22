-- 0058_work_order_status_history.sql
--
-- Work-order status-transition history (loose-ends batch). The mirror
-- (helixis_work_orders) holds only the CURRENT status — every webhook upsert
-- overwrites the last one, so time-in-status and reopen patterns were
-- unanswerable. This table appends one row per observed transition
-- (webhooks/handlers/maintenance.py records it BEFORE the mirror upsert);
-- the history starts accruing now so Feature 1's analyst wave has data to
-- read later. Append-only; no UPDATE/DELETE path exists in code.
--
-- Apply on prod (after 0057):
--   psql "$HELIXIS_SUPABASE__DB_URL" -f supabase/migrations/0058_work_order_status_history.sql

begin;

create table if not exists public.helixis_work_order_status_history (
    id                      bigint generated always as identity primary key,
    company_id              uuid        not null,
    work_order_buildium_id  bigint      not null,
    previous_status         text,                 -- NULL = first sighting
    status                  text        not null,
    source                  text        not null default 'webhook',
    occurred_at             timestamptz not null default now()
);

create index if not exists helixis_wo_status_history_wo_idx
    on public.helixis_work_order_status_history
    (company_id, work_order_buildium_id, occurred_at);

alter table public.helixis_work_order_status_history enable row level security;

-- Members read their company's history; the backend writes via the service
-- role (bypasses RLS) — same pattern as orchestrator_runs (0019).
drop policy if exists "members read wo status history"
    on public.helixis_work_order_status_history;
create policy "members read wo status history"
    on public.helixis_work_order_status_history for select
    using (
        exists (
            select 1
              from public.company_users cu
             where cu.company_id = helixis_work_order_status_history.company_id
               and cu.user_id    = auth.uid()
        )
    );

commit;
