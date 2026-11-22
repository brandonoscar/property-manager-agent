"""Agent invocation endpoints.

POST /agent/run      — Start an agent task, returns SSE stream
POST /agent/confirm  — Approve or deny a confirmation request
POST /agent/cancel   — Cancel a running agent task
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from helixis.agents.document_tools import (
    _SUPPORTED_MIME_TYPES,
    DocumentExtraction,
    extract_document_fields_impl,
)
from helixis.agents.orchestrator import run_agent_streaming
from helixis.agents.tools import RESOLVE_RESOLVED, resolve_confirmation
from helixis.analytics import capture as ph_capture
from helixis.api.auth import AuthenticatedUser, get_current_user, require_company_access
from helixis.api.rate_limit import agent_action_limiter, agent_run_limiter
from helixis.billing import gate as billing_gate
from helixis.common.events import error_event, watcher_update_event
from helixis.common.support import contact_support
from helixis.common.types import AgentRequest, ConfirmationResponse
from helixis.llm import spend
from helixis.security.file_types import content_matches
from helixis.streaming import run_registry
from helixis.streaming.sse import sse_event_generator

router = APIRouter(prefix="/agent", tags=["agent"])

logger = logging.getLogger(__name__)


@router.post("/run")
async def run_agent(
    request: Request,
    body: AgentRequest,
    user: AuthenticatedUser = Depends(get_current_user),
) -> StreamingResponse:
    """Start an agent task and stream events back via SSE.

    The client receives a stream of HelixisEvent objects:
    - thinking, token: LLM reasoning and text
    - tool_start, tool_result: tool activity
    - browser_start, browser_action, browser_done: browser automation
    - confirm: agent needs user approval (pauses until POST /agent/confirm)
    - done: task completed
    - error: something went wrong
    """
    require_company_access(user, body.company_id)

    # Rate limit AFTER auth/authorization so attackers with bad tokens
    # can't consume a real user's slots.
    await agent_run_limiter.check_by_user(request, user)

    # Daily token budget (0050): the one unbounded user-driven surface is
    # gated; webhook processing never is. Fails open on ledger errors.
    settings_for_budget = request.app.state.settings
    if await spend.budget_exceeded(body.company_id, settings_for_budget.llm.daily_token_budget):
        raise HTTPException(
            status_code=429,
            detail=(
                "This company has reached its daily AI usage limit. "
                "It resets at midnight UTC — " + contact_support("if you need more.")
            ),
        )

    # Subscription / trial gate. Sits AFTER the daily budget because that one
    # is an abuse ceiling and this one is a commercial state — a company over
    # its daily tokens should be told that, not told to go and pay.
    #
    # ⚠ Gated on `billing.enforce`, which is a SEPARATE switch from having
    # Stripe keys (gotcha 67: `configured` and `enabled` answer different
    # questions, and reading one for the other is how the CRM texted real
    # leads). Billing can be wired, watched in the meter and proven against
    # Stripe test mode with this false, turning nobody away.
    if settings_for_budget.billing.enforce:
        _block = await billing_gate.check_turn_allowed(body.company_id)
        if _block is not None:
            # 402 Payment Required — distinguishable from the 429 above, so
            # the client can route the user to Billing rather than telling
            # them to wait for midnight.
            raise HTTPException(status_code=402, detail=_block.message)

    settings = request.app.state.settings
    bus = request.app.state.event_bus
    composio_client = request.app.state.composio_client

    # Generate session ID if not provided
    if not body.session_id:
        body.session_id = str(uuid.uuid4())

    session_id = body.session_id

    # ⚠ SESSION OWNERSHIP. `require_company_access` above answers "is the
    # caller in that company", which says NOTHING about who owns this
    # session — and `session_id` is client-supplied (common/types.py). Without
    # this, POSTing another company's session id reaches `bus.create_session`,
    # which is a no-op for a session that already exists, and the response
    # subscribes to it — `subscribe_seq` replays the retained log first, so the
    # caller reads that company's turn. Their chat, their tenants, their
    # Buildium reads.
    #
    # ⚠ And the ids are GUESSABLE: the web client mints `t_${Date.now()}_${n}`
    # (frontend/src/stores/store.ts), a millisecond stamp plus a small counter,
    # so this is not a "you'd have to know the id" mitigation.
    #
    # `/agent/cancel` and `/agent/watch` have carried this check since the
    # cross-tenant ownership audit; that audit scoped itself to confirm+cancel
    # (tests/unit/test_agent_ownership.py) and never covered `/run`.
    #
    # ⚠ The condition differs from theirs by one word, and the difference is
    # the whole route: `owner is None` means a session nobody has run yet,
    # which is the NORMAL case here and must be ALLOWED. Copying watch's
    # `owner is None or ...` would 404 every new conversation.
    if body.session_id:
        from helixis.db import engine as db_engine
        from helixis.db.repositories.orchestrator_runs import get_company_for_session

        try:
            pool = db_engine.get_pool()
        except RuntimeError:
            pool = None
        if pool is not None:
            owner = await get_company_for_session(pool, session_id)
            if owner is not None and owner not in user.company_ids:
                # ⚠ LOG THE MISMATCH. This branch spent its whole life firing
                # for a reason nobody could see: the owner came back as a
                # uuid.UUID and `company_ids` holds str, so the membership test
                # was False for every follow-up turn (see
                # `get_company_for_session`). It raised a 404 whose user-facing
                # text is "session not found" and said nothing anywhere about
                # WHY, so the only evidence was a toast in a demo recording.
                # A deny that cannot be diagnosed is a deny that stays broken.
                logger.warning(
                    "run denied: session %s owner=%r (%s) not in caller companies %r",
                    session_id,
                    owner,
                    type(owner).__name__,
                    user.company_ids,
                )
                raise HTTPException(status_code=404, detail="session not found")
        else:
            logger.warning("run ownership check skipped — no DB pool (session=%s)", session_id)

    # PR #141 — tag the run's entry point for orchestrator_runs
    # telemetry. Default to 'chat' since this route is the chat UI.
    # Callers that already set ``body.source`` (none currently) are
    # honoured.
    if not body.source:
        body.source = "chat"

    # Activation signal (audit Q3): a server-side "agent run started" event with
    # the REAL company_id + user_id. This is the activation funnel's value step
    # ("reached a working task") — emitted here, not just from the frontend
    # "task submitted", so it can't be lost to an ad-blocker or a page-close and
    # carries the true company group. PostHog computes first-run-per-user from
    # its first occurrence; no per-run count query needed. Never raises.
    ph_capture(
        distinct_id=user.user_id,
        event="agent run started",
        properties={"entry_point": body.source, "session_id": session_id},
        company_id=body.company_id or (user.company_ids[0] if user.company_ids else ""),
    )

    # Session lock (live-watch PR): one driving run per thread. Fan-out made
    # the stream shareable, so a second concurrent /agent/run on the same
    # session would no longer just garble one client — it would interleave
    # two runs into every subscriber's stream AND the shared transcript.
    # claim → create_task has no await between them, so the claim is atomic
    # under asyncio; the done-callback releases on success, exception, and
    # cancellation alike.
    if not run_registry.claim(session_id):
        raise HTTPException(
            status_code=409,
            detail=(
                "An agent run is already active in this thread. "
                "Watch it live or wait for it to finish."
            ),
        )

    # Start the agent in a background task. user_id is threaded through so
    # the memory subsystem can tag stored facts with who triggered them.
    run_task = asyncio.create_task(
        run_agent_streaming(
            body,
            settings,
            bus,
            composio_client=composio_client,
            user_id=user.user_id,
        )
    )
    run_task.add_done_callback(lambda _t: run_registry.release(session_id))

    # Return SSE stream
    return StreamingResponse(
        sse_event_generator(bus, session_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "X-Session-Id": session_id,
        },
    )


@router.post("/confirm")
async def confirm_action(
    request: Request,
    body: ConfirmationResponse,
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict:
    """Approve or deny a pending confirmation request.

    When the agent emits a { type: "confirm" } event, it pauses
    and waits for the user to respond here.
    """
    await agent_action_limiter.check_by_user(request, user)

    # Ownership gate: a confirmation may only be resolved by a member of the
    # company whose run surfaced it. Without this, any authenticated user could
    # approve/deny another tenant's pending confirm — and an approval drives a
    # side-effecting write (Buildium/Gmail).
    #
    # FAIL-CLOSED (LED-014): a real company-scoped confirm ALWAYS has a durable
    # row (confirm_or_abort writes it when it raises). So when the store is
    # available, a missing row, a foreign-company row, or a lookup error (which
    # the try/except collapses to None) all mean we cannot attribute this write
    # to the caller — deny rather than fall through to resolve it. 404 so we
    # don't leak whether the confirm_id exists. The ONLY fall-through is when the
    # durable store is entirely absent (no DB pool) — denying every confirm
    # during a DB outage is a worse failure, and the in-process Future still
    # requires the live run + the server-minted UUID.
    from helixis.db.repositories.pending_confirmations import store_from_pool

    _store = store_from_pool()
    if _store is not None:
        try:
            _row = await _store.get(confirm_id=body.confirm_id)
        except Exception:  # noqa: BLE001 — a lookup error must deny, not resolve
            _row = None
        if _row is None or _row.company_id not in user.company_ids:
            raise HTTPException(status_code=404, detail="confirmation not found")
        # Role floor (approval-pipeline PR): an APPROVAL drives a side-effecting
        # write, so it takes manager-or-above — membership alone is no longer
        # enough. A DECLINE stays open to any member (fail-closed direction:
        # blocking a member from stopping an action would be backwards). Uses
        # the same require_role gate as the company-admin routes (ADR 0005).
        if body.approved:
            from helixis.api.roles import MANAGER
            from helixis.api.routes.company import require_role
            from helixis.db.engine import get_pool

            await require_role(
                get_pool(),
                user.user_id,
                _row.company_id,
                MANAGER,
                detail="Only managers and admins can approve agent actions.",
            )
    else:
        # Durable store unavailable (no DB pool). We can verify NEITHER
        # ownership NOR the manager role floor here — both need the DB. An
        # APPROVAL drives a side-effecting write (Buildium/Gmail), so fail
        # CLOSED (#70): refuse it rather than let an unverified caller approve
        # during a DB outage. A DECLINE still proceeds — blocking someone from
        # STOPPING an action would be backwards, and a decline has no side
        # effect. (Previously BOTH paths fell through unchecked, which let a
        # member drive a write with no role gate whenever the store was down.)
        if body.approved:
            logger.warning(
                "confirm approval refused — durable store unavailable, cannot verify "
                "ownership/role (confirm_id=%s)",
                body.confirm_id,
            )
            # Literal 503 (not ``status.HTTP_503_*``): a local ``status``
            # variable is assigned later in this function, so the name is
            # function-local here and the fastapi ``status`` module is shadowed.
            raise HTTPException(
                status_code=503,
                detail="Can't verify authorization right now — please retry shortly.",
            )
        logger.warning(
            "confirm decline allowed without ownership check — durable store "
            "unavailable (confirm_id=%s)",
            body.confirm_id,
        )

    # ``resolve`` reports whether a live, waiting run actually received the
    # answer. "orphaned" means the run that surfaced this confirm died (e.g. a
    # redeploy) before the user answered — the action will NOT run, so we tell
    # the client honestly rather than letting the UI hang on a silent no-op.
    resolve = await resolve_confirmation(
        body.confirm_id,
        body.approved,
        reason=(body.reason or "").strip() or None,
        resolved_by=user.user_id,
        # edit-before-approve: forwarded raw; confirm_or_abort filters to the
        # gate's declared-editable fields and caps value length, so nothing
        # here can widen what the executor accepts.
        edits=body.edits,
    )
    ph_capture(
        distinct_id=user.user_id,
        event="confirmation resolved",
        properties={
            "confirm_id": body.confirm_id,
            "approved": body.approved,
            "resolve": resolve,
        },
        company_id=user.company_ids[0] if user.company_ids else "",
    )
    if resolve == RESOLVE_RESOLVED:
        status = "approved" if body.approved else "denied"
    else:
        # "orphaned" / "unknown" — no live run received the answer.
        status = resolve
    return {
        "confirm_id": body.confirm_id,
        "status": status,
        "resolve": resolve,
    }


@router.post("/cancel/{session_id}")
async def cancel_agent(
    request: Request,
    session_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict:
    """Cancel a running agent task."""
    await agent_action_limiter.check_by_user(request, user)

    bus = request.app.state.event_bus

    if bus.has_session(session_id):
        # Ownership gate: only a member of the company that owns the session may
        # cancel its run (otherwise any authenticated user could DoS another
        # tenant's live run).
        #
        # FAIL-CLOSED (LED-014): a live session has a persisted run record
        # (begin_run writes it at run start). When the pool is available, a
        # missing record or a DB error both surface as None here
        # (get_company_for_session swallows errors), and a foreign company isn't
        # in the caller's list — deny in every case. 404 so we don't leak the
        # session's existence. Only when there's NO DB pool do we fall through
        # (the in-process bus session + server-minted UUID are the residual bind).
        from helixis.db import engine as db_engine
        from helixis.db.repositories.orchestrator_runs import get_company_for_session

        try:
            pool = db_engine.get_pool()
        except RuntimeError:
            pool = None
        if pool is not None:
            owner = await get_company_for_session(pool, session_id)
            if owner is None or owner not in user.company_ids:
                raise HTTPException(status_code=404, detail="session not found")
        else:
            logger.warning("cancel ownership check skipped — no DB pool (session=%s)", session_id)

        await bus.publish(session_id, error_event("Task cancelled by user.", recoverable=False))
        await bus.close_session(session_id)
        ph_capture(
            distinct_id=user.user_id,
            event="agent run cancelled",
            properties={"session_id": session_id},
            company_id=user.company_ids[0] if user.company_ids else "",
        )
        return {"session_id": session_id, "status": "cancelled"}

    return {"session_id": session_id, "status": "not_found"}


# ── Live watch (multiplayer PR) ──────────────────────────────────────
#
# Watchers per session, tracked route-side so the count excludes the
# driver's own SSE subscription. In-process is correct for the same
# reason as the run registry (single web instance).
_watcher_counts: dict[str, int] = {}


@router.get("/watch/{session_id}")
async def watch_agent(
    request: Request,
    session_id: str,
    after: int | None = None,
    user: AuthenticatedUser = Depends(get_current_user),
) -> StreamingResponse:
    """Attach read-only to a live agent session's event stream.

    Multiplayer entry point: a teammate (or the driver after a page
    refresh) receives the session's replayable event log, then live
    events, over the same SSE shape as ``/agent/run``.

    ``?after=<seq>`` RESUMES instead of replaying: a client whose stream
    dropped mid-turn passes the last SSE ``id`` it rendered and picks up
    from there. Without it the whole turn re-renders from event zero,
    which reads as a bug — and our turns run long enough that a dropped
    connection is a routine event, not an edge case.

    Watchers send
    nothing through this stream — a watching manager who wants to approve
    a surfaced confirm still goes through ``POST /agent/confirm`` with its
    own ownership + role gates.
    """
    await agent_action_limiter.check_by_user(request, user)
    bus = request.app.state.event_bus

    # Ownership gate — FAIL-CLOSED, byte-for-byte the /agent/cancel pattern
    # (LED-014): a live session has a persisted run record (begin_run). When
    # the pool is available, a missing record, a DB error (both collapse to
    # None), or a foreign company all deny. 404 so the session's existence
    # isn't leaked. Only with NO pool at all do we fall through (the
    # server-minted UUID + live-bus requirement are the residual bind).
    from helixis.db import engine as db_engine
    from helixis.db.repositories.orchestrator_runs import get_company_for_session

    try:
        pool = db_engine.get_pool()
    except RuntimeError:
        pool = None
    if pool is not None:
        owner = await get_company_for_session(pool, session_id)
        if owner is None or owner not in user.company_ids:
            raise HTTPException(status_code=404, detail="session not found")
    else:
        logger.warning("watch ownership check skipped — no DB pool (session=%s)", session_id)

    if not bus.has_session(session_id):
        # Nothing live to attach to — the client falls back to the
        # persisted-transcript replay it already does.
        raise HTTPException(status_code=404, detail="no live run for this session")

    ph_capture(
        distinct_id=user.user_id,
        event="agent watch started",
        properties={"session_id": session_id},
        company_id=user.company_ids[0] if user.company_ids else "",
    )

    async def watch_stream():
        _watcher_counts[session_id] = _watcher_counts.get(session_id, 0) + 1
        await bus.publish(session_id, watcher_update_event(_watcher_counts[session_id]))
        try:
            async for chunk in sse_event_generator(bus, session_id, after=after):
                yield chunk
        finally:
            remaining = max(0, _watcher_counts.get(session_id, 1) - 1)
            if remaining:
                _watcher_counts[session_id] = remaining
            else:
                _watcher_counts.pop(session_id, None)
            # Only announce the departure into a session that still exists —
            # publish would otherwise resurrect a closed session's queue.
            if bus.has_session(session_id):
                await bus.publish(session_id, watcher_update_event(remaining))

    return StreamingResponse(
        watch_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "X-Session-Id": session_id,
        },
    )


class ExtractDocumentRequest(BaseModel):
    """Payload for the in-chat document upload (composer attach button).

    The browser reads the picked file as base64 + its MIME type and
    posts it here; we hand it straight to the document agent's tested
    extraction core. Read-only — no write, no confirm gate."""

    company_id: str
    base64_content: str
    mime_type: str
    filename: str | None = None
    extraction_hint: str | None = None


# MIME types the upload endpoint accepts — the real (non-eval) subset of
# the document tool's supported set. text/plain is eval-only fixtures.
_UPLOAD_MIME_TYPES = frozenset(m for m in _SUPPORTED_MIME_TYPES if m != "text/plain")


@router.post("/extract-document")
async def extract_document(
    request: Request,
    body: ExtractDocumentRequest,
    user: AuthenticatedUser = Depends(get_current_user),
) -> DocumentExtraction:
    """Extract structured fields from a single uploaded document.

    Thin wrapper over the document agent's tested core
    (``extract_document_fields_impl`` → Gemini multimodal) so the chat
    composer's attach button can hand a lease / invoice / inspection
    report straight to extraction and render the structured fields. No
    write, no confirm gate — extraction is read-only."""
    require_company_access(user, body.company_id)
    await agent_action_limiter.check_by_user(request, user)

    if body.mime_type not in _UPLOAD_MIME_TYPES:
        raise HTTPException(
            status_code=415,
            detail=(
                f"Unsupported document type {body.mime_type!r}. "
                "Upload a PDF or an image (PNG / JPEG / WebP)."
            ),
        )

    settings = request.app.state.settings

    # ⚠ THIS ROUTE HAD NO SIZE CHECK AT ALL, and the reader it hands off to has
    # had one since it was written. So anything between the browser's ceiling
    # and `document.max_bytes` was accepted here, decoded in full, refused by
    # `_fetch_bytes`, and returned as a 200 carrying the bare token
    # `attachment_too_large` — which the chat client rendered verbatim. The
    # person waited out the upload of a file the product already knew it could
    # not open.
    #
    # Refused from the STRING LENGTH, before any decode: a 20 MB attachment is
    # ~27 MB of base64 in the request body, and decoding it to measure it is
    # doing the expensive half of the work we are about to decline.
    # `len * 3 // 4` is the decoded size exactly for a browser-produced payload
    # (`FileReader` emits no line breaks); whitespace could only make it read
    # HIGH, and the authoritative measurement is still `_fetch_bytes` on the
    # real bytes — this is the early, cheap, honest refusal in front of it.
    max_bytes = settings.document.max_bytes
    estimated_bytes = len(body.base64_content) * 3 // 4
    if estimated_bytes > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=(
                f"That file is larger than Occupella can read "
                f"({max_bytes // (1024 * 1024)} MB). Try a lower-resolution "
                "scan, or split it into parts."
            ),
        )

    # ⚠ `mime_type` is a CLIENT-SUPPLIED STRING and was taken entirely on
    # faith. The sibling upload route 340 lines away in documents.py has
    # checked its bytes since it was written; this one never did — gotcha 25's
    # "fix it at every layer", arriving as a rule that only ever reached one.
    #
    # Be precise about what this is: the bytes here are decoded and handed to
    # a multimodal model, never written to disk, executed, or served back to a
    # browser. So the harm is NOT code execution — it is that the product
    # tells a provider "this is a lease PDF", ships something else, and
    # reports whatever comes back as a confident extraction. A plausible wrong
    # answer is this repo's documented worst failure mode, and it spends a
    # real multimodal call to produce one.
    #
    # A malformed base64 body is left to the extraction core, which already
    # degrades to `attachment_unavailable` rather than raising — decoding it
    # twice would mean two places that can disagree about what is decodable.
    try:
        head = base64.b64decode(body.base64_content[:64], validate=False)
    except (binascii.Error, ValueError):
        head = b""
    if head and content_matches(head, body.mime_type) is False:
        raise HTTPException(
            status_code=422,
            detail=(
                f"That file's contents are not {body.mime_type}. "
                "Re-save it in the format you meant, or pick the right file."
            ),
        )

    try:
        return await extract_document_fields_impl(
            base64_content=body.base64_content,
            mime_type=body.mime_type,
            extraction_hint=body.extraction_hint,
            settings=settings,
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Extraction failed: {exc}") from exc
