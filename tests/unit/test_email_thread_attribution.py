"""A reply chain put several people's words under one person's name.

`build_email_enrichment` maps the FULL body verbatim onto `task.description`
and the LAST sender onto `requested_by_first_name` / `requested_by_last_name`.
On a thread those two disagree: the description carries several people's
messages and the attribution names exactly one of them. A planner reading that
card can answer a question the sender never asked, or quote a figure the person
below them wrote and credit it upstairs (coverage checklist #93).

That is gotchas 41/51/104's cross-boundary attribution — a name from one place
joined to words from another, stated confidently — arriving in an inbox card
instead of a tool result.

⚠ **It states a fact; it does not parse the thread.** Splitting quoted mail
correctly is famously lossy, and a wrong split would hide part of the sender's
own message — worse than the ambiguity it set out to fix. So the floor line
says the body is a chain and only the top of it belongs to the sender named,
which is gotcha 26's truncation footer and gotcha 51's uppercase FORMER: name
the limit rather than guess past it.

⚠ **The false-positive direction is what decides whether it survives.** A bare
``>`` turns up in ordinary prose ("rent > $2,000", a pasted table), and a
caveat on a large share of ordinary email is one people stop reading (gotcha
52). Every case below has its negative twin.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from helixis.webhooks.email_triage import (
    QUOTED_THREAD_SIGNAL,
    InboundEmail,
    build_email_action_surface,
    build_email_enrichment,
    carries_quoted_thread,
)

_TRIAGE = Path("src/helixis/webhooks/email_triage.py")


def _email(body: str) -> InboundEmail:
    return InboundEmail(
        message_id="m1",
        from_email="Dana Reyes <dana@example.com>",
        subject="Re: plumber quote",
        body_text=body,
        received_at="2026-09-09T12:00:00Z",
        from_name="Dana Reyes",
        thread_id="t1",
    )


_REPLY_CHAIN = """Thanks — please go ahead and approve it.

