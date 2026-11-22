-- 0032_crm_lead_call_channel.sql
--
-- CRM call intake — let the conversation log record phone calls.
--
-- Background. Leads already land by SMS (Twilio) and email (Gmail); this
-- adds the third channel, voice. When a lead calls the company's Twilio
-- number, Helixis creates/updates the lead by caller number and logs the
-- call on the same ``crm_lead_messages`` thread the SMS + email replies use
-- — so a lead's whole history (text, email, call, voicemail) is one log.
--
-- The only schema change is widening the ``channel`` CHECK from
-- ('sms','email') to also allow 'call' (an inbound/forwarded call event)
-- and 'voicemail' (a missed call that left a recording — the recording url
-- + duration ride in ``raw``). Everything else on the row is unchanged:
-- ``direction`` is 'inbound' for both, ``external_id`` carries the Twilio
-- CallSid (idempotent per company), ``body`` is the human line
-- ("📞 Inbound call" / "📞 Voicemail · 0:42").
--
-- Apply order: after 0031. Idempotent (drop + re-add the named CHECK).
--
-- NOTE: migrations have no runner (CLAUDE.md §4 / gotcha #8) — apply
-- manually to helixis-test.

begin;

-- The inline channel CHECK from 0029 is named ``crm_lead_messages_channel_check``.
alter table public.crm_lead_messages
    drop constraint if exists crm_lead_messages_channel_check;
alter table public.crm_lead_messages
    add constraint crm_lead_messages_channel_check
        check (channel in ('sms', 'email', 'call', 'voicemail'));

commit;
