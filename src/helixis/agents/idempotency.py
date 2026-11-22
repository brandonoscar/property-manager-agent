"""Agent-action idempotency primitive — fail-safe guard for irreversible writes.

``guarded_action`` owns the lifecycle of one irreversible action:
``claim → confirm → execute → record``. It guarantees an approved side effect
fires AT MOST ONCE across double-approval, retry, and process restart. When it
cannot prove the side effect did NOT already happen, it refuses to fire again.

This is the single chokepoint the three write sites route through
(``tool_gate.gated_invoke``, ``buildium_tools._create_to_do_task_impl``,
``browser_tools._submit_form_impl``). ``confirm_or_abort`` is unchanged and is
called *inside* this primitive.

Failure classification (fail-safe by default)
---------------------------------------------
The primitive must know whether a failure happened BEFORE or AT/AFTER the
external side effect:

* ``execute`` raises :class:`PreSideEffectError` → the failure is provably
  before the external call (validation / argument / confirm construction) →
  ``failed_retryable`` → a later attempt may re-confirm and retry.
* ``execute`` raises ANYTHING ELSE (timeout, connection error, ambiguous HTTP
  incl. 5xx after send, or any unclassified exception) → the side effect MAY
  have happened → ``abandoned_unknown`` → NEVER auto-retried; needs review.

So the burden of proof is on "safe to retry": callers must explicitly raise
``PreSideEffectError`` to opt into a retry. Silence ⇒ do-not-refire.

Key derivation
--------------
``derive_key`` hashes the FULL normalized action values (sorted keys, canonical
serialization). Values are never truncated or masked for the key — masking is
only ever for stored display. The raw values are not persisted: only the
sha256 digest is stored, so two inputs differing solely in a long tail produce
different keys (no truncation collision) while raw PII stays out of the table.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal, TypeVar

from helixis.agents.tools import confirm_or_abort
from helixis.agents.write_guard import executing_write
from helixis.db.repositories.action_idempotency import (
    STATUS_ABANDONED_UNKNOWN,
    STATUS_ABORTED,
    STATUS_AWAITING_APPROVAL,
    STATUS_FAILED_RETRYABLE,
    STATUS_IN_PROGRESS,
    STATUS_SUCCEEDED,
    IdempotencyStore,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")


class PreSideEffectError(Exception):
    """Raised by an ``execute`` callable for a failure that is PROVABLY before
    the external side effect (validation / argument / confirm construction).

    Only this exception class maps to ``failed_retryable``. Every other
    exception is treated as ambiguous (the side effect may have fired) and maps
    to ``abandoned_unknown`` — never auto-retried."""


GuardOutcome = Literal[
    "executed",  # fresh run; execute() ran exactly once
    "replayed",  # prior success in window; execute() NOT run, prior result returned
    "not_approved",  # user declined/timed out at the confirm gate; nothing fired
    "already_pending",  # another path holds the claim and is still in progress
    "blocked_needs_review",  # abandoned_unknown — refuse to refire, surface for review
    # required=true AND no durable store: we fail closed BEFORE executing, so
    # NOTHING happened. Distinct from blocked_needs_review (where the side
    # effect DID fire and we couldn't confirm it) so callers can phrase an
    # honest "nothing was done" message instead of "did it but can't confirm".
    "blocked_store_unavailable",
]


@dataclass
class GuardResult:
    outcome: GuardOutcome
    result: Any | None = None
    detail: str = ""


#: The ONLY outcomes on which a write tool may report that its action happened.
#: ⚠ An allowlist, never "everything I didn't handle". Every tail used to check
#: the outcomes it knew and fall through to its success line for the rest, so
#: ``already_pending`` (THIS call ran nothing) came back as "Task created" or
#: "Posted a $X charge" (audit 2026-09-29, gotcha 177).
SUCCESS_OUTCOMES: frozenset[str] = frozenset({"executed", "replayed"})

# The ``already_pending`` details, named once: :func:`pending_reply` words its
# answer by them, so a loose string match could drift from the producer.
DETAIL_WAIT_EXHAUSTED = "pending wait exhausted"
DETAIL_RECLAIM_LOST = "reclaim lost to peer"
DETAIL_PEER_FAILED = "peer failed_retryable"
DETAIL_RELEASED_AT_GATE = "claim released while awaiting approval — re-run the action"
# Of those, the ones where NOTHING is running: the side effect never fired and
# no other request holds it, so asking again is safe.
_NOTHING_RUNNING = frozenset({DETAIL_PEER_FAILED, DETAIL_RELEASED_AT_GATE})


def pending_reply(
    guard: GuardResult, *, action: str, system: str, unchanged: str | None = None
) -> str:
    """What a write tool tells the user for an outcome outside
    :data:`SUCCESS_OUTCOMES` that it did not word itself (today only
    ``already_pending``). In every case THIS call ran nothing.

    ``action`` is the verb phrase ("create the task"), ``system`` where the
    user would look ("Buildium"). Two honest answers: another request for the
    same action is still in progress (it may yet land, so check before asking
    again), or nothing is running at all (asking again is safe)."""
    if guard.detail in _NOTHING_RUNNING:
        done = unchanged or f"nothing was changed in {system}"
        return (
            f"I didn't {action}, and {done}. The request stopped before anything "
            "went out; ask me again to retry."
        )
    return (
        f"I didn't {action}: the same request is already in progress, and may be "
        "waiting for approval on an earlier card, so I didn't start a second one. "
        f"Check {system} before asking again."
    )


# How long to wait on an IN_PROGRESS claim held by another path before giving
# up and returning ``already_pending`` (never executing). Bounded so a hung
# peer can't block forever.
_PENDING_WAIT_SECONDS = 30.0
_PENDING_POLL_SECONDS = 0.25


def _canonical(value: Any) -> str:
    """Deterministic serialization of an arbitrary value for hashing.

    ``sort_keys`` makes argument ORDER irrelevant; ``default=str`` tolerates
    non-JSON scalars; no truncation so the FULL value contributes to the
    digest."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def derive_key(
    company_id: str,
    action_type: str,
    value_dict: dict[str, Any],
    *,
    extra: str = "",
) -> str:
    """sha256 hex over (company_id + action_type + FULL normalized values).

    Tenant-scoped (``company_id``), action-scoped (``action_type``), and
    content-scoped (every value, untruncated). ``extra`` folds in non-arg
    discriminators (e.g. browser session id + final url)."""
    material = "\x1f".join((company_id, action_type, extra, _canonical(value_dict)))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


