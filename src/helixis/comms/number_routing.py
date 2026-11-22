"""Which company owns the Twilio number a message arrived at.

Inbound SMS and voice carry no auth and no company context — only the number
the message was sent TO. Both routes previously resolved that through
``settings.twilio.default_company_id``: ONE global id for the whole
deployment. With a second customer that is a cross-tenant leak by
construction, not by accident: every inbound text lands in whichever company
the env var names.

``helixis_twilio_numbers`` (migration 0082) is the real mapping, and the
UNIQUE index on ``phone_number`` means the database refuses to represent a
number owned by two companies — so this module never has to pick.

**It declines rather than guesses**, the same posture as
``comms_ingest.companies_for_phone`` and gotcha 55: a guard that cannot see
must refuse to act, never act on a guess. Filing a resident's text or a lead
in another landlord's account is the one direction whose cost is unbounded.

⚠ **The legacy env var is honoured in exactly one state and no other.** If the
mapping table is EMPTY and ``default_company_id`` is set, the deployment is
single-tenant by definition and the old behaviour is preserved. The moment ONE
row exists, the env var stops being consulted — including for numbers that row
does not cover, which then route nowhere. That asymmetry is deliberate: a
silent fallback per-number would reintroduce the exact leak this module
exists to close, and would do it invisibly, on the first number somebody
forgot to map.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from helixis.comms.phone import normalize_e164
from helixis.comms.sms import redact_phone

logger = logging.getLogger(__name__)

# ⚠ Deliberately NOT company-scoped (gotcha 47 requires this said in the SQL):
# the whole job of this query is to DISCOVER the company from a bare phone
# number, so a company_id filter would be circular. The UNIQUE index on
# phone_number bounds it to at most one row.
_COMPANY_FOR_NUMBER_SQL = """
SELECT company_id
  FROM public.helixis_twilio_numbers
 WHERE phone_number = $1
 LIMIT 1
"""

_ANY_MAPPING_SQL = "SELECT 1 FROM public.helixis_twilio_numbers LIMIT 1"


async def resolve_company_for_number(
    pool: Any, to_number: str, *, twilio_settings: Any = None
) -> str | None:
    """The company that owns ``to_number``, or ``None`` to decline routing.

    ``None`` means "do not act" — the caller must drop the message (logging
    it), never fall back to a default. A dropped inbound is recoverable by a
    human; one filed in the wrong company's account is not.

    ⚠ Fails CLOSED on a lookup error. An unreadable mapping cannot establish
    ownership, and routing on the assumption that it would have matched is the
    same mistake as routing on a guess.
    """
    normalized = normalize_e164(to_number) if to_number else None
    if not normalized:
        logger.warning("inbound: no usable To number — cannot route")
        return None

    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(_COMPANY_FOR_NUMBER_SQL, normalized)
            if row is not None:
                return str(row["company_id"])
            # No row for THIS number. The env var applies only when nothing at
            # all is mapped — see the module docstring for why this is not
            # per-number.
            any_mapping = await conn.fetchrow(_ANY_MAPPING_SQL)
    except Exception:  # noqa: BLE001 — an unverifiable owner is no owner
        logger.warning(
            "inbound: number->company lookup failed for to=%s — declining to route",
            redact_phone(normalized),
            exc_info=True,
        )
        return None

    if any_mapping is not None:
        logger.warning(
            "inbound: no company owns to=%s and other numbers ARE mapped — "
            "declining to route (add a helixis_twilio_numbers row for it)",
            redact_phone(normalized),
        )
        return None

    legacy = (getattr(twilio_settings, "default_company_id", "") or "").strip()
    if legacy:
        logger.warning(
            "inbound: routing to=%s via the legacy default_company_id. This is "
            "SINGLE-TENANT ONLY and stops working the moment any number is "
            "mapped — add a helixis_twilio_numbers row.",
            redact_phone(normalized),
        )
        return legacy

    logger.warning(
        "inbound: no company owns to=%s and no default is configured — dropped",
        redact_phone(normalized),
    )
    return None


@dataclass(frozen=True)
class CompanyTelephony:
    """One company's telephony identity — the OUTBOUND half of 0082/0083.

    ``from_number`` is the number this company sends from and the number its
    recipients will reply TO, so it has to be the same one inbound routing
    resolves back to that company. Sending from a shared number and receiving
    on a per-company one is not a half-fix, it is a loop: the reply lands
    somewhere else entirely.
    """

    company_id: str
    from_number: str
    messaging_service_sid: str
    forward_to_number: str
    voice_greeting: str

    @property
    def can_send(self) -> bool:
        return bool(self.from_number or self.messaging_service_sid)


_TELEPHONY_FOR_COMPANY_SQL = """
SELECT phone_number, forward_to_number, voice_greeting
  FROM public.helixis_twilio_numbers
 WHERE company_id = $1::uuid
 ORDER BY created_at
 LIMIT 1
"""


async def telephony_for_company(
    pool: Any, company_id: str, *, twilio_settings: Any
) -> CompanyTelephony | None:
    """This company's own number / forwarding / greeting, or ``None``.

    ``None`` means the company has no telephony identity and NOTHING may be
    sent on its behalf. That is the outbound mirror of
    ``resolve_company_for_number`` declining: sending from another company's
    number puts our text in their conversation AND routes the reply to them.

    ⚠ Same single-tenant escape hatch, same one state: when the mapping table
    is EMPTY the deployment-global settings apply, because a deployment with
    no mapped numbers has exactly one company's worth of telephony config.
    One row anywhere and the global values stop being consulted — a
    per-company fallback to a shared ``from_number`` is the bidirectional leak
    this exists to close.
    """
    if not company_id:
        return None
    messaging_service = (getattr(twilio_settings, "messaging_service_sid", "") or "").strip()
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(_TELEPHONY_FOR_COMPANY_SQL, company_id)
            any_mapping = None if row is not None else await conn.fetchrow(_ANY_MAPPING_SQL)
    except Exception:  # noqa: BLE001 — an unverifiable identity is no identity
        logger.warning(
            "telephony lookup failed for company=%s — refusing to send", company_id, exc_info=True
        )
        return None

    if row is not None:
        return CompanyTelephony(
            company_id=company_id,
            from_number=(row["phone_number"] or "").strip(),
            # ⚠ A Messaging Service is an ACCOUNT-level pool, so it stays
            # deployment config. It is only usable while each company also has
            # its own number, because Twilio picks the sender from the pool —
            # which is exactly the shared-sender problem. Kept for the
            # single-number deployment and deliberately not preferred here.
            messaging_service_sid="",
            forward_to_number=(row["forward_to_number"] or "").strip(),
            voice_greeting=(row["voice_greeting"] or "").strip(),
        )

    if any_mapping is not None:
        logger.warning(
            "company=%s has no phone number of its own and other companies do — "
            "refusing to send from someone else's number",
            company_id,
        )
        return None

    # Nothing mapped anywhere: single-tenant, legacy config applies.
    return CompanyTelephony(
        company_id=company_id,
        from_number=(getattr(twilio_settings, "from_number", "") or "").strip(),
        messaging_service_sid=messaging_service,
        forward_to_number=(getattr(twilio_settings, "forward_to_number", "") or "").strip(),
        voice_greeting=(getattr(twilio_settings, "voice_greeting", "") or "").strip(),
    )


__all__ = ["CompanyTelephony", "resolve_company_for_number", "telephony_for_company"]
