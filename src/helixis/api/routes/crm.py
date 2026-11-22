"""CRM (lead pipeline) endpoints — scaffolded behind ``HELIXIS_CRM__ENABLED``.

The CRM section is a self-updating pipeline: Helixis moves each lead
between stages when a verifiable fact occurs (a reply sent, a tour on
Google Calendar, an application / lease in Buildium). Leads can arrive by
SMS (Twilio) and are answered by SMS from the same thread.

While ``settings.crm.enabled`` is ``False`` (the default) every route here
returns 404 — the section is "Coming soon" and the frontend renders a
local preview instead of calling this. When the flag flips on, the routes
serve the company's real leads + conversations (company-scoped; RLS on
``crm_leads`` / ``crm_lead_messages`` is the second line of defense).

Routes:
  * ``GET  /crm/board``                    — the pipeline (auth).
  * ``GET  /crm/leads/{id}/messages``      — a lead's SMS thread (auth).
  * ``POST /crm/leads/{id}/reply``         — send an approved SMS reply (auth).
  * ``POST /crm/sms/inbound``              — Twilio inbound webhook
                                             (NO auth — Twilio-signature verified).
  * ``POST /crm/sms/simulate``             — inject a fake inbound SMS for
                                             keyless testing (auth; gated on
                                             ``twilio.skip_signature_check``).

Safety: an inbound SMS only *creates/updates a lead + logs the text* — it
never triggers an outbound message. Replies are user-approved only
(``/reply``); Helixis never auto-sends.
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel

from helixis.agents.idempotency import (
    GuardResult,
    PreSideEffectError,
    derive_key,
    guarded_action,
)
from helixis.api.auth import AuthenticatedUser, get_current_user
from helixis.billing.gate import load_state
from helixis.billing.plans import FEATURE_CRM, Plan, cheapest_plan_with
from helixis.comms import opt_out
from helixis.comms.consent_keywords import apply_consent_keywords
from helixis.comms.number_routing import (
    resolve_company_for_number,
    telephony_for_company,
)
from helixis.comms.sms import outbound_block_for_company
from helixis.connectors import twilio_client
from helixis.crm.draft import generate_lead_draft
from helixis.crm.email_send import send_gmail_email
from helixis.crm.lead_memory import recall_for_lead, remember_approved_reply
from helixis.crm.template_merge import build_lead_merge_context, fill_template
from helixis.db.engine import get_pool
from helixis.db.repositories import company_knowledge as ck_repo
from helixis.db.repositories import docs_links as docs_repo
from helixis.db.repositories import leads as leads_repo
from helixis.db.repositories.action_idempotency import PgIdempotencyStore
from helixis.db.repositories.activity import ActivityEntry, write_activity
from helixis.db.repositories.leads import LeadRow, MessageRow
from helixis.security.hazard_lexicon import detect_hazards, hazard_label
from helixis.settings import Settings
from helixis.webhooks import crm_lead_ingest, lead_triage
from helixis.webhooks.crm_sms_inbound import handle_inbound_lead_sms
from helixis.webhooks.email_triage import InboundEmail

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/crm", tags=["crm"])


def _settings_from(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def _require_crm_flag(settings: Settings) -> None:
    """Raise 404 when the CRM flag is off, so the route's existence isn't
    leaked while the feature is dark (same shape as ``dev._require_dev_flag``)."""
    if not settings.crm.enabled:
        raise HTTPException(status_code=404)


def crm_plan_upgrade_detail(plan: Plan) -> str:
    """Why this caller cannot work leads, naming the plan they are ACTUALLY on.

    ⚠ This was a hardcoded constant reading "isn't part of the Starter plan.
    Upgrade to Pro", and the 2026-09-04 founder call took `FEATURE_CRM` off the
    TRIAL as well — so from that day every trialist who reached a Leasing route
    was told they were on a plan they had never bought. Nothing failed: the
    string had one call site and no test asserted a word of it, which is why it
    went stale silently (gotcha 48, and gotcha 127's "a decision rewrites the
    meaning of strings nobody edited").

    Both names come from the same source the client uses — `plan.label` for
    what they are on, `cheapest_plan_with` for the target — so the two surfaces
    cannot drift into naming different plans, and neither has to be edited when
    a capability moves rung.
    """
    target = cheapest_plan_with(FEATURE_CRM)
    if target is None:
        # No purchasable plan carries it. Naming an upgrade that does not exist
        # is an instruction the reader cannot carry out, which this repo has
        # recorded as worse than none (gotchas 102/106/118/133).
        return f"The Leasing pipeline isn't part of {plan.label}."
    return (
        f"The Leasing pipeline isn't part of {plan.label}. "
        f"Upgrade to {target.label} in Settings → Billing to work leads here."
    )


async def _primary_company_id(user: AuthenticatedUser) -> str:
    """The caller's company, AND the plan check that decides they may be here.

    ⚠ The two are deliberately one function. Sixteen authenticated Leasing
    routes resolve a company this way, and a separate `_require_crm_plan(...)`
    beside each of them is precisely the per-call-site fan-out gotchas 25 and
    81 both record: it works until the seventeenth route forgets it, and a
    forgotten capability check fails OPEN and silently. Folding it in means a
    new route cannot reach the pipeline without passing through the gate,
    because it cannot reach the pipeline without knowing whose it is.

    ⚠ Fails OPEN on an unreadable billing ROW — `load_state` returning `None`
    allows. That is `gate.py`'s rule 1 pointed at a section instead of at a
    turn: a database blip that blacks out Leasing for a paying Pro customer
    costs far more than the few sessions an outage lets through.

    ⚠ An unreadable PLAN KEY is the opposite and that asymmetry is deliberate.
    This docstring used to say the fallback allowed too, "because `plan_for`
    resolves an unknown key to the trial, which carries every feature" — true
    when written and FALSE since the 2026-09-04 founder call took `FEATURE_CRM`
    off the trial. A row we cannot read at all is an outage on our side; a plan
    key no build recognises is a row a NEWER deploy wrote, and handing that the
    richest capability set is how an unknown state buys something nobody sold
    it. `plans.plan_includes` states the same split.

    ⚠ 402, not 403. "Payment required" is the one status that says the caller
    is who they claim to be and the answer is still no — a 403 reads as "your
    role is wrong" and would send an admin to Team & Permissions.
    """
    if not user.company_ids:
        raise HTTPException(status_code=400, detail="User is not a member of any company")
    company_id = user.company_ids[0]

    state = await load_state(company_id)
    if state is not None and not state.plan.includes(FEATURE_CRM):
        raise HTTPException(status_code=402, detail=crm_plan_upgrade_detail(state.plan))
    return company_id


def _require_valid_lead_id(lead_id: str) -> None:
    """404 (not a raw DB crash) when ``lead_id`` isn't a UUID — e.g. a stale
    demo-fixture id (like ``demo-l-marcus``) reaching a real backend. The
    repository casts ``lead_id`` straight to ``::uuid`` in raw SQL, which
    otherwise surfaces as an unhandled asyncpg.DataError (500) — the
    PYTHON-FASTAPI-J/H production incidents."""
    try:
        uuid.UUID(lead_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=404) from None


def _guard_fair_housing_egress(body: str, *, lead_prompt: str = "") -> None:
    """Refuse to send lead-facing text that trips the deterministic gate.

    Mirrors ``agent/handlers/base.guard_fair_housing`` — the same tuned,
    negation-guarded ``check_draft`` the two Inbox lanes use — but raises the
    ``HTTPException`` this route layer speaks rather than ``HandlerError``.
    Empty violation list = pass.
    """
    from helixis.security.fair_housing_gate import check_draft

    violations = check_draft(lead_prompt, body)
    if violations:
        raise HTTPException(
            status_code=422,
            detail=(
                "Blocked by the fair-housing check ("
                + ", ".join(violations)
                + "). Revise the reply to remove steering or discriminatory "
                "language and resend."
            ),
        )


# ── outbound send guard — the CRM lane joins the write pipeline ──────
#
# Every CRM outbound (SMS reply, email reply, click-to-call) now runs through
# the SAME durable idempotency guard as agent/inbox Buildium writes
# (``guarded_action``). The human already approved in the CRM UI, so there is
# no SSE confirm gate (``confirm=None``) — the guard provides dedup, not
# consent (identical stance to the inbox execute endpoint). Before this, the
# CRM lane was the one send path OUTSIDE the guard library: a double-click or
# client retry double-texted/-emailed/-called with no durable record.
_REPLY_DEDUP_WINDOW = timedelta(hours=6)  # same-body re-send to a lead = duplicate
_CALL_DEDUP_WINDOW = timedelta(minutes=2)  # re-click minutes later = a new, legitimate call


def _crm_idempotency_store(pool: asyncpg.Pool) -> PgIdempotencyStore:
    """Seam so unit tests can inject a fake store (mirrors
    ``buildium._idempotency_store``)."""
    return PgIdempotencyStore(pool)


def _guard_result_or_raise(guard: GuardResult, *, noun: str) -> dict[str, Any]:
    """Return the (fresh or replayed) result dict, or map a non-execution
    outcome to the route error. ``replayed`` is a success to the client — the
    send already happened; returning the prior SID keeps the retry idempotent
    from the UI's point of view."""
    if guard.outcome in ("executed", "replayed"):
        return guard.result if isinstance(guard.result, dict) else {}
    if guard.outcome == "already_pending":
        raise HTTPException(status_code=409, detail=f"This {noun} is already being sent.")
    # blocked_needs_review (a prior attempt ended ambiguous) or any unknown
    # outcome: fail safe — never blind-refire an outbound message.
    raise HTTPException(
        status_code=502,
        detail=(
            f"A previous {noun} attempt ended in an unknown state — check the lead's "
            "thread before retrying."
        ),
    )


async def _log_crm_activity(
    pool: asyncpg.Pool,
    *,
    company_id: str,
    user_id: str,
    action_type: str,
    description: str,
    lead_id: str | None,
) -> None:
    """Record a CRM action on the Team Activity feed. Best-effort (``write_activity``
    never raises) — auditing must not fail the action that triggered it. ``source``
    is ``manual`` (user-initiated) so it groups with the other hand-run controls."""
    await write_activity(
        pool,
        ActivityEntry(
            account_id=None,
            company_id=company_id,
            source="manual",
            action_type=action_type,
            description=description,
            user_id=user_id,
            raw={"lead_id": lead_id} if lead_id else None,
        ),
    )


def _lead_name(lead: LeadRow) -> str:
    """A human label for a lead in an activity line — name, else phone/email."""
    return lead.name or lead.phone or lead.email or "a lead"


async def _flag_hazards_if_any(
    pool: asyncpg.Pool,
    *,
    company_id: str,
    text: str | None,
    channel: str,
    lead_id: str | None,
    external_id: str | None,
) -> list[str]:
    """Deterministic life-safety scan over inbound tenant text (SMS bodies,
    voicemail transcriptions). On a hit: a WARNING log + a Team Activity entry
    so the PM sees the flag even if no one is watching the CRM thread. Never
    acts (no auto-reply/dispatch) and never raises — flagging must not break
    the ingest that triggered it. Returns the hazard slugs found."""
    try:
        hazards = detect_hazards(text)
        if not hazards:
            return []
        label = hazard_label(hazards)
        logger.warning(
            "HAZARD flagged in inbound %s: %s (lead_id=%s external_id=%s)",
            channel,
            label,
            lead_id,
            external_id,
        )
        # Description carries only the hazard slugs — never the message body
        # (activity rows render on the shared team feed).
        await write_activity(
            pool,
            ActivityEntry(
                account_id=None,
                company_id=company_id,
                source="webhook",
                source_label=f"CRM · inbound {channel}",
                action_type="hazard_flagged",
                description=f"⚠ Possible emergency reported by {channel}: {label}",
                raw={
                    "hazards": hazards,
                    "channel": channel,
                    "lead_id": lead_id,
                    "external_id": external_id,
                },
            ),
        )
        return hazards
    except Exception:  # noqa: BLE001 — flagging is best-effort by contract
        logger.exception("hazard flagging failed (non-fatal)")
        return []


# ── read: board + thread ─────────────────────────────────────────────


class LeadOut(BaseModel):
    id: str | None
    name: str
    kind: str
    stage: str
    source: str
    status: str = "active"
    unit: str | None = None
    note: str | None = None
    phone: str | None = None
    email: str | None = None
    # Book-a-tour (0036): scheduled tour datetime (ISO string), None until booked.
    tour_at: str | None = None
    # Buildium applicant link (0040): set once an applicant webhook/backfill
    # matches this lead — the drawer badges it as "In Buildium".
    buildium_applicant_id: int | None = None


class BoardOut(BaseModel):
    leads: list[LeadOut]
    enabled: bool = True


# stage order + labels, mirrored on the frontend CRM_STAGES.
_STAGES: list[tuple[str, str]] = [
    ("new", "New"),
    ("contacted", "Contacted"),
    ("scheduled", "Scheduled"),
    ("applied", "Applied"),
    ("leased", "Leased"),
]


def _lead_out(r: LeadRow) -> LeadOut:
    return LeadOut(
        id=r.id,
        name=r.name,
        kind=r.kind,
        stage=r.stage,
        source=r.source,
        status=r.status,
        unit=r.unit_label,
        note=r.note,
        phone=r.phone,
        email=r.email,
        tour_at=r.tour_at,
        buildium_applicant_id=r.buildium_applicant_id,
    )


class StageGroupOut(BaseModel):
    key: str
    label: str
    count: int
    recent: list[LeadOut]


class MetricsOut(BaseModel):
    new_this_month: int
    new_prev_month: int
    replies_this_month: int
    active: int
    leased: int
    total: int


class OverviewOut(BaseModel):
    stages: list[StageGroupOut]
    metrics: MetricsOut
    # Leads still in play but quiet 3+ days — the "needs a nudge" alert count.
    cold_count: int = 0


class StageListOut(BaseModel):
    leads: list[LeadOut]
    has_more: bool


@router.get("/overview", response_model=OverviewOut)
async def get_overview(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    per_stage: int = Query(default=3, ge=1, le=10),
) -> OverviewOut:
    """The landing page: per-stage counts + the ``per_stage`` most-recent
    leads in each stage + this-month analytics. Company-scoped; 404 off."""
    settings = _settings_from(request)
    _require_crm_flag(settings)
    company_id = await _primary_company_id(user)
    pool = get_pool()
    counts = await leads_repo.count_leads_by_stage(pool=pool, company_id=company_id)
    recent = await leads_repo.recent_leads_by_stage(
        pool=pool, company_id=company_id, per_stage=per_stage
    )
    metrics = await leads_repo.lead_metrics(pool=pool, company_id=company_id)
    cold_count = await leads_repo.count_cold_leads(pool=pool, company_id=company_id)

    by_stage: dict[str, list[LeadOut]] = {key: [] for key, _ in _STAGES}
    for r in recent:
        if r.stage in by_stage:
            by_stage[r.stage].append(_lead_out(r))

    return OverviewOut(
        cold_count=cold_count,
        stages=[
            StageGroupOut(key=key, label=label, count=counts.get(key, 0), recent=by_stage[key])
            for key, label in _STAGES
        ],
        metrics=MetricsOut(
            new_this_month=metrics.new_this_month,
            new_prev_month=metrics.new_prev_month,
            replies_this_month=metrics.replies_this_month,
            active=metrics.active,
            leased=metrics.leased,
            total=metrics.total,
        ),
    )


@router.get("/leads", response_model=StageListOut)
async def list_stage_leads(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    stage: str | None = Query(default=None),
    q: str | None = Query(default=None),
    status: str | None = Query(default="active"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> StageListOut:
    """The stage-detail list: leads in one ``stage`` (or all), filtered by
    ``q``, paginated. ``status`` defaults to ``active`` (won't-pursue leads
    are hidden); pass ``status=lost`` for the records view or ``status=all``
    to include every status. Fetches one extra row to compute ``has_more``
    without a second count query."""
    settings = _settings_from(request)
    _require_crm_flag(settings)
    company_id = await _primary_company_id(user)
    pool = get_pool()
    rows = await leads_repo.list_leads(
        pool=pool,
        company_id=company_id,
        stage=stage,
        query=q,
        status=None if status == "all" else status,
        limit=limit + 1,
        offset=offset,
    )
    has_more = len(rows) > limit
    return StageListOut(leads=[_lead_out(r) for r in rows[:limit]], has_more=has_more)


class ColdListOut(BaseModel):
    leads: list[LeadOut]
    count: int
    threshold_days: int


@router.get("/leads/cold", response_model=ColdListOut)
async def list_cold_leads(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    limit: int = Query(default=100, ge=1, le=200),
) -> ColdListOut:
    """The "needs a nudge" list: leads still in play (active, not leased) that
    have gone quiet for 3+ days, coldest first. Company-scoped; 404 off. Note
    this path is declared before ``/leads/{lead_id}/...`` so ``cold`` is never
    read as a lead id."""
    settings = _settings_from(request)
    _require_crm_flag(settings)
    company_id = await _primary_company_id(user)
    pool = get_pool()
    rows = await leads_repo.cold_leads(pool=pool, company_id=company_id, limit=limit)
    return ColdListOut(
        leads=[_lead_out(r) for r in rows],
        count=len(rows),
        threshold_days=leads_repo.COLD_LEAD_DAYS,
    )


class MessageOut(BaseModel):
    id: str | None
    direction: str
    channel: str
    body: str
    created_at: str | None = None
    # Voicemail recording (channel='voicemail'); None otherwise.
    recording_url: str | None = None
    # Voicemail transcription text (attached after Twilio transcribes); None
    # until the transcribe callback lands.
    transcription: str | None = None


@router.get("/board", response_model=BoardOut)
async def get_board(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    limit: int = Query(default=200, ge=1, le=500),
) -> BoardOut:
    """Return the company's lead pipeline. Company-scoped; 404 while off."""
    settings = _settings_from(request)
    _require_crm_flag(settings)
    company_id = await _primary_company_id(user)
    pool = get_pool()
    rows = await leads_repo.list_leads(pool=pool, company_id=company_id, limit=limit)
    return BoardOut(leads=[_lead_out(r) for r in rows])


@router.get("/leads/{lead_id}/messages", response_model=list[MessageOut])
async def get_lead_messages(
    lead_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[MessageOut]:
    """Return a lead's conversation thread (oldest first). Company-scoped."""
    settings = _settings_from(request)
    _require_crm_flag(settings)
    _require_valid_lead_id(lead_id)
    company_id = await _primary_company_id(user)
    pool = get_pool()
    msgs = await leads_repo.list_messages(pool=pool, company_id=company_id, lead_id=lead_id)
    return [
        MessageOut(
            id=m.id,
            direction=m.direction,
            channel=m.channel,
            body=m.body,
            created_at=m.created_at,
            recording_url=(m.raw or {}).get("recording_url"),
            transcription=(m.raw or {}).get("transcription"),
        )
        for m in msgs
    ]


# ── AI assist: "what Helixis noticed" + drafted reply ────────────────


class DraftOut(BaseModel):
    """The lead's AI assist. Empty signals + null draft is a valid, fully-
    degraded result (the model was unavailable) — the drawer still renders."""

    signals: list[str]
    draft_reply: str | None = None


@router.get("/leads/{lead_id}/draft", response_model=DraftOut)
async def get_lead_draft(
    lead_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> DraftOut:
    """Return the lead's "what Helixis noticed" signals + a drafted reply.

    Two phases (per the "default message first, then conversational drafts"
    rule): while the lead has had NO outbound message, the drafted reply is the
    company's default template, merge-filled, so the PM just reviews and clicks
    Send. Once a reply has gone out, the draft is the conversational LLM one
    (generated once on that first open, cached on the lead). Never fails the
    drawer — a model outage / missing template just yields empty signals or no
    draft."""
    settings = _settings_from(request)
    _require_crm_flag(settings)
    _require_valid_lead_id(lead_id)
    company_id = await _primary_company_id(user)
    pool = get_pool()
    lead = await leads_repo.get_lead(pool=pool, company_id=company_id, lead_id=lead_id)
    if lead is None:
        raise HTTPException(status_code=404)

    messages = await leads_repo.list_messages(pool=pool, company_id=company_id, lead_id=lead_id)
    has_outbound = any(m.direction == "outbound" for m in messages)

    # Standing company profile (distilled from the company's uploaded documents)
    # so a draft can answer concrete questions — hours / pet policy / fees.
    # Best-effort: empty when there's no profile yet.
    ck = await ck_repo.load(pool, company_id=company_id)
    knowledge = ck.content_md if ck else ""

    # What this company has already told other prospects. The profile above is
    # distilled from uploaded DOCUMENTS; this is distilled from approved
    # REPLIES, which is the half that improves every time somebody hits send.
    # Empty is the normal early state, not a failure.
    recalled = await recall_for_lead(company_id=company_id, lead=lead, messages=messages)
    if recalled:
        knowledge = f"{knowledge}\n\n{recalled}" if knowledge else recalled

    # Phase 1 — first contact: pre-draft from the company's default template.
    if not has_outbound:
        template = await docs_repo.default_template(pool, company_id=company_id)
        if template is not None:
            ctx = build_lead_merge_context(name=lead.name, unit=lead.unit_label)
            template_reply = fill_template(template.body, ctx).strip() or None
            # Signals still come from the LLM (cached signals-only so the
            # conversational draft still generates once a reply goes out).
            signals = lead.signals
            if signals is None:
                generated = await generate_lead_draft(
                    settings, lead=lead, messages=messages, company_knowledge=knowledge
                )
                signals = generated.signals
                if signals:
                    await leads_repo.set_lead_signals(
                        pool, company_id=company_id, lead_id=lead_id, signals=signals
                    )
            return DraftOut(signals=signals or [], draft_reply=template_reply)
        # No default template configured → fall through to the conversational
        # draft so the drawer still offers a suggested first reply.

    # Phase 2 — conversational: return the cached LLM draft, or generate it once.
    if lead.draft_reply is not None:
        return DraftOut(signals=lead.signals or [], draft_reply=lead.draft_reply)

    draft = await generate_lead_draft(
        settings, lead=lead, messages=messages, company_knowledge=knowledge
    )
    # Only cache a non-empty result, so a transient model outage retries on
    # the next open instead of pinning an empty assist forever.
    if draft.signals or draft.draft_reply:
        await leads_repo.set_lead_draft(
            pool,
            company_id=company_id,
            lead_id=lead_id,
            signals=draft.signals,
            draft_reply=draft.draft_reply or "",
        )
    return DraftOut(signals=draft.signals, draft_reply=draft.draft_reply)


# ── write: approved reply (outbound, on the lead's channel) ──────────


class ReplyIn(BaseModel):
    body: str
    # 'sms' | 'email'. Omit to let the backend pick: text if the lead has a
    # phone, else email. A lead arrives by one channel and is answered on it.
    channel: str | None = None


class ReplyOut(BaseModel):
    message_sid: str
    stage: str
    channel: str = "sms"


def _reply_subject(lead: LeadRow) -> str:
    """Subject line for an emailed reply to a lead."""
    return f"Re: {lead.unit_label}" if lead.unit_label else "Re: your inquiry"


async def _lead_may_be_texted(
    pool: asyncpg.Pool, *, company_id: str, lead_id: str, phone: str
) -> bool:
    """Whether this company may text this lead: they texted the company first,
    or there is a live (unrevoked) consent row for their number.

    Fails CLOSED. A lookup that errors answers False, because "couldn't check
    permission" must never mean "send anyway" (the consent ledger's own rule,
    ``comms/sms.py``).
    """
    try:
        row = await pool.fetchrow(
            """
            SELECT
                EXISTS (
                    SELECT 1 FROM public.crm_lead_messages
                    WHERE company_id = $1::uuid AND lead_id = $2::uuid
                      AND direction = 'inbound' AND channel = 'sms'
                ) AS texted_in,
                EXISTS (
                    SELECT 1 FROM public.helixis_sms_consent
                    WHERE company_id = $1::uuid AND phone = $3 AND revoked_at IS NULL
                ) AS consented
            """,
            company_id,
            lead_id,
            phone,
        )
    except Exception:  # noqa: BLE001 — fail closed, never open
        logger.warning("crm: could not check text permission for lead %s", lead_id, exc_info=True)
        return False
    return bool(row) and (row["texted_in"] is True or row["consented"] is True)


async def _log_outbound_and_advance(
    pool: asyncpg.Pool,
    *,
    company_id: str,
    lead_id: str,
    channel: str,
    body: str,
    external_id: str | None,
    raw: dict[str, object],
    user_id: str,
    who: str,
) -> None:
    """Shared tail for both channels: log the outbound message, auto-move the
    lead to ``contacted`` (a verified fact — the reply went out), and record it
    on the Team Activity feed."""
    await leads_repo.insert_message(
        pool,
        MessageRow(
            company_id=company_id,
            lead_id=lead_id,
            direction="outbound",
            channel=channel,
            body=body,
            external_id=external_id,
            raw=raw,
        ),
    )
    await leads_repo.update_stage(pool, company_id=company_id, lead_id=lead_id, stage="contacted")
    # Learn from what the company actually chose to say. Deliberately here, in
    # the tail BOTH channels pass through, so adding a third channel cannot
    # quietly skip it (the `comms_triage` failure, gotcha 62). Never raises.
    await remember_approved_reply(
        company_id=company_id,
        lead=await leads_repo.get_lead(pool, company_id=company_id, lead_id=lead_id),
        body=body,
        channel=channel,
        user_id=user_id,
    )
    verb = "Emailed" if channel == "email" else "Texted"
    await _log_crm_activity(
        pool,
        company_id=company_id,
        user_id=user_id,
        action_type="crm_reply_sent",
        description=f"{verb} {who}",
        lead_id=lead_id,
    )


@router.post("/leads/{lead_id}/reply", response_model=ReplyOut)
async def reply_to_lead(
    lead_id: str,
    payload: ReplyIn,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> ReplyOut:
    """Send a user-approved reply to a lead on its channel (text or email), log
    it, and move the lead to ``contacted``. Never auto-sent — this fires only
    when the user hits approve.

    Channel defaults to SMS when the lead has a phone, else email. 400 on an
    empty body or a channel the lead can't be reached on; 503 if the channel's
    provider (Twilio / Gmail) isn't connected; 502 if the send fails."""
    settings = _settings_from(request)
    _require_crm_flag(settings)
    _require_valid_lead_id(lead_id)
    company_id = await _primary_company_id(user)
    body = (payload.body or "").strip()
    if not body:
        raise HTTPException(status_code=400, detail="Reply body is empty.")

    # ⚠ EGRESS FAIR-HOUSING GATE. This lane is, by ``crm/draft.py``'s own
    # assessment, "the highest fair-housing-risk surface (outbound messages to
    # prospects)" — steering liability under §3604 attaches to what you tell a
    # PROSPECTIVE renter — and it was the one such lane with no deterministic
    # check. ``send_tenant_message`` and ``reply_to_email`` both call this gate
    # immediately before their send; the CRM reply did not.
    #
    # The prompt block alone is not a guardrail, whatever ``draft.py`` calls
    # it: gotcha 52 recorded the model declining a protected-class question
    # correctly and then OFFERING the proxies the same prompt forbids by name,
    # with the rubric judge catching that shape only sometimes.
    #
    # Fail direction is deliberate and asymmetric. A blocked legitimate reply
    # is visible and recoverable — the manager already approved this text, so a
    # 422 is a "revise and resend" prompt, not a silent drop. An unblocked
    # steering message to a prospect is a Fair Housing Act violation that
    # nobody sees.
    _guard_fair_housing_egress(body, lead_prompt=payload.body or "")

    pool = get_pool()
    # Role floor (approval-pipeline PR): an outbound send takes manager+
    # (same gate as /agent/confirm approvals, ADR 0005).
    from helixis.api.roles import MANAGER
    from helixis.api.routes.company import require_role

    await require_role(
        pool,
        user.user_id,
        company_id,
        MANAGER,
        detail="Only managers and admins can send replies.",
    )
    lead = await leads_repo.get_lead(pool=pool, company_id=company_id, lead_id=lead_id)
    if lead is None:
        raise HTTPException(status_code=404)

    channel = (payload.channel or "").strip().lower() or ("sms" if lead.phone else "email")
    if channel not in ("sms", "email"):
        raise HTTPException(status_code=400, detail=f"Unknown reply channel: {channel!r}")

    if channel == "email":
        if not lead.email:
            raise HTTPException(status_code=400, detail="Lead has no email address to write to.")
        # Bound to a local so the narrowing survives into the closure (mypy
        # doesn't carry attribute narrowing into nested functions).
        lead_email: str = lead.email
        composio = getattr(request.app.state, "composio_client", None)
        if composio is None or not getattr(composio, "available", False):
            raise HTTPException(
                status_code=503,
                detail="Gmail isn't connected — connect it in Settings → Connectors.",
            )

        async def _send_email() -> dict[str, Any]:
            ok, message_id = send_gmail_email(
                composio,
                user_id=user.user_id,
                recipient_email=lead_email,
                subject=_reply_subject(lead),
                body=body,
            )
            if not ok:
                # ``ok=False`` collapses pre- and post-send failures (the
                # Composio call may time out AFTER Gmail accepted the message)
                # → treat as ambiguous; never blind-retried by the guard.
                raise RuntimeError("gmail send failed")
            return {"message_id": message_id or ""}

        try:
            guard = await guarded_action(
                store=_crm_idempotency_store(pool),
                company_id=company_id,
                idem_key=derive_key(
                    company_id, "crm.reply_email", {"lead_id": lead_id, "body": body}
                ),
                action_type="crm.reply_email",
                confirm=None,  # approved in the CRM UI
                execute=_send_email,
                window=_REPLY_DEDUP_WINDOW,
            )
        except RuntimeError as exc:
            raise HTTPException(
                status_code=502,
                detail="Couldn't send the email — check the lead's thread before retrying.",
            ) from exc
        result = _guard_result_or_raise(guard, noun="email reply")
        message_id = str(result.get("message_id") or "")
        if guard.outcome == "executed":
            await _log_outbound_and_advance(
                pool,
                company_id=company_id,
                lead_id=lead_id,
                channel="email",
                body=body,
                external_id=message_id or None,
                raw={"to": lead.email},
                user_id=user.user_id,
                who=_lead_name(lead),
            )
        return ReplyOut(message_sid=message_id, stage="contacted", channel="email")

    # channel == "sms"
    if not lead.phone:
        raise HTTPException(status_code=400, detail="Lead has no phone number to text.")
    lead_phone: str = lead.phone  # narrowed local for the closure
    # ⚠ A2P 10DLC registration covers EVERY application-to-person message
    # from the registered number, and this reply goes out from the SAME
    # ``from_number`` as the templated customer-care lane. This path used to
    # check ``configured`` ONLY, so the moment Twilio credentials landed in
    # Render the CRM would have started texting leads — before carrier
    # approval, which is exactly what the dark flag exists to prevent. The
    # predicate is shared with ``comms.sms.send_sms`` so the two lanes cannot
    # disagree about whether the channel may send.
    # ⚠ THIS company's own number, never a deployment-global one. Sending
    # from a shared number puts our message in another company's conversation
    # AND routes the recipient's reply back to them.
    telephony = await telephony_for_company(pool, company_id, twilio_settings=settings.twilio)
    if telephony is None or not telephony.can_send:
        raise HTTPException(
            status_code=503,
            detail=(
                "This company has no phone number of its own yet, so a text cannot be "
                "sent. An admin can add one under Settings."
            ),
        )
    # ⚠ THE SAME PREDICATE THE CUSTOMER-CARE LANE USES. Two lanes send from
    # the same registered number, so a rule that reaches one and not the other
    # is gotcha 67 repeating — that is exactly how this route came to check
    # `configured` while `send_sms` checked `enabled`.
    channel_block = await outbound_block_for_company(get_pool(), company_id, settings.twilio)
    if channel_block == "disabled":
        raise HTTPException(
            status_code=503,
            detail=(
                "Texting is not live yet: the A2P 10DLC campaign has not been approved, "
                "so no messages may leave this number. Finish setup in "
                "Settings → Connectors → Twilio SMS."
            ),
        )
    if channel_block == "campaign_pending":
        raise HTTPException(
            status_code=503,
            detail=(
                "Your A2P 10DLC campaign is still with the carriers, so no texts may "
                "leave your number yet. Registration review usually takes 10-15 days; "
                "you will be able to reply by text as soon as it clears."
            ),
        )
    if channel_block is not None:
        raise HTTPException(status_code=503, detail="Twilio is not configured on this backend.")

    # ⚠ THE RECIPIENT, not the channel. Everything above asks whether this
    # company may send at all; this asks whether we may text THIS person right
    # now, and the Leasing lane had neither half of it:
    #
    #   * a lead who replied STOP kept receiving messages, because the ledger
    #     UPDATE only touches rows that already had consent and no CRM path
    #     ever writes one (migration 0086 is the record that does not need a
    #     prior grant);
    #   * there was no quiet-hours check anywhere on this lane, so it would
    #     text a lead at 3am — a per-message statutory penalty.
    #
    # Deliberately NOT the consent ledger's "must have a live row" rule: a lead
    # who texts in first has started a conversation and replying to it is
    # conversational traffic, not a campaign blast. The floor still applies.
    recipient = await opt_out.recipient_block(pool, phone=lead_phone)
    if recipient == opt_out.OPTED_OUT:
        raise HTTPException(
            status_code=409,
            detail=(
                "This person asked us to stop texting them, so we can't send to that "
                "number. They can text START to resume. You can still call or email."
            ),
        )
    if recipient == opt_out.QUIET_HOURS:
        raise HTTPException(
            status_code=409,
            detail=(
                "It's outside texting hours where they are — messages are only allowed "
                "between 8am and 9pm in the recipient's own time zone. Try again in the "
                "morning, or give them a call."
            ),
        )
    # ⚠ A FIRST TEXT NEEDS PERMISSION. The paragraph above is right that a lead
    # who texted in has started a conversation. But a lead added by hand, or
    # from a listing site or an email, never did, and the route texted them
    # anyway: the public SMS terms (helixis-onboarding Legal.tsx) say no message
    # goes to any number without a live consent record and that nobody can
    # bypass it, and the registered campaign covers residents, applicants and
    # vendors, not cold prospects.
    if not await _lead_may_be_texted(
        pool, company_id=company_id, lead_id=lead_id, phone=lead_phone
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "This lead hasn't texted you and hasn't agreed to texts, so Occupella "
                "can't start a text conversation with them. Email or call them instead, "
                "or ask them to text your number first."
            ),
        )

    async def _send_sms() -> dict[str, Any]:
        try:
            sid = await twilio_client.send_sms(
                account_sid=settings.twilio.account_sid,
                auth_token=settings.twilio.auth_token,
                from_number=telephony.from_number,
                # A Messaging Service is a number POOL: new numbers are added
                # in the Twilio console, not as a Render env var per number.
                # This lane ignored it entirely, so a pool-only deployment
                # failed every reply while comms/sms.py sent fine. It is only
                # populated on a single-tenant deployment — see
                # telephony_for_company for why a pool cannot be per-company.
                messaging_service_sid=telephony.messaging_service_sid,
                to=lead_phone,
                body=body,
            )
        except twilio_client.TwilioSendError as exc:
            if exc.pre_side_effect:
                # Twilio provably did NOT create the message (config / 4xx) —
                # the claim stays retryable so the user can fix and resend.
                raise PreSideEffectError(str(exc)) from exc
            raise  # network / 5xx: the send MAY have landed → ambiguous
        return {"sid": sid}

    try:
        guard = await guarded_action(
            store=_crm_idempotency_store(pool),
            company_id=company_id,
            idem_key=derive_key(company_id, "crm.reply_sms", {"lead_id": lead_id, "body": body}),
            action_type="crm.reply_sms",
            confirm=None,  # approved in the CRM UI
            execute=_send_sms,
            window=_REPLY_DEDUP_WINDOW,
        )
    except (twilio_client.TwilioSendError, PreSideEffectError) as exc:
        raise HTTPException(status_code=502, detail=f"Could not send the reply: {exc}") from exc
    result = _guard_result_or_raise(guard, noun="reply")
    sid = str(result.get("sid") or "")
    if guard.outcome == "executed":
        await _log_outbound_and_advance(
            pool,
            company_id=company_id,
            lead_id=lead_id,
            channel="sms",
            body=body,
            external_id=sid or None,
            raw={"to": lead.phone},
            user_id=user.user_id,
            who=_lead_name(lead),
        )
    return ReplyOut(message_sid=sid, stage="contacted", channel="sms")


# ── click-to-call (Twilio bridge: ring the PM, connect to the lead) ──


class CallOut(BaseModel):
    call_sid: str


@router.post("/leads/{lead_id}/call", response_model=CallOut)
async def call_lead(
    lead_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> CallOut:
    """Click-to-call. Places an outbound Twilio call that rings the PM's cell
    (``forward_to_number``) first; when they answer, Twilio bridges them to the
    lead's number. The call originates from the business ``from_number`` and is
    logged as an outbound 'call' on the lead. 400 if the lead has no phone; 503
    if Twilio / the forwarding number isn't configured; 502 if the call fails."""
    settings = _settings_from(request)
    _require_crm_flag(settings)
    _require_valid_lead_id(lead_id)
    company_id = await _primary_company_id(user)

    pool = get_pool()
    # Role floor (approval-pipeline PR): placing a call takes manager+.
    from helixis.api.roles import MANAGER
    from helixis.api.routes.company import require_role

    await require_role(
        pool,
        user.user_id,
        company_id,
        MANAGER,
        detail="Only managers and admins can place calls.",
    )
    lead = await leads_repo.get_lead(pool=pool, company_id=company_id, lead_id=lead_id)
    if lead is None:
        raise HTTPException(status_code=404)
    if not lead.phone:
        raise HTTPException(status_code=400, detail="Lead has no phone number to call.")
    lead_phone: str = lead.phone  # narrowed local for the closure
    if not settings.twilio.configured:
        raise HTTPException(status_code=503, detail="Twilio is not configured on this backend.")
    # ⚠ Per-company, both halves. A shared forward_to_number rings ONE cell for
    # every company on the deployment — customer B's lead would connect a live
    # call to customer A's phone.
    telephony = await telephony_for_company(pool, company_id, twilio_settings=settings.twilio)
    if telephony is None or not telephony.from_number:
        raise HTTPException(
            status_code=503,
            detail="This company has no phone number of its own yet — an admin can add one.",
        )
    forward_to = telephony.forward_to_number
    if not forward_to:
        raise HTTPException(
            status_code=503,
            detail="Set a forwarding number (your cell) to use click-to-call.",
        )

    # ⚠ THE RECIPIENT, and this lane needs it MORE than the text one does.
    # The 8am-9pm rule was written for CALLS, and a phone ringing at 3am is
    # the version of this mistake somebody actually wakes up for. Before this,
    # click-to-call had no opt-out check and no clock at all — a lead who
    # replied STOP could still be cold-called at any hour.
    #
    # ⚠ An SMS opt-out blocks the CALL too, deliberately. Somebody who told us
    # to stop contacting them did not mean "stop texting but do ring me", and
    # over-blocking costs one call the agent is told about while under-
    # blocking is a per-call statutory penalty. Email is untouched.
    recipient = await opt_out.recipient_block(pool, phone=lead_phone)
    if recipient == opt_out.OPTED_OUT:
        raise HTTPException(
            status_code=409,
            detail=(
                "This person asked us to stop contacting them, so we can't call that "
                "number. Email still works, and they can text START to resume."
            ),
        )
    if recipient == opt_out.QUIET_HOURS:
        raise HTTPException(
            status_code=409,
            detail=(
                "It's outside calling hours where they are — calls are only allowed "
                "between 8am and 9pm in the recipient's own time zone. Try again in "
                "the morning."
            ),
        )

    async def _place_call() -> dict[str, Any]:
        try:
            sid = await twilio_client.create_call(
                account_sid=settings.twilio.account_sid,
                auth_token=settings.twilio.auth_token,
                from_number=telephony.from_number,
                to=forward_to,  # ring the PM first
                twiml=twilio_client.bridge_twiml(
                    to=lead_phone, message="Connecting you to your lead."
                ),
            )
        except twilio_client.TwilioCallError as exc:
            if exc.pre_side_effect:
                raise PreSideEffectError(str(exc)) from exc
            raise  # network / 5xx: the call MAY have been placed → ambiguous
        return {"sid": sid}

    try:
        guard = await guarded_action(
            store=_crm_idempotency_store(pool),
            company_id=company_id,
            idem_key=derive_key(company_id, "crm.call", {"lead_id": lead_id, "to": forward_to}),
            action_type="crm.call",
            confirm=None,  # user clicked call in the CRM UI
            execute=_place_call,
            window=_CALL_DEDUP_WINDOW,
        )
    except (twilio_client.TwilioCallError, PreSideEffectError) as exc:
        raise HTTPException(status_code=502, detail=f"Could not place the call: {exc}") from exc
    result = _guard_result_or_raise(guard, noun="call")
    sid = str(result.get("sid") or "")
    if guard.outcome == "executed":
        await leads_repo.insert_message(
            pool,
            MessageRow(
                company_id=company_id,
                lead_id=lead_id,
                direction="outbound",
                channel="call",
                body="📞 Called lead",
                external_id=sid or None,
                raw={"to": lead.phone, "via": "click-to-call"},
            ),
        )
        await _log_crm_activity(
            pool,
            company_id=company_id,
            user_id=user.user_id,
            action_type="crm_call_placed",
            description=f"Called {_lead_name(lead)}",
            lead_id=lead_id,
        )
    return CallOut(call_sid=sid)


# ── manual controls (hand-run the pipeline) ──────────────────────────
#
# The board self-updates on verified facts, but the PM still needs to
# override: move a lead by hand, drop one that went nowhere, or type in a
# lead from a channel Helixis doesn't watch. These three writes are that
# override. A manual stage move is NOT a "verified fact" — it just sets the
# column; the auto-move rules (reply/calendar/Buildium) are unaffected.


class MoveStageIn(BaseModel):
    stage: str


@router.post("/leads/{lead_id}/stage", response_model=LeadOut)
async def move_lead_stage(
    lead_id: str,
    payload: MoveStageIn,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> LeadOut:
    """Manually move a lead to any stage (the drawer stage picker). 400 on an
    unknown stage; 404 if the lead is missing or cross-tenant. Returns the
    updated lead."""
    settings = _settings_from(request)
    _require_crm_flag(settings)
    _require_valid_lead_id(lead_id)
    company_id = await _primary_company_id(user)
    stage = (payload.stage or "").strip()
    if stage not in leads_repo.LEAD_STAGES:
        raise HTTPException(status_code=400, detail=f"Unknown stage: {payload.stage!r}")
    pool = get_pool()
    lead = await leads_repo.get_lead(pool=pool, company_id=company_id, lead_id=lead_id)
    if lead is None:
        raise HTTPException(status_code=404)
    await leads_repo.update_stage(pool, company_id=company_id, lead_id=lead_id, stage=stage)
    stage_label = dict(_STAGES).get(stage, stage)
    await _log_crm_activity(
        pool,
        company_id=company_id,
        user_id=user.user_id,
        action_type="crm_stage_moved",
        description=f"Moved {_lead_name(lead)} to {stage_label}",
        lead_id=lead_id,
    )
    updated = await leads_repo.get_lead(pool=pool, company_id=company_id, lead_id=lead_id)
    return _lead_out(updated or lead)


class SetStatusIn(BaseModel):
    status: str


@router.post("/leads/{lead_id}/status", response_model=LeadOut)
async def update_lead_status(
    lead_id: str,
    payload: SetStatusIn,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> LeadOut:
    """Mark a lead ``lost`` (won't-pursue → drops off the board, kept for
    records) or restore it to ``active``. 400 on an unknown status; 404 if the
    lead is missing or cross-tenant. Returns the updated lead."""
    settings = _settings_from(request)
    _require_crm_flag(settings)
    _require_valid_lead_id(lead_id)
    company_id = await _primary_company_id(user)
    status = (payload.status or "").strip()
    if status not in leads_repo.LEAD_STATUSES:
        raise HTTPException(status_code=400, detail=f"Unknown status: {payload.status!r}")
    pool = get_pool()
    updated = await leads_repo.set_lead_status(
        pool, company_id=company_id, lead_id=lead_id, status=status
    )
    if not updated:
        raise HTTPException(status_code=404)
    if status == "lost":
        # The task is complete. A drafted reply on a closed lead points at a
        # conversation nobody will continue, and it is that conversation's
        # personal data kept past its purpose. Restoring the lead to active
        # regenerates it on the next open, from the whole thread.
        await leads_repo.clear_lead_assist(
            pool, company_id=company_id, lead_id=lead_id, reason="lead closed"
        )
    lead = await leads_repo.get_lead(pool=pool, company_id=company_id, lead_id=lead_id)
    if lead is None:
        raise HTTPException(status_code=404)
    action = "Marked" if status == "lost" else "Restored"
    tail = "lost" if status == "lost" else "to active"
    await _log_crm_activity(
        pool,
        company_id=company_id,
        user_id=user.user_id,
        action_type="crm_status_changed",
        description=f"{action} {_lead_name(lead)} {tail}",
        lead_id=lead_id,
    )
    return _lead_out(lead)


class CreateLeadIn(BaseModel):
    name: str = ""
    kind: str = "renter"
    source: str = "manual"
    stage: str = "new"
    phone: str | None = None
    email: str | None = None
    unit: str | None = None
    note: str | None = None


@router.post("/leads", response_model=LeadOut)
async def create_lead(
    payload: CreateLeadIn,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> LeadOut:
    """Hand-enter a lead (the "＋ Add lead" form) — for a walk-in, a phone
    call, or any channel Helixis doesn't watch. Validates kind/stage/source
    against the closed sets; requires at least one of name/phone/email so the
    lead is identifiable. 400 on a bad field or an empty lead; 500 if the
    insert fails."""
    settings = _settings_from(request)
    _require_crm_flag(settings)
    company_id = await _primary_company_id(user)

    name = (payload.name or "").strip()
    phone = (payload.phone or "").strip() or None
    email = (payload.email or "").strip() or None
    if not (name or phone or email):
        raise HTTPException(status_code=400, detail="A lead needs a name, phone, or email.")
    if payload.kind not in leads_repo.LEAD_KINDS:
        raise HTTPException(status_code=400, detail=f"Unknown kind: {payload.kind!r}")
    if payload.stage not in leads_repo.LEAD_STAGES:
        raise HTTPException(status_code=400, detail=f"Unknown stage: {payload.stage!r}")
    if payload.source not in leads_repo.LEAD_SOURCES:
        raise HTTPException(status_code=400, detail=f"Unknown source: {payload.source!r}")

    new_lead = LeadRow(
        company_id=company_id,
        name=name,
        kind=payload.kind,
        stage=payload.stage,
        source=payload.source,
        email=email,
        phone=phone,
        unit_label=(payload.unit or "").strip() or None,
        note=(payload.note or "").strip() or None,
        raw={"created_by": user.user_id, "manual": True},
    )
    pool = get_pool()
    lead_id = await leads_repo.insert_lead(pool, new_lead)
    if not lead_id:
        raise HTTPException(status_code=500, detail="Could not create the lead.")
    await _log_crm_activity(
        pool,
        company_id=company_id,
        user_id=user.user_id,
        action_type="crm_lead_created",
        description=f"Added lead: {_lead_name(new_lead)}",
        lead_id=lead_id,
    )
    created = await leads_repo.get_lead(pool=pool, company_id=company_id, lead_id=lead_id)
    return _lead_out(created) if created else _lead_out(new_lead)


# ── inbound SMS (Twilio webhook) ─────────────────────────────────────


async def _ingest_inbound_sms(
    *,
    pool: asyncpg.Pool,
    company_id: str,
    from_number: str,
    body: str,
    external_id: str | None,
) -> str | None:
    """Find-or-create a lead by phone, then log the inbound message.
    Returns the lead id (or None if the lead couldn't be created)."""
    lead = await leads_repo.find_lead_by_phone(pool=pool, company_id=company_id, phone=from_number)
    lead_id: str | None
    if lead is not None and lead.id:
        lead_id = lead.id
    else:
        new_lead = lead_triage.build_lead_from_sms(from_number, body, company_id)
        lead_id = await leads_repo.insert_lead(pool, new_lead)
    if not lead_id:
        return None
    await leads_repo.insert_message(
        pool,
        MessageRow(
            company_id=company_id,
            lead_id=lead_id,
            direction="inbound",
            channel="sms",
            body=body,
            external_id=external_id,
            raw={"from": from_number},
        ),
    )
    await _flag_hazards_if_any(
        pool,
        company_id=company_id,
        text=body,
        channel="sms",
        lead_id=lead_id,
        external_id=external_id,
    )
    return lead_id


@router.post("/sms/inbound")
async def inbound_sms(request: Request) -> Response:
    """Twilio inbound-SMS webhook. NOT JWT-authed (Twilio can't send one) —
    instead the request's ``X-Twilio-Signature`` is verified against the
    account auth token. A new text find-or-creates a lead by phone and logs
    the message; it never sends anything back.

    Company routing: an inbound text has no user, so its company is resolved
    from the number it was sent TO. v1 uses the single configured
    ``twilio.default_company_id``; multi-number → multi-company routing is a
    later refinement. Always returns an empty 200 (Twilio treats non-2xx as
    a failure and retries)."""
    settings = _settings_from(request)
    _require_crm_flag(settings)

    form = await request.form()
    params = {k: str(v) for k, v in form.items()}
    from_number = params.get("From", "").strip()
    body = params.get("Body", "").strip()
    message_sid = params.get("MessageSid") or params.get("SmsSid") or None

    # Verify the request really came from Twilio. The signed URL is the public
    # URL Twilio POSTed to.
    #
    # ⚠ NO ``skip_signature_check`` bypass on this route any more. It used to
    # honour the flag, which meant a deployment with the testing flag set
    # accepted ANY unsigned POST here and created leads from it. Worse now that
    # this route also mirrors STOP/START below: an unsigned request could forge
    # a consent revocation — the reason ``api/routes/twilio.py`` has always
    # refused a bypass. The ``/sms/simulate`` route is still gated on the flag
    # and remains the keyless testing path.
    signature = request.headers.get("X-Twilio-Signature")
    valid = twilio_client.validate_signature(
        settings.twilio.auth_token, str(request.url), params, signature
    )
    if not valid:
        raise HTTPException(status_code=403, detail="Invalid Twilio signature.")

    pool = get_pool()

    # ⚠ Company comes from the number this was sent TO, not from a global env
    # var. ``default_company_id`` was ONE id for the whole deployment, so with
    # a second customer every inbound text filed into whichever company it
    # named. ``resolve_company_for_number`` declines rather than guessing; a
    # dropped inbound is recoverable by a human, one filed in the wrong
    # company's account is not.
    company_id = await resolve_company_for_number(
        pool, params.get("To", ""), twilio_settings=settings.twilio
    )
    if not company_id:
        # Nothing we can safely act on. 200 so Twilio doesn't retry a request
        # that will fail identically every time.
        return Response(status_code=200)
    if not from_number:
        return Response(status_code=200)

    # Consent FIRST — see ``webhooks/crm_sms_inbound``. A Twilio number has one
    # inbound URL, so whichever of the two routes the console names must do the
    # whole job; the half whose omission is unlawful goes first.
    # ⚠ ``company_id`` is what makes a lead's STOP durable. The ledger UPDATE
    # touches only rows that already had consent, and no CRM path ever writes
    # one — so before this, a lead texting STOP left no record anywhere and
    # kept receiving replies.
    await apply_consent_keywords(
        pool,
        from_number=from_number,
        body=body,
        company_id=company_id,
        message_sid=message_sid or "",
    )

    await handle_inbound_lead_sms(
        params,
        settings=settings,
        pool=pool,
        company_id=company_id,
        from_number=from_number,
        body=body,
        message_sid=message_sid,
    )

    # Empty TwiML — accepted, no auto-reply.
    return Response(content="<Response></Response>", media_type="application/xml")


# ── simulate inbound (keyless testing) ───────────────────────────────


class SimulateIn(BaseModel):
    from_number: str
    body: str


class SimulateOut(BaseModel):
    lead_id: str | None


class ApplicantStatusIn(BaseModel):
    status: str
    application_id: int | None = None


class ApplicantStatusOut(BaseModel):
    ok: bool
    status: str
    application_id: int


@router.post("/leads/{lead_id}/applicant-status", response_model=ApplicantStatusOut)
async def set_applicant_status(
    lead_id: str,
    payload: ApplicantStatusIn,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> ApplicantStatusOut:
    """Push a decision on a Buildium-linked lead back to Buildium — the
    reverse of the applicant auto-move. Allowed: Undecided / Approved /
    Rejected / Cancelled / Deferred; the Buildium API has NO delete
    operation on applications, so deletion is impossible by construction.

    Fires only from an explicit user action in the CRM drawer. Mirror +
    lead-stage freshness rides the ApplicantApplication.Updated webhook
    the write triggers. 400 on an unlinked lead or a bad status; 409 when
    the applicant has several applications and none was chosen (the body
    lists them); 502 on a Buildium failure."""
    from helixis.buildium.endpoints.applicants import Applicants
    from helixis.webhooks.handlers._helpers import open_api_for_company

    settings = _settings_from(request)
    _require_crm_flag(settings)
    _require_valid_lead_id(lead_id)
    company_id = await _primary_company_id(user)
    if payload.status not in Applicants.APPLICATION_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"Status must be one of {', '.join(Applicants.APPLICATION_STATUSES)}.",
        )
    pool = get_pool()
    lead = await leads_repo.get_lead(pool=pool, company_id=company_id, lead_id=lead_id)
    if lead is None:
        raise HTTPException(status_code=404)
    if lead.buildium_applicant_id is None:
        raise HTTPException(
            status_code=400, detail="This lead isn't linked to a Buildium applicant."
        )

    applicant_id = lead.buildium_applicant_id
    async with open_api_for_company(company_id) as api:
        if api is None:
            raise HTTPException(
                status_code=503,
                detail="Buildium isn't connected — connect it in Settings → Connectors.",
            )
        try:
            applications = await api.applicants.list_applications(applicant_id)
        except Exception as exc:  # noqa: BLE001 — surfaced as a clean 502
            logger.exception("applicant-status: listing applications failed")
            raise HTTPException(
                status_code=502, detail="Couldn't reach Buildium — please try again."
            ) from exc
        app_ids = [a.get("Id") for a in applications if isinstance(a.get("Id"), int)]
        if not app_ids:
            raise HTTPException(
                status_code=400, detail="This applicant has no applications in Buildium."
            )
        if payload.application_id is not None:
            if payload.application_id not in app_ids:
                raise HTTPException(
                    status_code=400,
                    detail=f"Application {payload.application_id} isn't on this applicant.",
                )
            target_id = payload.application_id
        elif len(app_ids) == 1:
            target_id = app_ids[0]
        else:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Applicant has multiple applications — pick one.",
                    "applications": [
                        {"id": a.get("Id"), "status": a.get("ApplicationStatus")}
                        for a in applications
                    ],
                },
            )
        try:
            await api.applicants.update_application_status(applicant_id, target_id, payload.status)
        except Exception as exc:  # noqa: BLE001
            logger.exception("applicant-status: Buildium PUT failed")
            raise HTTPException(
                status_code=502, detail="Buildium rejected the update — please try again."
            ) from exc

    await _log_crm_activity(
        pool,
        company_id=company_id,
        user_id=user.user_id,
        action_type="crm_applicant_status",
        description=(
            f"Set {lead.name or 'lead'}'s Buildium application #{target_id} to {payload.status}"
        ),
        lead_id=lead_id,
    )
    return ApplicantStatusOut(ok=True, status=payload.status, application_id=target_id)


@router.post("/sms/simulate", response_model=SimulateOut)
async def simulate_inbound_sms(
    payload: SimulateIn,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> SimulateOut:
    """Inject a fake inbound SMS as the caller's company — so the whole
    lead→board→thread loop can be exercised WITHOUT a real Twilio number.
    Auth'd (a logged-in dev only) and gated on
    ``twilio.skip_signature_check`` so it can never run in a hardened prod
    config. Mirrors the email-ingest ALLOW_ALL testing flag."""
    settings = _settings_from(request)
    _require_crm_flag(settings)
    if not settings.twilio.skip_signature_check:
        # This is a testing affordance; it must be explicitly enabled.
        raise HTTPException(status_code=404)
    company_id = await _primary_company_id(user)
    lead_id = await _ingest_inbound_sms(
        pool=get_pool(),
        company_id=company_id,
        from_number=(payload.from_number or "").strip(),
        body=(payload.body or "").strip(),
        external_id=None,
    )
    return SimulateOut(lead_id=lead_id)


# ── inbound voice (Twilio call → lead, forward, voicemail) ───────────
#
# The third intake channel. A lead calls the company's Twilio number → the
# call hits POST /crm/voice/inbound. Helixis logs the lead by caller number,
# then returns TwiML that FORWARDS the call to the PM's cell
# (``forward_to_number``). If the PM doesn't answer, Twilio POSTs
# /crm/voice/status with DialCallStatus=no-answer and we return voicemail
# TwiML; the recording lands on /crm/voice/voicemail and is attached to the
# lead. Same signature-verification + company-routing rules as inbound SMS.


async def _twilio_form(request: Request) -> dict[str, str]:
    form = await request.form()
    return {k: str(v) for k, v in form.items()}


def _verify_twilio_or_403(request: Request, settings: Settings, params: dict[str, str]) -> None:
    """403 a voice webhook whose ``X-Twilio-Signature`` doesn't verify.

    ⚠ **NO ``skip_signature_check`` bypass, deliberately.** It used to return
    early here, so a deployment carrying that flag accepted any unsigned POST
    to all four REAL voice webhooks — anyone could forge an inbound call, a
    voicemail with a recording URL of their choosing, or a transcription
    attributed to a company they have nothing to do with. Gotcha 68 removed the
    identical bypass from the CRM's SMS inbound route for the same reason; the
    voice half was missed, and a phone number's webhooks are the same class of
    unauthenticated public surface.

    The three ``/simulate`` routes still REQUIRE that flag to exist at all,
    which is the safe direction: they are gated ON it rather than gated BY it.
    """
    signature = request.headers.get("X-Twilio-Signature")
    if not twilio_client.validate_signature(
        settings.twilio.auth_token, str(request.url), params, signature
    ):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature.")


#: Said to a caller when we cannot establish WHOSE line they dialled.
#: Deliberately names nobody: a greeting that claims the wrong company is
#: worse than one that claims none.
_NEUTRAL_GREETING = (
    "Sorry we missed you. Please leave a message after the tone and we'll call you right back."
)


def _twiml(xml: str) -> Response:
    return Response(content=xml, media_type="application/xml")


async def _company_greeting(to_number: str, settings: Settings) -> str:
    """The voicemail greeting for whoever owns the number that was CALLED.

    ⚠ Falls back to the NEUTRAL default, never to another company's greeting.
    ``settings.twilio.voice_greeting`` is one string for the whole deployment,
    so on a white-label install it is either empty or it belongs to exactly one
    customer — and playing that customer's greeting to somebody who dialled a
    different company is a brand leak the caller hears out loud.

    The generic wording is the honest fallback: it says nothing untrue about
    whose line this is.
    """
    if not to_number.strip():
        return _NEUTRAL_GREETING
    try:
        pool = get_pool()
        company_id = await resolve_company_for_number(
            pool, to_number, twilio_settings=settings.twilio
        )
        # ⚠ ``telephony_for_company`` is what preserves the SINGLE-TENANT
        # operator's configured greeting: with an empty mapping table it
        # returns the deployment globals, and only then. Reading
        # ``settings.twilio.voice_greeting`` here directly would hand that one
        # string to every company on a multi-tenant install, which is the leak.
        telephony = await telephony_for_company(
            pool, company_id or "", twilio_settings=settings.twilio
        )
    except Exception:  # noqa: BLE001 — a caller is on the line; say something
        logger.warning("could not resolve a company greeting for an inbound call")
        return _NEUTRAL_GREETING
    return (telephony.voice_greeting if telephony else "").strip() or _NEUTRAL_GREETING


def _duration_label(seconds: str | None) -> str:
    """ "42" → "0:42"; unparseable → ""."""
    try:
        s = int(seconds or "")
    except (TypeError, ValueError):
        return ""
    return f"{s // 60}:{s % 60:02d}"


async def _lead_id_for_caller(
    *, pool: asyncpg.Pool, company_id: str, from_number: str
) -> str | None:
    """Find-or-create a lead by caller number (the call-intake dedup key)."""
    lead = await leads_repo.find_lead_by_phone(pool=pool, company_id=company_id, phone=from_number)
    if lead is not None and lead.id:
        return lead.id
    return await leads_repo.insert_lead(
        pool, lead_triage.build_lead_from_call(from_number, company_id)
    )


async def _ingest_inbound_call(
    *, pool: asyncpg.Pool, company_id: str, from_number: str, call_sid: str | None
) -> str | None:
    """Log an inbound call: find-or-create the lead, append a 'call' message.
    Returns the lead id (or None if it couldn't be created)."""
    lead_id = await _lead_id_for_caller(pool=pool, company_id=company_id, from_number=from_number)
    if not lead_id:
        return None
    await leads_repo.insert_message(
        pool,
        MessageRow(
            company_id=company_id,
            lead_id=lead_id,
            direction="inbound",
            channel="call",
            body="📞 Inbound call",
            external_id=call_sid,
            raw={"from": from_number},
        ),
    )
    return lead_id


@router.post("/voice/inbound")
async def inbound_voice(request: Request) -> Response:
    """Twilio inbound-call webhook (NOT JWT-authed — signature-verified). Logs
    the lead by caller number, then forwards the call to the PM's cell; a
    missed call falls through to voicemail via the Dial ``action``. Company is
    resolved from the number that was CALLED (``helixis_twilio_numbers``), and
    the forwarding cell + greeting come from that company's row."""
    settings = _settings_from(request)
    _require_crm_flag(settings)
    params = await _twilio_form(request)
    _verify_twilio_or_403(request, settings, params)

    from_number = params.get("From", "").strip()
    call_sid = params.get("CallSid") or None
    pool = get_pool()
    # ⚠ The company owns the number that was CALLED. A deployment-global
    # default filed every company's inbound calls under one company — and then
    # forwarded them all to one cell, which is a live voice connection to the
    # wrong person. Declines rather than guessing.
    company_id = await resolve_company_for_number(
        pool, params.get("To", ""), twilio_settings=settings.twilio
    )
    if not company_id:
        logger.warning("Inbound call dropped: no company owns the number that was called.")
        return _twiml(twilio_client.say_hangup_twiml("Thanks for calling. Goodbye."))
    if from_number:
        await _ingest_inbound_call(
            pool=pool, company_id=company_id, from_number=from_number, call_sid=call_sid
        )

    telephony = await telephony_for_company(pool, company_id, twilio_settings=settings.twilio)
    forward_to = telephony.forward_to_number if telephony else ""
    # ⚠ Falls back to the NEUTRAL default, never to the deployment-global
    # greeting. That string belongs to at most one customer, so playing it to
    # somebody who dialled a different company is a brand leak the caller
    # hears. (``telephony`` already carries the global on a single-tenant
    # deployment, which is the one install where it IS theirs.)
    greeting = (telephony.voice_greeting if telephony else "").strip() or _NEUTRAL_GREETING
    if forward_to:
        # Forward to the PM; on a miss Twilio calls /voice/status → voicemail.
        return _twiml(
            twilio_client.dial_twiml(
                to=forward_to,
                action_url="/api/v1/crm/voice/status",
                timeout=settings.twilio.dial_timeout_seconds,
            )
        )
    # No forwarding number set → straight to voicemail.
    return _twiml(
        twilio_client.voicemail_twiml(
            greeting=greeting,
            action_url="/api/v1/crm/voice/voicemail",
            transcribe_callback="/api/v1/crm/voice/transcription",
        )
    )


@router.post("/voice/status")
async def voice_status(request: Request) -> Response:
    """The ``<Dial>`` action callback. If the PM answered and the call
    completed, we're done; otherwise (no-answer/busy/failed) send the caller
    to voicemail."""
    settings = _settings_from(request)
    _require_crm_flag(settings)
    params = await _twilio_form(request)
    _verify_twilio_or_403(request, settings, params)

    if params.get("DialCallStatus", "") == "completed":
        return _twiml(twilio_client.empty_twiml())
    # ⚠ THIS COMPANY's greeting, resolved from the number that was CALLED.
    # It used to read the deployment-global ``voice_greeting``, so on a
    # white-label install every missed call played the operator's greeting —
    # or, worse, whichever customer's greeting happened to be in the env var —
    # to somebody who dialled a different company. Gotcha 69 made the greeting
    # per-company on /voice/inbound and this path was missed, and THIS is the
    # common one: a missed call is the whole reason voicemail exists.
    greeting = await _company_greeting(params.get("To", ""), settings)
    return _twiml(
        twilio_client.voicemail_twiml(
            greeting=greeting,
            action_url="/api/v1/crm/voice/voicemail",
            transcribe_callback="/api/v1/crm/voice/transcription",
        )
    )


@router.post("/voice/voicemail")
async def voice_voicemail(request: Request) -> Response:
    """The ``<Record>`` action callback — a missed call left a message. Attach
    the recording to the lead as a 'voicemail' entry, keyed on RecordingSid so
    it doesn't collide with the earlier 'call' row (which used the CallSid)."""
    settings = _settings_from(request)
    _require_crm_flag(settings)
    params = await _twilio_form(request)
    _verify_twilio_or_403(request, settings, params)

    # The company owns the number that was CALLED — same rule as the inbound
    # route above. Twilio carries ``To`` on the Record action callback.
    company_id = await resolve_company_for_number(
        get_pool(), params.get("To", ""), twilio_settings=settings.twilio
    )
    from_number = params.get("From", "").strip()
    recording_url = params.get("RecordingUrl", "").strip()
    call_sid = params.get("CallSid") or None
    recording_sid = params.get("RecordingSid") or (f"{call_sid}-vm" if call_sid else None)
    if company_id and from_number and recording_url:
        lead_id = await _lead_id_for_caller(
            pool=get_pool(), company_id=company_id, from_number=from_number
        )
        if lead_id:
            dur = _duration_label(params.get("RecordingDuration"))
            await leads_repo.insert_message(
                get_pool(),
                MessageRow(
                    company_id=company_id,
                    lead_id=lead_id,
                    direction="inbound",
                    channel="voicemail",
                    body=f"📞 Voicemail{f' · {dur}' if dur else ''}",
                    external_id=recording_sid,
                    raw={
                        "from": from_number,
                        "recording_url": recording_url,
                        "duration": params.get("RecordingDuration", ""),
                    },
                ),
            )
    return _twiml(twilio_client.say_hangup_twiml("Thanks — we'll be in touch shortly. Goodbye."))


@router.post("/voice/transcription")
async def voice_transcription(request: Request) -> Response:
    """Twilio ``transcribeCallback`` — the voicemail's text. Attach it to the
    voicemail message, matched on RecordingSid (the same id the recording
    callback used as ``external_id``). Best-effort: an incomplete/failed
    transcription or an unmatched RecordingSid is simply dropped."""
    settings = _settings_from(request)
    _require_crm_flag(settings)
    params = await _twilio_form(request)
    _verify_twilio_or_403(request, settings, params)

    company_id = await resolve_company_for_number(
        get_pool(), params.get("To", ""), twilio_settings=settings.twilio
    )
    recording_sid = (params.get("RecordingSid") or "").strip()
    text = (params.get("TranscriptionText") or "").strip()
    status = params.get("TranscriptionStatus", "")
    if company_id and recording_sid and text and status == "completed":
        pool = get_pool()
        await leads_repo.set_message_transcription(
            pool,
            company_id=company_id,
            external_id=recording_sid,
            transcription=text,
        )
        # A voicemail's text is the other channel where "GAS LEAK" arrives
        # after hours — same deterministic scan as inbound SMS.
        await _flag_hazards_if_any(
            pool,
            company_id=company_id,
            text=text,
            channel="voicemail",
            lead_id=None,
            external_id=recording_sid,
        )
    return _twiml(twilio_client.empty_twiml())


@router.post("/voice/simulate", response_model=SimulateOut)
async def simulate_inbound_call(
    payload: SimulateIn,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> SimulateOut:
    """Inject a fake inbound call as the caller's company — exercises the
    lead-creation + call-log half WITHOUT a real Twilio number (the forward /
    voicemail TwiML needs a live call). Auth'd + gated on
    ``twilio.skip_signature_check``. ``payload.body`` is ignored for a call."""
    settings = _settings_from(request)
    _require_crm_flag(settings)
    if not settings.twilio.skip_signature_check:
        raise HTTPException(status_code=404)
    company_id = await _primary_company_id(user)
    lead_id = await _ingest_inbound_call(
        pool=get_pool(),
        company_id=company_id,
        from_number=(payload.from_number or "").strip(),
        call_sid=None,
    )
    return SimulateOut(lead_id=lead_id)


class SimulateVoicemailIn(BaseModel):
    from_number: str
    transcription: str = ""
    duration: str = ""


@router.post("/voice/simulate-voicemail", response_model=SimulateOut)
async def simulate_voicemail(
    payload: SimulateVoicemailIn,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> SimulateOut:
    """Inject a fake voicemail (+ optional transcription) onto the caller's lead
    — exercises the voicemail attach + transcription WITHOUT a real Twilio call.
    Auth'd + gated on ``twilio.skip_signature_check`` (keyless testing only)."""
    settings = _settings_from(request)
    _require_crm_flag(settings)
    if not settings.twilio.skip_signature_check:
        raise HTTPException(status_code=404)
    company_id = await _primary_company_id(user)
    pool = get_pool()
    from_number = (payload.from_number or "").strip()
    lead_id = await _ingest_inbound_call(
        pool=pool, company_id=company_id, from_number=from_number, call_sid=None
    )
    if lead_id:
        rec_sid = f"SIMVM-{from_number}"  # deterministic → re-sim updates the same row
        dur = _duration_label(payload.duration)
        await leads_repo.insert_message(
            pool,
            MessageRow(
                company_id=company_id,
                lead_id=lead_id,
                direction="inbound",
                channel="voicemail",
                body=f"📞 Voicemail{f' · {dur}' if dur else ''}",
                external_id=rec_sid,
                raw={"from": from_number, "recording_url": None, "duration": payload.duration},
            ),
        )
        if payload.transcription.strip():
            await leads_repo.set_message_transcription(
                pool,
                company_id=company_id,
                external_id=rec_sid,
                transcription=payload.transcription.strip(),
            )
    return SimulateOut(lead_id=lead_id)


# ── simulate inbound email lead (keyless testing) ────────────────────


class SimulateEmailIn(BaseModel):
    from_email: str
    subject: str = ""
    body: str = ""


class SimulateEmailOut(BaseModel):
    is_lead: bool
    lead_id: str | None


@router.post("/email/simulate", response_model=SimulateEmailOut)
async def simulate_inbound_email(
    payload: SimulateEmailIn,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> SimulateEmailOut:
    """Inject a fake inbound email as the caller's company — so the whole
    email→lead loop can be exercised WITHOUT a live Gmail inquiry. Runs the
    REAL lead gate (``lead_triage.is_lead``); a non-inquiry email returns
    ``is_lead=false`` and creates nothing. Auth'd + gated on
    ``crm.email_ingest_enabled`` so it can't run in a config that hasn't
    opted into email ingestion."""
    settings = _settings_from(request)
    _require_crm_flag(settings)
    if not settings.crm.email_ingest_enabled:
        raise HTTPException(status_code=404)
    company_id = await _primary_company_id(user)

    email = InboundEmail(
        message_id=f"sim-{user.user_id}-{payload.from_email}-{payload.subject}"[:120],
        from_email=(payload.from_email or "").strip(),
        subject=(payload.subject or "").strip(),
        body_text=(payload.body or "").strip(),
        received_at="",
    )
    # Same gate the poll uses. Known-contacts is empty here (no Buildium in the
    # test path) — leads are decided by listing-sender / inquiry keywords.
    if not lead_triage.is_lead(email, frozenset()):
        return SimulateEmailOut(is_lead=False, lead_id=None)
    lead_id = await crm_lead_ingest.ingest_email_as_lead(
        get_pool(), company_id=company_id, email=email
    )
    return SimulateEmailOut(is_lead=True, lead_id=lead_id)


__all__ = ["router"]
