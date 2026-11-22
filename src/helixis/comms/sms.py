"""Outbound customer-care SMS — templated, consent-gated, quiet-hours-bounded.

This is the A2P 10DLC texting channel (rent reminders, maintenance updates,
appointment confirmations, account notices) registered under Occupella's
customer-care/account-notification campaign. It ships DARK
(``HELIXIS_TWILIO__ENABLED`` defaults to ``False``) and lights up only when
carriers approve the campaign and a deploy flips the flag.

Compliance invariants (each one is load-bearing — do not relax without a
compliance review):

1. **Consent ledger required.** A phone number existing in Buildium is NOT
   consent. TCPA requires the recipient's prior express consent; a live row
   in ``helixis_sms_consent`` (migration 0068, ``revoked_at`` null) is that
   consent, and :func:`send_sms` FAILS CLOSED — no row, a revoked row, or any
   lookup error all block the send (same posture as ``_require_manager_role``
   in ``agents/buildium_tools.py``).
2. **Templates only.** Every send renders one of the four :data:`TEMPLATES`
   registered as the campaign's sample messages. There is deliberately NO
   free-form message path anywhere in this module — that is a compliance
   guarantee (the campaign registration covers exactly these message shapes),
   not a convenience shortcut.
3. **Quiet hours.** Sends are blocked outside the RECIPIENT-local calling
   window, which ``comms/local_time.window_for`` owns for the whole product.
   Federal is 8am-9pm (47 CFR § 64.1200(c)(1)) and is a FLOOR, not the answer:
   Florida, Oklahoma and Washington end at 8pm and Texas starts at 9am (noon
   Sunday). The zone comes from the consent row, the STATE from the number.
4. **Dark by default.** ``settings.twilio.enabled`` is ``False`` until A2P
   approval; every send raises :class:`SmsBlocked` (``reason='disabled'``)
   before touching the network.
5. **STOP handling.** Twilio Advanced Opt-Out blocks STOP'd numbers
   carrier-side, AND the inbound webhook (``api/routes/twilio.py``,
   ``POST /api/v1/twilio/inbound``) mirrors every STOP into the ledger
   (``revoked_at`` + ``revocation_source='twilio_stop_webhook'``) across
   ALL companies holding that number — so a STOP'd number is blocked by
   both Twilio and our own consent gate.

Every accepted send is also recorded in ``helixis_sms_log`` (migration
0069) BEST-EFFORT — a logging failure warns and never fails the send (the
message already left). Outbound log rows carry ``template_id`` but NEVER
the rendered body (reconstructible from the template; storing it would
duplicate PII at rest). Delivery receipts (``POST /api/v1/twilio/status``,
requested via ``StatusCallback`` when ``twilio.webhook_base_url`` is set)
advance each row's status.

Process logging never includes message bodies, and phone numbers are
redacted to their last four digits.
"""

from __future__ import annotations

import logging
import re
import string
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

import httpx

from helixis.comms import a2p as a2p_status
from helixis.comms import local_time
from helixis.comms.local_time import is_nanp, within_quiet_hours
from helixis.comms.opt_out import UNSUPPORTED_REGION

logger = logging.getLogger(__name__)

_TWILIO_API_BASE = "https://api.twilio.com/2010-04-01"
_SEND_TIMEOUT_S = 15.0

# TCPA calling-hours window, recipient-local (47 CFR § 64.1200(c)(1)).
# ⚠ The window MOVED to comms/local_time (window_for): it is federal-or-
# stricter and varies by recipient state, so it cannot be a pair of constants
# here. These aliases remain only so existing readers land on the real rule.
_QUIET_HOURS_START = local_time.QUIET_HOURS_START  # federal FLOOR, not the answer
_QUIET_HOURS_END = local_time.QUIET_HOURS_END

# The four registered campaign message shapes. Named {placeholders} only —
# render_template() enforces the exact set per template. Each begins
# "Occupella: " and carries the STOP opt-out language; rent_reminder and
# account_notice (the recurring/account classes) also carry HELP.
TEMPLATES: dict[str, str] = {
    "rent_reminder": (
        "Occupella: Hi {first_name}, your rent payment of {amount} for "
        "{property_name} is due on {due_date}. "
        "Reply STOP to cancel, HELP for help."
    ),
    "maintenance_update": (
        "Occupella: Update on your maintenance request at {property_name}: "
        "{status_update} Reply STOP to cancel."
    ),
    "appointment_confirmation": (
        "Occupella: Your {purpose} appointment at {property_name} is "
        "confirmed for {date_time}. Reply STOP to cancel."
    ),
    "account_notice": (
        "Occupella: {notice} Questions? Contact your property manager. "
        "Reply STOP to cancel, HELP for help."
    ),
}


