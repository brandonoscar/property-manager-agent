"""The ``send_task_to_teammate`` orchestrator tool (multiplayer PR B).

"Ask Alex to review the COI on vendor 4821" — the orchestrator resolves the
teammate against the company roster, surfaces a confirm gate (the user sees
exactly who gets what before anything moves), and on approval enqueues a
``Occupella.TaskRequest`` through the existing webhook pipeline
(``webhooks/team_tasks``). The teammate's Inbox renders it like any other
actionable event, and their agent runs on it via the inbox freeform path.

Safety posture:
* Confirm-gated like every write — the enqueue is unreachable without an
  explicit approval through ``confirm_or_abort`` (the structural gate).
* Company-scoped: the roster lookup is bound to the run's memory-context
  company, so a task can only ever target a member of the SAME company.
* Ambiguity never guesses: zero or multiple roster matches return the
  candidate list for the model to disambiguate with the user — a task for
  the wrong teammate is a trust-burner, not a retry.
"""

from __future__ import annotations

import logging

from agents import function_tool

from helixis.agents.tools import confirm_or_abort
from helixis.db.engine import get_pool
from helixis.db.repositories.members import Member, list_members
from helixis.memory.context import current_memory_context
from helixis.webhooks.team_tasks import enqueue_team_task

logger = logging.getLogger(__name__)


def member_label(member: Member) -> str:
    """Roster display label: name when known, else the email."""
    return (member.display_name or "").strip() or (member.email or "").strip() or member.user_id


def resolve_teammate(members: list[Member], query: str) -> list[Member]:
    """Match ``query`` against the roster. Exact email match wins outright;
    otherwise a case-insensitive substring match on display name or email.
    Returns ALL matches — the caller refuses to guess between several."""
    q = (query or "").strip().lower()
    if not q:
        return []
    exact = [m for m in members if (m.email or "").strip().lower() == q]
    if exact:
        return exact[:1]
    return [
        m for m in members if q in (m.display_name or "").lower() or q in (m.email or "").lower()
    ]


@function_tool
async def send_task_to_teammate(teammate: str, title: str, details: str) -> str:
    """Send a task to a teammate's Occupella Inbox.

    Use when the user asks you to hand work to a colleague — "ask Alex to
    review the COI", "have Jordan follow up with the vendor". The teammate
    is matched against this company's team roster by name or email. The
    task lands in the shared Inbox addressed to them, where their own
    assistant can pick it up. Always pauses for the user's confirmation
    before sending; nothing is sent on cancel.

    Args:
        teammate: The colleague's name or email, as the user referred to them.
        title: A short task title (one line).
        details: What needs doing, with enough context to act on — include
            the relevant property/tenant/vendor references from the
            conversation.
    """
    ctx = current_memory_context()
    if ctx is None or not ctx.company_id:
        return "I can't send tasks right now — no company context is bound to this run."
    try:
        pool = get_pool()
    except RuntimeError:
        return "I can't send tasks right now — the task queue is unavailable."

    try:
        members = await list_members(pool, company_id=ctx.company_id)
    except Exception:  # noqa: BLE001 — roster failure degrades, never crashes the run
        logger.exception("send_task_to_teammate: roster lookup failed")
        return "I couldn't load the team roster to find that teammate. Try again in a moment."

    matches = resolve_teammate(members, teammate)
    if not matches:
        roster = ", ".join(member_label(m) for m in members) or "(no teammates found)"
        return (
            f"I couldn't find a teammate matching {teammate!r} on this company's "
            f"team. Current members: {roster}. Ask the user which one they meant."
        )
    if len(matches) > 1:
        options = ", ".join(member_label(m) for m in matches)
        return (
            f"{teammate!r} matches more than one teammate: {options}. "
            "Ask the user which one they meant — never guess."
        )

    target = matches[0]
    target_name = member_label(target)
    sender = next((m for m in members if m.user_id == ctx.user_id), None)
    sender_name = member_label(sender) if sender else "A teammate"

    clean_title = (title or "").strip() or "(untitled task)"
    clean_details = (details or "").strip()

    approved = await confirm_or_abort(
        action="send_task_to_teammate",
        title=f"Send this task to {target_name}?",
        details=(
            f"A task titled “{clean_title}” will land in {target_name}'s "
            "Occupella Inbox, marked as requested by you."
        ),
        items=[clean_title] + ([clean_details[:200]] if clean_details else []),
        request_body={
            "to": target_name,
            "title": clean_title,
            "details": clean_details,
        },
    )
    if not approved:
        return "Cancelled — no task was sent."

    try:
        sent = await enqueue_team_task(
            pool,
            company_id=ctx.company_id,
            from_user_id=ctx.user_id,
            from_name=sender_name,
            to_user_id=target.user_id,
            to_name=target_name,
            title=clean_title,
            details=clean_details,
        )
    except Exception:  # noqa: BLE001 — surface a clean failure, never a raw trace
        logger.exception("send_task_to_teammate: enqueue failed")
        return "The task couldn't be queued — it has NOT been sent. Try again in a moment."

    if not sent:
        return "The task couldn't be queued — it has NOT been sent. Try again in a moment."
    return (
        f"Done — the task “{clean_title}” is on its way to {target_name}'s "
        "Inbox (it appears within about a minute)."
    )


__all__ = ["member_label", "resolve_teammate", "send_task_to_teammate"]
