"""Agent-facing outbound SMS — SMS Stage 4, the highest-sensitivity comms write.

One tool, ``send_text_message``, lets the agent send a TEMPLATED text to a
recipient with LIVE consent — and nothing else. It layers the chat-write guard
stack on top of the Stage-1 compliance guards in :mod:`helixis.comms.sms`, in
this exact order (each gate is load-bearing — do not reorder without reading
the SMS block in CLAUDE.md §4):

1. **Dark check.** ``settings.twilio.enabled`` is ``False`` until A2P carrier
   approval; the tool says texting isn't enabled BEFORE any consent lookup or
   confirm gate, so the user is never asked to approve a send that cannot
   happen.
2. **Consent-ledger resolution (fail closed).** ``recipient`` (a display name
   or an E.164 phone) resolves ONLY against the caller's company's LIVE
   ``helixis_sms_consent`` rows. No live row, a revoked row, or ANY lookup
   failure → refuse; an ambiguous name → an honest "which one?" list, never a
   guess. A phone number existing in Buildium is NOT consent — the ledger is.
3. **Strict template render.** One of the four registered campaign templates
   (:data:`helixis.comms.sms.TEMPLATES`), rendered by ``render_template``
   which rejects unknown ids and missing/extra params — there is NO free-form
   message path, even through the agent.
4. **Confirm gate.** The human sees the EXACT rendered message text, the
   recipient's display name + last-4 of phone, and the template id, and must
   approve (``confirm_or_abort`` via ``run_guarded`` — the emitter-safe
   pattern, gotcha 4). The full phone number never appears in the gate
   payload.
5. **Idempotency claim.** ``run_guarded`` claims a durable key before
   executing, so a double-approval / retry cannot double-send.
6. **``send_sms`` re-guards.** The Stage-1 primitive re-runs the kill-switch,
   consent (fail closed), quiet-hours, and templates-only checks internally;
   an :class:`SmsBlocked` surfaces as an honest reply naming the reason.

Never used for fair-housing-relevant targeting. Registered on the Buildium
agent via :func:`sms_tools` (tenant/owner/vendor comms live in that domain) —
deliberately NOT inside ``buildium_tools()``, whose write surface the 32x
negative golden cases pin by name (see
``tests/unit/test_negative_should_not_act.py``). The reviewed-write-surface
pin lives in ``tests/unit/test_release_safety_gates.py``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from agents import function_tool

from helixis.agents.idempotency import (
    SUCCESS_OUTCOMES,
    PreSideEffectError,
    derive_key,
    pending_reply,
    run_guarded,
)
from helixis.comms.phone import normalize_e164
from helixis.comms.sms import (
    TEMPLATES,
    SmsBlocked,
    SmsSendError,
    redact_phone,
    render_template,
    send_sms,
)
from helixis.memory.context import current_memory_context

logger = logging.getLogger(__name__)

_CONSENT_HINT = (
    "Texting consent can be added in Settings → Connectors → Twilio SMS — "
    "a phone number in Buildium is not consent."
)

_DARK_REPLY = (
    "Text messaging isn't enabled yet — the SMS channel is dark until the "
    "carrier (A2P) campaign is approved, so I can't send texts. Nothing was "
    "sent and there is nothing to approve."
)


def _settings() -> Any:
    """Indirected so unit tests can patch the settings the tool sees."""
    from helixis.settings import get_settings

    return get_settings()


def _pool() -> Any | None:
    """The app DB pool, or ``None`` when unavailable (tests / degraded boot).
    A ``None`` pool means consent can't be verified → the tool FAILS CLOSED."""
    try:
        from helixis.db.engine import get_pool

        return get_pool()
    except Exception:  # noqa: BLE001 — pool not initialized → fail closed upstream
        return None


@dataclass(frozen=True)
class _Recipient:
    """One resolved live-consent recipient."""

    phone: str  # full E.164 — used ONLY for the send; never surfaced in the gate
    display_name: str
    timezone: str


class _SmsBlockedInFlight(PreSideEffectError):  # noqa: N818 — "blocked" is the semantics (see SmsBlocked)
    """``send_sms`` refused inside the guarded execute. ``SmsBlocked`` is
    raised BEFORE any network call, so the failure is provably pre-side-effect
    — mapping it to ``failed_retryable`` lets a later attempt (e.g. after
    quiet hours end) re-confirm and retry instead of being blocked for review."""

    def __init__(self, blocked: SmsBlocked) -> None:
        self.blocked = blocked
        super().__init__(str(blocked))


