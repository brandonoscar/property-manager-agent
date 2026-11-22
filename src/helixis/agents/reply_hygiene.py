"""Keep Buildium record numbers out of the STREAMED chat reply.

PR #892 and #893 removed identifier leaks from Inbox cards — the summarizer
and the action surface. Both were fixed; the chat reply was not, because
nobody checked it. Pulled from a live chat answer 2026-08-25:

    the 8 I could pull individually all show Status = "New"
    (IDs 11314, 11320, 11325, 11330, 11337, 11342, 11350, 11356)
    the remaining 2 (11362, 11369) still appear on the current list

``agent.summarizer.sanitizer`` catches **none** of those. It is tuned for the
phrasing a summarizer produces (``task 39503``, ``Property ID 4610``), not for
a plural ``IDs`` heading a list, or a bare parenthesised run. So this module
adds the shapes the chat surface actually emits and reuses the summarizer rule
for the ones it already handles. Two hand-maintained tables would drift, so the
noun/qualifier rule stays imported rather than copied.

⚠ **THE REPLY STREAMS, so scrubbing the final text is too late.**
``run_agent_streaming`` publishes a ``token_event`` per delta
(``orchestrator.py``, the ``response.output_text.delta`` branch). By the time
a final-text scrub runs, the reader has already watched ``11314`` arrive. And
a per-delta scrub is worse than none: an identifier splits across deltas
(``"work order 11"`` + ``"314"``), so a naive per-chunk regex sees neither
half and passes both through, or mangles one.

:class:`StreamScrubber` buffers deltas and emits only COMPLETE LINES. A line
is safe by construction — an identifier never contains a newline, so a whole
line can never hold half a pattern. Two cleverer designs were tried first and
both shipped corruption; see the class docstring for what they were and how
they failed.

⚠ **What this deliberately does NOT do: rewrite tech vocabulary.**
``Status = "New"``, ``CreatedDate``, ``via the API`` are equally wrong in a
reply to a property manager, but they are a WORDING problem, not a pattern
problem. ``Status = "New"`` → "still open" is a semantic rewrite, and a regex
doing semantic rewrites on a token stream produces mangled sentences — a worse
failure than the one it fixes. That half belongs in the orchestrator prompt
with a golden case to catch regressions. A structural stop for what is
structural; prose for what is prose.

⚠ **Precision over recall.** A property manager's reply is full of numbers
that MUST survive: ``$4,500``, ``1,650``, ``2026-08-31``, ``10 work orders``,
``(555) 010-2288``. Scrubbing one of those is a worse bug than leaking an id,
because it silently corrupts a figure the reader will act on. Every pattern
here is anchored on identifier CONTEXT — an explicit ``ID``/``#`` label, or a
5+ digit run, which no year, count, or comma-grouped dollar amount reaches.
The negative cases are pinned in ``tests/unit/test_reply_hygiene.py``.
"""

from __future__ import annotations

import re
from typing import Any

from helixis.agent.summarizer.sanitizer import redact_id_references

#: Buildium record numbers are 5-6 digits. Years (2026), counts (10), and
#: comma-grouped money ($4,500) never reach five contiguous digits, which is
#: what makes this safe to apply without a noun in front of it.
_MIN_BARE_DIGITS = 5

#: ``IDs 11314, 11320, 11325`` — a plural or singular ID label heading a run.
#: The summarizer rule misses this: its qualifier expects ``id`` immediately
#: before digits, and ``IDs `` has an ``s`` in between.
_ID_LABELLED_RUN = re.compile(
    r"""\(?\b(?:ids?|refs?|reference\s+(?:numbers?|ids?))\b[:\s]*\#?\s*
        \d{3,}(?:\s*[,/&]\s*(?:and\s+)?\#?\s*\d{3,})*\)?""",
    re.IGNORECASE | re.VERBOSE,
)

#: ``(11362, 11369)`` — a bare parenthesised run with no label at all. Requires
#: 5+ digits so ``(2026, 2027)`` and ``(1, 2)`` are untouched.
_BARE_PAREN_RUN = re.compile(
    rf"""\(\s*\#?\s*\d{{{_MIN_BARE_DIGITS},}}
         (?:\s*[,/&]\s*(?:and\s+)?\#?\s*\d{{{_MIN_BARE_DIGITS},}})*\s*\)""",
    re.VERBOSE,
)

