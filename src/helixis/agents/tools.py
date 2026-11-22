"""Tool definitions for the Helixis agent.

These are @function_tool decorated functions that the OpenAI Agents SDK
orchestrator and specialist agents can call.

Confirmation gate (PR #208/#209): ``confirm_or_abort`` is the single
enforcement primitive. A side-effecting tool calls it inside its own body
and performs the write only on explicit approval. To surface the gate it
prefers the per-run **emitter** bound via ``set_confirm_emitter`` (the
orchestrator publishes it straight to the SSE bus the instant it's raised —
reaching the client even while the awaiting tool blocks the SDK stream loop).
When no emitter is bound (unit tests / non-streaming callers) it falls back to
pushing onto ``_pending_confirm_emissions`` (a module-level ``asyncio.Queue``)
which the orchestrator drains after each agent step via
``drain_pending_confirmations``. Either way it emits a ``confirm_gate`` SSE
event the frontend renders; the API layer calls ``resolve_confirmation`` to
unblock the wait.

There is no standalone "ask for confirmation" tool: enforcement lives in
the tool bodies (Buildium) and in the Composio write-gate wrapper
(``helixis.connectors.tool_gate``), never as a prompt convention.

Durability (migration 0022): the in-process ``Future`` above is the
authoritative fast path, but it dies with the process. ``confirm_or_abort``
additionally writes each surfaced confirmation to
``helixis_pending_confirmations`` (status ``pending``) and
``resolve_confirmation`` records the decision (``approved``/``denied``). This
is best-effort and NEVER blocks a write — if the durable store is unavailable
the gate behaves exactly as it did pre-0022. Its payoff is honesty on restart:
when ``/agent/confirm`` arrives and the run's coroutine no longer exists (a
redeploy / spin-down killed it), the durable row lets us report an ``orphaned``
status instead of a silent no-op that hangs the UI forever. This is NOT true
resume — the paused tool call is still lost on restart and the user must
re-issue. See ``db/repositories/pending_confirmations.py``.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any

from agents import function_tool

from helixis.agents.taint import taint_provenance_note
from helixis.security.prompt_delimit import wrap_untrusted

logger = logging.getLogger(__name__)

# --- Confirmation gate infrastructure ---
# Pending confirmations: confirm_id -> Future[(approved, edits)]. ``edits`` is
# the user's edit-before-approve payload (edit-before-approve PR): None on a
# decline/timeout and for gates that declared no editable fields.
_pending_confirmations: dict[str, asyncio.Future[tuple[bool, dict[str, str] | None]]] = {}

# The edits the user attached to the MOST RECENT approval on this task, keyed
# implicitly by task context. ``confirm_or_abort`` sets it just before
# returning; the gated executor (e.g. tool_gate's _do_invoke) reads it to
# apply the user's edited values to the outbound payload. Context-scoped so
# parallel runs can't cross-contaminate.
current_confirm_edits: ContextVar[dict[str, str] | None] = ContextVar(
    "current_confirm_edits", default=None
)

#: The ``confirm_id`` of the card the user just APPROVED in this task, or None.
#: Set by :func:`confirm_or_abort` on an approval (reset on every gate), and
#: read by the audit (``idempotency._audit``) and the provider call logs so the
#: Activity row and the log line name the approval that authorised the write
#: (Rentvine terms rule 22).
current_confirm_id: ContextVar[str | None] = ContextVar("current_confirm_id", default=None)

# Hard cap on a single edited field value — matches the email-body cap in
# visual_tools; an approver pasting a novel into the gate is a mistake.
_MAX_EDIT_VALUE_LEN = 8_000

# Queue of confirmation payloads waiting to be emitted as ``confirm_gate``
# SSE events. ``confirm_or_abort`` pushes here before awaiting the user
# response; the orchestrator drains after each agent step. This is the
# FALLBACK path (unit tests + non-streaming callers) — the live streaming path
# uses the context-scoped emitter below.
_pending_confirm_emissions: asyncio.Queue[dict] = asyncio.Queue()

# Context-scoped confirm-gate emitter (F1 fix). The drain above only runs
# BETWEEN the orchestrator's SDK stream events, but ``confirm_or_abort``
# enqueues its payload and then BLOCKS inside the same tool coroutine — so no
# further stream event arrives, the drain never runs, and the gate was never
# surfaced (the tool then timed out after 300s: the "email send hangs, no
# gate" bug). The client's SSE stream is fed by the event BUS, not by that
# drain, so the orchestrator binds an emitter here that publishes the gate to
# the bus the INSTANT it's raised — reaching the user even while this coroutine
# is blocked on the confirmation await. Per-run via a contextvar so concurrent
# sessions never cross-publish.
_confirm_emitter: ContextVar[Callable[[dict[str, Any]], Awaitable[None]] | None] = ContextVar(
    "helixis_confirm_emitter", default=None
)


def set_confirm_emitter(emitter: Callable[[dict[str, Any]], Awaitable[None]]) -> object:
    """Bind a confirm-gate emitter for the current run. Returns a reset token
    to pass to :func:`reset_confirm_emitter` when the run ends."""
    return _confirm_emitter.set(emitter)


def reset_confirm_emitter(token: object) -> None:
    """Unbind the emitter set by :func:`set_confirm_emitter`."""
    _confirm_emitter.reset(token)  # type: ignore[arg-type]


# Resolve outcome reported back to the API layer (and on to the UI).
#   "resolved" — a live in-process waiter existed and was unblocked (normal).
#   "orphaned" — no live waiter, but a durable ``pending`` row existed: the run
#                that was awaiting this confirm died (restart). The action will
#                NOT run; the UI should tell the user to re-issue the request.
#   "unknown"  — no live waiter and no durable ``pending`` row (already
#                resolved, never known, or the durable store is off). Treated as
#                a benign no-op by the UI.
RESOLVE_RESOLVED = "resolved"
RESOLVE_ORPHANED = "orphaned"
RESOLVE_UNKNOWN = "unknown"


def _durable_store():  # type: ignore[no-untyped-def]
    """Return the durable confirmation store, or ``None`` when no DB pool is
    initialized (CI / tests / degraded boot). Indirected through a module
    function so tests can monkeypatch it."""
    from helixis.db.repositories.pending_confirmations import store_from_pool

    return store_from_pool()


async def _persist_pending(
    confirm_id: str,
    *,
    action: str,
    title: str,
    details: str,
    items: list[str],
    timeout: float,
) -> None:
    """Best-effort durable record of a surfaced confirmation. Never raises and
    never blocks the gate: the live in-memory path is authoritative."""
    store = _durable_store()
    if store is None:
        return
    from helixis.memory.context import current_memory_context

    ctx = current_memory_context()
    if ctx is None or not ctx.company_id:
        return  # no tenant scope to attribute the record to
    try:
        await store.create(
            confirm_id=confirm_id,
            company_id=ctx.company_id,
            session_id=ctx.session_id,
            user_id=ctx.user_id,
            action=action,
            title=title,
            details=details,
            items=items,
            timeout_seconds=timeout,
        )
    except Exception:  # noqa: BLE001 — durability must never break a write
        logger.debug("durable confirmation persist failed (non-fatal)", exc_info=True)


async def _durable_terminate(
    confirm_id: str,
    kind: str,
    *,
    approved: bool = False,
    reason: str | None = None,
    resolved_by: str | None = None,
) -> str | None:
    """Best-effort terminal transition on the durable row. ``kind`` is one of
    ``resolve`` / ``expire`` / ``orphan``. Returns the new status if THIS call
    performed the transition (the row was ``pending``), else ``None``."""
    store = _durable_store()
    if store is None:
        return None
    try:
        if kind == "resolve":
            if reason is None and resolved_by is None:
                # Legacy 2-arg call so older store fakes/impls keep working.
                row = await store.resolve(confirm_id=confirm_id, approved=approved)
            else:
                row = await store.resolve(
                    confirm_id=confirm_id,
                    approved=approved,
                    reason=reason,
                    resolved_by=resolved_by,
                )
        elif kind == "expire":
            row = await store.mark_expired(confirm_id=confirm_id)
        else:
            row = await store.mark_orphaned(confirm_id=confirm_id)
    except Exception:  # noqa: BLE001 — durability must never break the round-trip
        logger.debug("durable confirmation %s failed (non-fatal)", kind, exc_info=True)
        return None
    return row.status if row is not None else None


async def resolve_confirmation(
    confirm_id: str,
    approved: bool,
    *,
    reason: str | None = None,
    resolved_by: str | None = None,
    edits: dict[str, str] | None = None,
) -> str:
    """Called by the API layer when the user approves/denies a confirmation.

    Returns a resolve status (see ``RESOLVE_*`` above) so the API can tell the
    UI whether the answer actually reached a live, waiting run or arrived too
    late (the run died on a restart). Sets the in-process Future result FIRST
    so a live waiter unblocks without waiting on the durable write. ``reason``
    (optional decline reason) and ``resolved_by`` are recorded on the durable
    row only — the live waiter needs just the boolean.
    """
    future = _pending_confirmations.pop(confirm_id, None)
    if future is not None and not future.done():
        # Edits ride only on an APPROVAL — a decline's edits are meaningless
        # and must not survive to any executor.
        future.set_result((approved, edits if approved else None))
        # Record the decision durably (best-effort, after unblocking).
        await _durable_terminate(
            confirm_id, "resolve", approved=approved, reason=reason, resolved_by=resolved_by
        )
        return RESOLVE_RESOLVED

    # No live waiter in this process. If a durable ``pending`` row exists, the
    # run that was awaiting it died — mark it orphaned and report honestly.
    if await _durable_terminate(confirm_id, "orphan") is not None:
        return RESOLVE_ORPHANED
    return RESOLVE_UNKNOWN


async def _wait_for_confirmation(
    confirm_id: str, timeout: float = 300.0
) -> tuple[bool, dict[str, str] | None]:
    """Block until the user responds to a confirmation, or timeout. Returns
    ``(approved, edits)`` — edits are non-None only on an approval that
    carried edit-before-approve values.

    ⚠ Bounded per RUN, not just per gate. The stall watchdog pauses for the
    whole of this wait by design, so a run that raises gates nobody answers
    has no upper bound: four serial 300s waits on one live session cost 24
    minutes. Once a gate in this run has run the full window unanswered,
    ``approval_is_unreachable`` is true and later gates return immediately
    rather than each buying another five minutes of silence. See
    ``agents/confirm_budget.py``.
    """
    from helixis.agents.confirm_budget import current_confirm_budget

    budget = current_confirm_budget()
    if budget is not None:
        budget.record_raised()
        if budget.approval_is_unreachable():
            # The card was still published and the durable row still exists —
            # the user can see WHAT was asked. We simply stop holding the run
            # open for an answer that this run has already established is not
            # coming. Re-sending the message starts a fresh run with a fresh
            # budget, which is the honest way back.
            budget.record_skipped()
            _pending_confirmations.pop(confirm_id, None)
            await _durable_terminate(confirm_id, "expire")
            logger.info(
                "confirm %s not awaited — %d gate(s) already expired unanswered this run",
                confirm_id,
                budget.expired,
            )
            return (False, None)

    loop = asyncio.get_event_loop()
    future: asyncio.Future[tuple[bool, dict[str, str] | None]] = loop.create_future()
    _pending_confirmations[confirm_id] = future
    started = time.monotonic()
    try:
        result = await asyncio.wait_for(future, timeout=timeout)
    except TimeoutError:
        _pending_confirmations.pop(confirm_id, None)
        # No answer within the window → record the timeout durably.
        await _durable_terminate(confirm_id, "expire")
        if budget is not None:
            budget.record_expiry(time.monotonic() - started)
        return (False, None)
    if budget is not None:
        budget.record_answered()
    return result


async def drain_pending_confirmations() -> list[dict]:
    """Non-blocking drain of pending confirmation emissions.

    PR #75 — the orchestrator's event-stream loop calls this after each
    agent step and emits any returned payloads as ``confirm_gate`` SSE
    events. Never blocks; returns ``[]`` if the queue is empty.
    """
    out: list[dict] = []
    while not _pending_confirm_emissions.empty():
        try:
            out.append(_pending_confirm_emissions.get_nowait())
        except asyncio.QueueEmpty:
            break
    return out


async def confirm_or_abort(
    *,
    action: str,
    title: str,
    details: str,
    items: list[str] | None = None,
    request_body: dict[str, Any] | None = None,
    editable_fields: list[str] | None = None,
    timeout: float = 300.0,
) -> bool:
    """Enforcement primitive: surface a confirm request and BLOCK until the
    user approves, denies, or it times out. Returns ``True`` ONLY on an
    explicit approval.

    This is the gate that makes write confirmation structural rather than a
    prompt convention. A side-effecting tool calls this *inside its own body*
    and must not perform the side effect unless the return value is ``True``.
    Because the await lives inside the tool, the model cannot reach the side
    effect without passing through the gate — regardless of whether it
    remembered to call a separate confirmation tool first, and regardless of
    tool ordering.

    Fail-closed: if the emission queue is full (the confirm request can't be
    surfaced to the user) we return ``False`` rather than silently proceeding.
    A unique ``confirm_id`` is minted per call so concurrent confirmations
    from parallel tool calls can never cross-resolve.

    Args:
        action: Machine action tag (e.g. ``"buildium_create_task"``).
        title: Short human prompt (e.g. ``"Create a task in Buildium?"``).
        details: Full preview of what will happen if approved.
        items: Optional list of affected items shown in the confirm card.
        editable_fields: Optional — field names of ``request_body`` the user
            may EDIT before approving (edit-before-approve). The gate event
            carries them so the client renders those fields as inputs; on an
            approval, the user's edited values come back through
            ``resolve_confirmation`` and are published to the executor via
            ``current_confirm_edits`` (filtered to THIS allowlist, values
            length-capped — an edit to any other field is discarded).
            ``None`` (the default) keeps the gate read-only; SMS gates MUST
            stay read-only (carrier-registered templates are not editable).
        request_body: Optional — the ACTUAL outbound request payload, so the
            approver sees what will really be sent, not just a summary of it
            (the thin-preview gap: the live card used to show only
            ``{action, items}``). Additive: absent for callers that don't
            pass it. Surfaced in the SSE event only, deliberately NOT
            persisted on the durable row (keeps raw payloads out of at-rest
            storage).
        timeout: Seconds to wait for the user before treating it as denied.

    Returns:
        ``True`` iff the user explicitly approved within ``timeout``.
    """
    confirm_id = str(uuid.uuid4())
    item_list = items or []
    payload: dict[str, Any] = {
        "confirm_id": confirm_id,
        "action": action,
        "title": title,
        "details": details,
        "items": item_list,
    }
    # ⚠ Provenance goes on the card the HUMAN reads. Fencing lowers the odds
    # the model is steered by attacker-written text; it cannot lower the blast
    # radius when it is, and the person approving the write is the only one who
    # can tell "email the owner list" from something they actually asked for.
    #
    # Additive and advisory — it never blocks and never alters the write. A
    # gate that silently refused on taint would break legitimate work on every
    # run that so much as read a work-order description, which is most of them.
    if (provenance := taint_provenance_note()) is not None:
        payload["provenance"] = provenance
    if request_body is not None:
        payload["request_body"] = request_body
    if editable_fields:
        payload["editable_fields"] = list(editable_fields)

    # Durable record BEFORE awaiting, so a confirm that the process never lives
    # to resolve is still reconcilable on restart (best-effort; never blocks).
    await _persist_pending(
        confirm_id,
        action=action,
        title=title,
        details=details,
        items=item_list,
        timeout=timeout,
    )

    # Surface the confirm request. PREFERRED: the context-scoped emitter
    # publishes it straight to the SSE bus NOW, so the gate reaches the user
    # even though this coroutine is about to block on the await below (the SDK
    # event loop that drives the legacy drain is starved while we block — the
    # F1 hang). FALLBACK (no emitter bound — unit tests / non-streaming
    # callers): the module queue the orchestrator drains after each event.
    emitter = _confirm_emitter.get()
    if emitter is not None:
        try:
            await emitter(payload)
        except Exception:  # noqa: BLE001 — an emit failure must not skip the gate
            logger.warning("confirm emitter failed; falling back to the drain queue", exc_info=True)
            emitter = None
    if emitter is None:
        try:
            _pending_confirm_emissions.put_nowait(payload)
        except asyncio.QueueFull:
            # Can't surface the prompt → must not proceed with the side effect.
            logger.warning("confirm-emission queue full; refusing to proceed without confirmation")
            await _durable_terminate(confirm_id, "expire")
            return False

    current_confirm_id.set(None)
    approved, edits = await _wait_for_confirmation(confirm_id, timeout=timeout)
    current_confirm_id.set(confirm_id if approved else None)
    # Publish sanitized edits for the gated executor: only fields this gate
    # DECLARED editable survive, every value is coerced to a length-capped
    # string, and a decline publishes None. Reset-first so a stale value from
    # an earlier gate on this task can never leak into a later executor.
    current_confirm_edits.set(None)
    if approved and edits and editable_fields:
        allowed = {
            k: str(v)[:_MAX_EDIT_VALUE_LEN]
            for k, v in edits.items()
            if k in set(editable_fields) and str(v).strip()
        }
        if allowed:
            current_confirm_edits.set(allowed)
    return approved


# --- Agent tools ---


# PR #153 — the legacy ``browse_website`` tool was removed. The new
# browser_agent specialist (helixis.agents.browser_agent) carries
# six structured tools (navigate, read_page, extract_text, click,
# screenshot, escalate_to_user) that follow the same
# structured-result-never-raises pattern as web_tools and
# document_tools. See helixis.agents.browser_tools for the
# replacement surface.


@function_tool
async def search_memory(query: str) -> str:
    """Search the company's shared memory for relevant context.

    Retrieves stored facts about properties, tenants, preferences, and
    past interactions. Memory is shared across all users in the company —
    facts stored by any team member are searchable by all.

    The company scope is picked up from the request's MemoryContext
    automatically; the caller does not pass an ID.

    Args:
        query: What to search for (e.g., "123 Oak St tenant info",
               "vendor for plumbing repairs", "rent for Unit 4B")

    Returns:
        Relevant memories as a formatted string, or a short note.
    """
    return await _search_memory_impl(query)


async def _search_memory_impl(query: str) -> str:
    """Backs the ``search_memory`` tool. Split out as a module-level coroutine
    so the eval harness can patch it (mirrors the browser/buildium ``*_impl``
    pattern); the ``@function_tool`` wrapper above only carries the LLM-facing
    docstring."""
    from helixis.memory.context import current_memory_context
    from helixis.memory.manager import get_memory_manager

    manager = get_memory_manager()
    if manager is None:
        return "Memory system not configured."

    ctx = current_memory_context()
    company_id = ctx.company_id if ctx else ""
    searched_by = ctx.user_id if ctx else ""
    if not company_id:
        # Memory is company-partitioned; without a company scope there is no
        # partition to search (the shared "default" fallback was removed).
        return "Memory is unavailable for this session (no company context)."

    # search_checked, NOT search: the latter returns [] for both "found
    # nothing" and "could not reach the embedder", and this result is read by
    # the model and relayed to a person. Reporting a failed lookup as an
    # absence is the failure mode gotchas 26/42 exist for — and it was firing
    # on the majority of calls under load, not at the margin.
    read = await manager.search_checked(query, company_id=company_id, searched_by=searched_by)
    if not read.ok:
        return (
            "Memory could not be searched just now — the lookup itself failed, so "
            "this says NOTHING about whether such a memory exists. Do NOT tell the "
            "user there is no record of it, and do not treat this as evidence of "
            "anything. Answer from your other sources and say that stored company "
            "memory could not be checked."
        )
    results = list(read.hits)
    if not results:
        return "No relevant memories found."

    formatted: list[str] = []
    for mem in results:
        text = mem.get("memory", mem.get("content", ""))
        meta = mem.get("metadata") or {}
        category = meta.get("category", "")
        stored_by = meta.get("stored_by", "")

        prefix = ""
        if category:
            prefix = f"[{category}] "
        if stored_by and stored_by != "system":
            prefix += f"(from {stored_by}) "

        formatted.append(f"- {prefix}{text}")

    # Recalled memory can echo content that originated from an untrusted party
    # (an email body, a web snippet) that some earlier turn chose to store.
    # Fence it as data so a stored injection can't act as instructions when the
    # reasoning loop reads it back mid-run (LED-018). Status/error strings above
    # are system messages and stay unfenced.
    return wrap_untrusted("\n".join(formatted), label="recalled memory")


@function_tool
async def store_memory(content: str, category: str = "") -> str:
    """Store a fact in the company's shared long-term memory.

    Use this to remember important property details, tenant info,
    management preferences, vendor details, or task outcomes. The fact
    is visible to every user in the company.

    Categories: tenant, property, maintenance, financial, preference,
    vendor, lease, compliance.

    Args:
        content: The fact to remember (e.g., "Unit 4B tenant is Sarah
                 Chen, lease expires 2026-09-30").
        category: Optional category tag from the list above.

    Returns:
        Confirmation message.
    """
    return await _store_memory_impl(content, category)


async def _store_memory_impl(content: str, category: str = "") -> str:
    """Backs the ``store_memory`` tool — module-level so the eval harness can
    patch it (see ``_search_memory_impl``)."""
    from helixis.memory.context import current_memory_context
    from helixis.memory.manager import get_memory_manager

    manager = get_memory_manager()
    if manager is None:
        return "Memory system not configured."

    ctx = current_memory_context()
    company_id = ctx.company_id if ctx else ""
    stored_by = ctx.user_id if ctx else ""
    if not company_id:
        return "Memory is unavailable for this session (no company context) — nothing was stored."

    # ⚠ CHECK THE RETURN. `add` returns None on every failure path — the
    # embedder being unreachable, mem0 raising, a spaCy SystemExit — and this
    # used to discard it and confirm unconditionally. The user was told a fact
    # had been remembered while it was silently dropped: two Sentry issues, 82
    # events, all of them that. Confirming a write that did not happen is worse
    # than failing loudly, because nobody goes back to check.
    stored = await manager.add(
        content,
        company_id=company_id,
        stored_by=stored_by,
        category=category,
    )
    if stored is None:
        return (
            "That was NOT stored — the memory write failed. Tell the user plainly "
            "that you could not save it, so they know to mention it again rather "
            "than assuming Occupella will remember."
        )
    return f"Stored in company memory: {content[:100]}..."
