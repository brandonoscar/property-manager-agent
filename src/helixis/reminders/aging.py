"""How a reminder card gets LOUDER when nobody deals with it.

WHAT WAS WRONG
--------------
Two defects, both silent, both found 2026-09-09 by reading the reminder
lifecycle rather than by any test failing.

**1. ``handled`` was a permanent mute, and the schema said it was a cooldown.**
``0045_inbox_reminders.sql`` describes the status in its own header comment
(quoted VERBATIM, old product name and all — it is evidence of what the schema
claimed, and paraphrasing evidence into today's vocabulary is how a quote stops
being one):

    'handled' means the PM acted outside Helixis (phone call) — suppressed
    for the producer's cooldown.

There was no cooldown anywhere in the reminder path. ``_upsert_one`` read
``WHEN status IN ('dismissed', 'handled') THEN <keep it>``, so a card marked
handled stayed hidden for as long as its condition kept holding — forever, in
practice. ``handled_at`` was WRITTEN by ``set_status`` and read by nothing.

That is gotcha 48's shape (a comment cannot fail, so it went on describing a
guarantee the code never had) sitting on top of a real product hole: "I called
them about it" is a CLAIM, and a claim that turns out to be wrong — the
condition still holds three months later — is exactly the item nobody is
tracking. Dismiss is deliberately permanent; handled never was meant to be.

**2. Nothing escalated for AGE.** ``escalation`` was computed by each producer
from the entity's own facts (days until a lease ends, whether a bill is
overdue) and never from how long the card had been sitting there. So a card
that appeared this morning and one that had been ignored for six weeks were
indistinguishable on the desk. ``first_fired_at`` was already on the row,
already correct, and read by nothing but the API DTO — the same
fetched-and-thrown-away shape as gotchas 41 / 50 / 51.

WHY THE RULE LIVES HERE AND NOT IN THE PRODUCERS
------------------------------------------------
Every producer would have to make the same good decision independently, and
one that forgot would be silently exempt. This product has learned that five
times over (gotchas 25 / 32 / 34 / 37 / 50): when N callers each have to get
one thing right, make it one thing that gets it right. So the ladder is
applied ONCE, in the repository's upsert, to every card from every producer
and from the event-driven ``upsert_card`` path too.

⚠ **The SQL is GENERATED from these constants, never hand-written beside
them.** A hand-written CASE and a Python ladder are two definitions of one
rule and they drift — gotcha 28's both-sides problem, with SQL as the far
side. :func:`age_escalation_sql` is the only place the arms exist.
"""

from __future__ import annotations

#: Longest a user may explicitly silence a card by asking ("remind me in N
#: days"). Owned here rather than in the route so the cooldown below can be
#: derived from it instead of coincidentally matching.
MAX_SNOOZE_DAYS = 30

#: How long "I handled it outside Occupella" suppresses a card whose condition
#: still holds. After this the sweep reopens it.
#:
#: ⚠ The bound is a RELATION, not a number: marking something handled must not
#: silence it for longer than the longest silence a user can explicitly ask
#: for. Snooze says "not now, ask me again"; handled says "this is dealt
#: with". If the second turns out to be false, the product should not keep the
#: user's own mistake from them for longer than they could have requested.
#: ``test_the_handled_cooldown_never_outlasts_the_longest_snooze`` pins the
#: relation, so raising the snooze cap does not silently uncouple them.
HANDLED_COOLDOWN_DAYS = MAX_SNOOZE_DAYS

#: (days a card has been open, minimum escalation) — highest band FIRST.
#:
#: These are a judgement call and not a measurement, which is worth saying
#: plainly. Seven days is already this codebase's unit of "late" (``bills_due``
#: calls a bill high-severity at ``days_over >= 7``); three weeks is the point
#: at which "it is on the list" has stopped being true in any useful sense.
AGE_ESCALATION_LADDER: tuple[tuple[int, int], ...] = ((21, 2), (7, 1))

#: The escalation at which a card is forced to ``high`` regardless of what its
#: producer asked for. See :func:`age_severity_sql` for why only the top band.
HIGH_SEVERITY_ESCALATION = 2


def age_escalation(days_open: float) -> int:
    """The minimum escalation a card carries purely for having sat there.

    ⚠ The clock is ``first_fired_at`` — when the OBLIGATION appeared — and it
    is deliberately NOT paused while a card is snoozed. Someone who snoozes a
    lease renewal twice has acted, and this is not a negligence score: a
    renewal that has been outstanding for three weeks is urgent whether or not
    somebody pressed "later" on it. Pausing the clock would make the ladder
    say "how rude has the user been" when the question it exists to answer is
    "how long has this been unresolved".
    """
    for days, floor in AGE_ESCALATION_LADDER:
        if days_open >= days:
            return floor
    return 0


def age_escalation_sql(*, status_column: str, fired_column: str) -> str:
    """The ladder as a Postgres expression, generated from the same tuple.

    ``status_column`` / ``fired_column`` are the EXISTING row's columns inside
    an ``ON CONFLICT DO UPDATE``, where a bare table reference reads the
    pre-update values.

    ⚠ **The ``cleared`` arm is load-bearing and is why this cannot be a plain
    two-arm CASE.** A card that self-cleared and whose condition later returns
    is a NEW instance — ``_upsert_one`` already resets ``first_fired_at`` for
    exactly that reason. But inside DO UPDATE the reference still reads the
    OLD value, so without this arm a problem that recurs a year after the last
    one would be born at escalation 2, on its first morning, having been
    ignored by nobody. Tested directly, because it is invisible by reading:
    the expression looks correct without it.
    """
    arms = "".join(
        f"WHEN {fired_column} <= now() - interval '{days} days' THEN {floor}\n            "
        for days, floor in AGE_ESCALATION_LADDER
    )
    return (
        "CASE\n"
        f"            WHEN {status_column} = 'cleared' THEN 0\n"
        f"            {arms}ELSE 0\n"
        "        END"
    )


def age_severity_sql(age_expression: str, *, producer_severity: str) -> str:
    """Raise severity to ``high`` at the top band; never lower anything.

    ⚠ Severity is NOT derived from escalation in general, and it must not be.
    The producers set the two independently — ``bills_due`` emits
    ``severity='high', escalation=0`` for a bill overdue by a week, while
    ``renewal_window`` ties them together — so a general mapping would be a
    rule this codebase does not actually follow, invented in one place and
    contradicted in five (gotcha 41's confident false claim, pointed at a
    column).

    What IS defensible in one direction only: a card nobody has touched in
    three weeks belongs at the top of the desk. ``list_active`` orders
    severity-first, so without this the age ladder would be a number the user
    never sees — an escalation that does not escalate anything, which is the
    invisible-control failure this module exists to fix.
    """
    return (
        f"CASE WHEN ({age_expression}) >= {HIGH_SEVERITY_ESCALATION} "
        f"THEN 'high' ELSE {producer_severity} END"
    )


__all__ = [
    "AGE_ESCALATION_LADDER",
    "HANDLED_COOLDOWN_DAYS",
    "HIGH_SEVERITY_ESCALATION",
    "MAX_SNOOZE_DAYS",
    "age_escalation",
    "age_escalation_sql",
    "age_severity_sql",
]
