"""Turn a run that ran out of time into an answer instead of nothing.

Measured 2026-08-24 across 14 days of ``orchestrator_runs``: **29 of 134 runs
(22%) died at the 240s wall-clock deadline with ``final_text`` NULL**, burning
6,801 seconds against 9,966 productive — **40.2% of all model wall-clock for
zero characters delivered**. Battery v4 independently put the same figure at
39% (gotcha 32). The user sees a generic error; everything the specialists
gathered is discarded.

The existing defences do not reach this. ``call_budget.SOFT_DEADLINE_FRACTION``
already refuses budgeted reads past 0.7× and tells the model "STOP READING —
the next thing you produce must be the ANSWER", but that channel is
**advice inside a tool result**, and it only speaks when a *budgeted read*
happens. The runs that die make 3–15 tool calls over 240s — overwhelmingly
``buildium_agent`` specialist consults, which are not budgeted reads — so the
message is nearly never delivered, and when it is, it is prose. Gotchas 25, 32
and 34 all end with the same sentence: *only a structural stop works.*

So this module is the structural stop. At the soft deadline the orchestrator
stops the agent loop outright and makes **one bounded completion** over the
material already gathered. The model is not asked to decide anything; it is
handed the transcript of what came back and told to write the answer.

Three properties worth stating, because each is a way this could go wrong:

* **It only ever runs where the alternative is nothing.** If there is no
  gathered material and no deliverable partial text, this does not fire and
  the run continues to the hard deadline exactly as before. Intervening with
  nothing to synthesize would replace an honest error with an invented answer.
* **It never raises.** A synthesis failure costs the salvage, never the turn —
  the caller falls through to the existing deadline handler. Same rule as
  ``effort_settings`` and the memory path (gotcha 7).
* **It is labelled.** The answer says it is partial and names what was cut.
  A truncated result presented as complete is this product's worst failure
  mode (gotcha 26), and that applies with more force to an answer assembled
  after the run was cut short.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from helixis.llm.sampling import sampling_kwargs
from helixis.observability.reason_codes import record_reason_code
from helixis.settings import Settings

logger = logging.getLogger(__name__)

#: Per-entry cap. A single specialist consult can return thousands of tokens;
#: past this the tail is almost always row detail the summary will not cite.
MAX_ENTRY_CHARS = 4_000

#: Total digest cap, sized so the synthesis prompt stays well inside one
#: request even when a run gathered aggressively.
MAX_DIGEST_CHARS = 24_000

#: The synthesis reply itself.
#:
#: ⚠ 1_500 WAS TOO SMALL, and #923's own taxonomy is what said so. Battery v9
#: (2026-08-27) lost one question to ``empty_completion:length`` — the rescue
#: ran, called the model, and the response came back with ``content`` as
#: ``None`` and ``finish_reason`` as ``length``. Note what that combination
#: means: not "the answer was cut off mid-sentence" (that arrives as a
#: truncated STRING, which this function returns and the user reads), but the
#: budget exhausted before a single text block completed. The same battery's
#: successful rescue was 2,932 characters ≈ 730 tokens, so the ceiling was
#: never close to binding on the ANSWER — something ahead of the text consumed
#: it. What that something is has not been measured from here, so this raises
#: the ceiling rather than claiming a mechanism.
#:
#: ⚠ AND THE CEILING IS NOT THE TIME BOUND — ``SYNTHESIS_TIMEOUT_S`` is,
#: enforced by the ``wait_for`` below and independent of ``max_tokens``. The
#: old comment ("small enough that the call cannot become the thing that blows
#: the deadline") described a guarantee this constant does not provide. But the
#: two DO interact in one direction: a ceiling the model cannot reach inside
#: the timeout converts a truncated answer into a timed-out one, which is the
#: same zero characters wearing a different cause. The v7 empties hit 1_500
#: tokens in ~18s (≈83 tok/s — ONE measurement, so treated as an estimate, not
#: a rate), which puts 3_000 at ~36s inside a 55s bound. Both moved together
#: for that reason; ``test_the_ceiling_fits_inside_the_timeout`` pins the
#: relationship so a future raise of one alone fails loudly.
MAX_SYNTHESIS_TOKENS = 3_000

#: Hard ceiling on the synthesis call. The soft deadline leaves ~30% of the
#: wall clock (72s of 240s), and this must finish inside it with room to
#: publish — so it is capped independently of how much time is nominally left.
#: The margin against that 72s is deliberate and is asserted from the ACTUAL
#: constants (``SOFT_DEADLINE_FRACTION`` × the configured deadline) rather than
#: from the numbers written in this sentence, which can go stale.
SYNTHESIS_TIMEOUT_S = 55.0

_SYSTEM_PROMPT = """\
You are finishing a property-management question that ran out of time.

