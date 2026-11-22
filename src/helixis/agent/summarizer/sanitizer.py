"""Strip Buildium internal IDs from summarizer output.

The hard rule (standing across every user-facing surface): no
Buildium internal numeric IDs in chat / inbox text. Humans are
named by first name (or full name); places by property name + unit
number. The system prompt instructs the LLM to follow this rule,
but defense in depth — we also run the model's output through this
sanitizer before persistence. If the sanitizer detects a leak that
looks like an ID-shaped reference (``Task #17435`` /
``Tenant 9001`` / ``Property ID 31``), it raises
``SummarySanitizationError`` so the worker can flip the row to
``summary_status='failed'`` rather than ship the bad output.

What this sanitizer does NOT reject:
  - Unit numbers (``Unit 204``, ``unit 4B``, ``#120``) — these are
    human-readable address anchors required by the V2 prompt.
  - Bare numbers in narrative (``20 prior tickets``, ``$1,950 rent``,
    ``2026-05-19``) — these are domain values, not identifiers.
  - Property names with street numbers (``160 East End Avenue``) —
    addresses are anchors, not IDs.
  - The word "property" without an explicit ID qualifier (e.g.
    "the property at 31 Main St") — only ``Property ID 31`` /
    ``Property #31`` get rejected.

We use the loud-fail policy (reject + retry) rather than the
silent-fix policy (strip + ship) because once a bad summary lands
on the row, the polling Inbox shows it immediately and the user
has no way to know they're seeing a sanitizer save. A failed row
with an explicit error in ``summary_error`` is observable and
recoverable via the Resummarize affordance.
"""

from __future__ import annotations

import re
from collections.abc import Callable


class SummarySanitizationError(ValueError):
    """Raised when the summarizer's output contains an ID-shaped
    reference that should never appear in user-facing text."""


# Patterns we treat as illegal. Each pattern matches a noun + an
# explicit numeric ID — bare numbers (phone numbers, unit numbers,
# years, prices, etc.) are intentionally NOT flagged because they
# legitimately appear in narrative text.
#
# NOTE: "unit" is intentionally NOT in this list. Unit numbers are
# a required anchor in V2 summaries (e.g. "Unit 204", "unit 4B") and
# are human-readable address components, never internal IDs. Earlier
# the over-broad regex rejected every V2 summary that included a
# unit number, leaving the Inbox detail pane stuck on V1 output.
#
# Threshold is \d{3,} because every real Buildium internal ID is
# 4+ digits (tenants, units, tasks, leases). \d{2,} produced false
# positives on short legitimate numbers in narrative text.
#
# "property" requires an explicit id/#/number qualifier — bare
# "Property 31" is borderline, but "the property at 31 Main St"
# is fine.
#: Nouns that, followed by a long number, are naming an internal record.
#: ⚠ ``unit`` is deliberately absent — "Unit 204" / "unit 4B" are required
#: address anchors, not IDs. ``work order`` and ``bill`` were added
#: 2026-08-25 after live cards shipped "Work order 9023" to the Inbox: the
#: original list was task/tenant/user/lease/owner/vendor only, so the whole
#: maintenance vocabulary walked straight past the validator.
#: ``ticket(s)`` added 2026-08-25: the first fix covered task/work order/bill,
#: and the model simply reached for the synonym -- a live card shipped
#: "two unresolved kitchen faucet/tap leak tickets (39503, 39504)".
_ID_NOUNS = (
    r"tasks?|tickets?|tenants?|users?|leases?|owners?|vendors?|"
    r"work\s?orders?|bills?|applicants?"
)

#: ``id`` / ``#`` / ``number`` between the noun and the digits. Optional —
#: production models write "task 39503" far more often than "Task ID 39503".
#: ⚠ The opening paren matters. "tickets (39503, 39504)" put the id behind a
#: bracket the old class could not cross, so a noun sitting right next to its
#: ids read as clean.
#: ⚠ The opening paren is CAPTURED (group 2), not just consumed by a
#: character class. It used to sit inside ``[:\s(]*`` while the run below
#: ended in an unconditional ``\)?`` — so the closing paren was eaten whether
#: or not an opening one had been matched, and any ``(noun id)`` construction
#: came back unbalanced:
#:
#:     "Paid (bill #11314) today."  ->  "Paid (bill today."
#:
#: An orphaned bracket in owner-facing prose, on BOTH surfaces this sanitizer
#: feeds (the chat reply and the Inbox card — gotcha 38 records that they
#: share this rule, so a defect here ships twice). The identifier was removed
#: correctly; the sentence around it was corrupted, which is the failure this
#: module's own docstring calls worse than a leak.
_QUALIFIER = r"(?:id|#|number|num)?[:\s]*(\()?\s*#?\s*"

#: ⚠ Matches a RUN of ids, not one. "tickets 39503/39504" and
#: "tickets (39503, 39504)" are one reference each; catching only the first
#: number leaves the rest sitting in the sentence.
#:
#: The trailing paren is now CONDITIONAL on group 2 having matched — consume
#: the closer only when this match owns the opener.
#: ⚠ The ``\s*`` lives INSIDE the yes-branch. Hoisting it out — writing
#: ``\s*(?(2)\)|)`` — swallows the space after a run that has no paren at all,
#: so "tickets 39503/39504 are open" came back "ticketsare open". A fix that
#: welds words together is the same corrupted-prose failure as the orphaned
#: bracket it was written to remove.
_ID_RUN = r"\d{3,}(?:\s*[/,]\s*#?\s*\d{3,})*(?(2)\s*\)|)"
_NOUN_ID = re.compile(rf"\b({_ID_NOUNS})\s*{_QUALIFIER}{_ID_RUN}", re.IGNORECASE)