async def _await_terminal(
    store: IdempotencyStore,
    *,
    company_id: str,
    idem_key: str,
    pending_wait: float,
    poll: float,
) -> GuardResult:
    """Poll an IN_PROGRESS claim until it leaves in_progress or we time out.

    NEVER executes — this path exists only to avoid double-firing while a peer
    holds the claim. If still pending at the deadline, returns
    ``already_pending`` (the side effect is not ours to run)."""
    waited = 0.0
    while waited < pending_wait:
        await asyncio.sleep(poll)
        waited += poll
        row = await store.get(company_id=company_id, idem_key=idem_key)
        if row is None or row.status in (STATUS_IN_PROGRESS, STATUS_AWAITING_APPROVAL):
            continue
        if row.status == STATUS_SUCCEEDED:
            return GuardResult(outcome="replayed", result=row.result, detail="peer succeeded")
        if row.status == STATUS_ABORTED:
            # Peer's user declined — nothing fired; caller may treat as fresh,
            # but to keep exactly-one-runner we report not_approved here.
            return GuardResult(outcome="not_approved", detail="peer declined")
        if row.status == STATUS_ABANDONED_UNKNOWN:
            return GuardResult(outcome="blocked_needs_review", detail="peer abandoned")
        # failed_retryable: leave the retry to a fresh top-level call.
        return GuardResult(outcome="already_pending", detail=DETAIL_PEER_FAILED)
    return GuardResult(outcome="already_pending", detail=DETAIL_WAIT_EXHAUSTED)


