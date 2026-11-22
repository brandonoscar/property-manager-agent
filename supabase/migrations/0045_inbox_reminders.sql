-- 0045: Inbox reminders — the "hits your desk only if neglected" engine.
--
-- One row per (company, producer, entity_key): the daily worker sweep
-- upserts cards whose condition holds AND passes the neglect gate; rows
-- whose condition stopped holding are self-cleared (status='cleared') so
-- the desk drains itself. Snooze hides a card until snoozed_until, then
-- the CONDITION IS RECHECKED before it can return. Dismiss is per-entity
-- and permanent for that instance; 'handled' means the PM acted outside
-- Helixis (phone call) — suppressed for the producer's cooldown.

create table if not exists public.helixis_reminders (
    id             uuid        primary key default gen_random_uuid(),
    company_id     uuid        not null
        references public.companies(id) on delete cascade,
    producer       text        not null,
    entity_key     text        not null,
    status         text        not null default 'open'
        check (status in ('open', 'snoozed', 'dismissed', 'handled', 'cleared')),
    severity       text        not null default 'normal'
        check (severity in ('low', 'normal', 'high')),
    title          text        not null,
    body           text,
    link_path      text,
    payload        jsonb       not null default '{}'::jsonb,
    snoozed_until  timestamptz,
    handled_at     timestamptz,
    escalation     integer     not null default 0,
    first_fired_at timestamptz not null default now(),
    last_fired_at  timestamptz not null default now(),
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now(),
    unique (company_id, producer, entity_key)
);

create index if not exists helixis_reminders_company_status_idx
    on public.helixis_reminders (company_id, status);

alter table public.helixis_reminders enable row level security;

drop policy if exists helixis_reminders_member_select
    on public.helixis_reminders;
create policy helixis_reminders_member_select
    on public.helixis_reminders for select
    using (
        exists (
            select 1
              from public.company_users cu
             where cu.company_id = helixis_reminders.company_id
               and cu.user_id = auth.uid()
        )
    );