async def _resolve_recipient(
    pool: Any, company_id: str, recipient: str
) -> tuple[_Recipient | None, str | None]:
    """Resolve ``recipient`` against the company's consent ledger.

    Returns ``(recipient, None)`` on exactly one LIVE match, else
    ``(None, honest_user_facing_message)``. Live rows only; ambiguous names
    return a "which one?" list; any lookup error fails closed (unverifiable
    consent is no consent — same posture as ``send_sms`` itself).
    """
    text = (recipient or "").strip()
    if not text:
        return None, (
            "Who should I text? Give me a name from the texting-consent list or their phone number."
        )

    phone = normalize_e164(text)
    try:
        if phone is not None:
            rows = await pool.fetch(
                "SELECT phone, display_name, recipient_kind, timezone, revoked_at "
                "FROM public.helixis_sms_consent "
                "WHERE company_id = $1::uuid AND phone = $2",
                company_id,
                phone,
            )
        else:
            rows = await pool.fetch(
                "SELECT phone, display_name, recipient_kind, timezone, revoked_at "
                "FROM public.helixis_sms_consent "
                "WHERE company_id = $1::uuid AND lower(display_name) = lower($2)",
                company_id,
                text,
            )
    except Exception:  # noqa: BLE001 — unverifiable consent is no consent (fail closed)
        logger.warning(
            "send_text_message: consent-ledger lookup failed — failing closed", exc_info=True
        )
        return None, (
            "I couldn't verify texting consent (the consent-ledger lookup failed), "
            "so I didn't send anything. Please try again shortly."
        )

    live = [r for r in rows if r["revoked_at"] is None]
    if len(live) == 1:
        row = live[0]
        display = (row["display_name"] or "").strip() or redact_phone(row["phone"])
        return _Recipient(phone=row["phone"], display_name=display, timezone=row["timezone"]), None
    if len(live) > 1:
        options = "; ".join(
            f"{r['display_name'] or 'unnamed'} ({redact_phone(r['phone'])}, {r['recipient_kind']})"
            for r in live
        )
        return None, (
            f"I found {len(live)} people matching {text!r} with live texting consent — "
            f"which one did you mean? {options}. Tell me which to text and I'll show "
            "you the exact message to approve."
        )
    if rows:  # rows exist but every one is revoked
        return None, (
            f"Texting consent for {text!r} was revoked (they may have replied STOP, or it "
            f"was revoked in Settings), so I can't text them. {_CONSENT_HINT}"
        )
    return None, (
        f"There's no texting consent on file for {text!r}, so I can't send them a text. "
        f"{_CONSENT_HINT}"
    )


def _parse_params(params_json: str) -> tuple[dict[str, str] | None, str | None]:
    """Parse the tool's params argument into a flat ``dict[str, str]``, or an
    honest error message. Scalars are stringified; nested values are refused
    (the strict render would misformat them silently otherwise)."""
    raw_text = (params_json or "").strip()
    try:
        raw = json.loads(raw_text) if raw_text else {}
    except json.JSONDecodeError:
        return None, (
            "The template params must be a JSON object of string values, like "
            '{"notice": "..."} — I couldn\'t parse what was given, so nothing was sent.'
        )
    if not isinstance(raw, dict):
        return None, (
            "The template params must be a JSON object (not a list or bare value), "
            "so nothing was sent."
        )
    params: dict[str, str] = {}
    for key, value in raw.items():
        if isinstance(value, dict | list):
            return None, (
                f"Template param {key!r} must be a plain string, not a nested "
                "object or list — nothing was sent."
            )
        params[str(key)] = value if isinstance(value, str) else str(value)
    return params, None


def _local_time(tz_name: str) -> str | None:
    """The recipient's current wall-clock time, for the quiet-hours reply.
    Best-effort — an unparseable zone just drops the clock from the message."""
    try:
        local = datetime.now(UTC).astimezone(ZoneInfo(tz_name))
    except Exception:  # noqa: BLE001 — cosmetic only; send_sms already failed closed
        return None
    return local.strftime("%I:%M %p").lstrip("0")