class SmsBlocked(Exception):  # noqa: N818 — "blocked" is the semantics, not an error suffix
    """A send was refused by a guardrail BEFORE any network call.

    ``reason`` is a stable machine-readable code:

    - ``'disabled'`` — the feature flag is off (dark until A2P approval).
    - ``'not_configured'`` — flag on but Twilio creds/sender missing.
    - ``'no_consent'`` — no ledger row for (company, phone), or the lookup
      failed (fail closed: unverifiable consent is no consent).
    - ``'revoked'`` — the ledger row has ``revoked_at`` set.
    - ``'quiet_hours'`` — outside the recipient-local calling window.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        # Kept, not just formatted into the message: `agents/sms_tools` renders
        # it so a refusal quotes the window that was actually applied. It was
        # accepted and dropped, so `blocked.detail` raised AttributeError.
        self.detail = detail
        super().__init__(f"SMS blocked ({reason}){': ' + detail if detail else ''}")


class SmsSendError(RuntimeError):
    """Twilio rejected or failed the send (non-2xx / network). The message
    includes Twilio's error code + a body snippet, NEVER the auth token."""


@dataclass(frozen=True)
class SmsResult:
    """A successfully accepted outbound message."""

    sid: str
    to_phone: str
    template_id: str
    status: str  # Twilio's message status at accept time (e.g. "queued")


def redact_phone(phone: str) -> str:
    """Last-4 redaction for logs: ``+15551234567`` → ``***-**4567``."""
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    return f"***-**{digits[-4:]}" if len(digits) >= 4 else "***-****"


def _placeholders(template: str) -> set[str]:
    """The named placeholders in a format string (rejects positional ones)."""
    names: set[str] = set()
    for _, field, _, _ in string.Formatter().parse(template):
        if field is None:
            continue
        if field == "" or field[0].isdigit():
            raise ValueError("templates must use named {placeholders} only")
        names.add(field)
    return names


#: Placeholders that accept PROSE rather than a datum. These are the free-form
#: blanks — everything else is a name, an amount or a date.
#:
#: ⚠ This module's docstring used to claim "there is deliberately NO free-form
#: message path", and `render_template` claimed templates-only was "a compliance
#: guarantee". Both were FALSE for these two: `render_template` enforced the SET
#: of placeholders and never the CONTENT of a value, and the agent tool takes an
#: arbitrary ``params_json``. So `account_notice` with {"notice": "<anything>"}
#: sent arbitrary text from the A2P-registered number. The confirm gate means a
#: human approved it, which is a real mitigation — but a carrier auditing sent
#: messages against the registered samples is not checking whether somebody
#: approved it.
_FREE_TEXT_PLACEHOLDERS: Final = frozenset({"notice", "status_update"})

#: The bound that matters is the RENDERED message, not any one parameter: the
#: boilerplate differs per template, so a per-param number is a guess that gets
#: it wrong in both directions. Two segments, matching what the manager digest
#: has always budgeted — my first cut invented 220 chars per param and
#: immediately broke the digest, which truncates its notice to 230.
_MAX_RENDERED = 320

#: A per-param sanity ceiling, far above any real value, so an absurd input
#: fails with a clear message instead of via the rendered-length check.
_MAX_PARAM = 1000

#: Opt-out machinery the TEMPLATE already appends. A blank that reproduces or
#: contradicts it is the harm worth blocking: "reply START to keep receiving"
#: inside a notice actively works against the footer two clauses later, and a
#: duplicated "Reply STOP" reads as a second, differently-scoped opt-out.
_OPT_OUT_FORGERY = re.compile(
    r"\b(reply|text|send)\s+(stop|start|unstop|yes|no|help|unsubscribe)\b"
    r"|\bunsubscribe\b|\bopt[\s-]?(in|out)\b",
    re.IGNORECASE,
)


