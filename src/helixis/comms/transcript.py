"""A voicemail transcript is a machine's guess, and it was presented as quotes.

`CrmPage.tsx` rendered the transcription as `“{m.transcription}”`. Quotation
marks are a claim that these are the caller's words; automatic speech
recognition does not make that claim and routinely gets exactly the tokens a
property manager acts on wrong — a rent amount, a unit number, a date.

**There is no confidence signal to lean on**, and that is worth writing down so
nobody goes looking for one. Twilio's ``transcribeCallback`` sends
``TranscriptionStatus`` (``completed`` / ``failed``) and ``TranscriptionText``;
there is no per-word or per-utterance confidence in the payload, and the route
already drops anything that is not ``completed``. So the honest control is not
"gate on low confidence" — it is to stop presenting a guess as a quotation, and
to point at the figures a reader is most likely to act on.

⚠ **This is a DISCLOSURE, not an instruction to behave well.** Gotchas
25/32/34/37 all record that prose cannot bound a loop; that rule is about
steering behaviour. Attaching a fact the reader needs is the opposite move and
this repo's own precedent works: gotcha 26's truncation footer, gotcha 51's
uppercase ``FORMER``, gotcha 57's "the mirror holds no paid bills". Each states
something true about the data rather than asking the model to be careful.

⚠ **Deliberately NOT a redaction or a refusal.** The transcript is the most
useful thing on a voicemail card and removing figures from it would be worse
than a wrong one — the recording is right there to check against. What changes
is that a reader is told which numbers to check, and that the text is a
transcription rather than a quotation.
"""

from __future__ import annotations

import re

#: Figures a property manager acts on directly, in the order they matter.
#:
#: ⚠ Money FIRST and matched with its symbol. A bare number is not what makes
#: this dangerous — "$1,500" versus "$15,000" is, and so is "unit 4B" versus
#: "unit 48". A scan that flagged every digit would fire on every voicemail
#: carrying a phone number or a date, which is nearly all of them, and a signal
#: that fires everywhere is one people stop reading (gotcha 52).
_FIGURE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("an amount", re.compile(r"\$\s?\d[\d,]*(?:\.\d{1,2})?")),
    # "unit 4B", "apt 12", "apartment 3-C", "# 208". The label is required —
    # a bare "4B" in running speech is not reliably a unit.
    (
        "a unit number",
        re.compile(r"\b(?:unit|apt\.?|apartment|suite|ste\.?)\s*#?\s*[0-9]+[a-z]?\b", re.I),
    ),
)

#: What the reader is told. One sentence, and it names the recording because
#: that is the thing that settles it — a caveat with no way to check is just
#: doubt (gotcha 46's "temporarily unavailable" was a true-sounding sentence
#: that told somebody to do something that could not work).
CAVEAT = (
    "This text is an automatic transcription, not the caller's exact words. "
    "It contains {what} — check against the recording before acting on the figures."
)

#: Shown wherever the transcript is rendered, figures or not. Quotation marks
#: around ASR output are the actual defect; this is the label that replaces
#: them.
TRANSCRIPT_LABEL = "Automatic transcription"


def figures_in(text: str | None) -> tuple[str, ...]:
    """Which kinds of act-on-it figure this transcript contains.

    Returns the LABELS ("an amount", "a unit number"), not the matched values:
    the reader has the transcript in front of them, and echoing the matched
    text into a caveat would repeat a possibly-wrong number in a second place
    where it looks more official.
    """
    body = text or ""
    return tuple(label for label, pattern in _FIGURE_PATTERNS if pattern.search(body))


def transcript_caveat(text: str | None) -> str | None:
    """The line to show, or ``None`` when the transcript carries no figures.

    ⚠ ``None`` is not "this transcript is accurate" — every transcript is a
    guess, which is what ``TRANSCRIPT_LABEL`` says wherever one is rendered.
    This caveat is the narrower claim: there is something numeric in here that
    a reader is likely to act on and that ASR is likely to get wrong.
    """
    found = figures_in(text)
    if not found:
        return None
    if len(found) == 1:
        what = found[0]
    else:
        what = f"{', '.join(found[:-1])} and {found[-1]}"
    return CAVEAT.format(what=what)


__all__ = ["CAVEAT", "TRANSCRIPT_LABEL", "figures_in", "transcript_caveat"]