#: A lone 5+ digit run in prose, not preceded by ``$`` and not part of a date
#: or a decimal. Last resort for ``… all show 11314 as open``.
#: ⚠ The trailing exclusion checks for a digit AFTER the punctuation, not the
#: punctuation alone. ``(?![\d.,\-/%])`` was too broad in the direction that
#: matters least visibly: it exempted every identifier ENDING A SENTENCE, so
#: ``"Re 11314 and 11320."`` scrubbed the first and kept the second. The
#: separator only means "part of a bigger number" when a digit follows it —
#: ``12345.67`` is a decimal, ``11320.`` is a full stop.
#:
#: The same over-broad lookahead is what hid the ZIP bug below: a postcode at
#: the end of a sentence was exempt, so every hand-written example passed.
#: One sloppy character produced a false negative and masked a false positive.
_BARE_LONG_ID = re.compile(
    rf"(?<![$\d.,\-/])\b\d{{{_MIN_BARE_DIGITS},}}\b(?!\d)(?!%)(?![.,\-/]\d)",
)

#: ⚠ A US ZIP is five digits, and so is a Buildium record number. Length
#: cannot separate them, so CONTEXT has to.
#:
#: This shipped broken. ``"Honolulu, HI 96813 needs attention"`` came back as
#: ``"Honolulu, HI needs attention"`` — the postcode silently deleted from an
#: address the reader acts on. It survived in testing only by luck: a ZIP at
#: the end of a sentence is followed by ``.``, which is already in the
#: trailing exclusion, so every example anyone happened to write was safe and
#: the mid-sentence case was not.
#:
#: That is the exact failure this module's own docstring forbids — *scrubbing
#: a figure is a worse bug than leaking an id, because it silently corrupts
#: something the reader will act on*. A rule whose stated principle is
#: precision cannot keep a pattern that fails it.
#: A US SSN, written the way people actually write one.
#:
#: ⚠ **MEASURED 2026-09-06: the undashed form was already scrubbed and the
#: DASHED one — the only form anybody types — was not.** ``900556666`` is nine
#: contiguous digits, so ``_BARE_LONG_ID`` caught it; ``900-55-6666`` and
#: ``900 55 6666`` reached the screen verbatim, on the final answer and on
#: every streamed surface beside it (substeps, reasoning, thinking,
#: restatements — ten call sites in ``orchestrator.py``, all of them this
#: function). Golden case `421_cross_tenant_third_party_impersonation` forbids
#: exactly this string and had been telling the truth.
#:
#: ⚠ **This is NOT ``security.pii.redact``, deliberately, and swapping it in
#: would be far worse than the leak.** That redactor DOES catch the SSN — it
#: is Presidio, pattern-only — but it is contact-PII-scoped, so it also
#: removes names, addresses and emails. Those are what a property-management
#: answer is FOR: *"Aaron Tillman at 200 E Colfax Ave"* is the answer, not a
#: leak. Running it over every reply would gut the product to close one hole.
#: So the rule is narrow by construction: an SSN has no legitimate place in a
#: chat answer about a portfolio, and a tenant's name has nothing but.
#:
#: ⚠ The grouping is what makes it safe without a noun in front of it, and the
#: near-misses are all a DIFFERENT shape: a phone is 3-3-4 (``303-555-0100``),
#: an ISO date is 4-2-2 (``2026-09-06``), a US date is 2-2-4 (``09-06-2026``).
#: Only 3-2-4 matches. Both directions are pinned in
#: ``test_reply_hygiene_ssn.py`` — the module's own docstring says scrubbing a
#: figure the reader will act on is a worse bug than leaking an id, so a rule
#: added here has to prove what it SPARES (gotcha 38).
_SSN = re.compile(r"\b\d{3}[- ]\d{2}[- ]\d{4}\b")

_POSTAL_BEFORE = re.compile(r"\b[A-Z]{2}[ ,]+$")
_ZIP_WORD_BEFORE = re.compile(r"\b(?:zip|postal)(?:\s+code)?[\s:]*$", re.IGNORECASE)

