"""May we text this person at all — the rule both outbound lanes read.

``comms/sms.py`` requires a LIVE consent row, which is right for templated
customer-care messages to a tenant. The Leasing lane is different: a lead who
texts in first has started a conversation, and replying to it is conversational
traffic, not an unsolicited campaign message. What both lanes share, and what
the CRM lane had none of, is the floor:

* somebody who said STOP is never texted again, and
* nobody is texted outside the TCPA 08:00-21:00 recipient-local window.

⚠ The CRM lane had NEITHER. It called ``twilio_client.send_sms`` directly, so
it skipped the consent ledger, the quiet-hours window and the message log in
one step. A lead could reply STOP and keep receiving texts, which is precisely
what carriers audit for.

⚠ Both checks FAIL CLOSED. An unreadable opt-out table is not permission, and a
recipient-local time we cannot establish is not a legal hour — the same posture
as the consent ledger and the role floor.

⚠ **A STOP is recorded in Twilio's spelling and was looked up in the caller's,
so the two never met.** The webhook writes ``From`` — always E.164,
``+14155550134``. The CRM's send and click-to-call paths pass ``lead.phone``,
which for a lead a manager typed into the New Lead form is whatever they typed:
``(415) 555-0134``. ``phone = $1`` is an exact string compare, so the lookup
found nothing and we texted somebody who had asked us to stop — the violation
this whole module exists to prevent, reached by the one lane that does not
create its own consent row (gotcha 67).

The comparison is over every SPELLING of the number now, so a hand-typed lead
and an inbound text are one person. ``comms/phone.py`` already said this out
loud about the CRM — *"``find_lead_by_phone`` already has this latent bug (it
matches ``phone = $2`` verbatim)"* — and the fix belongs here rather than at
each caller, because a caller that forgets is a silent statutory penalty
(gotcha 25: the rule lives once).

⚠ Over-matching is the SAFE direction here and under-matching is not, which is
why the candidate list is generous. The module already states the same
asymmetry for cross-company scope: an over-broad revocation is a
customer-service problem, a missed one is a penalty.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any

from helixis.comms.local_time import is_nanp, within_quiet_hours
from helixis.comms.phone import normalize_e164, phone_match_key

logger = logging.getLogger(__name__)

#: Shortest routable E.164 including the country code. Under this there is no
#: number to look up, and a lookup that finds nothing reads as permission.
_MIN_E164_DIGITS = 8


def spellings(phone: str | None) -> list[str]:
    """Every form of ``phone`` the ledger could be holding, for an ``= ANY``.

    Rows written by the webhook are E.164; a row written before this, or by a
    caller passing a hand-typed number, can be anything. Everything returned
    denotes the SAME number, so matching any of it cannot block a DIFFERENT
    person — only this one under another spelling.

    Empty when the input cannot be a phone number at all. The callers treat
    that as unverifiable, which is a refusal, not a pass — so a junk string
    must not come back carrying itself as a candidate, look up cleanly, and
    read as "this person never opted out".

    ⚠ **The NANP forms are only offered to a NANP number.** ``phone_match_key``
    returns the last ten digits whatever the country code, so a UK
    ``+442071838750`` yields ``2071838750`` — which as a US number is a real
    Maine subscriber. Adding ``+12071838750`` to a UK number's candidate list
    would silently block a stranger who never texted anybody, and its own
    docstring warns that the key cannot tell +44 from +1. Over-matching the
    same person is the safe direction; inventing a different one is not
    (gotcha 55).
    """
    if not phone:
        return []
    digits = re.sub(r"\D", "", phone)

    # A country code that is not +1: the last-ten-digits key means nothing
    # here, so the only safe candidates are what we were given and its own
    # canonical form.
    if phone.lstrip().startswith("+") and not digits.startswith("1"):
        # ⚠ A digit floor, or "+" alone comes back as its own candidate, looks
        # up cleanly, and reads as "this person never opted out" — the pass
        # this function must never hand out. The shortest routable E.164 is
        # around eight digits including the country code; anything under that
        # is unverifiable, and unverifiable is a refusal.
        if len(digits) < _MIN_E164_DIGITS:
            return []
        found = [phone]
        canonical = normalize_e164(phone)
        if canonical and canonical not in found:
            found.append(canonical)
        return found

    key = phone_match_key(phone)
    if not key:
        # Fewer than ten digits. Not a number we can look up, and saying so
        # is what makes the caller refuse rather than send.
        return []

    found = [phone]
    for candidate in (normalize_e164(phone), key, f"1{key}"):
        if candidate and candidate not in found:
            found.append(candidate)
    return found


#: Why a specific RECIPIENT may not be texted, whatever the channel's state.
#: Separate from ``outbound_channel_block``: that answers "may this deployment
#: and this company send at all", this answers "may we send to this person now".
OPTED_OUT = "opted_out"
QUIET_HOURS = "quiet_hours"
#: We cannot reach this number at all — not "not right now".
#:
#: ⚠ A SEPARATE reason from `quiet_hours` on purpose. Telling an agent
#: "outside their local window, try later" about a number this product can
#: never text sends them to wait for something that will not arrive — gotcha
#: 46/39's shape, a message written for one reader landing on another, and the
#: same reason `outbound_channel_block` distinguishes dark from unconfigured.
UNSUPPORTED_REGION = "unsupported_region"

_OPT_OUT_SQL = """
SELECT 1
  FROM public.helixis_sms_opt_outs
 WHERE phone = ANY($1::text[])
   AND resumed_at IS NULL
 LIMIT 1