async def _run_verify(
    verify: Callable[[T], Awaitable[bool]],
    result: T,
    action_type: str,
    fail_tag: str = "verify_failed:not_found",
) -> tuple[bool, str]:
    """Run a read-back verifier, mapping its outcome to (verified, error_tag).

    Two distinct non-success modes, both → needs-review (same terminal state,
    distinguishable in the record):
      * returns False  → (False, ``fail_tag``)  — default ``verify_failed:not_found``
      * raises E       → (False, "verify_error:<Type>")
    A verifier that ERRORS is never read as "verified". The verifier performs
    reads only; it must not re-execute the side effect.
    """
    try:
        ok = await verify(result)
    except Exception as exc:  # noqa: BLE001 — a verifier error must NOT mean success
        tag = f"verify_error:{type(exc).__name__}"
        logger.warning("%s: read-back verifier raised %s → needs-review", action_type, tag)
        return False, tag
    if not ok:
        logger.warning("%s: read-back could not confirm the write → needs-review", action_type)
        return False, fail_tag
    return True, ""


async def guarded_action(
    *,
    store: IdempotencyStore,
    company_id: str,
    idem_key: str,
    action_type: str,
    confirm: dict[str, Any] | None,
    execute: Callable[[], Awaitable[T]],
    window: timedelta | None,
    record: Callable[[GuardResult], Awaitable[None]] | None = None,
    verify: Callable[[T], Awaitable[bool]] | None = None,
    verify_fail_tag: str = "verify_failed:not_found",
    pending_wait: float = _PENDING_WAIT_SECONDS,
    poll: float = _PENDING_POLL_SECONDS,
) -> GuardResult:
    """Run an irreversible action at most once.

    ``confirm`` is the kwargs dict passed straight to ``confirm_or_abort``, or
    ``None`` to skip the in-band gate entirely when confirmation already
    happened out-of-band (e.g. the human approved in the UI before the HTTP
    execute endpoint fired). The at-most-once guarantee is independent of the
    gate — it comes from the durable claim, not the confirmation.
    ``execute`` performs the external side effect and returns its result; it
    must raise :class:`PreSideEffectError` for failures known to be before the
    external call (everything else is treated as ambiguous → not retried).
    ``verify`` (optional) is a READ-ONLY read-back run AFTER execute and BEFORE
    recording success: it returns True only if the side effect is independently
    confirmed. Returns False (not confirmed) or raises (verifier errored) →
    ``abandoned_unknown`` / needs-review; the action is NEVER re-executed.
    ``record`` (optional) is invoked on BOTH a fresh success and a replay so
    the audit trail is written in both paths.
    """
    claim = await store.claim(
        company_id=company_id, idem_key=idem_key, action_type=action_type, window=window
    )

    if not claim.won:
        status = claim.row.status
        if status == STATUS_SUCCEEDED:
            res = GuardResult(outcome="replayed", result=claim.row.result, detail="prior success")
            if record is not None:
                await record(res)
            return res
        if status in (STATUS_IN_PROGRESS, STATUS_AWAITING_APPROVAL):
            # awaiting_approval = a peer holds the claim parked at its confirm
            # gate — same "not ours to run" semantics as in_progress.
            return await _await_terminal(
                store,
                company_id=company_id,
                idem_key=idem_key,
                pending_wait=pending_wait,
                poll=poll,
            )
        if status == STATUS_ABANDONED_UNKNOWN:
            return GuardResult(outcome="blocked_needs_review", detail="abandoned_unknown")
        if status in (STATUS_FAILED_RETRYABLE, STATUS_ABORTED):
            # Both mean the side effect did NOT fire — safe to re-run. ``reclaim``
            # is the single-winner flip back to in_progress; a loser treats it
            # as pending rather than racing a second execution.
            won_retry = await store.reclaim(company_id=company_id, idem_key=idem_key)
            if not won_retry:
                return GuardResult(outcome="already_pending", detail=DETAIL_RECLAIM_LOST)
            # Fall through to confirm+execute below.
        else:  # unknown status — fail safe, do not execute
            return GuardResult(outcome="blocked_needs_review", detail=f"unknown status {status}")

    # ── We hold the claim (fresh insert, or won a retry/abort reclaim). ──
    # ``confirm is None`` ⇒ confirmation handled out-of-band; no SSE gate to
    # surface, so proceed straight to execute.
    if confirm is not None:
        # Park the claim while the human decides: a process death during the
        # wait is then PROVABLY pre-side-effect — the reaper/shutdown release
        # turns it into ``failed_retryable`` (retry just works) instead of
        # ``abandoned_unknown`` (blocked needs-review). getattr-guarded so
        # stores without the parking methods (older fakes) keep today's
        # behavior unchanged.
        mark_awaiting = getattr(store, "mark_awaiting_approval", None)
        parked = False
        if mark_awaiting is not None:
            try:
                parked = bool(await mark_awaiting(company_id=company_id, idem_key=idem_key))
            except Exception:  # noqa: BLE001 — parking is an enhancement, never load-bearing
                logger.warning("%s: could not park claim at confirm gate", action_type)

        approved = await confirm_or_abort(**confirm)
        if not approved:
            # Declined/timed out — nothing fired. ``aborted`` (not
            # failed_retryable) so a later attempt is treated as fresh.
            await store.finalize_aborted(company_id=company_id, idem_key=idem_key)
            return GuardResult(outcome="not_approved", detail="declined or timed out")

        if parked:
            # Un-park BEFORE execute so a death after this point is correctly
            # ambiguous again. Losing the un-park race (shutdown release or
            # reaper already flipped the row) means the claim is no longer
            # ours — never execute on a claim we don't hold.
            resume = getattr(store, "resume_from_awaiting", None)
            resumed = False
            if resume is not None:
                try:
                    resumed = bool(await resume(company_id=company_id, idem_key=idem_key))
                except Exception:  # noqa: BLE001
                    logger.warning("%s: resume_from_awaiting errored", action_type)
            if not resumed:
                return GuardResult(outcome="already_pending", detail=DETAIL_RELEASED_AT_GATE)

    try:
        # ⚠ The marker must span the AWAIT (gotcha 39): the write happens
        # while this coroutine is SUSPENDED, which is exactly the window
        # our own soft deadline was cutting.
        with executing_write(action_type):
            result = await execute()
    except PreSideEffectError as exc:
        await store.finalize_retryable(
            company_id=company_id, idem_key=idem_key, error=f"pre_side_effect:{type(exc).__name__}"
        )
        raise
    except Exception as exc:  # noqa: BLE001 — ambiguous: side effect may have fired
        # ⚠ KEEP WHY, NOT JUST WHAT. This stored the exception CLASS alone, so
        # a real Buildium rejection reaped as a bare `ambiguous:BuildiumApiError`
        # and the reason was gone for good: `abandoned_unknown` is never
        # retried, and the model's reply is a GUESS about the cause rather than
        # a record of it (2026-08-30, both create_* tools -- the run text said
        # "the most common missing field that trips up Buildium" while the
        # actual status and path were discarded). Gotcha 59's lesson exactly:
        # the status came back, the diagnosis did not.
        #
        # Bounded and structured -- enough to act on, not a blob.
        await store.finalize_abandoned(
            company_id=company_id, idem_key=idem_key, error=_ambiguous_detail(exc)
        )
        logger.warning(
            "guarded_action %s: ambiguous failure %s → abandoned_unknown (NOT retried)",
            action_type,
            type(exc).__name__,
        )
        raise

    # ── Read-back verification (between execute and record-success). ──
    # The side effect already fired; verify only DECIDES whether we may
    # report success. A verifier must never re-execute or fire a second
    # side effect — its reads are reversible and unwrapped.
    if verify is not None:
        verified, verify_error = await _run_verify(verify, result, action_type, verify_fail_tag)
        if not verified:
            await store.finalize_abandoned(
                company_id=company_id, idem_key=idem_key, error=verify_error
            )
            return GuardResult(outcome="blocked_needs_review", detail=verify_error)

    await store.finalize_success(company_id=company_id, idem_key=idem_key, result=_jsonable(result))
    res = GuardResult(outcome="executed", result=result, detail="fresh execution")
    if record is not None:
        await record(res)
    return res


