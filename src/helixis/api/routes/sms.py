"""SMS consent management — Stage 3 of the outbound-SMS system.

The Settings → Connectors card's backend: JWT-authed, company-scoped CRUD
over the ``helixis_sms_consent`` ledger (migration 0068) plus a status
probe so the UI can show dark/pending-A2P vs live. Compliance posture
mirrors the ledger's design:

* **Consent rows are never hard-deleted.** DELETE revokes (``revoked_at``
  + ``revocation_source='app_settings'``) — the ledger must prove BOTH the
  grant and the revocation (same reason the STOP webhook only updates).
* **``source`` is server-set, always ``'app_settings'``.** The A2P
  campaign declares HOW each consent was collected; a client-supplied
  source would let the UI claim a collection flow that didn't happen.
* **Re-consent mirrors the START-webhook semantics.** POSTing a phone
  whose row was revoked re-consents it in place (``revoked_at`` cleared,
  ``consented_at`` refreshed, source updated) — one row per
  (company, phone), full history in the timestamps.
* **Phones are stored E.164 only** (``comms/phone.py`` normalizes;
  un-normalizable input 422s) and **timezones must be real IANA zones**
  (they drive the TCPA quiet-hours window — a junk zone would make
  ``send_sms`` fail closed forever, so reject it at write time).

Every endpoint operates ONLY on the caller's company (the standard
``company_ids[0]`` scoping used by the other JWT routes).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from helixis.api.auth import (
    AuthenticatedUser,
    get_current_user,
    require_unambiguous_company,
)
from helixis.api.roles import ADMIN, normalize_role, role_at_least
from helixis.comms import delivery_health
from helixis.comms.local_time import is_nanp
from helixis.comms.phone import normalize_e164
from helixis.comms.sms import outbound_block_for_company
from helixis.connectors import twilio_client
from helixis.db.engine import get_pool
from helixis.settings import Settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sms", tags=["sms"])

RecipientKind = Literal["manager", "tenant", "owner", "vendor"]

# The consent disclosure shown next to the add-consent checkbox — the four
# required elements (message type, frequency, rates, opt-out) with the
# verbatim "Reply STOP to cancel" phrase. SERVER-OWNED: this exact text is
# stamped into each row's evidence so the record can't drift from what a
# tampered client claims was shown. Must match CONSENT_LANGUAGE in
# frontend/src/components/connectors/SmsDetail.tsx — bump the version when
# either changes. Pinned in tests/unit/test_sms_routes.py.
_CONSENT_LANGUAGE = (
    "This person has agreed to receive account-notification text messages "
    "from Occupella. Message frequency varies, message and data rates may "
    "apply. Reply STOP to cancel, HELP for help."
)
_CONSENT_LANGUAGE_VERSION = 1


def _capture_evidence(request: Request, user: AuthenticatedUser) -> str:
    """The server-stamped proof of how this consent was captured.

    Produces the JSON for the ``evidence`` column (0070): who recorded it,
    the exact disclosure text in force, and best-effort request origin.
    This is the artifact an A2P vetting appeal (code 30555) and a TCPA
    defense both ask for — a contemporaneous record of the opt-in.
    """
    # Render sits behind a proxy — the client is the first X-Forwarded-For
    # hop when present, else the socket peer.
    fwd = request.headers.get("x-forwarded-for", "")
    ip = fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "")
    return json.dumps(
        {
            "method": "app_settings_form",
            "consent_language": _CONSENT_LANGUAGE,
            "language_version": _CONSENT_LANGUAGE_VERSION,
            "recorded_by": user.user_id,
            "recorded_by_email": user.email,
            "ip": ip,
            "user_agent": request.headers.get("user-agent", ""),
        }
    )


def _company_id(user: AuthenticatedUser) -> str:
    if not user.company_ids:
        raise HTTPException(status_code=403, detail="No company associated with this account.")
    return user.company_ids[0]


class ConsentRow(BaseModel):
    id: str
    phone: str
    display_name: str | None = None
    recipient_kind: str
    source: str
    timezone: str
    consented_at: datetime | None = None
    revoked_at: datetime | None = None
    #: The account that CLAIMED this number as theirs, or None for a row
    #: nobody has claimed. The panel renders the difference, because an
    #: unattributed staff number is one a departure cannot revoke — and a
    #: gap the product does not show is one nobody closes.
    owner_user_id: str | None = None
    #: How this number's MOST RECENT outbound message actually ended —
    #: ``delivered`` / ``undelivered`` / ``no_receipt`` / ``pending``, or
    #: ``None`` when nothing has been sent to it (or the log was unreadable).
    #:
    #: ⚠ ``None`` is "no messages sent", never "fine". The panel says so in
    #: those words: a number that has never been texted and a number whose
    #: texts all vanished must not read the same (gotchas 57/89).
    last_outcome: str | None = None
    #: Twilio's ErrorCode on that last message — 30006 is the landline case
    #: (checklist #89), which is the one a manager fixes by replacing the
    #: number rather than by re-sending.
    last_error_code: str | None = None


class ConsentListOut(BaseModel):
    consents: list[ConsentRow]


class ConsentIn(BaseModel):
    """The add-consent form. NOTE: no ``source`` field — the server always
    records ``'app_settings'`` (extra client fields are ignored by design)."""

    phone: str
    display_name: str | None = None
    recipient_kind: RecipientKind = "manager"
    timezone: str = "America/New_York"
    #: "This is MY phone." Records the caller as the row's owner, which is the
    #: only thing that lets a departure revoke it later (migration 0096).
    #:
    #: ⚠ Defaults FALSE and is never inferred. The obvious shortcut — treat
    #: whoever submitted the form as the owner — is wrong for every number
    #: recorded on somebody else's behalf, and acting on it would silence a
    #: colleague's alerts when the person who typed their number leaves.
    #: Unclaimed rows stay unattributed and the panel says so.
    is_own_number: bool = False


class SmsStatusOut(BaseModel):
    #: ⚠ **May THIS COMPANY send** — not the deployment flag it used to be.
    #:
    #: ``twilio.enabled`` is one switch for the whole deployment while A2P
    #: approval is per company, so with five customers the first approval
    #: turned this true for all five: four of them saw a green Leasing gate
    #: and then got a 503 on their first reply. A status field the send path
    #: disagrees with is worse than no status field.
    enabled: bool
    configured: bool
    #: ⚠ WHICH blocker fired — ``outbound_block_for_company``'s own return
    #: value, ``''`` when the company may send. The client used to infer this
    #: from two booleans and got a different answer than the send path did.
    #: Deciding it once, here, is the only way the panel's explanation and the
    #: refusal it explains cannot contradict each other (gotcha 28: a value
    #: that crosses a producer/consumer boundary is decided on ONE side).
    block: str = ""
    #: This company's own registration status (``''`` when they never
    #: registered — the single-tenant operator). Distinguishes "still with the
    #: carriers" from "was rejected, fix and resubmit", which need different
    #: instructions and are the same ``block`` value.
    campaign_status: str = ""
    consent_count: int
    revoked_count: int
    #: Outbound delivery over the last ``delivery_window_days``. ⚠ Every count
    #: is ``None`` when the log could not be read — "nothing failed" and
    #: "nothing was checked" are opposite facts and the card renders them
    #: differently (gotcha 89's `None`-is-not-zero, on both sides).
    delivered_count: int | None = None
    undelivered_count: int | None = None
    #: Accepted by Twilio, never confirmed, and past the window. This is the
    #: carrier-spam-filter shape (checklist #77) and is DELIBERATELY not
    #: folded into `undelivered_count`: Twilio reported a failure in one case
    #: and reported nothing in the other.
    no_receipt_count: int | None = None
    pending_count: int | None = None
    delivery_window_days: int = 7


def _row_out(r: Any, outcomes: dict[str, tuple[str, str | None]] | None = None) -> ConsentRow:
    outcome, error_code = (outcomes or {}).get(r["phone"], (None, None))
    return ConsentRow(
        id=str(r["id"]),
        phone=r["phone"],
        display_name=r["display_name"],
        recipient_kind=r["recipient_kind"],
        source=r["source"],
        timezone=r["timezone"],
        consented_at=r["consented_at"],
        revoked_at=r["revoked_at"],
        owner_user_id=(str(r["owner_user_id"]) if r["owner_user_id"] else None)
        if "owner_user_id" in r
        else None,
        last_outcome=outcome,
        last_error_code=error_code,
    )


@router.get("/consent", response_model=ConsentListOut)
async def list_consent(user: AuthenticatedUser = Depends(get_current_user)) -> ConsentListOut:
    """Every consent row for the caller's company — live AND revoked (the
    ledger shows both directions). No pagination pre-launch."""
    pool = get_pool()
    company_id = _company_id(user)
    rows = await pool.fetch(
        "SELECT id, phone, display_name, recipient_kind, source, timezone, "
        "       consented_at, revoked_at, owner_user_id "
        "FROM public.helixis_sms_consent WHERE company_id = $1::uuid "
        "ORDER BY consented_at DESC",
        company_id,
    )
    # ⚠ ONE query for every row, not one per row. `helixis_sms_log` was
    # written by three paths and read by exactly one — a `SELECT 1` daily-cap
    # check — so `status` and `error_code` had never been surfaced anywhere.
    # A landline came back `undelivered` (30006), got written down, and the
    # number stayed in this table looking perfectly healthy.
    outcomes = await delivery_health.last_outcome_by_phone(pool, company_id)
    return ConsentListOut(consents=[_row_out(r, outcomes) for r in rows])


@router.post("/consent", response_model=ConsentRow)
async def add_consent(
    payload: ConsentIn,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> ConsentRow:
    """Record (or re-grant) consent for one phone number.

    Upserts on (company_id, phone): a previously revoked row is
    RE-CONSENTED in place (``revoked_at`` cleared, ``consented_at``
    refreshed) — mirrors the START-webhook semantics, one row per number.
    ``source`` is ALWAYS ``'app_settings'`` (server-set), and each grant
    stamps fresh capture ``evidence`` (who recorded it, the exact
    disclosure text, request origin — see ``_capture_evidence``).
    """
    phone = normalize_e164(payload.phone)
    if phone is None:
        raise HTTPException(
            status_code=422,
            detail="Phone number could not be normalized to E.164 — use a full US number.",
        )
    # ⚠ Refuse a number this product can never text, at the moment somebody
    # types it. `normalize_e164` deliberately preserves an international
    # country code (inbound matching needs that), so without this a +44 number
    # was accepted, stored, listed as an ACTIVE consent — and every send to it
    # failed later where nobody was looking. A ledger row that looks live for
    # an unreachable number is the same lie as a missing delivery receipt.
    #
    # The backend refusal in `send_sms` is the enforcement; a form gate cannot
    # bind an API (gotcha 67). This is the half that tells somebody WHY, while
    # they can still fix it.
    if not is_nanp(phone):
        raise HTTPException(
            status_code=422,
            detail=(
                "Occupella can only text US and Canadian numbers — the "
                "registered messaging campaign covers North America."
            ),
        )
    try:
        ZoneInfo(payload.timezone)
    except Exception:  # noqa: BLE001 — any unresolvable zone is a client error
        raise HTTPException(
            status_code=422,
            detail=f"Unknown IANA timezone {payload.timezone!r}.",
        ) from None

    row = await get_pool().fetchrow(
        "INSERT INTO public.helixis_sms_consent "
        "(company_id, phone, display_name, recipient_kind, source, timezone, "
        " evidence, owner_user_id) "
        "VALUES ($1::uuid, $2, $3, $4, 'app_settings', $5, $6::jsonb, $7::uuid) "
        "ON CONFLICT (company_id, phone) DO UPDATE SET "
        "    display_name      = EXCLUDED.display_name, "
        "    recipient_kind    = EXCLUDED.recipient_kind, "
        "    timezone          = EXCLUDED.timezone, "
        "    source            = 'app_settings', "
        "    consented_at      = now(), "
        "    revoked_at        = NULL, "
        "    revocation_source = NULL, "
        "    evidence          = EXCLUDED.evidence, "
        # ⚠ COALESCE, not EXCLUDED. Re-consenting a number — which is what a
        # START text and an admin re-adding both land on — must not ERASE an
        # ownership somebody already claimed, or a departure silently stops
        # being able to revoke it. A new claim still wins because EXCLUDED
        # leads; only absence falls through to what is stored.
        "    owner_user_id     = COALESCE(EXCLUDED.owner_user_id, "
        "                                 public.helixis_sms_consent.owner_user_id) "
        "RETURNING id, phone, display_name, recipient_kind, source, timezone, "
        "          consented_at, revoked_at, owner_user_id",
        _company_id(user),
        phone,
        (payload.display_name or "").strip() or None,
        payload.recipient_kind,
        payload.timezone,
        _capture_evidence(request, user),
        user.user_id if payload.is_own_number else None,
    )
    return _row_out(row)


@router.delete("/consent/{consent_id}", response_model=ConsentRow)
async def revoke_consent(
    consent_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> ConsentRow:
    """REVOKE (never hard-delete) one consent row — the ledger keeps the
    row so it proves both the grant and the revocation. 404 when the id
    isn't the caller's company (scoping, not existence, decides)."""
    row = await get_pool().fetchrow(
        "UPDATE public.helixis_sms_consent "
        "SET revoked_at = COALESCE(revoked_at, now()), "
        "    revocation_source = COALESCE(revocation_source, 'app_settings') "
        "WHERE id = $1::uuid AND company_id = $2::uuid "
        "RETURNING id, phone, display_name, recipient_kind, source, timezone, "
        "          consented_at, revoked_at, owner_user_id",
        consent_id,
        _company_id(user),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="No consent row with that id.")
    return _row_out(row)


@router.get("/status", response_model=SmsStatusOut)
async def sms_status(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> SmsStatusOut:
    """Channel state for the settings card and the Leasing gate.

    ⚠ ``enabled`` answers "may THIS COMPANY send", which is the question both
    callers were already asking. It used to return ``twilio.enabled`` — one
    flag for the whole deployment — so on a white-label install the first
    customer's carrier approval flipped it true for every customer, and the
    ones still in review got a green gate followed by a 503 on their first
    send. It is computed from the SAME predicate the send path uses, so the
    two cannot disagree about whether a text may leave.
    """
    settings: Settings = request.app.state.settings
    twilio = settings.twilio
    company_id = _company_id(user)
    configured = bool(
        twilio.account_sid
        and twilio.auth_token
        and ((twilio.from_number or "").strip() or (twilio.messaging_service_sid or "").strip())
    )
    pool = get_pool()
    counts = await pool.fetchrow(
        "SELECT COUNT(*) FILTER (WHERE revoked_at IS NULL)     AS live, "
        "       COUNT(*) FILTER (WHERE revoked_at IS NOT NULL) AS revoked "
        "FROM public.helixis_sms_consent WHERE company_id = $1::uuid",
        company_id,
    )
    # ⚠ Reported separately from `enabled` so the UI can say WHOSE wait it is.
    # An unreadable row shows as unknown rather than as "registered" — the
    # gate below already refuses on it, and a status line that guesses would
    # contradict the refusal.
    try:
        reg = await pool.fetchrow(
            "SELECT status FROM public.helixis_a2p_registrations WHERE company_id = $1::uuid",
            company_id,
        )
        campaign_status = str(reg["status"] or "") if reg else ""
    except Exception:  # noqa: BLE001 — the gate is the authority, not this line
        logger.warning("a2p status lookup failed for company=%s", company_id)
        campaign_status = ""
    block = await outbound_block_for_company(pool, company_id, twilio)
    delivery = await delivery_health.summarize(pool, company_id)
    return SmsStatusOut(
        enabled=block is None,
        configured=configured,
        block=block or "",
        campaign_status=campaign_status,
        consent_count=int(counts["live"] if counts else 0),
        revoked_count=int(counts["revoked"] if counts else 0),
        delivered_count=delivery.delivered,
        undelivered_count=delivery.undelivered,
        no_receipt_count=delivery.no_receipt,
        pending_count=delivery.pending,
        delivery_window_days=delivery.window_days,
    )


# ─────────────────────────────────────────────────────────────────────
# Twilio number → company mapping (migration 0082).
#
# Inbound SMS and voice carry no auth. They used to resolve the company from
# ``settings.twilio.default_company_id`` — ONE id for the whole deployment, so
# a second customer's inbound texts filed into the first customer's account.
# These routes are how a number gets an owner without a Render env var and a
# redeploy, which is also what makes adding a number for a new company routine.
#
# ADMIN ONLY. Claiming a phone number decides where other people's inbound
# messages land — a strictly stronger power than managing your own company's
# consent rows, so it sits above the manager floor used elsewhere here.
# ─────────────────────────────────────────────────────────────────────


class TwilioNumberRow(BaseModel):
    id: str
    phone_number: str
    label: str | None = None
    forward_to_number: str | None = None
    voice_greeting: str | None = None
    created_at: datetime | None = None


class TwilioNumberListOut(BaseModel):
    numbers: list[TwilioNumberRow]


class TwilioNumberIn(BaseModel):
    phone_number: str
    label: str | None = None
    #: Where an inbound CALL rings. Blank/absent means voicemail, which is the
    #: documented default and a legitimate choice.
    #:
    #: ⚠ Settable because it was NOT, and a company provisioned by the A2P
    #: sweep gets a number with this column empty — so a new customer's phone
    #: never rang and there was no surface anywhere to change it. `None` here
    #: LEAVES the stored value alone; `""` clears it back to voicemail. A
    #: plain "absent means clear" would wipe forwarding every time someone
    #: renamed a number.
    forward_to_number: str | None = None
    voice_greeting: str | None = None


async def _require_admin(user: AuthenticatedUser, company_id: str) -> None:
    """Admin-or-refuse, FAIL CLOSED. An unverifiable role is not an admin —
    the same posture as ``buildium_tools._require_manager_role``."""
    try:
        row = await get_pool().fetchrow(
            "SELECT role FROM public.company_users "
            "WHERE user_id = $1::uuid AND company_id = $2::uuid",
            user.user_id,
            company_id,
        )
    except Exception:  # noqa: BLE001 — an unverifiable role is no role
        logger.warning("number-mapping role check failed for user=%s", user.user_id)
        raise HTTPException(status_code=403, detail="Could not verify your role.") from None
    role = normalize_role(row["role"]) if row and row["role"] else None
    if not role_at_least(role, ADMIN):
        raise HTTPException(
            status_code=403, detail="Only an admin can manage the company's phone numbers."
        )


@router.get("/numbers", response_model=TwilioNumberListOut)
async def list_numbers(
    user: AuthenticatedUser = Depends(get_current_user),
) -> TwilioNumberListOut:
    """The caller's company's numbers. Never another company's."""
    rows = await get_pool().fetch(
        "SELECT id, phone_number, label, forward_to_number, voice_greeting, created_at "
        "FROM public.helixis_twilio_numbers WHERE company_id = $1::uuid "
        "ORDER BY created_at",
        _company_id(user),
    )
    return TwilioNumberListOut(
        numbers=[
            TwilioNumberRow(
                id=str(r["id"]),
                phone_number=r["phone_number"],
                label=r["label"],
                forward_to_number=r["forward_to_number"],
                voice_greeting=r["voice_greeting"],
                created_at=r["created_at"],
            )
            for r in rows
        ]
    )


