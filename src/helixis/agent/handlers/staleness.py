"""An Inbox card is a photograph. Check the record still looks like it.

A card is built from a Buildium record at the moment its webhook arrived, and
then it sits in the Inbox until somebody clicks. Between those two moments the
record can move — most consequentially, the task can be **completed or closed**
by whoever was already working it, in Buildium, where Occupella never sees the
click. Nothing on the execute path noticed. The handler happily posted "just
following up, this is still outstanding" onto a job that finished on Tuesday.

That is a worse failure than an ordinary bug because the output is *plausible*:
a well-written note, correctly addressed, about a premise that stopped being
true. The reader has no way to tell it was written from a stale photograph.

WHAT IS CHECKED, AND WHY ONLY THIS
Only the open→terminal transition, and only when we positively observed it.
Blocking on every status drift (``New`` → ``InProgress``) would refuse a
perfectly good follow-up note because somebody started the work, and a guard
that cries wolf gets switched off. So this is the same precision-over-recall
trade the identifier scrubber makes: block on evidence that the action's
PREMISE is gone, not on evidence that something changed.

⚠ **The status lives under ``TaskStatus``, and ``Status`` is NULL on every
production row** (gotcha 26 — verified across all 179). Reading ``.status``
here would find nothing, conclude "not terminal", and pass every stale card
through while looking like a working check.

⚠ **The open-status list is IMPORTED, not restated.** A second hand-maintained
copy of a Buildium enum is precisely the drift ``id_hygiene`` was written to
clean up after: the day someone adds a status, one table learns about it and
the other silently keeps its old answer.

FAILING OPEN, ON PURPOSE, AND SAYING SO
If the re-read itself fails — Buildium down, credentials rotated, task deleted —
this does **not** block. A staleness check that fails closed turns every
provider blip into "no action in the Inbox works", which is a much larger
outage than the one it prevents, and these actions are already
human-confirmed and manager-gated. But an unchecked execution is NOT a checked
one, so :attr:`StalenessVerdict.checked` records which happened and the caller
logs it. Silent degradation of a safety check is gotcha 17's whole lesson: the
memory embedder 403'd for nineteen days because the failure path was quiet.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StalenessVerdict:
    """Whether the card's premise still holds, and whether we could tell.

    ``stale=False`` and ``checked=False`` are **not** the same statement.
    The first says the record still looks right; the second says we never
    found out. Collapsing them into one boolean is how a check that never
    runs starts reading as a check that passed.
    """

    stale: bool
    checked: bool
    reason: str = ""


#: Nothing to compare against — the action does not reference a Buildium task
#: (``create_followup_task`` makes a new one; ``reply_to_email`` never touches
#: Buildium at all), so there is no photograph to go stale.
NOT_APPLICABLE = StalenessVerdict(stale=False, checked=False, reason="no task to revalidate")


def _open_statuses() -> tuple[str, ...]:
    """The live open-status tuple, imported so it cannot drift from a copy.

    Imported inside the function purely to keep the heavyweight tool module
    off this one's import path; the value itself is the single source.
    """
    from helixis.agents.buildium_tools import _OPEN_TASK_STATUSES

    return _OPEN_TASK_STATUSES


def compare_task_status(card_status: str | None, live_status: str | None) -> StalenessVerdict:
    """Pure verdict from two status strings. No I/O, no Buildium, no clock.

    The whole decision is here so it can be tested exhaustively without a
    provider, and so the async wrapper below contains nothing but the fetch.
    """
    if not live_status or not str(live_status).strip():
        # A blank live status is the gotcha-26 shape: reading the wrong field
        # yields None and would silently mean "not terminal". Refuse to draw a
        # conclusion from it rather than manufacturing a reassuring one.
        return StalenessVerdict(
            stale=False, checked=False, reason="live status unreadable; not revalidated"
        )

    open_statuses = {s.casefold() for s in _open_statuses()}
    live = str(live_status).strip()
    if live.casefold() in open_statuses:
        return StalenessVerdict(stale=False, checked=True)

    card = (card_status or "").strip()
    if card and card.casefold() not in open_statuses:
        # It was already finished when the card was written. The card is not
        # STALE — it is about a finished thing and always was, which is a
        # planner question, not a freshness one. Blocking here would refuse
        # legitimate "close the loop" notes on completed work.
        return StalenessVerdict(stale=False, checked=True)

    return StalenessVerdict(
        stale=True,
        checked=True,
        reason=(
            f"This card was written when the task was {card or 'open'}, but it is now "
            f"{live} in Buildium. Someone finished it after the card was created, so "
            f"the action would be based on a premise that no longer holds. Refresh the "
            f"Inbox and take another look before acting."
        ),
    )


async def revalidate_task(client: Any, task_id: Any, card_status: str | None) -> StalenessVerdict:
    """Re-read the task and compare. Never raises — see the module docstring.

    ⚠ Reads ``task_status``, not ``status``: ``GET /tasks/{id}`` returns the
    value under ``TaskStatus`` and leaves ``Status`` NULL on every production
    row (gotcha 26). The wrong attribute would return ``None`` on a genuinely
    completed task and wave it straight through.
    """
    try:
        task_id_int = int(task_id)
    except (TypeError, ValueError):
        return NOT_APPLICABLE
    if client is None:
        return NOT_APPLICABLE

    try:
        task = await client.tasks.get(task_id_int)
    except Exception:  # noqa: BLE001 — a failed check must never block the action
        logger.warning(
            "staleness re-read failed for task %s; executing WITHOUT revalidation",
            task_id_int,
            exc_info=True,
        )
        return StalenessVerdict(stale=False, checked=False, reason="re-read failed")

    live = getattr(task, "task_status", None) or getattr(task, "status", None)
    return compare_task_status(card_status, live)


__all__ = [
    "NOT_APPLICABLE",
    "StalenessVerdict",
    "compare_task_status",
    "revalidate_task",
]
