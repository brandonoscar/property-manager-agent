-- 0083 — the rest of a company's telephony config lives with its number.
--
-- WHY (this is the half 0082 did not close)
-- 0082 fixed INBOUND attribution: which company owns the number a message
-- arrived at. It left every OUTBOUND path on deployment-global settings, and
-- that is the worse leak because it is bidirectional:
--
--   * ``twilio.from_number`` — EVERY company's outbound SMS and click-to-call
--     went out from ONE number. So company B texts its tenant from company A's
--     number, the tenant replies to the number they see, and that reply lands
--     on A's number and routes to A. B's conversation ends up in A's inbox and
--     A's Leasing pipeline, with neither party doing anything wrong.
--   * ``twilio.forward_to_number`` — every inbound call forwarded to ONE cell.
--     A second customer's caller rings the first customer's phone: a live
--     voice connection to the wrong person.
--   * ``twilio.voice_greeting`` — one company's greeting played to every
--     company's callers.
--
-- All three are per-company facts that were stored as deployment config. They
-- belong on the row that already says who owns the number.
--
-- ⚠ ``forward_to_number`` is nullable on purpose: blank means "no forwarding,
-- straight to voicemail", which is the existing documented behaviour and a
-- legitimate configuration. NOT NULL would force a placeholder, and a
-- placeholder number is a call routed to a stranger.

alter table public.helixis_twilio_numbers
    add column if not exists forward_to_number text,
    add column if not exists voice_greeting    text;

comment on column public.helixis_twilio_numbers.forward_to_number is
    'The PM cell inbound calls to this number forward to. NULL = straight to voicemail.';
comment on column public.helixis_twilio_numbers.voice_greeting is
    'Greeting played before voicemail. NULL = the built-in default.';