def _reject_unusable_value(template_id: str, name: str, value: str) -> None:
    """Raise ``ValueError`` if a placeholder value cannot lawfully be sent.

    Deliberately NOT a content classifier. There is no reliable way to decide
    "is this marketing", and pretending otherwise would be a guarantee we cannot
    keep — the same overstatement this function exists to correct. What it does
    enforce is what the code CAN: the message stays the registered shape, one
    readable size, and carries exactly one set of opt-out instructions.
    """
    if "\n" in value or "\r" in value or any(ord(c) < 32 for c in value):
        raise ValueError(
            f"template {template_id!r} param {name!r} contains a line break or control "
            "character — a message body is one line, and extra lines can forge a "
            "second message"
        )
    if len(value) > _MAX_PARAM:
        raise ValueError(
            f"template {template_id!r} param {name!r} is {len(value)} chars — far past "
            "anything that belongs in a text message"
        )
    if name in _FREE_TEXT_PLACEHOLDERS and _OPT_OUT_FORGERY.search(value):
        raise ValueError(
            f"template {template_id!r} param {name!r} contains its own opt-out or "
            "opt-in instructions. The template already appends the registered "
            "STOP/HELP footer; a second set inside the body conflicts with it"
        )


def render_template(template_id: str, params: dict[str, str]) -> str:
    """Render one of the registered campaign templates. STRICT:

    - an unknown ``template_id`` raises ``ValueError``;
    - a missing or extra param raises ``ValueError`` (the rendered text must
      be exactly the registered shape — nothing dropped, nothing smuggled in).

    This is the ONLY way this module produces a message body: the SHAPE is
    always one of the registered templates and the STOP/HELP footer is always
    appended.

    ⚠ It is NOT true that "no free-form path exists" — that claim used to be
    here and was wrong. ``account_notice``'s {notice} and
    ``maintenance_update``'s {status_update} are prose blanks, and the agent
    tool fills them from an arbitrary ``params_json``. ``_reject_unusable_value``
    bounds what can go in one; it cannot decide whether the prose belongs to
    the registered use case, and a human approving the confirm gate is what
    actually stands behind that.
    """
    template = TEMPLATES.get(template_id)
    if template is None:
        raise ValueError(f"unknown SMS template {template_id!r} — registered: {sorted(TEMPLATES)}")
    expected = _placeholders(template)
    provided = set(params)
    missing = expected - provided
    extra = provided - expected
    if missing:
        raise ValueError(f"template {template_id!r} missing params: {sorted(missing)}")
    if extra:
        raise ValueError(f"template {template_id!r} got unexpected params: {sorted(extra)}")
    for name, value in sorted(params.items()):
        _reject_unusable_value(template_id, name, value)
    body = template.format(**params)
    if len(body) > _MAX_RENDERED:
        raise ValueError(
            f"template {template_id!r} renders to {len(body)} chars, over the "
            f"{_MAX_RENDERED} allowed (two SMS segments) — shorten the parameters"
        )
    return body


def _within_quiet_hours_window(now_utc: datetime, tz_name: str, to_phone: str = "") -> bool:
    """May we text this consented recipient right now, recipient-local.

    ⚠ **Delegates to ``local_time`` — this module no longer owns the window.**
    Two copies of one statute drift, and the drift is silent until somebody
    sues (gotcha 25). The federal 8am-9pm this function used to hardcode is a
    FLOOR: Florida, Oklahoma and Washington end at 8pm, and Texas starts at
    9am (noon Sunday). Shipping the federal pair alone is a per-message
    penalty in four states.

    The consent row's stored zone WINS over the area code — it is the
    recipient's actual zone rather than an inference — while the STATE, which
    selects the statute, still comes from the number. An unknown/invalid
    timezone FAILS CLOSED: "can't tell the recipient's local time" must never
    mean "send anyway".
    """
    return within_quiet_hours(to_phone, now_utc, zone=tz_name)


async def _consent_row(pool: Any, company_id: str, to_phone: str) -> Any:
    """The consent row for (company, phone), or ``None``. Raises on lookup
    failure — the caller maps that to a fail-closed ``no_consent`` block."""
    return await pool.fetchrow(
        "SELECT revoked_at, timezone FROM public.helixis_sms_consent "
        "WHERE company_id = $1::uuid AND phone = $2",
        company_id,
        to_phone,
    )


