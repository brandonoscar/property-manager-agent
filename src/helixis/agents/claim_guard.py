"""Absence claims, checked against what was actually searched.

This product's worst failure is a confident "there is no X". Battery v2's Q9
told the user a task did not exist "anywhere in the portfolio" at
``Confidence: likely`` — while task 39431, "URGENT: water heater leaking into
the closet", sat in Buildium at priority Low. The cause (gotcha 26) was a
truncated list read as proof of absence: the tool showed 25 of 179 rows and
the model treated the 25 as the world.

**Why this is deterministic and not a second-opinion model call.** The plan
called for an adversarial sub-agent. That would not have caught Q9 — a
verifier re-reading the same truncated tool output sees the same 25 rows and
agrees. What actually proves the claim unsafe is a fact we already have on the
wire: *a list in this run said it was truncated, and the answer claims
absence.* No model needs to weigh in on that, it costs nothing, and it cannot
itself hallucinate.

The same reasoning is why the money half of that plan item is NOT here. Our
money failures were field-shape bugs (``TotalAmount`` absent, the value living
in ``Lines[].Amount`` — gotcha 25), and a second model reading the same wrong
field would confidently confirm the wrong number. That needs live-row
verification, which is where it was fixed; it does not need a judge.

**Advisory, never blocking.** The guard rewrites the ``Confidence:`` and
``Unverified:`` footer lines and leaves the answer body untouched. That is
deliberate: ``synthesis_contract`` carries the #271 lesson — no hard runtime
gate on model prose before a dogfood tuning pass. Downgrading a confidence
word is reversible and legible; suppressing an answer is neither.
"""

from __future__ import annotations

import logging
import re

from helixis.agents.synthesis_contract import parse_synthesis_footer

logger = logging.getLogger(__name__)

#: What a truncating list tool appends. One string, one place — if
#: ``_truncate_footer`` in ``buildium_tools`` ever changes wording, the test
#: below fails rather than this silently never matching again.
TRUNCATION_MARKER = "more not shown"

#: The answer is asserting that something ISN'T there. Deliberately narrow:
#: these are claim shapes, not topic words, so ordinary prose ("no problem",
#: "nothing urgent came up in the notes") mostly doesn't trip them.
_ABSENCE_CLAIM = re.compile(
    r"""(?:
        \bno\s+(?:such\s+)?(?:task|tasks|work\s+order|work\s+orders|record|records|
                             tenant|tenants|lease|leases|bill|bills|vendor|vendors|
                             match|matches|result|results|entry|entries)\b
      | \b(?:doesn'?t|does\s+not|didn'?t|did\s+not)\s+(?:exist|appear|show)\b
      | \b(?:couldn'?t|could\s+not|unable\s+to)\s+find\b
      | \bnothing\s+(?:found|scheduled|open|outstanding|matching)\b
      | \bnone\s+(?:found|exist|matching)\b
      | \bnot\s+(?:present|found|listed)\s+(?:in|anywhere)\b
      | \banywhere\s+in\s+the\s+portfolio\b
    )""",
    re.IGNORECASE | re.VERBOSE,  # every gap is \s+ — VERBOSE eats bare spaces (gotcha 30)
)

#: Confidence words this guard is willing to overwrite. "unverified" is
#: already honest, so an answer that reached it needs nothing from us.
_OVERWRITABLE = ("confident", "likely")

_CAVEAT = (
    "an absence claim rests on a list that was truncated — the scope searched "
    "did not cover the whole portfolio"
)

# ── absence claimed about a source that was never actually read ─────────────
#
# The truncated-list case above is one way to assert absence without evidence.
# On 2026-08-25 the live agent found another: it reported *"Neither company
# Drive nor the company profile has any record of staff spending/authorization
# limits"* on a run that made ZERO tool calls, while SOP-01 sat in the
# connected Drive listing three spend tiers. A second answer in the same
# thread generalised a Buildium miss into "no policies have been uploaded" —
# a claim about Drive, from a turn that never touched it.
#
# Same defect as gotcha 26, one level up: not a partial read treated as the
# world, but NO read treated as a search.