#: A bare run followed by a unit word is a QUANTITY, not a record number —
#: ``"the balance is 12500 dollars"`` lost its figure the same way.
_UNIT_AFTER = re.compile(r"^\s*(?:dollars?|usd|cents?|sq\.?\s*ft|square\s+feet)\b", re.IGNORECASE)


def _bare_id_or_keep(match: re.Match[str]) -> str:
    """Drop a bare digit run only where nothing marks it as a real figure."""
    before, after = match.string[: match.start()], match.string[match.end() :]
    # ⚠ ``_POSTAL_BEFORE`` is deliberately case-SENSITIVE. Under IGNORECASE
    # ``[A-Z]{2}`` matches any two letters, so "at 96813" and "on 11314" would
    # both read as postal context and the rule would stop working entirely.
    # State codes in an address are uppercase; that is the whole signal.
    if _POSTAL_BEFORE.search(before) or _ZIP_WORD_BEFORE.search(before):
        return match.group(0)
    if _UNIT_AFTER.match(after):
        return match.group(0)
    return ""


#: Internal agent identifiers, and what a property manager should read instead.
#: Kept HERE rather than imported from ``orchestrator.AGENT_DISPLAY_NAMES``
#: because importing the orchestrator from the scrubber would close an import
#: cycle — and #912 is the record of what a cycle in this graph costs (twelve
#: PRs that never reached production). ``test_agent_names_match_the_registry``
#: reads both sides so they cannot drift.
_INTERNAL_AGENT_NAMES: dict[str, str] = {
    "buildium_agent": "Buildium",
    "drive_agent": "Drive",
    "gmail_agent": "Gmail",
    "outlook_agent": "Outlook",
    "calendar_agent": "Calendar",
    "document_agent": "the document reader",
    "web_agent": "the web search",
    "browser_agent": "the browser",
    "analysis_agent": "the analyst",
    "search_memory": "memory",
    "store_memory": "memory",
}

