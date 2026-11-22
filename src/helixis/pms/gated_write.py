"""The only way a connector built on ``helixis.pms`` changes a vendor record.

Rent Manager and Propertyware write through :func:`gated_write`, never through
the client directly: ``PmsHttpClient.send_once`` is called in this module and
nowhere else (``test_pms_gated_write`` walks the tree for it), and inside the
closure handed to ``run_guarded`` as ``execute=``, which is what
``test_rentvine_confirm_gate``'s walk requires of every write.

GATE ORDER, AND IT IS THE ORDER THAT MATTERS (Rentvine's, ``rentvine_tools``)
    switch → company → role floor → the connector reads and plans → money
    refusal → confirm card → idempotency claim → re-read (refuse if the
    record moved since the card) → the one send → read back and compare.

    ⚠ The switch is FIRST (gotcha 67): nobody should approve a write that
    cannot happen. ``registry.writes_enabled`` is off unless the connector
    itself is on, so a dark connector never shows a card.

    ⚠ A refusal that provably sent nothing (a rate hold, the breaker, a 4xx,
    a request Occupella would not send, the record moving) is filed
    RETRYABLE through :class:`NotSentError`. Anything else after the send
    started (a 5xx, a dropped connection) may have landed, so it is filed
    ``abandoned_unknown`` and never sent again (gotcha 70's mirror image).

    ⚠ A vendor's "conflict" (409) or "needs override" (412) is never retried
    blind. A 412 is reported with the vendor's reason, fenced, and nothing is
    sent: Occupella never sends an override. A 409 means the record changed:
    the person asks again and gets a new card built from a fresh read. The one
    exception is opt-in per write (``resend_after_conflict``, Rent Manager's
    rule): re-read, and if every field the approver saw is unchanged, send
    once more under the same approval. A 409 is a refusal, so the first send
    did not land, and :func:`same_write` holds the resend to the approved
    request except for the keys the plan names ``volatile``.

NEVER MOVE MONEY (Brandon's plan)
    A request path naming money (:data:`MONEY_WORDS`) is refused before any
    card, whatever the connector's operation index allows. Changing that list
    is a shared-layer PR, which is the review the plan asks for.

THE ACTIVITY ROW
    ``run_guarded`` writes it (``write_ledger.record_write``), with the
    approval id and the card's ``details`` sentence. ``write_ledger`` names the
    system from the ``action_type`` prefix, and anything without a known
    prefix is named Buildium, so a plan whose ``action_type`` does not start
    with ``"<pms>_"`` is refused here (a connector bug, so it raises).
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from helixis.agents.idempotency import (
    SUCCESS_OUTCOMES,
    PreSideEffectError,
    derive_key,
    pending_reply,
    run_guarded,
)
from helixis.api.roles import ADMIN, MANAGER, MEMBER, ROLES, role_at_least
from helixis.pms import registry
from helixis.pms.errors import Failure, PmsApiError, PmsUnavailableError, RefusedBeforeSendingError
from helixis.pms.http import PmsHttpClient, PmsResponse

logger = logging.getLogger(__name__)

#: How long a finished write answers a repeat of itself instead of sending
#: again (``run_guarded``'s window). Rentvine's status change uses the same.
WINDOW = timedelta(minutes=10)

#: Read-back attempts, for a vendor that answers a read before the write shows.
VERIFY_ATTEMPTS = 3
VERIFY_BACKOFF_S = 0.2

#: Words in a request path that name money. Matched in each path segment,
#: lower-cased, so ``/Payments``, ``/leases/{id}/charges`` and
#: ``/ownerDistributions`` are all refused. Over-matching fails closed: a
#: write it wrongly refuses is a shared-layer PR away, a payment sent is not.
MONEY_WORDS: tuple[str, ...] = (
    "bank",
    "bill",
    "charge",
    "check",
    "credit",
    "deposit",
    "disburse",
    "distribution",
    "epay",
    "invoice",
    "journal",
    "ledger",
    "payment",
    "receipt",
    "refund",
    "transaction",
    "transfer",
)

_FLOOR_WORDS = {ADMIN: "an Admin", MANAGER: "a Manager or Admin", MEMBER: "a member"}

#: Every connector write's answer when its card was not approved: declined, or
#: its window ran out (``run_guarded`` files both as ``not_approved``). One
#: template, built by :func:`not_approved_reply` and read back by
#: :func:`is_not_approved_reply`, so the per-run hide that keys on it
#: (``tool_registries.connector_tools``) cannot drift from the words.
_NOT_APPROVED = "Okay, I didn't {action}. Nothing was changed in {name}."


#: A connector write's answer when it was turned away before any card
#: (``tool_registries.connector_tools``): another write to the same system was
#: already in flight in this run, or its card was not approved. It reads to the
#: model, which tells the person: the other change was in progress or turned
#: down, this one was never sent, and a different change the person asked for
#: gets its own card next.
#:
#: ⚠ "In progress", never "on a card": the first write holds the claim from
#: before its card, while it still reads and plans, and may be refused before
#: any card is shown. ⚠ Never the not-approved sentence: read as one, it would
#: end the system's writes for a card nobody turned down.
_HELD_BACK = (
    "Not sent: only one {name} change is handled at a time, and another one was "
    "already in progress (being prepared, or waiting on the person's approval card) "
    "or had been turned down. Nothing was changed by this request and no card was "
    "shown for it. If it repeats that change, nothing more is needed. If it is a "
    "different change the person asked for, and they did not turn the other one "
    "down, make it again in its own request so it gets its own card. Never describe "
    "this request as failed or lost."
)


def not_approved_reply(*, action: str, name: str) -> str:
    """The reply to a write whose card was declined or never answered."""
    return _NOT_APPROVED.format(action=action, name=name)


def held_back_reply(*, name: str) -> str:
    """The reply to a write turned away while another write to system
    ``name`` was in flight (:data:`_HELD_BACK`)."""
    return _HELD_BACK.format(name=name)


def is_not_approved_reply(reply: object, name: str) -> bool:
    """True when ``reply`` is :func:`not_approved_reply` for system ``name``.

    ⚠ Read from the reply, not recorded inside :func:`gated_write`: the reply is
    what reaches the tool's caller both in production and in the eval harness,
    which replaces each connector write's ``_impl`` with a mock that returns
    this sentence. A record made in here would never run in the cases that
    test it (531, 534).
    """
    if not isinstance(reply, str):
        return False
    head, _, tail = _NOT_APPROVED.partition("{action}")
    pattern = re.escape(head) + r"[^\n]+?" + re.escape(tail.format(name=name))
    return re.fullmatch(pattern, reply) is not None


async def _sleep(seconds: float) -> None:
    """``asyncio.sleep``, patchable in tests."""
    await asyncio.sleep(seconds)


@dataclass(frozen=True)
class Send:
    """One request :func:`gated_write` may send."""

    method: str
    template: str
    body: Any = None
    path_params: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WritePlan:
    """What the approver sees and exactly what will be sent, built by the
    connector from a read taken just before the card."""

    #: ``"<pms>_<verb>"``. Names the write in the idempotency store and the
    #: activity feed, which reads the system from the prefix.
    action_type: str
    #: The verb phrase for replies: "change the work order's status".
    action: str
    #: The card's question: "Change this work order's status in Rent Manager?"
    title: str
    #: The sentence the approver reads. The activity row copies it verbatim.
    details: str
    #: The card's lines: each field's current value and its new one.
    items: tuple[str, ...]
    #: The request, exactly as it will be sent.
    send: Send
    #: What makes two requests the same write (the idempotency key's values).
    key: Mapping[str, Any]
    #: The reply once the write landed, from the vendor's answer (a created
    #: record's id). It runs after the send, so a failure in it never turns a
    #: write that happened into "may have landed": the card's ``details`` is
    #: said instead.
    done: Callable[[PmsResponse], str]
    #: Top-level body keys a re-read may refresh between the card and the
    #: send, such as a concurrency value. Every other key must be what the
    #: approver saw (:func:`same_write`).
    volatile: frozenset[str] = frozenset()


class NotSentError(PreSideEffectError):
    """Nothing reached the vendor, or the vendor refused it: the write did not
    happen and asking again is safe. ``run_guarded`` files it retryable.
    Carries the reply for the user."""

    def __init__(self, reply: str):
        self.reply = reply
        super().__init__(reply)


def money_words_in(template: str) -> list[str]:
    """The :data:`MONEY_WORDS` in a request path's segments (``{params}`` are
    not segments). Empty when the path names no money."""
    found: list[str] = []
    for segment in template.lower().split("/"):
        if not segment or segment.startswith("{"):
            continue
        found.extend(word for word in MONEY_WORDS if word in segment and word not in found)
    return found


def _without(body: Any, volatile: frozenset[str]) -> Any:
    """``body`` without the volatile keys, at the top level of a dict or of
    each dict in a list (Rent Manager posts a list holding one object)."""
    if isinstance(body, dict):
        return {k: v for k, v in body.items() if k not in volatile}
    if isinstance(body, list):
        return [_without(item, volatile) if isinstance(item, dict) else item for item in body]
    return body


def same_write(approved: Send, now: Send, volatile: frozenset[str] = frozenset()) -> bool:
    """Whether ``now`` is the request the approver saw: the same verb, path
    and path values, and the same body except for the ``volatile`` keys."""
    return (
        approved.method.upper() == now.method.upper()
        and approved.template == now.template
        and dict(approved.path_params) == dict(now.path_params)
        and _without(approved.body, volatile) == _without(now.body, volatile)
    )


def provably_not_sent(exc: PmsApiError) -> bool:
    """Refused before sending (a hold, the breaker), or refused by the vendor
    with a 4xx. A 5xx or a transport error is ambiguous: it may have landed."""
    return (not exc.sent) or 400 <= exc.status < 500


def refusal(exc: PmsApiError, *, action: str, name: str) -> str:
    """A vendor failure in terms the user can act on. Nothing in it claims a
    change happened: every case it words is one where nothing was sent or
    the vendor refused.

    ⚠ 401 and 403 are different (gotcha 102): a 403 means the credential was
    accepted and the operation refused, so "reconnect" would send somebody to
    redo a connection that works.
    """
    unchanged = f"Nothing was changed in {name}."
    if isinstance(exc, PmsUnavailableError):
        wait = (
            f"about {max(1, math.ceil(exc.remaining / 60))} minute(s)"
            if exc.remaining
            else "a few minutes"
        )
        return (
            f"I couldn't {action}: {name} kept failing, so Occupella paused calls to it and "
            f"sent nothing. {unchanged} Try again in {wait}."
        )
    # ⚠ Credentials before the hold: a broken credential and a refused login
    # are also "not sent", and telling somebody to wait a minute about a
    # password that will never work sends them back to fail again.
    if exc.failure is Failure.CREDENTIALS:
        return (
            f"I couldn't {action}: {name} rejected the credentials. {unchanged} Reconnect "
            f"{name} on the Connectors page (Settings → Connectors)."
        )
    if exc.failure is Failure.PERMISSION:
        return (
            f"I couldn't {action}: {name} accepted the credentials and refused this "
            f"operation, so the {name} user Occupella signs in as isn't allowed to do it. "
            f"That is changed in {name}'s user permissions, not by reconnecting. {unchanged}"
        )
    if exc.status == 429 and not exc.sent:
        return (
            f"I couldn't {action}: {name} asked Occupella to wait before sending more, so "
            f"nothing was sent. {unchanged} Try again in a minute or two."
        )
    if exc.failure is Failure.NOT_FOUND:
        return f"I couldn't {action}: {name} has no record with that id. {unchanged}"
    if exc.failure is Failure.CONFLICT:
        return (
            f"I couldn't {action}: the record changed in {name} after you approved, so I "
            f"didn't send the change. {unchanged} Ask me again and I'll show you the current "
            "record to approve."
        )
    if exc.failure is Failure.NEEDS_OVERRIDE:
        from helixis.security.prompt_delimit import wrap_untrusted

        reason = exc.detail.strip()[:300]
        said = (
            f" {name}'s reason:\n" + wrap_untrusted(reason, label=f"{name} reply") if reason else ""
        )
        return (
            f"I couldn't {action}: {name} only accepts this change with an override, and "
            f"Occupella never sends one. {unchanged} If it should still happen, make it in "
            f"{name} directly.{said}"
        )
    if exc.failure is Failure.BAD_REQUEST:
        return (
            f"I couldn't {action}: {name} rejected the request Occupella built, which is a "
            f"fault in Occupella, not in anything you did. {unchanged} Make the change in "
            f"{name} directly for now."
        )
    if exc.status == 429:
        return (
            f"I couldn't {action}: {name} is limiting requests right now. {unchanged} "
            "Try again in a few minutes."
        )
    if 400 <= exc.status < 500:
        return f"I couldn't {action}: {name} refused the request. {unchanged}"
    return f"I couldn't {action}: {name} could not be reached. {unchanged}"


def _will_not_send(exc: RefusedBeforeSendingError, *, action: str, name: str) -> str:
    logger.error("pms gated write refused before sending: %s", exc)
    return (
        f"I couldn't {action}: Occupella refused to send a request {name} does not "
        f"document, which is a fault in Occupella. Nothing was changed in {name}. Make the "
        f"change in {name} directly for now."
    )


def _current_company_id() -> str:
    from helixis.memory.context import current_memory_context

    ctx = current_memory_context()
    return ctx.company_id if ctx else ""


async def _caller_role() -> str | None:
    """The one answer to "what role is the caller" (``buildium_tools``), as
    Rentvine's write tools use it (gotcha 25: two answers drift)."""
    from helixis.agents.buildium_tools import _caller_role as caller_role

    return await caller_role()