#: How each specialist's source gets named in ordinary prose.
#:
#: ⚠ The bare forms are deliberately case-SENSITIVE, for the reason gotcha 38
#: records: under ``IGNORECASE`` a short common word matches far more than it
#: should. "a short drive to the property, and no one was home" is not a claim
#: about Google Drive, and lowercase is what tells them apart. Qualified forms
#: ("google drive", "company drive") carry their own disambiguation and are
#: matched case-insensitively.
SOURCE_LABELS: dict[str, tuple[str, str]] = {
    # agent name -> (display name, label pattern)
    "drive_agent": (
        "Google Drive",
        r"(?:\bDrive\b|(?i:\b(?:google|company|shared|team)\s+drive\b))",
    ),
    "gmail_agent": (
        "Gmail",
        r"(?i:\bgmail\b|\b(?:your|the|company)\s+(?:inbox|email\s+inbox)\b)",
    ),
    "outlook_agent": ("Outlook", r"(?i:\boutlook\b)"),
    "calendar_agent": ("the calendar", r"(?i:\b(?:google\s+)?calendar\b)"),
}

#: Words that turn a mention of a source into a claim ABOUT its contents.
#: Scanned in a window around the label rather than sentence-parsed — prose is
#: not reliably punctuated and a window is what the postal-context rules in
#: ``reply_hygiene`` already use for the same job.
_NEAR_ABSENCE = re.compile(
    r"\b(?:no|not|n't|never|nothing|none|neither|nor|without|empty|lacks?|absent|unable)\b",
    re.IGNORECASE,
)

#: How far either side of the label to look. "Neither company Drive nor the
#: company profile has any record of…" needs the left edge; "Drive has no
#: record" needs the right.
_ABSENCE_WINDOW = 90

#: A broader absence net, used ONLY when the run gathered nothing at all. The
#: precondition is already decisive there — nothing was checked — so the net
#: can be wide without manufacturing findings on well-evidenced answers.
_ABSENCE_CLAIM_BROAD = re.compile(
    r"""(?:
        \bthere\s+(?:is|are|was|were)\s+no\b
      | \bneither\b[^.]{0,80}\bnor\b
      | \b(?:has|have|holds?|contains?)\s+(?:no|any)\s+(?:record|records|entry|entries)\b
      | \bno\s+(?:documented|recorded|stored|uploaded|existing)\b
      | \bno\s+\w+(?:\s+\w+){0,2}\s+(?:exists?|on\s+file|has\s+been\s+uploaded)\b
      | \b(?:isn'?t|is\s+not|aren'?t|are\s+not)\s+(?:a\s+)?(?:recognized|recognised|known|valid)\b
      | \bnothing\s+(?:on\s+file|to\s+check|uploaded|stored)\b
    )""",
    re.IGNORECASE | re.VERBOSE,  # every gap is \s+ — VERBOSE eats bare spaces (gotcha 30)
)

_SOURCE_CAVEAT = (
    "the answer describes {sources} as holding nothing, but {verb} not "
    "successfully read on this turn — that is not evidence about what {pronoun} "
    "contain{s}"
)

_NO_EVIDENCE_CAVEAT = (
    "this answer asserts that something isn't there, but no source was read on "
    "this turn — nothing here rests on a lookup"
)

#: ⚠ The chat lane had NO runtime fair-housing check at all. The deterministic
#: gate (``security.fair_housing_gate.check_draft``) ran on exactly two Inbox
#: send handlers; the assistant's own answers — which is where a manager reads
#: "yes, you can deny that" — were covered by the prompt block and the 41x
#: golden cases only. Those cases gate at BUILD time. Nothing looked at a live
#: reply.
#:
#: This is ADVISORY and footer-only, on purpose. #271 pulled the runtime
#: prose-gate experiment and gotcha 42 records the rule that replaced it: a
#: guard on prose annotates, never blocks. Replacing an answer with an error
#: is how a manager loses a legitimate turn — and unlike the CRM lane there is
#: no approved draft to revise, so a block would just be a dead end.
#:
#: The operator-facing half carries the real signal: a live reply matching a
#: steering pattern is a fact somebody should see, and ``fair_housing_flagged``
#: puts it in the escalation channel rather than leaving it only in prose the
#: user may not read.
_FAIR_HOUSING_CAVEAT = (
    "part of this answer matches a fair-housing steering pattern ({rules}) — "
    "check it against the Fair Housing Act before acting on it or repeating it "
    "to a resident or applicant"
)


