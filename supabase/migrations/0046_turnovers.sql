-- 0046: Turnover checklist — the property-page make-ready tracker.
--
-- One row per (company, lease): opened by the Lease.MoveOut.Created
-- webhook (the same trigger that drops the Inbox turnover card), worked
-- on the property page as three tracked steps — move-out inspection,
-- make-ready work orders, list the unit. Steps are timestamps, not
-- booleans, so "done" carries when it happened; the row flips to
-- status='done' when all three are set and back to 'active' if one is
-- unchecked. Dismiss means "not tracking this turnover in Helixis".

create table if not exists public.helixis_turnovers (
    id                  uuid        primary key default gen_random_uuid(),
    company_id          uuid        not null
        references public.companies(id) on delete cascade,
    lease_buildium_id   bigint      not null,
    property_id         bigint,
    unit_id             bigint,
    move_out_date       date,
    status              text        not null default 'active'
        check (status in ('active', 'done', 'dismissed')),
    inspection_done_at  timestamptz,
    work_orders_done_at timestamptz,
    listed_done_at      timestamptz,
    completed_at        timestamptz,
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now(),
    unique (company_id, lease_buildium_id)
);

create index if not exists helixis_turnovers_company_property_idx
    on public.helixis_turnovers (company_id, property_id);

alter table public.helixis_turnovers enable row level security;

drop policy if exists helixis_turnovers_member_select
    on public.helixis_turnovers;
create policy helixis_turnovers_member_select
    on public.helixis_turnovers for select
    using (
        exists (
            select 1
              from public.company_users cu
             where cu.company_id = helixis_turnovers.company_id
               and cu.user_id = auth.uid()
        )
    );
