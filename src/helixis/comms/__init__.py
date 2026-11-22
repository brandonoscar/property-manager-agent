"""Contact-comms spine — resolve a phone number to a Buildium contact and
carry a conversation's context back into the agent.

This package is the Twilio-independent half of the click-to-call/text feature
(founder goal 2026-08: "call/text anyone in Buildium from Occupella with one
click; the call/text context feeds the AI or auto-creates a task"). Nothing
here dials or sends — it's the resolution + persistence + mapping layer that
the telephony transport (``connectors/twilio_client.py``) and the CRM voice/SMS
routes plug into once a live Twilio number exists.

Modules:
- ``phone`` — E.164 normalization + a lenient match key so a Twilio E.164
  number matches the human-format phone the Buildium mirror stores.
- ``contact_resolver`` — reverse lookup: a phone number → the tenant / owner /
  vendor it belongs to, company-scoped, off the local mirror.
- ``sms`` — outbound customer-care SMS (A2P 10DLC): templates-only rendering,
  consent-ledger + quiet-hours guardrails, dark until carrier approval.
"""