Below is the material already gathered by the lookups that completed. Write \
the best answer you can FROM THAT MATERIAL ALONE.

Rules:
- Do NOT ask for another lookup, suggest one, or say you will check something. \
There is no more time; this is the final answer.
- Use only what the material states. Do not estimate, extrapolate or fill gaps \
from general knowledge.
- If the material only partly answers the question, give the part it answers \
and say plainly which part is missing and why (the lookup did not finish).
- Do not describe your process. No "I checked", no "let me". Just the answer.
- Lead with the figures or facts the user asked for.
"""

#: Nouns whose IDENTITY is the answer when a question asks which one.
#: ⚠ Singular forms only — the ``s?`` in :data:`_WHICH_ONE` supplies the
#: plural. Listing plurals by hand is what this got wrong first: ``tenant``
#: was present, ``tenants`` was not, and ``\btenant\b`` cannot match
#: "tenants" because ``s`` is a word character. So "Which tenants should we
#: contact?" fell through to no-opinion while "Which tenant" worked — a gap
#: invisible from reading the list. ``properties`` is spelled out because it
#: is irregular.
_IDENTITY_NOUNS = (
    r"lease|unit|tenant|resident|propert(?:y|ies)|vendor|owner|work\s+order|bill|invoice|task"
)

#: ⚠ Words that turn an identity noun into a MODIFIER, so the question is a
#: list request and not an identification. Found by scanning the 130 real
#: questions in ``evals/golden`` — "What resident **requests** are open?" and
#: "What resident and owner **announcements** have been posted?" both matched
#: on ``resident`` and would have been told to name a resident. Eight
#: hand-written cases all passed while these two did not exist in them; the
#: same lesson ``table_policy`` learned the hard way.
_HEAD_NOUNS = (
    r"request|announcement|report|notice|message|communication"
    r"|document|record|statement|charge|payment|balance|issue|complaint"
)

#: "Which lease…", "What unit…" — the answer is a NAME, not a ranking.
#: ⚠ Every inter-word gap is an explicit ``\s+``: under ``re.VERBOSE`` a
#: literal space is DELETED, which has silently broken a classifier in this
#: repo before (gotcha 30).
_WHICH_ONE = re.compile(
    rf"\b(?:which|what)\s+(?:{_IDENTITY_NOUNS})s?\b"
    rf"(?!\s+(?:and|or)\b)"  # "resident and owner announcements"
    rf"(?!\s+(?:{_HEAD_NOUNS})s?\b)",  # "resident requests"
    re.IGNORECASE,
)

#: "Who should take this?", "Who approved it?" — same shape, person-valued.
#: The second arm carries the past/present verbs an auxiliary list misses
#: ("who approved", "who owns", "who handles"); enumerating them was the
#: other half of the same mistake as the plurals above.
_WHO = re.compile(
    r"\bwho\s+(?:should|shall|do|does|did|is|are|was|were|can|could|will|has|have|had)\b"
    r"|\bwho\s+\w+(?:ed|s)\b",
    re.IGNORECASE,
)


def identity_question_noun(task: str) -> str | None:
    """The thing a question demands be NAMED, or ``None`` for no opinion.

    Same contract as ``classify_effort`` and ``classify_table_need``: return a
    verdict only for a shape positively recognised, and ``None`` — meaning say
    nothing — for everything else, so an unrecognised question produces a
    byte-for-byte unchanged prompt.
    """
    if not task:
        return None
    if m := _WHICH_ONE.search(task):
        # Drop the "which"/"what", collapse internal whitespace, singularise.
        noun = re.sub(r"\s+", " ", m.group(0).split(None, 1)[1]).lower()
        return {"properties": "property"}.get(noun) or noun.removesuffix("s")
    if _WHO.search(task):
        return "person or vendor"
    return None


def _identity_rule(noun: str) -> str:
    """The instruction that stops a ranking standing in for an identification."""
    return (
        f"## What this question is actually asking\n"
        f"It asks WHICH {noun}. The answer is therefore a specific {noun} — named by "
        f"name, address, or unit.\n"
        f"- If the material identifies one, name it in your FIRST sentence.\n"
        f"- If it does NOT, say in your first sentence that you could not determine "
        f"which {noun}, and why. That is the honest answer and it is acceptable.\n"
        f"- Either way, do NOT rank, compare, or characterise unnamed candidates as "
        f"though one had been selected. A confident ranking of things you cannot name "
        f"reads as an answer while answering nothing, and burying the admission in a "
        f"closing 'what's missing' note inverts the reply: here the missing part IS "
        f"the question."
    )


@dataclass
class GatheredMaterial:
    """Bounded, in-order record of what the run's tools actually returned.

    Ordering is preserved and overflow is dropped from the END, not the start:
    the earliest results in one of these runs are the broad list reads that a
    summary is mostly built from, while later ones tend to be per-record
    hydration — the very fan-out that caused the overrun. Dropping the tail
    therefore loses the least.

    ``dropped`` is carried so the answer can say what it did not see. A digest
    that silently omits material would be a truncated read presented as
    complete (gotcha 26).
    """

    entries: list[tuple[str, str]] = field(default_factory=list)
    dropped: int = 0
    _chars: int = 0

    def add(self, tool_name: str, output: str) -> None:
        """Record one tool result, truncating per-entry and in total."""
        name = (tool_name or "tool").strip() or "tool"
        text = (output or "").strip()
        if not text:
            return
        if len(text) > MAX_ENTRY_CHARS:
            text = text[:MAX_ENTRY_CHARS] + " …[entry truncated]"
        if self._chars + len(text) > MAX_DIGEST_CHARS:
            self.dropped += 1
            return
        self.entries.append((name, text))
        self._chars += len(text)

    @property
    def is_empty(self) -> bool:
        return not self.entries

    def to_digest(self) -> str:
        """Render the gathered material as the synthesis prompt's evidence."""
        blocks = [f"### Result from `{name}`\n{text}" for name, text in self.entries]
        if self.dropped:
            blocks.append(
                f"### Note\n{self.dropped} further tool result(s) were gathered but "
                "did not fit and are NOT shown. Do not claim completeness."
            )
        return "\n\n".join(blocks)


