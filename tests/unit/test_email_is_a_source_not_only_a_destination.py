"""Two prompt blocks contradicted each other, and the model obeyed the stricter one.

WHAT HAPPENED (live cross-source battery, 2026-08-26)
-----------------------------------------------------
Six real questions, six answers, and ``gmail_agent`` was consulted **zero
times** — four of the six answers were thin for exactly that reason. The first
diagnosis was "Gmail must be disconnected". It wasn't; nothing supported that.

The cause was in the prompt, and it was two blocks apart:

    ORCHESTRATOR_INSTRUCTIONS   "Never route a non-email query to Gmail or
                                 Outlook" — with *vendors* named in the list of
                                 subjects that belong to the Buildium Agent.

    REASONING_LOOP_ADDENDUM     "decide which sources the question needs and
                                 call each specialist tool. Call them all
                                 before you conclude."

"Was Premier insured when we sent them to Garden Row 2C?" is a question about a
VENDOR. One block names that subject and forbids the mailbox absolutely; the
other asks for every source the question needs, generically. An absolute
prohibition beats a generic instruction, so the model never opened the mailbox
— which is where the certificate actually was.

The first block was written to stop email becoming a dumping ground for
property questions, and it is right about that. It was over-broad by one
distinction it never drew: **ownership vs evidence.** Buildium owns the
question; it does not hold the email that authorised the work.

⚠ WHY THIS IS A PROSE PIN AND NOT AN AST WALK
----------------------------------------------
Gotcha 40's rule — assert on the tree, not the text — has no purchase here:
the artefact IS text, there is no tree, and the defect was a *contradiction
between two strings*. So the guard reads BOTH sides (gotcha 28) and pins the
PAIR: keeping the blanket ban while dropping the carve-out fails, which is the
regression that actually happened. It cannot police wording, and does not try.

⚠ AND WHY THERE IS NO STRUCTURAL HALF
--------------------------------------
Gotchas 25/32/34/37 all end "only a structural stop works" — but every one of
them is about BOUNDING runaway fan-out, where prose must beat a strong
gradient. This is the opposite direction: the model was *obeying* an explicit
rule, and the fix is amending the rule. There is no code that can force a
consult without re-creating the very fan-out those four gotchas fought, so a
cap would buy the thin answers back at the price of the loops. The verification
is a battery run, not a unit test.
"""

from __future__ import annotations

from helixis.agents.orchestrator import (
    ORCHESTRATOR_INSTRUCTIONS,
    REASONING_LOOP_ADDENDUM,
    create_orchestrator,
)
from helixis.settings import Settings

#: The blanket prohibition. Correct, and load-bearing — it is what keeps
#: "who lives in 4B" out of the mailbox.
_BAN = "Never route a non-email query to Gmail or Outlook"

#: The distinction the ban was missing. Without it the ban reads as a ban on
#: email as EVIDENCE, which is what emptied four answers.
_CARVE_OUT = "Evidence is not ownership"


def test_the_email_ban_still_exists() -> None:
    """Guard the guard. If the ban itself is ever deleted, the carve-out below
    is meaningless and this file would go on passing while email quietly became
    the fallback for everything again — the defect the ban was written for."""
    assert _BAN in ORCHESTRATOR_INSTRUCTIONS, (
        "the email-only prohibition is gone — a property question can now be "
        "answered out of the mailbox"
    )


def test_the_ban_carries_its_evidence_carve_out() -> None:
    """⚠ THE guard. A blanket ban with no evidence exception is what produced
    six answers that never opened the mailbox."""
    assert _CARVE_OUT in ORCHESTRATOR_INSTRUCTIONS, (
        f"{_BAN!r} is present with no evidence carve-out. The orchestrator will "
        f"read it as a ban on CONSULTING email about a vendor/tenant/owner "
        f"question, not just on delegating that question — which is how the "
        f"2026-08-26 battery answered six cross-source questions with Gmail "
        f"consulted zero times."
    )
    # The carve-out has to permit the thing it exists to permit.
    tail = ORCHESTRATOR_INSTRUCTIONS.split(_CARVE_OUT, 1)[1][:900]
    assert "MAY consult" in tail, "the carve-out never actually grants the consult"
    assert "vendor" in tail, (
        "the carve-out must name the subjects the ban names — 'vendors' is the "
        "word that decided the Premier-insurance question"
    )


def test_both_sides_are_live_in_the_same_prompt() -> None:
    """The contradiction only existed because both blocks ship together. Pin
    that they still do, so this file cannot pass by reading a block the running
    orchestrator no longer assembles."""
    # ⚠ One line's worth. The first cut asserted "call each specialist tool",
    # which the source wraps across a newline mid-phrase — so it failed against
    # prose that says exactly that. A prose pin must match how the string is
    # WRITTEN, not how it reads once rendered (gotcha 40's lesson, in the one
    # form that survives having no AST to walk).
    assert "which sources the question needs" in REASONING_LOOP_ADDENDUM, (
        "the cross-source GATHER contract moved or was reworded — re-read it "
        "against the email ban before trusting this file"
    )


def test_the_mailbox_tool_exists_even_with_nothing_connected() -> None:
    """⚠ This is what turns the diagnosis from an inference into a measurement.

    The first explanation offered for "Gmail was consulted zero times" was that
    Gmail must be disconnected. That is unfalsifiable from the repo — until you
    notice that ``gmail_agent`` is built and appended to ``specialists``
    UNCONDITIONALLY, whatever ``get_tools(toolkits=["GMAIL"])`` returned. A
    disconnected mailbox therefore yields a callable ``gmail_agent`` tool that
    answers "connect Gmail", which would have appeared in the battery answers.

    None of them said that, because the tool was never called at all. A missing
    connection cannot produce zero calls; only the model declining to choose it
    can. That is the whole argument, and this test is the half of it that can
    rot — so it is pinned here rather than left in a commit message.

    Runs with no Composio key, which is precisely the empty-toolkit case.
    """
    settings = Settings()
    orchestrator = create_orchestrator(settings, loop_mode=True)
    names = {getattr(t, "name", "") for t in orchestrator.tools}
    assert "gmail_agent" in names, (
        "gmail_agent is no longer registered when Gmail returns no tools — "
        "'the model never called it' and 'the tool did not exist' are now "
        "indistinguishable, and the 2026-08-26 diagnosis rests on telling "
        "them apart"
    )