def store_from_pool() -> IdempotencyStore | None:
    """Build a Pg-backed store from the process pool, or ``None`` if the pool
    isn't initialized (CI, tests, browser degraded mode). Callers fall back to
    the ungated-but-still-confirmed path when this is ``None`` — see
    :func:`run_guarded`."""
    from helixis.db import engine as db_engine
    from helixis.db.repositories.action_idempotency import PgIdempotencyStore

    try:
        pool = db_engine.get_pool()
    except RuntimeError:
        return None
    return PgIdempotencyStore(pool)


def _idempotency_required() -> bool:
    """Whether the durable guard is mandatory (prod) vs. best-effort (local/test)."""
    from helixis.settings import get_settings

    try:
        return bool(get_settings().idempotency.required)
    except Exception:  # noqa: BLE001 — never let settings load break a write path
        return False


def _signal_passthrough(action_type: str, company_id: str, *, fail_closed: bool) -> None:
    """Emit an observability signal that the durable guard was unavailable.

    Uses the EXISTING PostHog analytics channel (best-effort, no DB needed —
    the DB is precisely what's unavailable here), so a real-write passthrough
    or a fail-closed block is never fully silent. Paired with a warning log."""
    try:
        from helixis.analytics import capture

        capture(
            distinct_id=company_id or "system",
            event="action idempotency store unavailable",
            properties={"action_type": action_type, "fail_closed": fail_closed},
            company_id=company_id,
        )
    except Exception:  # noqa: BLE001 — signalling must never break the action
        logger.debug("idempotency passthrough signal failed (non-fatal)", exc_info=True)