"""

#: ⚠ ``DO UPDATE`` on a RESUMED row only. A second STOP from somebody already
#: opted out must NOT move ``opted_out_at`` forward — that timestamp is the
#: evidence of how long we had been told to stop, and refreshing it quietly
#: shortens the record. But a STOP after a START is a NEW opt-out and has to
#: take effect, which a bare DO NOTHING would silently discard.
_RECORD_SQL = """
INSERT INTO public.helixis_sms_opt_outs
  (company_id, phone, source, evidence_sid, evidence_body)
VALUES ($1::uuid, $2, $3, $4, $5)
ON CONFLICT (company_id, phone) DO UPDATE SET
  opted_out_at  = now(),
  resumed_at    = NULL,
  source        = EXCLUDED.source,
  evidence_sid  = EXCLUDED.evidence_sid,
  evidence_body = EXCLUDED.evidence_body
WHERE public.helixis_sms_opt_outs.resumed_at IS NOT NULL
"""

#: START lifts the block without erasing it. Only rows that are actually
#: blocking are touched, so a repeated START is a no-op rather than a
#: timestamp that walks forward.
_RESUME_SQL = """
UPDATE public.helixis_sms_opt_outs
   SET resumed_at = now()
 WHERE phone = ANY($1::text[])
   AND resumed_at IS NULL
"""


async def has_opted_out(pool: Any, phone: str) -> bool:
    """Has this number ever told us to stop — ACROSS companies.

    ⚠ Deliberately not company-scoped, and gotcha 47 requires that said out
    loud. One STOP silences every sender: an over-broad revocation is a
    customer-service problem, a missed one is a statutory penalty. The same
    choice ``apply_consent_keywords`` already made for the consent ledger.

    Raises nothing — an unreadable table returns True, because "we could not
    check whether they opted out" must never mean "so we sent it".
    """
    known_as = spellings(phone)
    if not known_as:
        # Too few digits to be a number, so we cannot establish that they
        # did NOT opt out. Unverifiable is a refusal here, not a pass.
        return True
    try:
        return await pool.fetchrow(_OPT_OUT_SQL, known_as) is not None
    except Exception:  # noqa: BLE001 — unverifiable is opted out
        logger.warning("sms opt-out lookup failed — refusing to send", exc_info=True)
        return True


async def record_opt_out(
    pool: Any,
    *,
    company_id: str,
    phone: str,
    source: str = "inbound_keyword",
    evidence_sid: str | None = None,
    evidence_body: str | None = None,
) -> bool:
    """Write the opt-out. Idempotent; never raises into the webhook.

    A repeat STOP from somebody already blocked changes nothing (their
    original date is the evidence); a STOP after a START re-blocks. See
    ``_RECORD_SQL``.
    """
    if not (company_id and phone):
        return False
    # ⚠ Stored canonically so two spellings of one number cannot become two
    # rows — `ON CONFLICT (company_id, phone)` keys on this string. The raw
    # value is kept when it cannot be normalised: recording an opt-out we
    # cannot canonicalise still beats not recording it.
    stored_as = normalize_e164(phone) or phone
    try:
        await pool.execute(
            _RECORD_SQL,
            company_id,
            stored_as,
            source,
            evidence_sid or None,
            (evidence_body or "").strip()[:320] or None,
        )
        return True
    except Exception:  # noqa: BLE001 — a failed write must not 500 a webhook
        # ⚠ Logged loudly. This is the one failure that lets a STOP go
        # unrecorded, and Twilio will not redeliver a 200'd webhook.
        logger.error("FAILED to record an SMS opt-out for company=%s", company_id, exc_info=True)
        return False


async def clear_opt_out(pool: Any, phone: str) -> bool:
    """START: lift the block, keep the record.

    ⚠ Cross-company, matching ``has_opted_out``. A one-sided pair — blocking
    everywhere but resuming only where the START happened to land — would
    leave the person permanently unreachable from every OTHER company, which
    is the failure this whole column exists to prevent.
    """
    known_as = spellings(phone)
    if not known_as:
        return False
    try:
        await pool.execute(_RESUME_SQL, known_as)
        return True
    except Exception:  # noqa: BLE001 — a failed resume must not 500 a webhook
        logger.warning("could not lift an SMS opt-out", exc_info=True)
        return False


async def recipient_block(
    pool: Any,
    *,
    phone: str,
    now: datetime | None = None,
) -> str | None:
    """``'opted_out'`` / ``'quiet_hours'`` / ``None`` if we may text them now.

    ⚠ Takes NO ``company_id``, deliberately. Both questions here are about the
    person, not the sender: an opt-out binds every company (see
    ``has_opted_out``) and their local clock does not change depending on who
    is texting. A company parameter that no branch reads would suggest a
    scoping this function does not do.

    Opt-out is checked FIRST and its wording wins: telling somebody who asked
    to be left alone that we will text them later is worse than the original
    message.
    """
    if await has_opted_out(pool, phone):
        return OPTED_OUT
    # Reachability before the clock: "we cannot text this number" outranks
    # "not at this hour", and the clock answer is meaningless for a number we
    # cannot place on any timezone we know.
    if not is_nanp(phone):
        return UNSUPPORTED_REGION
    if not within_quiet_hours(phone, now or datetime.now(UTC)):
        return QUIET_HOURS
    return None


__all__ = [
    "OPTED_OUT",
    "QUIET_HOURS",
    "UNSUPPORTED_REGION",
    "clear_opt_out",
    "has_opted_out",
    "recipient_block",
    "record_opt_out",
]
