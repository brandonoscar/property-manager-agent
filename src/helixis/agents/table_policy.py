"""A table is an answer shape, not a decoration. Refuse it where it isn't one.

Live chat, 2026-08-25. The question was a judgment — *"Marcus says he closed
the ceiling work orders and that our window is 30 days, is he right?"* — and
the answer was correct: no, they're open, and the window is 14 days. Then a
**ten-row table** rendered underneath it.

Nothing in that table answered the question. The reader wanted a verdict and
got a verdict plus a spreadsheet, and every row of it was a row they had to
decide to ignore. That is over-answering, and over-answering is not a
harmless surplus: it buries the sentence that mattered.

WHY A STRUCTURAL STOP AND NOT A BETTER DOCSTRING
``show_table``'s docstring already says to use it "whenever the answer is a
set of items with shared fields". The model read that, had a set of items in
hand, and emitted one. Four rounds of this repo's history say the same thing —
#850 docstrings, #860 result footer, #863 TRIAGE, #866 refusal escalation, and
gotchas 25/32/34/37 all end on the same sentence: *only a structural stop
works.* So the refusal lives in the tool.

WHY IT IS SAFE TO SHIP WITHOUT A TUNING PASS
It copies ``classify_effort``'s shape exactly: return a verdict ONLY for a
question it positively recognises, and ``None`` — meaning "say nothing" —
for everything else. An unbound run behaves byte-for-byte as before. The
suppression fires only when the question is judgment-shaped AND carries no
list-shaped request, so "which units are behind?" keeps its table and "is he
right?" does not.

⚠ **The asymmetry is deliberate.** A wrongly-suppressed table costs a reader
one follow-up question. A wrongly-emitted table costs every reader of every
judgment answer a paragraph of noise, forever. When the two signals conflict —
"is he right about which units are behind?" — the LIST wins and the table is
allowed, because that is the cheaper mistake.
"""

from __future__ import annotations

import re
from contextvars import ContextVar, Token

#: Openers that make the answer a VERDICT: true/false, yes/no, should/shouldn't.
#: A table cannot hold a verdict, so one alongside it is always surplus.
_JUDGMENT = re.compile(
    # ⚠ Anchored to a SENTENCE, not to the string. The first cut used ``^`` and
    # missed the very answer that motivated this module: the real question put
    # the context first and the ask last — "Marcus emailed saying … isn't
    # really a concern. Is he right?" — so the judgment opener sat 140
    # characters in. It passed nine hand-written cases and failed the one from
    # production, which is the failure mode this repo keeps re-learning: a
    # guard is only trustworthy once it has been run against the real input.
    r"""(?:^|(?<=[.!?])|(?<=[.!?]\s))\s*(?:
          (?:is|was|are|were|does|do|did|has|have|had|can|could|will|would|
             should|am)\s+\w+.*\?          # is he right / should we file
        | (?:is|are)\s+(?:that|this|it|he|she|they)\b
        | why\b
        | (?:do|should)\s+(?:i|we|you)\b
        | (?:can|could)\s+(?:i|we|you)\b
        | what\s+(?:do|should)\s+(?:i|we)\b # what do we do
        | (?:handle|confirm|verify|check\s+whether)\b
    )""",
    re.IGNORECASE | re.VERBOSE,
)

#: ⚠ A question ENDING in "why?" is a judgment, wherever it sits.
#: ``_JUDGMENT`` is anchored to sentence boundaries, and the commonest form of
#: this — "Which lease is most urgent, and why?" — puts the ask after a COMMA,
#: so no anchor can reach it. The original alternation was also ``why\s+``,
#: which REQUIRES trailing whitespace and therefore never matched "why?" at
#: all (gotcha 30's family: a whitespace assumption inside a VERBOSE pattern
#: that still compiles and still matches something).
#: Found by the first real validation question, not by the eleven synthetic
#: ones — the same lesson as the sentence-anchor fix it sits beside.
_TRAILING_WHY = re.compile(r"\bwhy\b[^.?!]*\?\s*$", re.IGNORECASE)

#: Signals the reader genuinely wants rows. Any of these outranks the
#: judgment signal — see the asymmetry note in the module docstring.
_WANTS_ROWS = re.compile(
    r"""\b(?:
          list|table|show\s+me|break\s?down|breakdown|spreadsheet|export
        | which\s+(?:ones|units|tenants|properties|leases|work\s?orders|bills)
        | how\s+many|all\s+(?:the\s+)?(?:open|unpaid|expiring|overdue)
        | each\s+(?:unit|tenant|property|lease)
        | compare|side\s+by\s+side|rank(?:ed)?|sort(?:ed)?
    )\b""",
    re.IGNORECASE | re.VERBOSE,
)

#: Refusal text. Says what to do INSTEAD — a bare "no" invites a retry, which
#: costs a model turn and lands in the same place (gotcha 32: a refusal is
#: free to the ledger and expensive on the clock).
REFUSAL = (
    "No table shown: this question asks for a judgment, not a list, so a table "
    "would be surplus next to the answer. State the verdict in prose and name "
    "only the specific items that carry it."
)

_policy: ContextVar[bool | None] = ContextVar("helixis_table_policy", default=None)


def classify_table_need(task: str) -> bool | None:
    """``False`` when a table cannot answer this question. ``None`` = no opinion.

    Never returns ``True``: this decides only whether to REFUSE, and staying
    silent everywhere else is what makes it shippable without a tuning pass.
    """
    if not task:
        return None
    if _WANTS_ROWS.search(task):
        return None  # an explicit request for rows always wins
    if _JUDGMENT.search(task) or _TRAILING_WHY.search(task):
        return False
    return None


def bind_table_policy(task: str) -> Token[bool | None] | None:
    """Bind the per-run verdict, or nothing if the question isn't recognised."""
    verdict = classify_table_need(task)
    return None if verdict is None else _policy.set(verdict)


def tables_are_surplus() -> bool:
    """True only inside a run whose question a table cannot answer."""
    return _policy.get() is False


def reset_table_policy(token: Token[bool | None] | None) -> None:
    if token is not None:
        _policy.reset(token)


__all__ = [
    "REFUSAL",
    "bind_table_policy",
    "classify_table_need",
    "reset_table_policy",
    "tables_are_surplus",
]