#: Why outbound SMS may not leave this deployment at all, keyed by the reason
#: string ``SmsBlocked`` already uses. Public so every outbound lane shares ONE
#: definition of "is this channel legally allowed to send" (gotcha 25's rule:
#: a rule that matters is written once and read by every layer).
def outbound_channel_block(twilio: Any) -> str | None:
    """``'disabled'`` / ``'not_configured'`` / ``None`` if the channel may send.

    ⚠ **This is not specific to the templated customer-care lane.** The A2P
    10DLC registration covers EVERY application-to-person message sent from
    the registered number, and the CRM's lead replies go out from the same
    ``from_number``. So ``enabled`` — "carriers have approved the campaign" —
    is a property of the CHANNEL, not of one module that happens to own the
    flag. The settings docstring scoped it to ``comms/sms.py``; the CRM reply
    path checked only ``configured`` and would therefore have started texting
    leads the moment credentials landed in Render, before approval. That is
    precisely the violation the dark flag exists to prevent.

    Consent and quiet hours are deliberately NOT here: they are per-RECIPIENT
    and per-MESSAGE, and ``send_sms`` applies them below. This function
    answers only the channel-level question, which is the one a UI can ask
    before a recipient is even chosen.
    """
    if not twilio.enabled:
        return "disabled"
    if not twilio.account_sid or not twilio.auth_token:
        return "not_configured"
    # ⚠ The SENDER is deliberately NOT checked here any more. Since migration
    # 0082/0083 each company sends from its OWN number, so "is there a sender"
    # is a per-company question answered by ``number_routing.
    # telephony_for_company`` at send time. Requiring a deployment-global
    # ``from_number`` here would block every company on a multi-tenant install
    # that correctly has none — and accepting one would be the shared-sender
    # leak. This function answers only the ACCOUNT-level question.
    return None


_CHANNEL_BLOCK_DETAIL = {
    "disabled": "outbound SMS is dark until A2P approval",
    "not_configured": (
        "missing account_sid/auth_token or a sender (from_number / messaging_service_sid)"
    ),
    "campaign_pending": (
        "this company's own A2P 10DLC campaign has not been approved by the carriers yet"
    ),
}

#: The one status that lets a registered company send.
#: Sourced from the status machine, never re-typed — see a2p.APPROVED.
_APPROVED = a2p_status.APPROVED

_COMPANY_CAMPAIGN_SQL = """
SELECT status
  FROM public.helixis_a2p_registrations
 WHERE company_id = $1::uuid
"""


async def outbound_block_for_company(pool: Any, company_id: str, twilio: Any) -> str | None:
    """Channel-level block, THEN this company's own campaign. ``None`` to send.

    ⚠ **A2P APPROVAL IS PER COMPANY AND ``twilio.enabled`` IS NOT.** That flag
    means "carriers approved the campaign" — singular — because it was written
    when there was one. Under white-label registration each customer gets their
    OWN brand and campaign, approved on their own date, so a single global flag
    is wrong in both directions: leave it off and an approved customer cannot
    text, turn it on and every customer with a still-pending campaign starts
    sending. The second is the violation carriers audit for, and it is the one
    a global flag produces by default.

    The rule:

    * **No registration row → the global flag alone decides.** That is the
      single-tenant path: an operator who registered directly with Twilio has
      no row and must not be blocked by a table built for the white-label
      flow. It cannot be used as a bypass, because the only way a company gets
      telephony without registering is the operator configuring it by hand —
      and a company provisioned through ``ensure_number`` ALWAYS has a row, as
      that step only runs on an approved one.
    * **A row → it must say ``approved``.** Pending, failed, in-review: no.
    * **Unreadable → refuse.** Same posture as the consent ledger and the role
      floor: unverifiable is invalid. A compliance gate that fails open on a
      database blip is not a gate.

    ⚠ The account-level check runs FIRST. On a dark deployment the honest
    message is "texting is not enabled", not "your campaign is pending" —
    telling a customer to wait on the carriers when the operator has not turned
    the channel on sends them to wait for something that will never arrive.
    Same ordering reason as ``outbound_channel_block``'s dark-before-
    unconfigured.
    """
    channel = outbound_channel_block(twilio)
    if channel is not None:
        return channel
    if not company_id:
        return "campaign_pending"
    # ⚠ ``pool.fetchrow``, not ``pool.acquire()``. Every other read in this
    # module goes straight to the pool, and the odd one out made each caller's
    # fixture have to model TWO shapes — which is not a cosmetic problem: a
    # fixture that only knows one of them raises, this fails closed, and every
    # send in that suite is refused for a reason unrelated to what it tests.
    try:
        row = await pool.fetchrow(_COMPANY_CAMPAIGN_SQL, company_id)
    except Exception:  # noqa: BLE001 — an unverifiable campaign is no campaign
        logger.warning(
            "a2p campaign lookup failed for company=%s — refusing to send",
            company_id,
            exc_info=True,
        )
        return "campaign_pending"
    if row is None:
        # Never registered through the white-label flow — the single-tenant
        # deployment, already gated by the account-level check above.
        return None
    return None if (row["status"] or "") == _APPROVED else "campaign_pending"