def fold_in_partials(material: GatheredMaterial, partials: Mapping[str, str]) -> int:
    """Add what in-flight specialists had streamed. Returns how many landed.

    A specialist consult is the longest thing a run can start, and at the
    deadline it is usually the thing the run is in the middle of — so the
    material most relevant to the question is the material that has not come
    back yet. Both blank runs measured on 2026-09-09 were exactly that shape:
    ``consults.with_evidence: [buildium_agent, gmail_agent]`` beside
    ``synthesis: {"cause": "nothing_gathered", "entries": 0}``.

    ⚠ **Appended, so it is the FIRST thing dropped under the digest cap**, and
    that ordering is deliberate rather than incidental. A completed tool result
    is a whole answer; a partial is a fragment of one. ``GatheredMaterial``
    already drops from the end for the same reason.

    ⚠ **Fenced as untrusted**, using the same markers the exhaustion path uses,
    because this is model-written prose from a sub-agent that never crossed a
    tool boundary — and it is being pasted into a fresh completion's prompt.
    """
    # Late import: ``specialist_limits`` is where the fence is defined, and one
    # definition beats two that can drift (the ``buildium/money.py``
    # precedent). Imported here rather than at module scope to keep this
    # module's import graph flat — the orchestrator pulls in both.
    from helixis.agents.specialist_limits import fence_partial

    folded = 0
    for name, text in partials.items():
        before = len(material.entries)
        material.add(
            f"{name} (interrupted — this consult had not finished)",
            fence_partial(text),
        )
        if len(material.entries) > before:
            folded += 1
    return folded


#: Appended to every synthesized answer. The user must be able to tell this
#: apart from a run that finished — an assembled answer that reads as complete
#: is worse than the error it replaced.
PARTIAL_NOTE = (
    "\n\n_(This question took longer than I allow for one answer, so I stopped "
    "and wrote this up from what I'd already gathered. It may be incomplete — "
    "ask for any specific piece and I'll get it directly.)_"
)


#: Provider finish reasons we will repeat verbatim. Anything else becomes
#: ``other`` — the reason-code contract forbids anything free-form in
#: ``detail``, and a provider is free to put a sentence there.
_KNOWN_FINISH_REASONS = frozenset({"stop", "length", "content_filter", "tool_calls", "error"})


