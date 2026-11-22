-- 0082 — which company owns which Twilio number.
--
-- WHY
-- Inbound SMS and voice carry no auth and no company context. Both routes
-- resolved the company from ``settings.twilio.default_company_id``: ONE global
-- id, shared by the whole deployment. With a second customer that is a
-- cross-tenant leak by construction — every inbound text in the deployment
-- lands in whichever company the env var names, so customer B's leads and
-- residents' messages file into customer A's pipeline and Inbox.
--
-- The routable fact is the number the message was sent TO. This table is that
-- mapping, and it also removes the operational problem that motivated it: a
-- new company's number is a row, not a Render env var and a redeploy.
--
-- ⚠ ``phone_number`` is stored E.164-normalized (``comms/phone.py``) because
-- Twilio's ``To`` param is E.164 and a mixed-format column would silently miss.
-- The UNIQUE constraint is the load-bearing part: a number owned by two
-- companies has no correct routing, so the database refuses to represent it
-- rather than leaving the application to pick.
--
-- ⚠ Company-scoped, so ToS §18 applies: this table is listed in
-- ``buildium/purge.py::PURGE_TABLES`` classification (see
-- ``test_buildium_purge.py``, which fails CI on an unclassified company-scoped
-- table).

create table if not exists public.helixis_twilio_numbers (
    id           uuid primary key default gen_random_uuid(),
    company_id   uuid not null references public.companies(id) on delete cascade,
    phone_number text not null,
    -- Operator-facing note ("Blue Ridge main line"). Never used for routing.
    label        text,
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now()
);

-- One company per number. See the header: this is the cross-tenant guarantee,
-- not a tidiness constraint.
create unique index if not exists helixis_twilio_numbers_phone_uniq
    on public.helixis_twilio_numbers (phone_number);

create index if not exists helixis_twilio_numbers_company_idx
    on public.helixis_twilio_numbers (company_id);

alter table public.helixis_twilio_numbers enable row level security;

-- Members of the owning company may read their own mapping. Writes go through
-- the API (service role), which applies the admin role floor — the same shape
-- as the other operator-managed tables.
drop policy if exists helixis_twilio_numbers_select on public.helixis_twilio_numbers;
create policy helixis_twilio_numbers_select
    on public.helixis_twilio_numbers
    for select
    using (
        company_id in (
            select company_id from public.company_users where user_id = auth.uid()
        )
    );