def _blocked_reply(blocked: SmsBlocked, *, display: str, tz_name: str) -> str:
    """An honest, reason-naming reply for a guard-blocked send. Nothing went
    out on any of these paths — ``SmsBlocked`` fires before the network call."""
    reason = blocked.reason
    if reason == "quiet_hours":
        clock = _local_time(tz_name)
        now_part = f"it's {clock} for them right now, " if clock else ""
        # ⚠ The window comes from the BLOCK, not from a constant here. This
        # used to hardcode "8:00 AM-9:00 PM" — the pre-2026-09-01 federal
        # default — and kept saying it after comms/local_time made the window
        # state-aware, so a Texan refused at 08:30 was quoted 8:00 and a
        # Floridian refused at 20:30 was quoted 9:00.
        window = blocked.detail or "the recipient-local calling window"
        return (
            f"I didn't send the text — {now_part}{window} in "
            f"{display}'s local time (TCPA quiet hours). Try again during their daytime."
        )
    if reason in ("no_consent", "revoked"):
        state = "was revoked" if reason == "revoked" else "isn't on file"
        return (
            f"I didn't send the text — texting consent for {display} {state} "
            f"(it may have just changed). {_CONSENT_HINT}"
        )
    if reason == "disabled":
        return _DARK_REPLY
    if reason == "not_configured":
        return (
            "I didn't send the text — the Twilio sender or credentials aren't configured "
            "yet, so the channel can't send. Nothing went out."
        )
    return f"I didn't send the text — it was blocked ({reason}). Nothing went out."


async def _send_text_message_impl(recipient: str, template_id: str, params_json: str) -> str:
    """Backs the ``send_text_message`` tool. Module-level so tests (and the
    eval harness, if ever needed) can patch it — the ``@function_tool``
    wrapper below only carries the LLM-facing docstring."""
    settings = _settings()
    twilio = getattr(settings, "twilio", None)
    # 0. Dark state — BEFORE any consent lookup and BEFORE the confirm gate,
    # so the user is never asked to approve a send that cannot happen.
    if twilio is None or not getattr(twilio, "enabled", False):
        return _DARK_REPLY

    ctx = current_memory_context()
    company_id = ctx.company_id if ctx else ""
    if not company_id:
        return "I can't send texts in this session (no company scope), so nothing was sent."

    pool = _pool()
    if pool is None:
        return (
            "I couldn't reach the texting consent ledger, so I didn't send anything — "
            "consent must be verified before every text. Please try again shortly."
        )

    # 1. Consent-ledger resolution (live rows only, fail closed).
    resolved, problem = await _resolve_recipient(pool, company_id, recipient)
    if resolved is None:
        return problem or "I couldn't resolve that recipient, so nothing was sent."
    # Rebound to a non-Optional name so the nested execute closure below keeps
    # the narrowed type (mypy doesn't carry narrowing into closures).
    who: _Recipient = resolved

    # 2. Strict template render — this is what makes free-form impossible even
    # through the agent (registered campaign shapes only).
    params, params_problem = _parse_params(params_json)
    if params is None:
        return params_problem or "I couldn't read the template params, so nothing was sent."
    if template_id not in TEMPLATES:
        return (
            f"{template_id!r} isn't a registered SMS template — texting is templates-only "
            f"(a compliance guarantee). The registered templates are "
            f"{', '.join(sorted(TEMPLATES))}. Nothing was sent."
        )
    try:
        body = render_template(template_id, params)
    except ValueError as exc:
        return f"I couldn't render the {template_id!r} template — {exc}. Nothing was sent."

    # 3-5. Confirm gate (rendered text + name + last-4 + template id — the
    # FULL phone number deliberately never enters the gate payload), then the
    # idempotency claim, then the send. run_guarded owns the ordering; a
    # double-approval or retry replays instead of double-sending.
    redacted = redact_phone(who.phone)
    display = who.display_name
    details = (
        f"Send this text to {display} ({redacted}) using the {template_id!r} template. "
        f"Exact message: {body!r}"
    )
    items = [f"To: {display} ({redacted})", f"Template: {template_id}", f"Message: {body}"]
    key = derive_key(
        company_id,
        "send_text_message",
        {"phone": who.phone, "template_id": template_id, "params": params},
    )

    async def _do_send() -> str:
        # send_sms re-runs the kill-switch, consent (fail closed), quiet-hours,
        # and templates-only guards internally — the agent path adds gates, it
        # never removes any.
        try:
            result = await send_sms(
                settings=settings,
                pool=pool,
                company_id=company_id,
                to_phone=who.phone,
                template_id=template_id,
                params=params,
            )
        except SmsBlocked as blocked:
            raise _SmsBlockedInFlight(blocked) from blocked
        return f"Sent the text to {display} ({redacted}) — carrier status {result.status!r}."

    try:
        guard = await run_guarded(
            company_id=company_id,
            idem_key=key,
            action_type="send_text_message",
            confirm={
                "action": "send_text_message",
                "title": "Send this text message?",
                "details": details,
                "items": items,
                # Last-4 only — the raw E.164 stays out of the confirm payload.
                "request_body": {
                    "to": f"{display} ({redacted})",
                    "template_id": template_id,
                    "params": params,
                    "message": body,
                },
            },
            execute=_do_send,
            window=timedelta(minutes=10),
        )
    except _SmsBlockedInFlight as exc:
        return _blocked_reply(exc.blocked, display=display, tz_name=who.timezone)
    except SmsSendError:
        logger.warning("send_text_message: Twilio rejected/failed the send", exc_info=True)
        return (
            f"Twilio couldn't accept the text to {display}, so it was not delivered. "
            "I've flagged it for review — please don't resend until it's checked."
        )

    if guard.outcome == "not_approved":
        return "Okay — I didn't send the text. Nothing was sent."
    if guard.outcome == "replayed":
        return f"That exact text to {display} was already sent — I didn't send it again."
    if guard.outcome == "blocked_store_unavailable":
        return (
            "I couldn't safely process the send right now, so no text was sent. "
            "Please try again shortly."
        )
    if guard.outcome == "blocked_needs_review":
        return (
            "I attempted the send but couldn't confirm the outcome, so I've flagged it "
            f"for review — please don't resend to {display} until it's checked."
        )
    if guard.outcome not in SUCCESS_OUTCOMES:
        # "Already being processed" was false whenever nothing was running
        # (an earlier attempt failed before sending, or the claim was released
        # at the gate): the helper tells the two apart.
        return pending_reply(
            guard, action="send the text", system="the conversation", unchanged="no text was sent"
        )
    if guard.result is not None:
        return str(guard.result)
    return f"Sent the text to {display} ({redacted})."


