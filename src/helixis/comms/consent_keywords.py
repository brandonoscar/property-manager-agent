"""STOP / START / HELP handling against the consent ledger.

Lifted out of ``api/routes/twilio.py`` so BOTH inbound routes can apply it. A
Twilio number has exactly one "A message comes in" URL; whichever route the
console names has to honour opt-out, because a missed STOP is a TCPA
violation while a missed lead is an inconvenience.

The keyword sets and the two ledger statements are unchanged from the
original — this module is a move, not a rewrite.
"""

from __future__ import annotations

import logging
from typing import Any

from helixis.comms.opt_out import clear_opt_out, record_opt_out
from helixis.comms.sms import redact_phone

logger = logging.getLogger(__name__)

# Twilio's standard opt-out / opt-in keyword families (Advanced Opt-Out
# handles the carrier-side auto-reply; we mirror the ledger effect).
# The FCC's April 2025 revocation rule standardizes the full keyword set
# (stop, quit, revoke, opt out, cancel, unsubscribe, end) — a consumer using
# ANY of them has revoked, whatever Twilio's Advanced Opt-Out happens to
# auto-handle. "OPT OUT"/"OPT-OUT" are matched after whitespace/hyphen
# normalization below. Erring broad is the safe direction: an extra
# revocation is conservative, a missed one is a TCPA violation.
STOP_KEYWORDS = {
    "STOP",
    "STOPALL",
    "UNSUBSCRIBE",
    "CANCEL",
    "END",
    "QUIT",
    "REVOKE",
    "OPTOUT",
}
START_KEYWORDS = {"START", "UNSTOP", "YES"}
HELP_KEYWORDS = {"HELP"}


def classify_keyword(body: str) -> str | None:
    """``'stop'`` / ``'start'`` / ``'help'`` / ``None``.

    Normalizes so "Opt Out" / "opt-out" / " STOP " all resolve: uppercase,
    then strip spaces and hyphens before matching.
    """
    keyword = (body or "").strip().upper().replace("-", "").replace(" ", "")
    if keyword in STOP_KEYWORDS:
        return "stop"
    if keyword in START_KEYWORDS:
        return "start"
    if keyword in HELP_KEYWORDS:
        return "help"
    return None


async def apply_consent_keywords(
    pool: Any,
    *,
    from_number: str,
    body: str,
    company_id: str = "",
    message_sid: str = "",
) -> str | None:
    """Mirror a STOP/START keyword into the ledger. Returns what it matched.

    ⚠ Deliberately NOT company-scoped (gotcha 47 requires this stated): one
    STOP silences EVERY sender, so the update spans all companies holding that
    number. Conservative on purpose — an over-broad revocation is a customer
    service problem, a missed one is a TCPA violation.

    Idempotent both ways: rows already revoked keep their original
    ``revoked_at``/``revocation_source``, and START restores ONLY
    previously-revoked rows — it never creates consent, because no prior
    relationship means no consent to restore.

    ⚠ **The UPDATE alone was silently a no-op for every CRM lead**, which is
    why ``company_id`` is now taken. A lead has no consent row — nobody ever
    writes one — so ``WHERE phone = $1`` matched zero rows, the STOP left no
    trace anywhere, and the Leasing lane went on texting them. The opt-out
    record (migration 0086) is the half that does not depend on a prior grant
    existing. Both run: the ledger keeps proving grant-and-revocation for
    people who consented, and the opt-out table covers everyone else.
    """
    if not from_number:
        return None
    kind = classify_keyword(body)
    if kind is None:
        return None
    log_phone = redact_phone(from_number)

    if kind == "stop":
        result = await pool.execute(
            "UPDATE public.helixis_sms_consent "
            "SET revoked_at = now(), revocation_source = 'twilio_stop_webhook' "
            "WHERE phone = $1 AND revoked_at IS NULL",
            from_number,
        )
        # ⚠ Unconditional, not "only when the UPDATE touched nothing". Reading
        # asyncpg's tag to decide would make the durable record depend on
        # parsing a status string, and the write is idempotent anyway — the
        # cheap belt beats the clever brace on the path where a miss is a
        # statutory penalty.
        recorded = await record_opt_out(
            pool,
            company_id=company_id,
            phone=from_number,
            source="inbound_keyword",
            evidence_sid=message_sid,
            evidence_body=body,
        )
        if not recorded and not company_id:
            # The caller could not resolve which company's number received
            # this. The ledger update still ran, so a consenting recipient is
            # covered; a lead is NOT, and that is worth saying out loud.
            logger.error(
                "sms inbound STOP from=%s could not be recorded — no company for the "
                "receiving number, so a lead with no consent row is not covered",
                log_phone,
            )
        logger.info("sms inbound STOP from=%s ledger=%s opt_out=%s", log_phone, result, recorded)
    elif kind == "start":
        result = await pool.execute(
            "UPDATE public.helixis_sms_consent "
            "SET revoked_at = NULL, revocation_source = NULL, "
            "    consented_at = now(), source = 'twilio_start_webhook' "
            "WHERE phone = $1 AND revoked_at IS NOT NULL",
            from_number,
        )
        # ⚠ Lifts the opt-out record too, or somebody who texted STOP and then
        # changed their mind is unreachable forever: the ledger restore only
        # touches rows that had consent, and a lead never had one.
        await clear_opt_out(pool, from_number)
        logger.info("sms inbound START from=%s ledger=%s", log_phone, result)
    else:
        # No ledger change — Twilio Advanced Opt-Out auto-responds.
        logger.info("sms inbound HELP from=%s", log_phone)
    return kind


__all__ = [
    "HELP_KEYWORDS",
    "START_KEYWORDS",
    "STOP_KEYWORDS",
    "apply_consent_keywords",
    "classify_keyword",
]