@router.post("/numbers", response_model=TwilioNumberRow, status_code=201)
async def claim_number(
    payload: TwilioNumberIn,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> TwilioNumberRow:
    """Claim a number for the caller's company.

    ⚠ E.164-normalized before insert, because Twilio's ``To`` param is E.164
    and the routing lookup normalizes too — a mixed-format column would match
    nothing and route nowhere, which fails silently.

    ⚠ A number already claimed by ANOTHER company is a 409, never a takeover.
    Reassigning it would redirect that company's live inbound traffic, and the
    unique index means one of the two claims must be wrong; a human decides
    which one.
    """
    company_id = _company_id(user)
    await _require_admin(user, company_id)
    normalized = normalize_e164(payload.phone_number)
    if not normalized:
        raise HTTPException(status_code=422, detail="Not a usable phone number.")

    existing = await get_pool().fetchrow(
        "SELECT company_id FROM public.helixis_twilio_numbers WHERE phone_number = $1",
        normalized,
    )
    if existing is not None and str(existing["company_id"]) != company_id:
        raise HTTPException(
            status_code=409,
            detail="That number is already claimed by another company on this deployment.",
        )

    # ⚠ Forwarding is E.164-normalized like the number itself. A call is
    # DIALED to this value, so a mistyped one rings a stranger — the same
    # reason the number column is normalized, with a worse failure.
    forward = (payload.forward_to_number or "").strip()
    if forward:
        forward_norm = normalize_e164(forward)
        if not forward_norm:
            raise HTTPException(
                status_code=422, detail="The forwarding number is not a usable phone number."
            )
    else:
        forward_norm = forward  # "" clears; None below leaves it alone

    # ⚠ WIRE IT BEFORE RECORDING IT. ``buy_number`` points a number at us in
    # the same request that buys it, so everything Occupella purchases is
    # configured by construction — but this route claims a number that ALREADY
    # exists, bought by hand in the console, and nothing had ever wired it. It
    # appeared in the app, the Leasing gate opened because the company now had
    # a number, and every call and text went nowhere. Silent, and only visible
    # when a real person rings.
    #
    # Ordered first on purpose: a row recorded for a number we could not
    # configure is the same lie in a different place. Refusing leaves the
    # company with no number, which is at least true.
    settings: Settings = request.app.state.settings
    twilio = settings.twilio
    if not (twilio.account_sid and twilio.auth_token):
        raise HTTPException(
            status_code=503,
            detail="Texting isn't connected on this deployment yet — an administrator "
            "needs to add the Twilio credentials before a number can be wired.",
        )
    try:
        await twilio_client.configure_number(
            account_sid=twilio.account_sid,
            auth_token=twilio.auth_token,
            phone_number=normalized,
            webhook_base_url=twilio.webhook_base_url,
        )
    except twilio_client.TwilioProvisionError as exc:
        # ⚠ 502, not a quiet 201. The caller has to know the number is not
        # receiving anything — that is the entire failure this closes.
        raise HTTPException(
            status_code=502,
            detail=f"Could not point that number's webhooks at Occupella: {exc}",
        ) from exc

    # ⚠ COALESCE, not EXCLUDED, for the two optional columns. An absent field
    # must LEAVE the stored value alone — otherwise renaming a number's label
    # silently wipes its forwarding, and the caller has no way to know. An
    # explicit "" still clears, because "back to voicemail" has to be
    # expressible.
    row = await get_pool().fetchrow(
        "INSERT INTO public.helixis_twilio_numbers "
        "  (company_id, phone_number, label, forward_to_number, voice_greeting) "
        "VALUES ($1::uuid, $2, $3, $4, $5) "
        "ON CONFLICT (phone_number) DO UPDATE SET "
        "  label = EXCLUDED.label, "
        "  forward_to_number = COALESCE(EXCLUDED.forward_to_number, "
        "                               public.helixis_twilio_numbers.forward_to_number), "
        "  voice_greeting = COALESCE(EXCLUDED.voice_greeting, "
        "                            public.helixis_twilio_numbers.voice_greeting), "
        "  updated_at = now() "
        "RETURNING id, phone_number, label, forward_to_number, voice_greeting, created_at",
        company_id,
        normalized,
        (payload.label or "").strip() or None,
        None if payload.forward_to_number is None else forward_norm,
        None if payload.voice_greeting is None else (payload.voice_greeting or "").strip(),
    )
    return TwilioNumberRow(
        id=str(row["id"]),
        phone_number=row["phone_number"],
        label=row["label"],
        forward_to_number=row["forward_to_number"],
        voice_greeting=row["voice_greeting"],
        created_at=row["created_at"],
    )


@router.delete("/numbers/{number_id}", status_code=204)
async def release_number(
    number_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> None:
    """Release a claim. 404 outside the caller's company — never a probe for
    whether another company holds a given id."""
    company_id = _company_id(user)
    await _require_admin(user, company_id)
    result = await get_pool().execute(
        "DELETE FROM public.helixis_twilio_numbers WHERE id = $1::uuid AND company_id = $2::uuid",
        number_id,
        company_id,
    )
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="No number with that id.")


class ProvisionIn(BaseModel):
    """Optional area-code preference. Not a requirement — an unavailable area
    code falls back to a national search rather than failing the setup."""

    area_code: str = ""
    #: Where inbound CALLS to the new number should ring. Blank means
    #: voicemail, which is the documented default.
    #:
    #: ⚠ Captured HERE because this is the one moment a human is present. The
    #: A2P sweep provisions a number unattended on carrier approval and has
    #: nobody to ask, so a number bought that way rings nowhere until someone
    #: sets this — which is the gap. It is NOT seeded from the registration's
    #: ``rep_phone``: that is the person who signs the carrier paperwork, who
    #: may well be an accountant, and guessing wrong means a stranger's phone
    #: rings (gotcha 55 — decline rather than guess).
    forward_to_number: str = ""


class ProvisionOut(BaseModel):
    number: TwilioNumberRow
    #: True when this call BOUGHT a number; False when the company already had
    #: one and we returned it. The UI says "you're set up" either way, but the
    #: distinction matters in logs — one costs money and the other does not.
    purchased: bool


@router.post("/numbers/provision", response_model=ProvisionOut)
async def provision_number(
    payload: ProvisionIn,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> ProvisionOut:
    """Buy this company a phone number and wire its webhooks. ADMIN ONLY.

    This is the "a new company should not need me" path: onboarding stays
    untouched, and Leasing walls itself until someone clicks Set up here.

    ⚠ **This spends money** — a Twilio number is a recurring monthly charge.
    Three guards, in this order:

    1. **Already-has-a-number wins over everything.** Checked FIRST, before any
       Twilio call, so a double-click or an impatient retry returns the
       existing row instead of buying a second number. Two numbers for one
       company is both a real cost and an ambiguous outbound identity —
       ``telephony_for_company`` would pick by ``created_at`` and the other
       number would keep receiving replies nobody reads.
    2. **Admin only.** Claiming a number decides where other people's inbound
       messages land, and this variant also charges the account.
    3. **Configuration before purchase.** No credentials or no public webhook
       base URL → refuse. Buying a number we cannot wire produces one that
       receives messages that go nowhere, and the charge is already incurred.
    """
    # ⚠ Not `_company_id(user)` (which takes [0]). This route SPENDS MONEY on a
    # recurring charge with no undo, and the idempotency guard below is scoped
    # to whatever company we pick — so a wrong pick cannot even stop the second
    # purchase. Refuse rather than guess.
    company_id = require_unambiguous_company(user)
    settings: Settings = request.app.state.settings
    twilio = settings.twilio

    # 1. Idempotency FIRST — before the role check even, so a member
    #    double-clicking a button on an already-provisioned company gets the
    #    honest answer rather than a 403 that reads like a bug.
    existing = await get_pool().fetchrow(
        "SELECT id, phone_number, label, forward_to_number, voice_greeting, created_at "
        "FROM public.helixis_twilio_numbers WHERE company_id = $1::uuid "
        "ORDER BY created_at LIMIT 1",
        company_id,
    )
    if existing is not None:
        return ProvisionOut(
            number=TwilioNumberRow(
                id=str(existing["id"]),
                phone_number=existing["phone_number"],
                label=existing["label"],
                forward_to_number=existing["forward_to_number"],
                voice_greeting=existing["voice_greeting"],
                created_at=existing["created_at"],
            ),
            purchased=False,
        )

    await _require_admin(user, company_id)

    # ⚠ Validated BEFORE the purchase. A junk forwarding number discovered
    # after the buy leaves a charged, half-configured number and a 422 that
    # reads like the purchase failed.
    forward = (payload.forward_to_number or "").strip()
    forward_norm = normalize_e164(forward) if forward else ""
    if forward and not forward_norm:
        raise HTTPException(
            status_code=422, detail="The forwarding number is not a usable phone number."
        )

    if not (twilio.account_sid and twilio.auth_token):
        raise HTTPException(
            status_code=503,
            detail="Texting isn't connected on this deployment yet — an administrator "
            "needs to add the Twilio credentials.",
        )
    base = (twilio.webhook_base_url or "").strip()
    if not base:
        raise HTTPException(
            status_code=503,
            detail="This deployment has no public webhook URL configured, so a new "
            "number could not receive anything. An administrator needs to set it.",
        )

    # 2. Search. An area-code preference that yields nothing falls back to a
    #    national search — a setup that fails because one area code is sold out
    #    is a worse outcome than a number in the next town.
    try:
        candidates = await twilio_client.search_available_numbers(
            account_sid=twilio.account_sid,
            auth_token=twilio.auth_token,
            area_code=payload.area_code,
        )
        if not candidates and payload.area_code.strip():
            logger.info("no numbers in area code %r — retrying nationally", payload.area_code)
            candidates = await twilio_client.search_available_numbers(
                account_sid=twilio.account_sid, auth_token=twilio.auth_token
            )
    except twilio_client.TwilioProvisionError as exc:
        raise HTTPException(
            status_code=502, detail=f"Could not search for a number: {exc}"
        ) from exc
    if not candidates:
        raise HTTPException(
            status_code=502,
            detail="Twilio had no available numbers to offer. Try again, or pick a "
            "different area code.",
        )

    # 3. Buy + wire, in one Twilio call. See buy_number: the gap between
    #    "bought" and "configured" is where a half-finished setup lives.
    try:
        bought = await twilio_client.buy_number(
            account_sid=twilio.account_sid,
            auth_token=twilio.auth_token,
            phone_number=candidates[0]["phone_number"],
            webhook_base_url=base,
            friendly_name=f"Occupella {company_id[:8]}",
        )
    except twilio_client.TwilioProvisionError as exc:
        raise HTTPException(status_code=502, detail=f"Could not buy a number: {exc}") from exc

    normalized = normalize_e164(bought) or bought
    # ⚠ ON CONFLICT, not a plain insert. The number is already BOUGHT by now;
    # a unique-violation raised here would charge the account and record
    # nothing, which is the one outcome with no recovery path from the UI.
    row = await get_pool().fetchrow(
        "INSERT INTO public.helixis_twilio_numbers "
        "  (company_id, phone_number, label, forward_to_number) "
        "VALUES ($1::uuid, $2, $3, $4) "
        "ON CONFLICT (phone_number) DO UPDATE SET "
        "  forward_to_number = COALESCE(EXCLUDED.forward_to_number, "
        "                               public.helixis_twilio_numbers.forward_to_number), "
        "  updated_at = now() "
        "RETURNING id, phone_number, label, forward_to_number, voice_greeting, "
        "          created_at, company_id",
        company_id,
        normalized,
        "Provisioned in app",
        forward_norm or None,
    )
    if row is None or str(row["company_id"]) != company_id:
        # The number we just bought is recorded against a DIFFERENT company.
        # Practically impossible (we just bought it) but the alternative is
        # reporting success for a number this company does not own.
        logger.error("provisioned number %s is owned by another company", normalized)
        raise HTTPException(
            status_code=409,
            detail="That number is already registered to another company on this deployment.",
        )
    logger.info("provisioned a number for company=%s", company_id)
    return ProvisionOut(
        number=TwilioNumberRow(
            id=str(row["id"]),
            phone_number=row["phone_number"],
            label=row["label"],
            forward_to_number=row["forward_to_number"],
            voice_greeting=row["voice_greeting"],
            created_at=row["created_at"],
        ),
        purchased=True,
    )


__all__ = ["router"]