@function_tool
async def send_text_message(recipient: str, template_id: str, params_json: str) -> str:
    """Send one templated SMS text message to a recipient who has LIVE texting
    consent on file (a comms write — consent-gated, templates-only, and
    confirm-gated with the exact rendered message shown for human approval).

    ``recipient`` is a display name from the company's texting-consent ledger
    (matched case-insensitively) or an E.164 phone number like +15551234567.
    Only people with a live consent row can be texted — a phone number in
    Buildium is NOT consent, and this tool refuses without a live row. If a
    name matches more than one consented person, the tool returns the list so
    the user can pick — never guess for them.

    ``template_id`` must be one of the four registered campaign templates
    (rent_reminder, maintenance_update, appointment_confirmation,
    account_notice). ``params_json`` is a JSON object supplying exactly the
    chosen template's placeholders as strings, for example
    '{"first_name": "Sarah", "amount": "$1,200", "property_name": "Garden
    Row", "due_date": "Sep 1"}' for rent_reminder. There is no free-form
    message path — texting is templates-only by compliance design.

    Safety pins that always hold — recipients come from the consent ledger
    only; registered templates only; a human approves the exact rendered text
    before anything sends; quiet hours (8am–9pm recipient-local) are
    enforced; and texting is never used for fair-housing-relevant targeting,
    steering, or screening of any kind. While the SMS channel is dark
    (awaiting carrier approval) the tool says so and nothing is sent.
    """
    return await _send_text_message_impl(recipient, template_id, params_json)


def sms_tools() -> list[Any]:
    """The agent-facing SMS tool set (SMS Stage 4).

    Kept OUT of ``buildium_tools()`` deliberately — that registry is the
    Buildium write surface the 32x negative golden cases pin by name; the SMS
    lane is pinned separately in ``tests/unit/test_release_safety_gates.py``
    (reviewed write surface + stress-net flag) and
    ``tests/unit/test_negative_should_not_act.py`` (registry separation).
    """
    return [send_text_message]


__all__ = ["send_text_message", "sms_tools"]