#: ⚠ **This one is unconditional, and that is the difference from every other
#: caveat in this file.** The three above ask what the ANSWER claims — absence
#: over a truncated read, absence about an unread source, absence with no
#: lookup at all — because the claim is what makes them wrong. This one is a
#: fact about how the answer was PRODUCED, so there is no sentence to inspect:
#: the reply reads identically whichever SKU wrote it, which is exactly why
#: silence here is indistinguishable from the primary having answered.
#:
#: Why it is worth a footer at all: gotcha 86 MEASURED the failover SKU
#: answering differently. ``411_fair_housing_safe_area_steering`` red-gates on
#: claude-haiku-4-5, and the runtime scanner had to be widened from 6/10 to
#: 9/10 recall to catch what that model actually says. So "a backup answered"
#: is not a footnote about infrastructure — it is a statement about how much
#: the next sentence is worth.
#:
#: ⚠ **The SKU name is deliberately NOT in the user-facing text.** A property
#: manager can do nothing with ``anthropic/claude-haiku-4-5``, and putting an
#: internal identifier in a reply is how internals leak into prose generally.
#: The operator-facing half carries it: ``model_failover``'s reason-code detail
#: and the wrapper's log line both name the SKU, which is the same split
#: ``fair_housing_flagged`` already makes — the actionable half goes to the
#: person who can act.
_FAILOVER_CAVEAT = (
    "the usual model was unavailable, so a backup answered this — backups are "
    "smaller and have been measured answering differently, so re-check anything "
    "you are about to act on"
)


def fair_housing_violations(answer: str) -> list[str]:
    """Steering rules the ANSWER trips, or ``[]``.

    Runs the same tuned, negation-guarded check the send handlers use, so the
    two cannot drift into disagreeing about what steering looks like
    (gotcha 25's rule pointed at a safety check). Never raises — a guard on
    prose must not be able to cost the turn.
    """
    try:
        from helixis.security.fair_housing_gate import check_draft

        return check_draft("", answer or "")
    except (Exception, SystemExit):  # noqa: BLE001 — advisory, never load-bearing
        logger.debug("fair-housing scan failed for one answer", exc_info=True)
        return []


def apply_fair_housing_caveat(answer: str, rules: list[str]) -> str:
    """Annotate an answer that matched a steering pattern. Body untouched."""
    if not rules:
        return answer
    return _apply_caveat(answer, _FAIR_HOUSING_CAVEAT.format(rules=", ".join(sorted(rules))))


def saw_truncated_read(tool_output: str) -> bool:
    """Did this tool output admit it was showing a partial list?"""
    return TRUNCATION_MARKER in (tool_output or "")


def claims_absence(answer: str) -> bool:
    """Is the answer asserting something ISN'T there?"""
    return bool(answer) and bool(_ABSENCE_CLAIM.search(answer))


def needs_scope_caveat(answer: str, *, truncated_reads: bool) -> bool:
    """The one condition worth acting on: absence asserted over a partial read.

    Both halves are required. An absence claim backed by complete reads is
    fine — that is the answer working. A truncated read on a question that
    claims nothing is also fine. It is the pair that is a defect.
    """
    return truncated_reads and claims_absence(answer)


def unsupported_source_claims(answer: str, evidenced: set[str] | None) -> list[str]:
    """Sources the answer reports as empty that this run never actually read.

    Both halves are required, exactly as in ``needs_scope_caveat``. Naming a
    source is fine ("you could check Drive"). Saying a source holds nothing is
    fine when the source was read. It is the pair — an absence claim about a
    source with no successful consult behind it — that is a defect.

    ``evidenced`` is the set of specialist names that returned something
    usable this run. ``None`` or an empty set means nothing was evidenced, so
    every absence claim in the answer is unsupported.
    """
    if not answer:
        return []
    have = evidenced or set()
    found: list[str] = []
    for agent, (display, pattern) in SOURCE_LABELS.items():
        if agent in have:
            continue
        for match in re.finditer(pattern, answer):
            left = max(0, match.start() - _ABSENCE_WINDOW)
            window = answer[left : match.end() + _ABSENCE_WINDOW]
            if _NEAR_ABSENCE.search(window):
                found.append(display)
                break
    return found