def _ambiguous_detail(exc: BaseException) -> str:
    """``ambiguous:<Class>`` plus the detail needed to diagnose it later.

    ``BuildiumApiError`` carries the status, path and code that say WHAT was
    rejected; a bare class name says only that something was. Truncated,
    because this column is a breadcrumb rather than a log.
    """
    label = f"ambiguous:{type(exc).__name__}"
    parts: list[str] = []
    status = getattr(exc, "status", None)
    if status is not None:
        parts.append(f"status={status}")
    for attr in ("code", "path"):
        value = getattr(exc, attr, None)
        if value:
            parts.append(f"{attr}={value}")
    message = str(exc).strip()
    if message:
        parts.append(message)
    return f"{label} {' '.join(parts)}"[:500] if parts else label


async def run_guarded(
    *,
    company_id: str,
    idem_key: str,
    action_type: str,
    confirm: dict[str, Any] | None,
    execute: Callable[[], Awaitable[T]],
    window: timedelta | None,
    record: Callable[[GuardResult], Awaitable[None]] | None = None,
    verify: Callable[[T], Awaitable[bool]] | None = None,
    verify_fail_tag: str = "verify_failed:not_found",
    store: IdempotencyStore | None = None,
) -> GuardResult:
    """Idempotency-guarded action with a policy-driven no-store fallback.

    * store available → exactly :func:`guarded_action`.
    * store unavailable (no DB pool, or no ``company_id`` to scope by) →
      behavior depends on ``settings.idempotency.required``:
        - ``required=False`` (local/test/eval) → PASSTHROUGH: still gate on
          ``confirm_or_abort`` and execute on approval (pre-guard behavior, so
          the 060–064 sweep stays identical), but emit a warning + PostHog
          signal so it is never fully silent.
        - ``required=True`` (prod) → FAIL CLOSED: do NOT execute; return the
          distinct ``blocked_store_unavailable`` outcome (nothing happened, so
          callers can say so honestly); emit the loud log + signal.

    We never silently skip the confirm gate, and when uncertain we never fire
    the irreversible side effect.
    """
    resolved = store if store is not None else store_from_pool()
    if resolved is None or not company_id:
        reason = "no company_id" if not company_id else "no durable store"
        if _idempotency_required():
            logger.warning(
                "idempotency REQUIRED but unavailable (%s) for %s — failing closed, "
                "NOT executing the side effect",
                reason,
                action_type,
            )
            _signal_passthrough(action_type, company_id, fail_closed=True)
            return GuardResult(
                outcome="blocked_store_unavailable",
                detail=f"idempotency required but unavailable ({reason})",
            )
        logger.warning(
            "idempotency store unavailable (%s) for %s — passthrough confirm+execute "
            "WITHOUT durable dedup (set HELIXIS_IDEMPOTENCY__REQUIRED=true in prod)",
            reason,
            action_type,
        )
        _signal_passthrough(action_type, company_id, fail_closed=False)
        if confirm is not None:
            approved = await confirm_or_abort(**confirm)
            if not approved:
                return GuardResult(outcome="not_approved", detail="declined (no durable dedup)")
        with executing_write(action_type):
            result = await execute()
        # Verify runs even without a store, so a missing store never silently
        # skips the read-back. No store calls on this path — needs-review is
        # surfaced via the returned GuardResult only.
        if verify is not None:
            verified, verify_error = await _run_verify(verify, result, action_type, verify_fail_tag)
            if not verified:
                return GuardResult(outcome="blocked_needs_review", detail=verify_error)
        res = GuardResult(outcome="executed", result=result, detail="executed (no durable dedup)")
        if record is not None:
            await record(res)
        await _audit(company_id, action_type, confirm, res, idem_key)
        return res
    outcome = await guarded_action(
        store=resolved,
        company_id=company_id,
        idem_key=idem_key,
        action_type=action_type,
        confirm=confirm,
        execute=execute,
        window=window,
        record=record,
        verify=verify,
        verify_fail_tag=verify_fail_tag,
    )
    await _audit(company_id, action_type, confirm, outcome, idem_key)
    return outcome