def _finish_reason(response: object) -> str:
    """The provider's finish reason, reduced to a safe slug.

    ⚠ THE question this exists to answer. Battery v8 (2026-08-27) showed the
    empty-completion exit firing with ``content`` as ``NoneType`` after a
    SUCCESSFUL ~19s call — so the run was neither an outage nor a skip, and
    the three causes #922 added could not narrow it further. ``length`` means
    the answer was truncated before any text block (raise the ceiling);
    ``stop`` means the model deliberately produced nothing (fix the prompt);
    ``content_filter`` means it refused. Those are three different fixes and
    nothing in the previous logging told them apart.
    """
    try:
        choice = response.choices[0]  # type: ignore[attr-defined]
        raw = getattr(choice, "finish_reason", None) or getattr(choice, "stop_reason", None)
    except Exception:  # noqa: BLE001 — a diagnostic must never raise
        return "unknown"
    if raw is None:
        return "absent"
    slug = str(raw).strip().lower()
    return slug if slug in _KNOWN_FINISH_REASONS else "other"


def _stamp(outcome: dict[str, Any] | None, **fields: object) -> None:
    """Record the synthesis outcome for the run row, best-effort.

    Separate from :func:`_record_empty` on purpose: that one feeds dashboards
    and must stay a fixed slug vocabulary, while this one feeds a JSON column
    a human queries. Both are written on every exit INCLUDING success, so
    "synthesis did not run" and "synthesis ran and produced nothing" are
    distinguishable in the row — the distinction that cost battery v7 two runs.
    """
    if outcome is None:
        return
    try:
        outcome.update(fields)
    except Exception:  # noqa: BLE001 — observability must never cost the turn
        logger.debug("could not stamp synthesis outcome", exc_info=True)


def _record_empty(cause: str, extra: str | None = None) -> None:
    """Attribute a null rescue, best-effort.

    The three exits need OPPOSITE fixes — gather more, fix the provider call,
    or fix the prompt — so a bare "synthesis returned None" is not actionable.
    ``detail`` carries only this fixed slug set: no task text, no gathered
    material, no model output (the reason-code contract forbids PII in detail,
    and gathered material is raw tenant records).

    ``extra`` appends one further slug after a colon — today the finish
    reason, so the NEXT occurrence is answerable from the reason code alone
    rather than by reading Render logs at a timestamp. The cause slug stays
    the stable PREFIX: it is the taxonomy, and a dashboard grouping on it must
    not fragment because a provider added an enum value.
    """
    detail = f"{cause}:{extra}" if extra else cause
    try:
        record_reason_code("deadline_synthesis_empty", detail=detail)
    except Exception:  # noqa: BLE001 — observability must never cost the turn
        logger.debug("could not record deadline_synthesis_empty=%s", detail, exc_info=True)


def should_synthesize(material: GatheredMaterial, partial_text: str) -> bool:
    """Only intervene where the alternative is a null answer.

    With nothing gathered there is nothing to write from, and inventing an
    answer from an empty transcript is strictly worse than the honest
    "I ran out of time" the existing handler already gives.
    """
    return not material.is_empty or bool(partial_text.strip())