def claims_absence_broadly(answer: str) -> bool:
    """The wide net, safe only behind a "nothing was read at all" precondition."""
    return bool(answer) and (claims_absence(answer) or bool(_ABSENCE_CLAIM_BROAD.search(answer)))


def needs_evidence_caveat(answer: str, *, gathered_any: bool) -> bool:
    """An absence asserted by a run that read nothing whatsoever.

    This is the shape that produced the worst live answer of the 2026-08-25
    probe: nine seconds, zero tool calls, and a confident account of what two
    separate sources did not contain.
    """
    return not gathered_any and claims_absence_broadly(answer)


def _apply_caveat(answer: str, caveat: str) -> str:
    """Attach one caveat to the footer, leaving the answer body untouched.

    Shared by all three guards so they cannot drift into treating the footer
    differently. Returns the answer unchanged when there is nothing to
    downgrade, so every caller can invoke it unconditionally.
    """
    if not answer:
        return answer
    footer = parse_synthesis_footer(answer)
    out = answer

    if footer.confidence in _OVERWRITABLE:
        # Rewrite only the LAST Confidence line — the same rule the parser
        # uses, so a quoted example earlier in the reply can't be clobbered.
        matches = list(re.finditer(r"^\s*Confidence:.*$", out, re.MULTILINE))
        if matches:
            last = matches[-1]
            out = f"{out[: last.start()]}Confidence: unverified — {caveat}{out[last.end() :]}"

    unverified = list(re.finditer(r"^\s*Unverified:\s*(?P<body>.*)$", out, re.MULTILINE))
    if unverified:
        last = unverified[-1]
        body = last.group("body").strip()
        merged = caveat if body.lower() in {"", "none", "none."} else f"{body}; {caveat}"
        out = f"{out[: last.start()]}Unverified: {merged}{out[last.end() :]}"
    elif footer.confidence is None:
        # No footer at all (the common non-cross-source case). Say it plainly
        # rather than silently letting the absence claim stand unqualified.
        out = f"{out.rstrip()}\n\n_Note: {caveat}._"

    return out


def apply_scope_caveat(answer: str) -> str:
    """Downgrade the footer's confidence and record what wasn't searched.

    Body text is never touched. Returns the answer unchanged if there is
    nothing to downgrade, so this is safe to call unconditionally.
    """
    return _apply_caveat(answer, _CAVEAT)


def apply_source_caveat(answer: str, sources: list[str]) -> str:
    """Say which named sources the answer speaks for without having read them."""
    if not sources:
        return answer
    plural = len(sources) > 1
    return _apply_caveat(
        answer,
        _SOURCE_CAVEAT.format(
            sources=" and ".join(sources),
            verb="they were" if plural else "it was",
            pronoun="they" if plural else "it",
            s="" if plural else "s",
        ),
    )


def apply_no_evidence_caveat(answer: str) -> str:
    """Say plainly that an absence claim rests on no lookup at all."""
    return _apply_caveat(answer, _NO_EVIDENCE_CAVEAT)


def apply_failover_caveat(answer: str) -> str:
    """Say that a backup model produced some of this answer.

    Takes no argument on purpose: the caller has already decided a failover
    happened, and the SKU belongs in the reason code rather than in the reply
    (see :data:`_FAILOVER_CAVEAT`). A signature that accepted the model name
    would invite putting it on the screen.
    """
    return _apply_caveat(answer, _FAILOVER_CAVEAT)


__all__ = [
    "SOURCE_LABELS",
    "TRUNCATION_MARKER",
    "apply_fair_housing_caveat",
    "apply_failover_caveat",
    "apply_no_evidence_caveat",
    "apply_scope_caveat",
    "fair_housing_violations",
    "apply_source_caveat",
    "claims_absence",
    "claims_absence_broadly",
    "needs_evidence_caveat",
    "needs_scope_caveat",
    "saw_truncated_read",
    "unsupported_source_claims",
]
