"""A voicemail transcript was rendered in quotation marks.

`CrmPage.tsx` showed `“{m.transcription}”`. Quotes are a claim that these are
the caller's words; automatic speech recognition does not make that claim and
routinely gets exactly the tokens a property manager acts on wrong — a rent
amount, a unit number.

⚠ **There is no confidence value to gate on**, recorded so nobody goes looking
for one. Twilio's `transcribeCallback` sends `TranscriptionStatus`
(completed / failed) and the text, nothing per-word, and the route already
drops anything that is not completed. So the controls are: stop presenting a
guess as a quotation, and name the figures worth checking against the
recording.

⚠ **A disclosure, not an instruction to behave well.** Gotchas 25/32/34/37
record that prose cannot bound a loop — that rule is about steering behaviour.
Attaching a fact the reader needs is the opposite move, and this repo's own
precedents work: gotcha 26's truncation footer, gotcha 51's uppercase FORMER,
gotcha 57's "the mirror holds no paid bills".

⚠ **The false-positive direction is the one that kills it.** A scan over bare
digits fires on every voicemail carrying a phone number or a date — nearly all
of them — and a caveat that appears everywhere is one people stop reading
(gotcha 52). Every test below has its negative twin.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from helixis.comms.transcript import (
    TRANSCRIPT_LABEL,
    figures_in,
    transcript_caveat,
)

_TS = Path("frontend/src/lib/transcript.ts")


# ── what counts as an act-on-it figure ───────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "Hi, it's about the rent, I can pay $1,500 on Friday.",
        "The invoice came to $15,000 and I want to dispute it.",
        "I owe $850.00 right?",
        "put it on the account, $ 250 please",
    ],
)
def test_a_dollar_amount_is_flagged(text: str) -> None:
    assert "an amount" in figures_in(text)


@pytest.mark.parametrize(
    "text",
    [
        "This is unit 4B calling about the heat.",
        "I'm in apartment 12, the lock is broken.",
        "apt 3c has no water",
        "Suite 208 needs someone today.",
    ],
)
def test_a_labelled_unit_number_is_flagged(text: str) -> None:
    assert "a unit number" in figures_in(text)


@pytest.mark.parametrize(
    "text",
    [
        # ⚠ Every one of these is a voicemail that must stay UNCAVEATED.
        "Hi, this is Dana, please call me back when you get a chance.",
        "Call me on 555 123 4567 tomorrow.",  # a phone number is not an amount
        "I'll be there on the 15th of next month.",  # a date is not a unit
        "It's been broken for 3 days now.",  # a bare count
        "",
        "   ",
    ],
)
def test_an_ordinary_transcript_gets_no_caveat(text: str) -> None:
    """The direction that decides whether the caveat survives contact with
    real voicemails. A signal on every card is a signal nobody reads."""
    assert figures_in(text) == ()
    assert transcript_caveat(text) is None


def test_a_bare_number_is_not_an_amount() -> None:
    """⚠ Money is matched WITH its symbol on purpose. "1500" in running speech
    is as likely to be a door code, a year or a square footage, and flagging it
    would put the caveat on most transcripts."""
    assert figures_in("the meter reads 1500") == ()


def test_a_bare_alphanumeric_is_not_a_unit() -> None:
    """Same rule on the other pattern: "4B" needs its label. Without one this
    fires on any letter-after-digit token."""
    assert figures_in("my policy number is 4B7") == ()


# ── the sentence ─────────────────────────────────────────────────────


def test_the_caveat_names_what_to_check_and_where() -> None:
    line = transcript_caveat("unit 4B owes $1,500")
    assert line is not None
    assert "an amount" in line and "a unit number" in line
    assert "recording" in line, (
        "a caveat with no way to settle it is just doubt — gotcha 46's "
        "'temporarily unavailable', which told somebody to retry something "
        "that could not work"
    )


def test_the_caveat_does_not_echo_the_number() -> None:
    """⚠ Labels, never the matched values. Repeating a possibly-wrong figure
    into the caveat prints it a second time, somewhere it looks MORE official
    than the transcript it came from."""
    line = transcript_caveat("I can pay $15,000 on Friday")
    assert line is not None
    assert "15,000" not in line


def test_one_figure_reads_naturally() -> None:
    line = transcript_caveat("I can pay $500")
    assert line is not None
    assert "It contains an amount —" in line


# ── one rule, two surfaces ───────────────────────────────────────────


def _ts_string(name: str) -> str:
    """A single-quoted or template-literal constant out of the TS module."""
    src = _TS.read_text(encoding="utf-8")
    match = re.search(rf"{name} = '([^']*)';", src)
    assert match, f"{name} not found as a single-quoted constant in {_TS}"
    return match.group(1)


def test_the_label_is_identical_on_both_surfaces() -> None:
    """The Inbox comms card and the CRM voicemail row show the same words. Two
    copies of a sentence drift and the drift is invisible (gotcha 73's
    "approved" in three modules) — the difference here would be one surface
    calling a transcript a quotation and the other not."""
    assert _ts_string("TRANSCRIPT_LABEL") == TRANSCRIPT_LABEL


def test_the_caveat_sentence_is_identical_on_both_surfaces() -> None:
    src = _TS.read_text(encoding="utf-8")
    python_line = transcript_caveat("unit 4B owes $1,500")
    assert python_line is not None
    head, _, tail = python_line.partition("It contains ")
    # The TS version builds the same sentence from the same two halves.
    assert head.strip() in src.replace("\\'", "'"), (
        "the opening sentence differs between the Python and TypeScript "
        "copies — one surface is telling the reader something the other is not"
    )
    assert tail.split(" — ")[1] in src, "the 'check against the recording' half differs"


def test_the_figure_patterns_agree_on_the_hard_cases() -> None:
    """⚠ Pinning the PATTERNS as text would only prove they were typed the
    same. What matters is that both answer alike on the inputs the rule turns
    on, so this asserts the TS source carries the same two anchors — a symbol
    for money, a label for a unit — rather than a bare-digit scan.
    """
    src = _TS.read_text(encoding="utf-8")
    assert "\\$" in src, "the TS money pattern lost its currency symbol anchor"
    assert "unit|apt" in src, "the TS unit pattern lost its label anchor"
    assert "an amount" in src and "a unit number" in src


# ── the surface that had the defect ──────────────────────────────────

_CRM_PAGE = Path("frontend/src/components/CrmPage.tsx")


def _strip_jsx_comments(src: str) -> str:
    """Remove `{/* … */}` and `/* … */`, so the comment explaining this guard
    is not read as the code that trips it.

    ⚠ Gotcha 40/59's trap, and it is LIVE here: the comment above the render
    quotes the broken form (`“{m.transcription}”`) in order to explain it, so a
    raw scan fires on the sentence documenting the fix. Stripping without
    proving the stripper can still say no is how a guard goes quietly blind,
    which is why the guard-the-guard below feeds it both.
    """
    out = re.sub(r"\{/\*.*?\*/\}", "", src, flags=re.DOTALL)
    return re.sub(r"/\*.*?\*/", "", out, flags=re.DOTALL)


def test_the_stripper_can_still_say_no() -> None:
    quoted = "\u201c{m.transcription}\u201d"
    sample = f'const a = "{quoted}";\n{{/* mentions {quoted} */}}\n'
    stripped = _strip_jsx_comments(sample)
    assert "const a" in stripped, "the stripper is eating real code"
    assert stripped.count("m.transcription") == 1, "the stripper left the comment behind"


def test_the_voicemail_transcript_is_not_rendered_as_a_quotation() -> None:
    """Quotation marks are a claim that these are the caller's words. They are
    not — they are a machine's guess at them, and the tokens it gets wrong are
    the ones a manager acts on."""
    src = _strip_jsx_comments(_CRM_PAGE.read_text(encoding="utf-8"))
    assert "\u201c{m.transcription}\u201d" not in src, (
        "the voicemail transcript is back inside quotation marks, which "
        "presents automatic speech recognition as verbatim"
    )
    assert "{m.transcription}" in src, "the transcript itself is no longer rendered"


def test_the_crm_page_labels_and_caveats_the_transcript() -> None:
    """A behavioural test of the helper cannot see a surface that stopped
    calling it (gotcha 62), and this is the surface the defect was on."""
    src = _strip_jsx_comments(_CRM_PAGE.read_text(encoding="utf-8"))
    assert "TRANSCRIPT_LABEL" in src, "the transcript is rendered with no label saying it is one"
    # ⚠ TWICE: once to decide whether to render the block, once to render it.
    # Found by MUTATION — replacing the condition with `{false && (` left one
    # call in the dead block, so a presence check passed while the caveat could
    # never appear. A count is blunt but it fails LOUDLY on a refactor that
    # hoists the call into a variable, which is the right way round.
    assert src.count("transcriptCaveat(") >= 2, (
        "the figure caveat is no longer both guarded and rendered by a call — "
        "a garbled rent amount reads as the caller's own words with nothing "
        "pointing at the recording. If you hoisted it into a variable, update "
        "this assertion deliberately rather than lowering the count."
    )
    assert 'data-testid="voicemail-figure-caveat"' in src
