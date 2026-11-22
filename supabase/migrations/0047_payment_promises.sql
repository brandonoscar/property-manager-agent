-- 0047: Payment promises — reminders v1.5.
--
-- One row per (company, sender email): the latest payment promise we
-- extracted from an ingested email ("will pay the 17th"). The
-- rent_missing reminder producer suppresses a lease while any of its
-- tenants has a pending promise (promised_date + 1 grace day >= today);
-- once the date passes the suppression lapses and the next sweep
-- re-fires the card — that IS the follow-up. Rows are advisory and
-- self-expiring by comparison; no cleanup job needed.

create table if not exists public.helixis_payment_promises (
    id                 uuid        primary key default gen_random_uuid(),
    company_id         uuid        not null
        references public.companies(id) on delete cascade,
    email              text        not null,
    promised_date      date        not null,
    source             text        not null default 'email_ingest',
    source_message_id  text,
    created_at         timestamptz not null default now(),
    updated_at         timestamptz not null default now(),
    unique (company_id, email)
);

alter table public.helixis_payment_promises enable row level security;

drop policy if exists helixis_payment_promises_member_select
    on public.helixis_payment_promises;
create policy helixis_payment_promises_member_select
    on public.helixis_payment_promises for select
    using (
        exists (
            select 1
              from public.company_users cu
             where cu.company_id = helixis_payment_promises.company_id
               and cu.user_id = auth.uid()
        )
    );