On Mon, 8 Sep 2026 at 14:02, Marcus Whitfield <marcus@example.com> wrote:
> The plumber quoted $4,000 and wants approval before starting.
> Let me know either way.
"""


# ── what counts as a thread ──────────────────────────────────────────


@pytest.mark.parametrize(
    "body",
    [
        _REPLY_CHAIN,
        "Approved.\n\n-----Original Message-----\nFrom: Marcus\nHere is the quote.",
        "See below.\n\n--- Forwarded message ---\nFrom: the vendor\n",
        "FYI.\n\nBegin forwarded message:\nFrom: leasing@example.com",
        # The Outlook block header — all three keys, which is the point.
        "Sure.\n\nFrom: Marcus Whitfield\nSent: Monday\nTo: Dana Reyes\nSubject: quote",
        # A chain with no header at all, but plainly quoted.
        "Yes.\n> the plumber quoted $4,000\n> please confirm\n",
    ],
)
def test_a_reply_chain_is_recognised(body: str) -> None:
    assert carries_quoted_thread(body) is True


@pytest.mark.parametrize(
    "body",
    [
        # ⚠ Every one of these must stay UNCAVEATED, or the signal appears on
        # ordinary mail and stops being read.
        "Hi — the heater in 4B stopped working last night. Can someone come out?",
        "Our rent > $2,000 now, is that right?",  # a bare > is not a quote
        "Numbers below:\n> 12 units",  # ONE quoted line is not a chain
        "I wrote to the vendor on Monday and never heard back.",  # 'wrote' alone
        "From: the front desk — they said the key is ready.",  # From: alone
        "",
        "   ",
    ],
)
def test_ordinary_mail_is_left_alone(body: str) -> None:
    assert carries_quoted_thread(body) is False


def test_none_is_not_a_thread() -> None:
    assert carries_quoted_thread(None) is False


# ── what the card says ───────────────────────────────────────────────


def test_the_thread_line_reaches_the_noticed_section() -> None:
    surface = build_email_action_surface(_email(_REPLY_CHAIN))
    assert QUOTED_THREAD_SIGNAL in surface["signals"]


def test_the_thread_line_leads() -> None:
    """⚠ Ahead of the LLM's signals on purpose. It is a fact about WHO wrote
    the words below, and the model that would otherwise have to notice it is
    the same one liable to mis-attribute them."""
    surface = build_email_action_surface(_email(_REPLY_CHAIN), signals=["Rent is overdue"])
    assert surface["signals"][0] == QUOTED_THREAD_SIGNAL
    assert "Rent is overdue" in surface["signals"]


def test_an_ordinary_email_gets_no_extra_line() -> None:
    surface = build_email_action_surface(_email("The heater in 4B stopped working last night."))
    assert surface["signals"] == []


def test_the_enrichment_signals_are_untouched_for_ordinary_mail() -> None:
    """The change is additive or it is a regression."""
    surface = build_email_action_surface(_email("Just checking in."), signals=["a", "b"])
    assert surface["signals"] == ["a", "b"]


def test_the_line_is_not_printed_twice() -> None:
    """An enrichment that already said it must not double it up."""
    surface = build_email_action_surface(
        _email(_REPLY_CHAIN), signals=[QUOTED_THREAD_SIGNAL, "Rent is overdue"]
    )
    assert surface["signals"].count(QUOTED_THREAD_SIGNAL) == 1


def test_the_line_names_the_actual_risk() -> None:
    """A caveat that does not say what to do with it is just doubt (gotcha
    46). This one names the boundary: only the top belongs to the sender."""
    assert "Only the top belongs to the sender" in QUOTED_THREAD_SIGNAL
    assert "check who wrote a quote" in QUOTED_THREAD_SIGNAL


# ── the planner sees it too ──────────────────────────────────────────


def test_the_enrichment_flags_the_mismatch_it_creates() -> None:
    """`description` is the whole chain and `requested_by_*` is one sender.
    The flag sits beside them so the planner can see the disagreement rather
    than having to spot it in prose."""
    enriched = build_email_enrichment(_email(_REPLY_CHAIN), "co-1")
    task = enriched["task"]
    assert task["raw"]["has_quoted_thread"] is True
    # The two fields that disagree are both still there — this is a
    # disclosure, not a redaction.
    assert "Marcus Whitfield" in task["description"]
    assert task["requested_by_first_name"] == "Dana"


def test_an_ordinary_email_is_flagged_false_not_absent() -> None:
    """⚠ Absent and False are different to a reader. A missing key reads as
    "nobody checked"; False says "checked, it is not a thread" (gotchas
    55/57/70's None-is-not-zero, pointed at a boolean)."""
    enriched = build_email_enrichment(_email("The heater in 4B stopped working."), "co-1")
    assert enriched["task"]["raw"]["has_quoted_thread"] is False


# ── it is wired where it has to be ───────────────────────────────────


def _fn(name: str) -> ast.FunctionDef:
    tree = ast.parse(_TRIAGE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {_TRIAGE}")


def test_the_surface_builds_its_signals_through_the_floor() -> None:
    """⚠ A behavioural test cannot see a surface that goes back to passing the
    caller's signals straight through — it would simply print whatever the
    enrichment said, which is the defect (gotchas 62/66/69)."""
    # ⚠ RE-POINTED, never deleted (gotchas 89/93/95): the helper was renamed
    # ``_signals_with_thread_floor`` -> ``_signals_with_floor`` when the
    # accommodation floor joined it. The property is unchanged — the surface
    # must not pass the caller's signals straight through.
    src = ast.unparse(_fn("build_email_action_surface"))
    assert "_signals_with_floor" in src, (
        "build_email_action_surface no longer routes its signals through the "
        "deterministic floor, so a reply chain renders with no note that the "
        "body carries somebody else's words"
    )


def test_the_enrichment_still_records_the_flag() -> None:
    src = ast.unparse(_fn("build_email_enrichment"))
    assert "has_quoted_thread" in src and "carries_quoted_thread" in src, (
        "the enrichment stopped flagging a quoted thread, so the planner sees "
        "a whole chain attributed to one sender with nothing marking it"
    )


def test_the_quote_run_threshold_is_more_than_one_line() -> None:
    """⚠ Pinned because ONE quoted line is the difference between a signal
    that fires on reply chains and one that fires on any pasted table — the
    cry-wolf direction that gets it switched off (gotcha 52)."""
    from helixis.webhooks.email_triage import _MIN_QUOTED_RUN

    assert _MIN_QUOTED_RUN >= 2