async def gated_write(
    *,
    client: PmsHttpClient,
    prepare: Callable[[], Awaitable[WritePlan | str]],
    verify: Callable[[WritePlan, PmsResponse], Awaitable[bool]],
    recheck: Callable[[WritePlan], Awaitable[Send | None]] | None = None,
    resend_after_conflict: bool = False,
    role_floor: str = MANAGER,
    window: timedelta = WINDOW,
) -> str:
    """Run one connector write through every gate. Returns the reply.

    * ``prepare``: read what the write changes and build the plan. A string
      is a reply instead (nothing to change, or a refusal): no card is shown.
    * ``verify``: read the record back and compare (rule 6). Read-only. A 2xx
      is not the write.
    * ``recheck``: re-read just before sending. Returns the request to send
      now, or ``None`` when the record moved in a way the approver did not
      see. Without it, the approved request is sent as it is.
    * ``resend_after_conflict``: after a 409, run ``recheck`` and send once
      more if the request is still :func:`same_write` as the approved one.
      Off unless a connector's terms call for it (Rent Manager's do).
    * ``role_floor``: the least role that may approve. Manager by default,
      Rentvine's floor: a change whose undo is not verified is treated as
      hard to undo.

    A connector bug (an unknown floor, a resend with nothing to recheck, a
    client for another company, a plan for another system) raises before any
    card is shown or anything is sent.
    """
    if role_floor not in ROLES:
        raise ValueError(f"role_floor must be one of {ROLES}, not {role_floor!r}")
    if resend_after_conflict and recheck is None:
        raise ValueError("resend_after_conflict needs a recheck to decide what to resend")
    pms = client.pms
    name = registry.name_of(pms) or pms

    # 1. The switch, before anything is read or shown (gotcha 67).
    if not registry.writes_enabled(pms):
        return (
            f"Changing records in {name} from Occupella isn't switched on, so I didn't change "
            f"anything. Make the change in {name} directly."
        )

    # 2. This company, and a client carrying this company's credentials.
    company_id = _current_company_id()
    if not company_id:
        return (
            f"{name} isn't connected for this workspace, so there's nothing for me to change. "
            f"Connect {name} on the Connectors page (Settings → Connectors)."
        )
    if client.company_id != company_id:
        raise ValueError(
            f"a {pms} client for company {client.company_id!r} was handed a write for "
            f"{company_id!r}"
        )

    # 3. The role floor. An unknown role is a refusal, never a pass.
    role = await _caller_role()
    if role is None or not role_at_least(role, role_floor):
        return (
            f"This needs {_FLOOR_WORDS[role_floor]} on this workspace, so I didn't do anything "
            f"in {name}. Ask a teammate with that role, or make the change in {name} directly."
        )

    # 4. The connector reads the record and plans the write.
    try:
        planned = await prepare()
    except PmsApiError as exc:
        return refusal(exc, action="read the record first", name=name)
    except RefusedBeforeSendingError as exc:
        return _will_not_send(exc, action="read the record first", name=name)
    if isinstance(planned, str):
        return planned
    plan = planned
    if not plan.action_type.startswith(f"{pms}_"):
        raise ValueError(
            f"{plan.action_type!r} must start with {pms!r}_ so the activity feed names {name}"
        )

    # 5. Never move money, whatever the connector's index allows.
    money = money_words_in(plan.send.template)
    if money:
        logger.error(
            "pms gated write refused a money path %s %s (%s)",
            plan.send.method,
            plan.send.template,
            ", ".join(money),
        )
        return (
            f"I can't {plan.action}: Occupella never moves or records money in {name}. "
            f"Nothing was changed in {name}. Make this change in {name} directly."
        )

    key = derive_key(company_id, plan.action_type, dict(plan.key))
    #: What the vendor answered to the send that landed, for ``verify``.
    answered: list[PmsResponse] = []

    async def _current_request() -> Send:
        """The request to send now: the approved one, or the re-read's when it
        is still the approved write. Nothing has been sent when this raises."""
        if recheck is None:
            return plan.send
        try:
            now = await recheck(plan)
        except PmsApiError as exc:
            raise NotSentError(refusal(exc, action=plan.action, name=name)) from exc
        except Exception as exc:  # noqa: BLE001 — before the send: provably nothing sent
            logger.exception("%s: re-read before sending failed", plan.action_type)
            raise NotSentError(
                f"I couldn't re-check the record before sending, so I didn't {plan.action}. "
                f"Nothing was changed in {name}. Ask me again in a moment."
            ) from exc
        if now is None or not same_write(plan.send, now, plan.volatile):
            raise NotSentError(
                f"The record changed in {name} after you approved, so I didn't {plan.action}. "
                f"Nothing was changed in {name}. Ask me again and I'll show you the current "
                "record to approve."
            )
        return now

    async def _send() -> str:
        request = await _current_request()
        # ⚠ At most two sends, and the second only after the vendor refused the
        # first with a 409 (nothing landed) and the re-read still matches
        # what was approved. Bounded here, in code (rule 13).
        attempts = 2 if resend_after_conflict else 1
        for attempt in range(1, attempts + 1):
            try:
                response = await client.send_once(
                    request.method,
                    request.template,
                    json_body=request.body,
                    path_params=request.path_params,
                )
            except RefusedBeforeSendingError as exc:
                raise NotSentError(_will_not_send(exc, action=plan.action, name=name)) from exc
            except PmsApiError as exc:
                if exc.failure is Failure.CONFLICT and attempt < attempts:
                    request = await _current_request()
                    continue
                if provably_not_sent(exc):
                    raise NotSentError(refusal(exc, action=plan.action, name=name)) from exc
                raise
            answered.append(response)
            try:
                return plan.done(response)
            except Exception:  # noqa: BLE001 — the write landed; only the wording failed
                logger.exception("%s: building the reply failed", plan.action_type)
                return plan.details
        raise AssertionError("unreachable: the loop returns or raises")  # pragma: no cover

    async def _verify(_reply: str) -> bool:
        """Read back (rule 6), up to :data:`VERIFY_ATTEMPTS` times: a vendor
        may answer a read before its own write shows (Rentvine's verify
        retries the same way). "Not yet" (``False``, or a 404 for a record
        just created) is read again; so is a failed read. Whatever the last
        attempt said decides: a no, or the error raised, which ``run_guarded``
        files as needs-review. Never a success."""
        if not answered:
            return False
        last: Exception | None = None
        for attempt in range(VERIFY_ATTEMPTS):
            last = None
            try:
                if await verify(plan, answered[-1]):
                    return True
            except PmsApiError as exc:
                if exc.failure is not Failure.NOT_FOUND:
                    last = exc
            except Exception as exc:  # noqa: BLE001 — a transient read failure
                last = exc
            if attempt < VERIFY_ATTEMPTS - 1:
                await _sleep(VERIFY_BACKOFF_S * (attempt + 1))
        if last is not None:
            raise last
        return False

    body = plan.send.body
    confirm = {
        "action": plan.action_type,
        "title": plan.title,
        "details": plan.details,
        "items": list(plan.items),
        # The ACTUAL request body, not a summary of it (the Rentvine rule).
        "request_body": body if isinstance(body, dict) else {"body": body},
    }
    try:
        guard = await run_guarded(
            company_id=company_id,
            idem_key=key,
            action_type=plan.action_type,
            confirm=confirm,
            execute=_send,
            verify=_verify,
            window=window,
        )
    except NotSentError as refused:
        return refused.reply
    except PmsApiError:
        logger.warning("%s: may have reached %s; filed for review", plan.action_type, name)
        return (
            f"I sent the request to {plan.action} to {name} but couldn't tell whether it "
            f"went through, so I've flagged it for review and won't send it again. Please "
            f"check {name} before asking me to retry."
        )

    if guard.outcome == "not_approved":
        return not_approved_reply(action=plan.action, name=name)
    if guard.outcome == "blocked_store_unavailable":
        return (
            f"I couldn't safely process this right now, so I didn't {plan.action}. Nothing "
            f"was changed in {name}. Please try again shortly."
        )
    if guard.outcome == "blocked_needs_review":
        # True for both causes: this call's read-back failed, or an earlier
        # attempt at the same change is already filed for review.
        return (
            f"A request to {plan.action} was sent to {name} and could not be confirmed, so "
            f"it's flagged for review and won't be sent again. Please check {name} before "
            "asking me to retry."
        )
    if guard.outcome not in SUCCESS_OUTCOMES:
        return pending_reply(guard, action=plan.action, system=name)
    return str(guard.result) if guard.result is not None else plan.details


__all__ = [
    "MONEY_WORDS",
    "VERIFY_ATTEMPTS",
    "WINDOW",
    "NotSentError",
    "Send",
    "WritePlan",
    "gated_write",
    "held_back_reply",
    "is_not_approved_reply",
    "money_words_in",
    "not_approved_reply",
    "provably_not_sent",
    "refusal",
    "same_write",
]