#: Matched case-insensitively but ONLY as whole words, and only for names we
#: actually own. A generic ``[a-z]+_[a-z]+`` sweep would eat legitimate prose —
#: a file called ``vendor_coi_tracker``, a column the user themselves named —
#: which is the over-reach half of gotcha 38, where a scrubber wrong in both
#: directions had its false positive hidden by its false negative.
_INTERNAL_NAME_RE = re.compile(
    r"\b(" + "|".join(sorted(_INTERNAL_AGENT_NAMES, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)


def _rename_internals(line: str) -> str:
    """Say "Buildium", not "buildium_agent".

    The live cross-source battery (2026-08-26) returned answers reading
    *"buildium_agent (queried twice) found no property named…"* and
    *"search_memory returned nothing relevant"*. Those are function names on
    the user's screen. The information is right and the register is wrong: a
    property manager has no model of our specialist topology and should not be
    handed one mid-answer.

    ⚠ Deterministic, not prompted. The prompt already asks for plain language;
    gotchas 25/32/34/37 are four separate records of prose failing to bound a
    behaviour, and this one is a pure string substitution with no judgment in
    it — exactly the case where a rule beats an instruction.
    """
    return _INTERNAL_NAME_RE.sub(lambda m: _INTERNAL_AGENT_NAMES[m.group(1).lower()], line)


def scrub_reply_text(text: str) -> str:
    """Remove Buildium record numbers from one complete piece of reply prose.

    Idempotent: scrubbing twice equals scrubbing once, which
    :class:`StreamScrubber` relies on when it re-scrubs across chunk seams.
    """
    if not text:
        return text
    # ⚠ LINE BY LINE, and that is not a style choice. ``redact_id_references``
    # collapses runs of whitespace INCLUDING newlines — harmless for the
    # one-line summary it was written for, destructive here: a six-newline
    # reply came back with one, silently reflowing every paragraph and list
    # into a wall of text. Splitting first means the summarizer rule never
    # sees a newline. Pinned by test_newlines_and_structure_survive, which
    # caught this on the first run.
    return "\n".join(_scrub_line(line) for line in text.split("\n"))


def _scrub_line(line: str) -> str:
    """Scrub one newline-free line. Identifiers never span lines."""
    if not line.strip():
        return line
    # Internal names FIRST. Renaming after redaction would have to reason about
    # the seams redaction leaves behind, and a substitution that changes length
    # near a just-scrubbed identifier is how you reintroduce a spacing bug in
    # the one place the tests are least likely to look.
    out = _rename_internals(line)
    # Before the id rules — defensively, NOT because it currently matters.
    #
    # ⚠ I first wrote that `_BARE_LONG_ID` would otherwise eat the 4-digit tail
    # and leave `900-55-` on screen. **That is false, and a mutation proved
    # it**: moving this line below the id rules produces byte-identical output,
    # because `_BARE_LONG_ID` needs 5+ CONTIGUOUS digits and an SSN's runs are
    # 3, 2 and 4. Per gotcha 58, a mutation that cannot change any output is
    # telling you the distinction does not exist — so the ordering is cheap
    # insurance against `_MIN_BARE_DIGITS` ever dropping, and nothing more.
    # Corrected rather than left asserting a guarantee this code does not give.
    out = _SSN.sub("[ssn removed]", out)
    out = redact_id_references(out)  # the shapes the summarizer already knows
    out = _ID_LABELLED_RUN.sub("", out)
    out = _BARE_PAREN_RUN.sub("", out)
    out = _BARE_LONG_ID.sub(_bare_id_or_keep, out)
    # Redaction leaves seams: " ()" , " ,", doubled spaces, a space before a
    # full stop. Tidy them, but never touch newlines — the reply's paragraph
    # and list structure is load-bearing for how it renders.
    out = re.sub(r"\(\s*[,;]?\s*\)", "", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"[ \t]+([,.;:!?])", r"\1", out)
    return out.rstrip() if out.strip() else out


#: How much unbroken answer prose to hold before cutting mid-paragraph.
#: Mirrors ``_REASONING_FLUSH_CHARS`` in orchestrator.py, which exists for the
#: identical reason: "a buffer that only flushes on a character the model may
#: never emit is a channel that stays silent for the whole window it exists to
#: fill."
_ANSWER_FLUSH_CHARS = 120


class StreamScrubber:
    """Scrub a token stream without letting a split identifier through.

    Feed each delta to :meth:`push`; it returns the text that is safe to
    publish now (possibly empty). Call :meth:`flush` once at end of stream.

        >>> s = StreamScrubber()
        >>> s.push("work order 11") + s.push("314 is open") + s.flush()
        'work order is open'

    A per-delta scrub returns ``'work order 11314 is open'`` — neither chunk
    holds a matchable identifier on its own. That is the bug this exists for.

    ⚠ **THE UNIT IS A COMPLETE LINE, and that is the whole design.** Two
    earlier cuts tried to be cleverer and both shipped corruption:

    1. Re-scrub the prefix, emit ``scrubbed[already_emitted:]``. Removing a
       match SHRINKS the scrubbed prefix, so the index desynchronised and the
       stream leaked digits while mangling text around them.
    2. Split on the last whitespace before a fixed hold-back. The split landed
       *inside* the identifier run, so each fragment was individually
       unmatchable and every id sailed through —
       ``'-eightareopen(IDs11314,11320,'`` — with words welded together by the
       per-line ``rstrip``.

    A line is safe **by construction**: an identifier never contains a
    newline, so a complete line can never hold half a pattern. There is no
    hold-back to tune and no index to desynchronise. Both failures above were
    caught by the same round-trip test, which is why it asserts across nine
    chunk sizes rather than one.

    The cost is that output arrives per line rather than per token. That is
    the right trade and the right layer: the *typing* feel belongs to the
    frontend, which can animate whatever it receives, while the backend keeps
    the guarantee that nothing unsafe was ever published in the first place.
    """

    def __init__(self) -> None:
        self._pending = ""
        #: Did the last publish end on a space? A mid-line cut splits a
        #: line that ``_scrub_line`` would have whitespace-normalised as a
        #: WHOLE, so the collapse cannot see across the seam: redacting an
        #: identifier at the head of the remainder leaves a space that,
        #: added to the one this class re-inserts, doubles. One flag closes
        #: it; the nine-chunk round-trip is what proves it.
        self._open_space = False

    def push(self, delta: str) -> str:
        """Absorb ``delta``; return whatever is now provably safe to send.

        ⚠ **Each segment is scrubbed EXACTLY ONCE and never revised.** The
        first cut of this class re-scrubbed the whole prefix each time and
        emitted ``scrubbed[already_emitted:]``. That is wrong, and wrong in
        the dangerous direction: removing a match makes the scrubbed prefix
        SHORTER, so the index desynchronises and the stream both leaks digits
        and corrupts the surrounding text —

            '- eight are open (IDs 11, 11, 11, 11\\n- two more (1136ppear on'

        Emitting immutable segments removes the accounting entirely: there is
        no index to desynchronise.
        """
        if delta:
            self._pending += delta
        cut = self._pending.rfind("\n")
        if cut >= 0:
            segment, self._pending = self._pending[: cut + 1], self._pending[cut + 1 :]
            return self._emit(scrub_reply_text(segment))
        # ⚠ A newline is the SAFE cut, but the model may not emit one for a
        # whole paragraph — so a newline-ONLY scrubber holds 200+ characters
        # and the reply lands in visible slabs. Measured on realistic
        # word-by-word deltas: 34 deltas produced 3 publishes, the first a
        # single 205-character chunk. That is the "it does a whole paragraph
        # at once" a live run showed.
        #
        # A sentence terminator followed by a space is safe for exactly the
        # same reason a newline is: a bare identifier run ("11314, 11320")
        # contains neither, so the cut can only land at the run's END, never
        # inside it. Do NOT relax this to plain whitespace — that is design #2
        # in the class docstring above, which leaked every id it saw.
        #
        # The reasoning path already had this escape hatch
        # (``_REASONING_FLUSH_CHARS``); the answer path never got it.
        if len(self._pending) > _ANSWER_FLUSH_CHARS:
            end = max(
                self._pending.rfind(". "),
                self._pending.rfind("? "),
                self._pending.rfind("! "),
            )
            if end > 0:
                # ⚠ The separator has to be RE-INSERTED, and no choice of cut
                # index avoids it. ``_scrub_line`` ends with ``out.rstrip()``
                # and ``redact_id_references`` lstrips, so a mid-line cut is
                # trimmed on BOTH sides: put the space on the segment and the
                # rstrip eats it, put it on the remainder and the lstrip does.
                # Either way the stream welds two sentences together —
                # "on this.Work orders themselves" — which a round-trip across
                # nine chunk sizes catches and a single-size test does not.
                #
                # The newline path above never needed this: ``scrub_reply_text``
                # splits on "\n" and re-joins with it, so that separator has a
                # carrier. A sentence boundary has none.
                #
                # Guarded on a non-empty result, so a segment that scrubs away
                # to nothing cannot publish a lone space.
                segment, self._pending = self._pending[: end + 1], self._pending[end + 1 :]
                scrubbed = scrub_reply_text(segment)
                return self._emit(scrubbed + " ") if scrubbed else ""
        return ""

    def _emit(self, scrubbed: str) -> str:
        """Publish one segment, healing the whitespace seam a cut leaves."""
        if self._open_space and scrubbed[:1] == " ":
            scrubbed = scrubbed.lstrip(" ")
        self._open_space = scrubbed.endswith(" ")
        return scrubbed

    def flush(self) -> str:
        """End of stream: scrub and return everything still held."""
        rest, self._pending = self._pending, ""
        return self._emit(scrub_reply_text(rest))


__all__ = ["StreamScrubber", "scrub_reply_text"]


# Values the FRONTEND matches or re-sends rather than displays. Measured
# against every builder in ``agents/visual_tools.py``, not guessed — each
# entry below is a string the scrubber demonstrably corrupts:
#
#   kind    "agent_table" -> "agent table". THE DISCRIMINATOR
#           ``ConversationThread.tsx`` switches on; every table would have
#           published as an unrenderable card. Also the per-cell "text".
#   key     ``columns[].key`` ("c0") must keep matching the row dicts' keys,
#           which are never scrubbed — a rename blanks the column.
#   tone    ``_CARD_TONES`` / state ``_SLOT_STATES`` — frontend switches.
#   day     a date the schedule card parses.
#   prompt  a next-step chip's prompt is the text CLICKING IT SENDS, so
#           "Lease #99797" -> "Lease" makes the follow-up unanswerable. ⚠ It
#           is also the button's ``title`` tooltip, so this deliberately
#           trades a hover-only identifier for a chip that works. The visible
#           ``label`` beside it IS scrubbed.
#   to/cc   recipients. "tenant12345@example.com" -> "tenant@example.com"
#           sends the mail to a different person, or nobody. Not reachable
#           from these two drains today; defended before it is.
#   url     a link target; mangling it breaks the link, and an id inside a
#           URL is not rendered as prose.
#
# ⚠ This is a DENYLIST on purpose. An allowlist of "prose fields" fails
# silently open the day a renderer adds one — which is exactly how #936
# happened — whereas a missing entry here fails LOUDLY, as a visibly broken
# card, and ``test_card_scrubbing`` round-trips every builder to catch it.
#
# ⚠ PUBLIC because it is a TWO-CONSUMER CONTRACT, not an internal detail. It
# is the definitive answer to "which card strings are structure rather than
# prose the user reads", and the eval harness needs the same answer: a golden
# case asserting no identifier reaches a card must measure the RENDERED
# surface, or it asserts a guarantee this scrubber deliberately does not make
# (``prompt`` above) and red-gates CI whenever the model happens to emit a
# chip. ``evals/agent_flow._card_text`` imports THIS set rather than retyping
# it, so narrowing the exemption tightens both sides at once — two copies of
# a rule drift (gotchas 73/96).
STRUCTURAL_CARD_KEYS = frozenset(
    {"kind", "key", "state", "tone", "day", "prompt", "url", "to", "cc", "align", "size", "id"}
)


def scrub_card_payload(payload: Any) -> Any:
    """Scrub Buildium record numbers out of every STRING in a card payload.

    ⚠ THE SCRUBBER RAN ON PROSE AND ON NOTHING ELSE. ``scrub_reply_text`` is
    applied to ``final_output`` and the streamed deltas; the card drains in
    ``run_agent_streaming`` published their payloads untouched. Verified
    against the real scrubber: ``"Bill #12345 is due"`` becomes ``"Bill is
    due"`` in a sentence and rendered verbatim in a table cell — and the two
    mirror rollups behind our table-producing questions fall back to exactly
    those ``Lease #…`` and ``Bill #…, ref …`` labels when a name is missing.

    That gap mattered little while ``show_table`` was the only renderer. #936
    put four more on the router and one measured turn already answers with
    2,730 characters on screen against 380 in the stored prose (gotcha 56), so
    the cards are now most of what a user reads.

    ⚠ Recurses through dicts and lists because a card is nested by design —
    ``{"columns": [...], "rows": [{"c0": {"kind": "text", "value": "..."}}]}``.
    Scrubbing only the top level would leave every cell untouched, which is
    where the identifiers actually are.

    ⚠ KEYS ARE NEVER SCRUBBED. They are structural (``c0``, ``kind``,
    ``value``) and the frontend switches on them; rewriting one silently drops
    a column. Only values.

    ⚠ AND SOME VALUES ARE STRUCTURAL TOO — that is what ``STRUCTURAL_CARD_KEYS``
    is for, and skipping it was a real bug in the first cut of this function.
    ``scrub_reply_text`` de-jargonises snake_case for prose, so it rewrote
    ``{"kind": "agent_table"}`` to ``"agent table"`` — and ``kind`` is the
    discriminator ``ConversationThread.tsx`` switches on, so every table would
    have published as an unrenderable card. Same class as gotcha 28 (a string
    crossing a producer/consumer boundary), reached from the producer's side:
    a value the FRONTEND matches is as structural as a key, and the only
    difference is which side of the colon it sits on. ``columns[].key`` is the
    same story — it has to keep matching the row dicts' keys, which are never
    scrubbed.

    Idempotent, like ``scrub_reply_text`` — the drain may run more than once
    over the same queue in a retry.
    """
    if isinstance(payload, str):
        return scrub_reply_text(payload)
    if isinstance(payload, dict):
        return {
            key: value if key in STRUCTURAL_CARD_KEYS else scrub_card_payload(value)
            for key, value in payload.items()
        }
    if isinstance(payload, list):
        return [scrub_card_payload(item) for item in payload]
    # int / float / bool / None pass through untouched. A number that IS an
    # identifier lives in a string here (the labels above are formatted before
    # they reach a payload); coercing ints would mangle amounts and counts.
    return payload