#: ``property`` is special. "the property at 160 East End Ave" is an ADDRESS
#: and must survive, so a bare number after "at" is always allowed. Anything
#: else with 4+ digits is an id — live Buildium property ids on this account
#: are five digits (33375, 37975, 38038), street numbers rarely are.
#: Previously this required an explicit id/#/number qualifier, so the live
#: card text "unit 101 at property 33378" passed clean.
#:
#: ⚠ No "at" lookbehind. The first cut used ``(?<!at\s)`` to protect
#: addresses and it exempted "at property 33378" — the leak itself. The
#: address case needs no special handling: "property at 1600 Penn Ave" cannot
#: match because "at" is not one of the qualifiers, so the digits never sit
#: where the pattern expects them.
#: Two rules, because the digit threshold depends on the qualifier.
#: WITH an explicit "ID"/"number", ANY length is an identifier — "Property
#: ID 31" is never an address. Collapsing these into one 4+-digit rule
#: dropped that case and broke the original PR-92 samples.
_PROPERTY_QUALIFIED_ID = re.compile(
    r"\b(property|properties)\s*(?:id|#|number|num)[:\s]*#?\s*\d+\b",
    re.IGNORECASE,
)
#: WITHOUT one, only a long number is — short numbers are street numbers.
_PROPERTY_BARE_ID = re.compile(
    r"\b(property|properties)\s+#?\s*\d{4,}\b",
    re.IGNORECASE,
)

#: A bare ``ID 1234`` / ``#12345`` with no noun in front.
_BARE_ID = re.compile(r"(?:\bid|#)\s*\d{4,}\b", re.IGNORECASE)

#: Internal FIELD names. Not an id at all — the schema itself, leaking into
#: owner-facing prose: live cards shipped "has no property_id or unit_id",
#: "payments_3mo figure of -900". A property manager has never heard of these
#: columns, and snake_case does not otherwise occur in English narrative, so
#: the shape alone is a safe signal.
_FIELD_NAME = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")

#: The validator's patterns. Every entry MUST have a redaction rule below —
#: pinned by a test, because a pattern the validator rejects and the redactor
#: cannot repair turns the fail-safe into an infinite "still leaking" path.
_ID_PATTERNS: tuple[re.Pattern[str], ...] = (
    _NOUN_ID,
    _PROPERTY_QUALIFIED_ID,
    _PROPERTY_BARE_ID,
    _BARE_ID,
    _FIELD_NAME,
)


def _humanize_field_name(match: re.Match[str]) -> str:
    """``property_id`` → ``property``; ``payments_3mo`` → ``payments 3mo``.

    Keeps the reader's meaning while dropping the schema. A trailing ``_id``
    is pure plumbing, so it goes; other underscores become spaces.
    """
    token = match.group(0)
    if token.endswith("_id"):
        token = token[: -len("_id")]
    return token.replace("_", " ")


def sanitize_summary_text(text: str) -> str:
    """Validate the LLM-produced summary text for ID leaks.

    Returns the original text unchanged when clean. Raises
    ``SummarySanitizationError`` when an ID-shaped reference is
    found, with the matched span in the error message so logs
    can pinpoint the offending phrase.

    Note: this is a validator, not a transformer. We do NOT strip
    the IDs and ship the rest — see the module docstring for the
    loud-fail rationale. The one exception is the summarizer's
    retry-EXHAUST fail-safe, which calls :func:`redact_id_references`
    explicitly so a persistently-leaking model degrades to a clean
    (terser) summary instead of failing the row — the validator here
    is unchanged.
    """
    for pattern in _ID_PATTERNS:
        match = pattern.search(text)
        if match is not None:
            raise SummarySanitizationError(
                f"summary contains a Buildium ID-shaped reference: {match.group(0)!r}"
            )
    return text


# Redaction patterns mirror ``_ID_PATTERNS`` but capture the leading noun so we
# can keep it while dropping the numeric ID. These exist ONLY for the
# retry-exhaust fail-safe below — the validator (``sanitize_summary_text``) is
# deliberately left as a loud-fail detector.
_Replacement = str | Callable[[re.Match[str]], str]

_REDACT_PATTERNS: tuple[tuple[re.Pattern[str], _Replacement], ...] = (
    # Keep the noun, drop the number: "task 39503" -> "task".
    (_NOUN_ID, r"\1"),
    (_PROPERTY_QUALIFIED_ID, r"\1"),
    (_PROPERTY_BARE_ID, r"\1"),
    # No noun to keep.
    (_BARE_ID, ""),
    # Schema -> English.
    (_FIELD_NAME, _humanize_field_name),
)


def redact_id_references(text: str) -> str:
    """Strip Buildium ID-shaped references from ``text``, keeping the noun
    where present (``"Task #4610"`` → ``"task"``; ``"ID 4610"`` → removed).

    This is a TRANSFORMER, used only as the summarizer's retry-exhaust
    fail-safe (see ``Summarizer.summarize``): when the model keeps leaking an
    ID even after a correction nudge, we ship a redacted, ID-free summary
    rather than fail the row. The validator is unchanged — this does not
    weaken it; it is a separate, explicit degrade path. The result is
    re-checkable with :func:`sanitize_summary_text` and should pass.
    """
    out = text
    for pattern, repl in _REDACT_PATTERNS:
        out = pattern.sub(repl, out)
    # Tidy the whitespace / punctuation the removals leave behind.
    out = re.sub(r"\(\s*\)|\[\s*\]", "", out)  # empty () / [] from removed refs
    out = re.sub(r"\s{2,}", " ", out)
    out = re.sub(r"\s+([,.;:)])", r"\1", out)
    out = re.sub(r"([,;:])\s*(?=[,.;:])", "", out)  # collapse doubled punctuation
    return out.strip()