async def _audit(
    company_id: str,
    action_type: str,
    confirm: dict[str, Any] | None,
    res: GuardResult,
    idem_key: str | None = None,
) -> None:
    """Record in the ACTIVITY FEED that Occupella changed something.

    ⚠ **Here, and not at the eleven write tools.** Measured 2026-09-10:
    `agents/buildium_tools.py` and `agents/sms_tools.py` contained ZERO calls to
    `write_activity`, so the whole confirm-gated Buildium write surface —
    payments, charges, closing a work order, move-outs, notes — left no trace a
    property manager could read, while `web_tools` had logged 53 searches. This
    function is on the path all nineteen write call sites already take, so a
    twentieth is covered by construction rather than by remembering (gotchas
    25/81's insert-at-each-caller fan-out).

    ⚠ Covers BOTH exits deliberately. The passthrough branch above runs when
    there is no durable store and `idempotency.required` is false — a real write
    against a real Buildium account, so an audit row that only existed on the
    stored path would go missing exactly where the dedup already had.

    ⚠ Never raises: the write has ALREADY happened by the time this runs, and
    turning a lost audit row into a failed tool call would report a change that
    did not occur.
    """
    from helixis.agents.tools import current_confirm_id
    from helixis.agents.write_ledger import record_write

    await record_write(
        company_id=company_id,
        action_type=action_type,
        confirm=confirm,
        outcome=res.outcome,
        detail=res.detail,
        # Only a gated write has an approval. A confirm=None write (approved in
        # the UI, or scheduled) must not inherit an earlier gate's id.
        approval_id=current_confirm_id.get() if confirm is not None else None,
        idem_key=idem_key,
    )


def _jsonable(value: Any) -> Any:
    """Coerce a result to something json-serializable for the replay record.

    Pydantic models expose ``model_dump``; everything else is round-tripped
    through ``_canonical`` only if it isn't already JSON-native."""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, str | int | float | bool | type(None) | dict | list):
        return value
    return str(value)


__all__ = [
    "STATUS_ABANDONED_UNKNOWN",
    "STATUS_ABORTED",
    "STATUS_FAILED_RETRYABLE",
    "STATUS_SUCCEEDED",
    "GuardResult",
    "PreSideEffectError",
    "derive_key",
    "guarded_action",
    "run_guarded",
    "store_from_pool",
]