async def synthesize(
    *,
    task: str,
    material: GatheredMaterial,
    partial_text: str,
    settings: Settings,
    completion_fn: object | None = None,
    outcome: dict[str, Any] | None = None,
) -> str | None:
    """One bounded completion over the gathered material. Never raises.

    Returns the answer text (without :data:`PARTIAL_NOTE`), or ``None`` when
    synthesis was skipped or failed — in which case the caller keeps today's
    behaviour exactly.

    ``completion_fn`` is an injection seam for tests, mirroring the planner and
    summarizer clients: patching the module-level ``litellm.acompletion``
    wrapper would also swallow the Langfuse metadata injector.

    ``outcome``, when given, is MUTATED IN PLACE with what happened — the same
    idiom the watchdog's ``clocks`` dict already uses on this call path, and
    for the same reason: the caller needs it for the run row, and a return
    value cannot carry it without changing what every existing caller reads.

    ⚠ WHY THE RUN ROW AND NOT JUST THE LOG. #922 sent the attribution to the
    reason-code channel (logs + Langfuse), and #922's own PR body then claimed
    it would appear in ``orchestrator_runs.raw``. It did not, and a session
    spent a round-trip looking for it there. Logs are also not always
    reachable — the Render MCP was down for part of that same session — while
    the run row is the one surface that always answers. So the outcome goes
    BOTH places: the reason code for dashboards, this dict for the row.
    """
    if not should_synthesize(material, partial_text):
        # ⚠ NOT silent. This is the one legitimate skip — there was genuinely
        # nothing to write from — but it is indistinguishable at the call site
        # from a provider blow-up or an empty completion, and those need
        # opposite fixes. Battery v7 lost two runs to a null rescue and the
        # cause could not be named from the telemetry at all.
        _record_empty("nothing_gathered")
        _stamp(outcome, cause="nothing_gathered", entries=len(material.entries))
        return None

    sections = [f"## The user's question\n{(task or '').strip()[:2000]}"]
    if noun := identity_question_noun(task or ""):
        sections.append(_identity_rule(noun))
    if digest := material.to_digest():
        sections.append(f"## Material gathered before time ran out\n{digest}")
    if partial := partial_text.strip():
        # Whatever the model had already started writing. It is the closest
        # thing to an intended answer that exists, so it leads the evidence —
        # but it is explicitly NOT trusted as finished prose, because a
        # mid-thought fragment shipped as an answer is the #857 failure.
        sections.append(
            "## The partial answer already in progress (may stop mid-sentence)\n"
            + partial[:MAX_ENTRY_CHARS]
        )

    async def _call() -> object:
        """Run the completion, converting ``SystemExit`` BEFORE it crosses the
        Task boundary.

        ⚠ ``except (Exception, SystemExit)`` around an ``asyncio.wait_for`` is
        NOT enough. asyncio deliberately re-raises ``SystemExit`` /
        ``KeyboardInterrupt`` out of a Task into the running loop as well as to
        the awaiter — so the handler below catches it, returns ``None`` as
        designed, and the event loop dies anyway. That matters here because a
        spaCy-backed dependency calling ``sys.exit(1)`` is a live failure mode
        in this codebase (gotcha 7), and this call sits on the request path.
        Converting inside the coroutine keeps it a Task-local error.
        """
        if completion_fn is None:
            import litellm

            fn: object = litellm.acompletion
        else:
            fn = completion_fn
        try:
            return await fn(  # type: ignore[operator]
                model=settings.llm.default_model,
                api_key=settings.llm.anthropic_api_key or None,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": "\n\n".join(sections)},
                ],
                max_tokens=MAX_SYNTHESIS_TOKENS,
                # NOT a literal ``temperature=0``: the production model rejects
                # any explicit temperature but 1 with a client-side 400, so a
                # literal here would make this whole salvage dead on arrival —
                # it would return None every time and look like "nothing was
                # gathered". See ``llm.sampling``.
                **sampling_kwargs(settings.llm.default_model, 0),
            )
        except SystemExit as exc:  # noqa: TRY302 — see the docstring above
            raise RuntimeError(f"synthesis dependency called sys.exit: {exc}") from exc

    try:
        response = await asyncio.wait_for(_call(), timeout=SYNTHESIS_TIMEOUT_S)
        text = response.choices[0].message.content  # type: ignore[attr-defined]
    except (Exception, SystemExit):  # noqa: BLE001 — costs the salvage, never the turn
        logger.warning("deadline synthesis failed — falling back to the error path", exc_info=True)
        _record_empty("call_failed")
        _stamp(outcome, cause="call_failed", entries=len(material.entries))
        return None

    if not isinstance(text, str) or not text.strip():
        # ⚠ THE SILENT ONE. The call SUCCEEDED and returned no usable text, so
        # nothing raised and nothing logged — the run simply produced zero
        # characters and reported the generic stall. Measured on battery v7
        # (2026-08-27): two runs reached here with real gathered material,
        # ~18s of synthesis each, and left no trace of why. Same class as
        # gotcha 17 (silent degradation nothing tells you about), sitting on
        # the path that ONLY runs when a turn is already going badly — which
        # is exactly where nobody is watching.
        reason = _finish_reason(response)
        logger.warning(
            "deadline synthesis returned no usable text (type=%s, len=%d, "
            "finish_reason=%s) over %d gathered entries — the run will report "
            "the generic deadline error",
            type(text).__name__,
            len(text) if isinstance(text, str) else -1,
            reason,
            len(material.entries),
        )
        _record_empty("empty_completion", reason)
        _stamp(
            outcome,
            cause="empty_completion",
            entries=len(material.entries),
            finish_reason=reason,
        )
        return None
    _stamp(
        outcome,
        cause="ok",
        entries=len(material.entries),
        finish_reason=_finish_reason(response),
        chars=len(text.strip()),
    )
    return text.strip()


__all__ = [
    "MAX_DIGEST_CHARS",
    "MAX_ENTRY_CHARS",
    "MAX_SYNTHESIS_TOKENS",
    "PARTIAL_NOTE",
    "SYNTHESIS_TIMEOUT_S",
    "GatheredMaterial",
    "identity_question_noun",
    "should_synthesize",
    "synthesize",
]
