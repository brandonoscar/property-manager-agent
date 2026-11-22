"""What counts as an ANSWER when the clock runs out.

A wall-clock cutoff catches a run wherever it happens to be. Sometimes that is
mid-synthesis and worth handing over; sometimes it is "Now let me check open
maintenance at each of these units" — which reads like an answer to a scorer
and like a broken product to a user.

Battery v3's Q5 did exactly that: 190 characters of narration shipped as
``done``, scored ``completed=True``, counted as an answered question. A
measurement that inflates itself is worse than a missing one, so the salvage
path now has a floor and the event carries a ``truncated`` flag.
"""

from __future__ import annotations

from helixis.agents.orchestrator import _SALVAGE_MIN_CHARS, _is_deliverable
from helixis.common.events import EventType, done_event

_Q5 = (
    "I've identified all 20 delinquent tenants across the portfolio. "
    "Now let me check open maintenance at each of these units before "
    "recommending late notices."
)

_REAL_ANSWER = (
    "Here is the collections plan. Three tiers:\n\n"
    "**Hold — habitability first.** Marcus Whitfield (Montgomery) is two "
    "months down, but there's an active water-heater leak at his unit filed "
    "at Low priority. Fix the leak before any dunning notice goes out; "
    "chasing rent from a resident we've left with a broken unit is both bad "
    "practice and legal exposure.\n\n"
    "**No action — already resolving.** Elena Vasquez's memo says the balance "
    "is coming Friday, so she is not a collections target this cycle.\n\n"
    "**Standard notice.** The remaining eighteen accounts get the usual "
    "late-notice sequence, largest balance first."
)


def test_the_q5_fragment_is_not_an_answer() -> None:
    """The exact text that shipped as a completed answer."""
    assert not _is_deliverable(_Q5)


def test_real_synthesis_still_gets_delivered() -> None:
    """The salvage path exists for a reason — a run that died mid-answer
    should still hand over what it had. The floor must not eat that."""
    assert _is_deliverable(_REAL_ANSWER)


def test_a_long_ramble_that_ends_mid_plan_is_still_narration() -> None:
    """Length alone can't be the test: a wordy run that ends by announcing its
    next step has still not answered anything."""
    rambling = (
        "The portfolio has considerable activity this month. " * 12
    ) + " Let me check each unit."
    assert len(rambling) > _SALVAGE_MIN_CHARS
    assert not _is_deliverable(rambling)


def test_a_table_counts_as_delivered() -> None:
    """Answers legitimately end on a table row or list item rather than a full
    stop — the frequent shape for this product."""
    table = "| Tenant | Balance | Days |\n" + "".join(
        f"| Tenant {i} | $1,200 | 61 |\n" for i in range(20)
    )
    assert _is_deliverable(table.strip())


def test_a_short_but_complete_reply_is_not_salvage_material() -> None:
    """Deliberately conservative in one direction: a false negative costs a
    partial the user can re-ask for; a false positive ships narration as fact.
    A genuinely terse answer loses the salvage, which is the safe side."""
    assert not _is_deliverable("You have 50 properties.")


def test_the_done_event_marks_a_salvage_as_truncated() -> None:
    """Without the flag a salvage is indistinguishable from a real answer on
    the wire, which is how every scorer downstream counted one."""
    plain = done_event("all finished")
    salvaged = done_event("partial work", truncated=True)
    assert plain.type is EventType.DONE
    assert "truncated" not in plain.data
    assert salvaged.data["truncated"] is True


def test_the_deadline_has_its_own_reason_code() -> None:
    """A deadline is a run that worked too long; a stall is one that stopped
    talking. Filing both as stream_stall made #854's dashboard read as a stall
    epidemic — especially after #857 stopped the watchdog killing healthy runs."""
    from helixis.observability.reason_codes import REASON_CODES

    assert "wall_clock_deadline" in REASON_CODES
    assert "stream_stall" in REASON_CODES
