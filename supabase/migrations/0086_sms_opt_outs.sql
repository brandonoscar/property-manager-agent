-- "Stop texting me" from somebody who never consented in the first place.
--
-- THE GAP. ``helixis_sms_consent`` records a grant and its revocation, and
-- ``apply_consent_keywords`` mirrors STOP into it with an UPDATE — which
-- touches nothing when no row exists. Every CRM lead is exactly that case: a
-- lead texts in, an agent replies, and no consent row is ever written. So a
-- lead who replies STOP left NO trace anywhere, and the Leasing lane went on
-- texting them. That is the violation carriers audit for, and with several
-- customers each working a pipeline it is a matter of weeks, not chance.
--
-- ⚠ WHY NOT A REVOKED CONSENT ROW. Two reasons, and the first is decisive:
-- ``consented_at`` is NOT NULL, so a row created at opt-out time asserts a
-- consent that never happened — inside the very ledger whose job is to be the
-- truthful TCPA record. (``recipient_kind``'s CHECK also has no value meaning
-- "a prospect".) Recording an opt-out is safe in a way recording a consent is
-- not: it can only ever stop a message. It gets its own table so it cannot
-- corrupt the meaning of the other one.
--
-- ⚠ RECORDED per company, READ across companies — the same split
-- ``apply_consent_keywords`` already uses, and stated here because gotcha 47
-- requires a deliberate cross-company read to say so. We know which company's
-- number received the message, so that is what we store; but one STOP
-- silences every sender, because an over-broad revocation is a customer
-- service problem and a missed one is a statutory penalty.

create table if not exists public.helixis_sms_opt_outs (
    id            uuid        primary key default gen_random_uuid(),
    -- The company whose number RECEIVED the opt-out. Not a scope for reads.
    company_id    uuid        not null
        references public.companies(id) on delete cascade,
    phone         text        not null,  -- E.164 (comms/phone.py normalizes)
    opted_out_at  timestamptz not null default now(),
    -- How we learned: 'inbound_keyword' today; a manual one would be
    -- 'app_settings'. Never defaulted — an opt-out with no provenance is not
    -- evidence.
    source        text        not null,
    -- ⚠ The carrier-side proof, kept verbatim. A TCPA defense is a
    -- contemporaneous record, and "the system says they opted out" is not
    -- one. The SID is Twilio's own receipt for the inbound message.
    evidence_sid  text,
    evidence_body text,
    -- ⚠ START must be honourable. Twilio's Advanced Opt-Out treats START as
    -- resuming, and without this a person who texted STOP and then changed
    -- their mind is unreachable FOREVER with no way for anyone to fix it.
    -- Lifted rather than deleted: the row is the audit record of both the
    -- opt-out and the resumption, the same reason the consent ledger keeps
    -- revoked rows instead of removing them.
    resumed_at    timestamptz,
    unique (company_id, phone)
);

comment on table public.helixis_sms_opt_outs is
    'Numbers that told us to stop, INCLUDING ones with no consent row to '
    'revoke (every CRM lead). Read across companies: one STOP silences every '
    'sender. Never DELETE a row — a later START sets resumed_at, so the '
    'record keeps proving both directions.';

-- The read is "is this number currently opted out", by phone, across
-- companies. Partial: a resumed row can never block, so it need not be
-- indexed and the index stays small.
create index if not exists helixis_sms_opt_outs_phone_idx
    on public.helixis_sms_opt_outs (phone)
    where resumed_at is null;

alter table public.helixis_sms_opt_outs enable row level security;

-- Company-scoped read for members, matching every other helixis_* table
-- (gotcha 47). The service role writes and does the cross-company read; this
-- policy only gates user-scoped JWT reads, which have no business seeing
-- another company's opt-outs.
drop policy if exists "members can read helixis sms opt outs"
    on public.helixis_sms_opt_outs;
create policy "members can read helixis sms opt outs"
    on public.helixis_sms_opt_outs for select
    using (
        exists (
            select 1
              from public.company_users cu
             where cu.company_id = helixis_sms_opt_outs.company_id
               and cu.user_id    = auth.uid()
        )
    );