async def send_sms(
    *,
    settings: Any,
    pool: Any,
    company_id: str,
    to_phone: str,
    template_id: str,
    params: dict[str, str],
    now: datetime | None = None,
) -> SmsResult:
    """Send one templated, consent-gated SMS. See the module docstring for
    the compliance invariants; the guard order is:

    1. feature flag (``SmsBlocked('disabled')``) then config
       (``'not_configured'``) — both before any DB or network I/O, then a
       reachable region (``'unsupported_region'``);
    2. consent ledger (``'no_consent'`` / ``'revoked'``, FAIL CLOSED on any
       lookup error);
    3. quiet hours in the consent row's timezone (``'quiet_hours'``);
    4. strict template render (``ValueError``) then the Twilio REST POST.

    ``now`` is injectable for tests; defaults to the current UTC time.
    Raises :class:`SmsSendError` on a non-2xx Twilio response — the message
    carries Twilio's error code but never the auth token.
    """
    from helixis.comms.number_routing import telephony_for_company

    twilio = settings.twilio
    log_phone = redact_phone(to_phone)

    # 1. Dark-by-default flag, then configuration — both from the shared
    # channel predicate, so the CRM's lead-reply lane and this one can never
    # drift into disagreeing about whether the channel may send at all.
    # ⚠ Account-level AND this company's own campaign. ``twilio.enabled`` is
    # one flag for the whole deployment; A2P approval is per company, so a
    # customer whose campaign is still with the carriers must not send just
    # because a different customer's was approved.
    channel_block = await outbound_block_for_company(pool, company_id, twilio)
    if channel_block is not None:
        logger.info(
            "sms blocked reason=%s company=%s to=%s template=%s",
            channel_block,
            company_id,
            log_phone,
            template_id,
        )
        raise SmsBlocked(channel_block, _CHANNEL_BLOCK_DETAIL[channel_block])

    # 1b. THIS COMPANY's own sender. Every company texts from its own number so
    # that the recipient's reply comes back to a number inbound routing maps to
    # the same company. A shared sender is a bidirectional leak: our message
    # lands in another company's conversation AND their reply lands in ours.
    # No number for this company → refuse; sending from somebody else's number
    # is worse than not sending.
    telephony = await telephony_for_company(pool, company_id, twilio_settings=twilio)
    if telephony is None or not telephony.can_send:
        logger.info(
            "sms blocked reason=not_configured (no company number) company=%s to=%s",
            company_id,
            log_phone,
        )
        raise SmsBlocked(
            "not_configured", "this company has no Twilio number of its own to send from"
        )
    sender_number = telephony.from_number
    messaging_service = telephony.messaging_service_sid

    # 1c. Somewhere we can actually text. The A2P 10DLC campaign is a US
    # programme — its registered samples, consent language and brand are all
    # US — and Twilio's geo permissions are off for other regions by default.
    # So a non-NANP number is not a recipient we are merely blocked from
    # texting right now; it is one this product cannot reach, and the honest
    # failure is here with a reason rather than a 21408 the sender never sees.
    #
    # ⚠ Before the consent lookup, deliberately: nobody should be told their
    # consent record is the problem when the number is.
    if not is_nanp(to_phone):
        logger.info(
            "sms blocked reason=unsupported_region company=%s to=%s template=%s",
            company_id,
            log_phone,
            template_id,
        )
        raise SmsBlocked(
            UNSUPPORTED_REGION,
            "this number is outside North America and the registered campaign covers the US only",
        )

    # 2. Consent ledger — FAIL CLOSED: any lookup error is treated as no
    # consent (same posture as _require_manager_role in buildium_tools.py).
    try:
        row = await _consent_row(pool, company_id, to_phone)
    except Exception:  # noqa: BLE001 — unverifiable consent is no consent
        logger.warning(
            "sms blocked reason=no_consent (ledger lookup failed) to=%s template=%s",
            log_phone,
            template_id,
            exc_info=True,
        )
        raise SmsBlocked("no_consent", "consent lookup failed — failing closed") from None
    if row is None:
        logger.info("sms blocked reason=no_consent to=%s template=%s", log_phone, template_id)
        raise SmsBlocked("no_consent", "no consent ledger row for this number")
    if row["revoked_at"] is not None:
        logger.info("sms blocked reason=revoked to=%s template=%s", log_phone, template_id)
        raise SmsBlocked("revoked", "consent was revoked for this number")

    # 3. Quiet hours, recipient-local. The window is federal-or-stricter and
    # depends on the recipient's STATE, so the phone goes in too — see
    # comms/local_time.window_for.
    now_utc = now if now is not None else datetime.now(UTC)
    if not _within_quiet_hours_window(now_utc, row["timezone"], to_phone):
        logger.info("sms blocked reason=quiet_hours to=%s template=%s", log_phone, template_id)
        # The DETAIL carries the window that was actually applied, so a caller
        # rendering a refusal quotes the statute we enforced rather than a
        # constant of its own that drifts the next time a state rule changes.
        window = local_time.describe_window(to_phone, now_utc, zone=row["timezone"])
        raise SmsBlocked(
            "quiet_hours",
            f"outside the recipient-local calling window ({window})",
        )

    # 4. Strict render, then the Twilio REST POST (direct httpx, Basic auth —
    # the repo style, see memory/voyage_embedder.py; no twilio SDK dep).
    body = render_template(template_id, params)
    form: dict[str, str] = {"To": to_phone, "Body": body}
    if messaging_service:
        form["MessagingServiceSid"] = messaging_service
    else:
        form["From"] = sender_number
    # Delivery receipts: when the public backend origin is configured, ask
    # Twilio to POST status transitions to /api/v1/twilio/status (the
    # receipt endpoint verifies X-Twilio-Signature against this same base —
    # see TwilioSettings.webhook_base_url).
    webhook_base = (getattr(twilio, "webhook_base_url", "") or "").strip().rstrip("/")
    if webhook_base:
        form["StatusCallback"] = f"{webhook_base}/api/v1/twilio/status"
    endpoint = f"{_TWILIO_API_BASE}/Accounts/{twilio.account_sid}/Messages.json"

    async with httpx.AsyncClient(timeout=_SEND_TIMEOUT_S) as client:
        resp = await client.post(endpoint, data=form, auth=(twilio.account_sid, twilio.auth_token))

    if resp.status_code < 200 or resp.status_code >= 300:
        code: Any = None
        try:
            code = resp.json().get("code")
        except Exception:  # noqa: BLE001 — non-JSON error body
            pass
        logger.warning(
            "sms send failed http=%s twilio_code=%s to=%s template=%s",
            resp.status_code,
            code,
            log_phone,
            template_id,
        )
        # Body snippet for diagnosis — Twilio error bodies carry {code,
        # message}, never our credentials, and we never echo the auth token.
        raise SmsSendError(
            f"Twilio returned HTTP {resp.status_code} (error code {code}): {resp.text[:300]}"
        )

    payload: dict[str, Any] = {}
    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001 — a 2xx with an unparseable body still sent
        logger.warning("sms sent but Twilio response JSON was unparseable")
    sid = str(payload.get("sid") or "")
    status = str(payload.get("status") or "queued")
    logger.info("sms sent sid=%s status=%s to=%s template=%s", sid, status, log_phone, template_id)

    # Message log (helixis_sms_log, migration 0069) — BEST-EFFORT: the
    # message already left Twilio's queue, so a logging failure must warn,
    # never fail the send. Outbound rows carry template_id but NEVER the
    # rendered body (reconstructible from the template — storing it would
    # duplicate PII at rest). Delivery receipts advance status by sid.
    try:
        await pool.execute(
            "INSERT INTO public.helixis_sms_log "
            "(company_id, direction, phone, template_id, twilio_sid, status) "
            "VALUES ($1::uuid, 'outbound', $2, $3, $4, 'queued')",
            company_id,
            to_phone,
            template_id,
            sid or None,
        )
    except Exception:  # noqa: BLE001 — logging is best-effort, the send stands
        logger.warning(
            "sms log insert failed (send already accepted) sid=%s to=%s template=%s",
            sid,
            log_phone,
            template_id,
            exc_info=True,
        )
    return SmsResult(sid=sid, to_phone=to_phone, template_id=template_id, status=status)
