"""A run that ran out of time must produce an answer, not nothing.

Measured 2026-08-24 over 14 days of ``orchestrator_runs``: 29 of 134 runs (22%)
hit the 240s wall clock with ``final_text`` NULL, burning 40.2% of all model
wall-clock for zero characters. This file pins the recovery.

Two layers, kept apart on purpose:

* the pure accumulator + prompt assembly (``GatheredMaterial``, ``synthesize``),
  which is where the bounds and the never-raises contract live;
* the wiring in ``orchestrator.py`` — that the soft cutoff fires BEFORE the hard
  one, that the exception subclasses the existing one so a failed synthesis
  falls through to today's behaviour, and that tool results are actually fed in.

Testing only the first would repeat gotcha 28: both halves well tested, the
seam between them untested, and the bug lives in the seam.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from helixis.agents import deadline_synthesis as ds
from helixis.agents.deadline_synthesis import (
    MAX_DIGEST_CHARS,
    MAX_ENTRY_CHARS,
    GatheredMaterial,
    identity_question_noun,
    should_synthesize,
    synthesize,
)
from helixis.settings import Settings


def _settings() -> Settings:
    return Settings(
        env="test",
        llm={"default_model": "anthropic/claude-sonnet-5", "anthropic_api_key": "k"},
        memory={"enabled": False},
    )


def _reply(text: str | None) -> Any:
    """Minimal stand-in for a LiteLLM ModelResponse."""

    class _Msg:
        content = text

    class _Choice:
        message = _Msg()

    class _Resp:
        choices = [_Choice()]

    return _Resp()


# ── the accumulator ─────────────────────────────────────────────────────────


def test_records_tool_results_in_order() -> None:
    m = GatheredMaterial()
    m.add("buildium_agent", "12 properties, 75 units")
    m.add("delinquency_report", "$14,200 past due")
    assert [n for n, _ in m.entries] == ["buildium_agent", "delinquency_report"]
    digest = m.to_digest()
    assert "12 properties" in digest and "$14,200" in digest


def test_empty_and_blank_outputs_are_not_recorded() -> None:
    """A tool that returned nothing is not evidence and must not pad the prompt."""
    m = GatheredMaterial()
    m.add("t", "")
    m.add("t", "   \n ")
    assert m.is_empty


def test_oversized_entry_is_truncated_not_dropped() -> None:
    m = GatheredMaterial()
    m.add("big", "x" * (MAX_ENTRY_CHARS * 3))
    assert len(m.entries) == 1
    assert len(m.entries[0][1]) < MAX_ENTRY_CHARS + 100
    assert "truncated" in m.entries[0][1]


def test_overflow_drops_the_tail_and_says_so() -> None:
    """Overflow must be visible in the prompt, not silent.

    A digest that quietly omits material is a truncated read presented as
    complete — gotcha 26, and worse here because the answer is assembled after
    the run was cut short.
    """
    m = GatheredMaterial()
    for i in range(40):
        m.add(f"tool_{i}", "y" * (MAX_ENTRY_CHARS - 1))

    assert m.dropped > 0
    assert sum(len(t) for _, t in m.entries) <= MAX_DIGEST_CHARS
    # Earliest survive (the broad list reads a summary is built from).
    assert m.entries[0][0] == "tool_0"
    assert "Do not claim completeness" in m.to_digest()


# ── the guard ───────────────────────────────────────────────────────────────


def test_no_material_and_no_partial_means_do_not_synthesize() -> None:
    """With an empty transcript there is nothing to write from.

    Inventing an answer here would be strictly worse than the honest "I ran out
    of time" the existing handler already produces.
    """
    assert should_synthesize(GatheredMaterial(), "") is False
    assert should_synthesize(GatheredMaterial(), "   ") is False


def test_partial_text_alone_is_enough_to_synthesize() -> None:
    assert should_synthesize(GatheredMaterial(), "The portfolio has 12") is True


@pytest.mark.asyncio
async def test_skipped_synthesis_returns_none_without_calling_the_model() -> None:
    calls: list[Any] = []

    async def _fn(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return _reply("should never happen")

    out = await synthesize(
        task="anything",
        material=GatheredMaterial(),
        partial_text="",
        settings=_settings(),
        completion_fn=_fn,
    )
    assert out is None
    assert calls == [], "spent a model call with nothing to synthesize from"


# ── the call ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_synthesizes_an_answer_from_gathered_material() -> None:
    seen: dict[str, Any] = {}

    async def _fn(**kwargs: Any) -> Any:
        seen.update(kwargs)
        return _reply("12 properties, 75 units, $14,200 past due.")

    m = GatheredMaterial()
    m.add("buildium_agent", "12 properties, 75 units")
    m.add("delinquency_report", "$14,200 past due")

    out = await synthesize(
        task="Give me a full portfolio review",
        material=m,
        partial_text="",
        settings=_settings(),
        completion_fn=_fn,
    )

    assert out == "12 properties, 75 units, $14,200 past due."
    prompt = seen["messages"][1]["content"]
    assert "Give me a full portfolio review" in prompt
    assert "12 properties" in prompt and "$14,200" in prompt
    # ⚠ RETIRED: ``assert seen["max_tokens"] <= 2000``, whose comment claimed
    # the ceiling was what stopped this call blowing the deadline. It is not —
    # ``SYNTHESIS_TIMEOUT_S`` is, enforced by a ``wait_for`` and independent of
    # ``max_tokens`` — and 1_500 was small enough to truncate a rescue before it
    # emitted any text at all (battery v9, ``empty_completion:length``). So the
    # assertion was defending a property it did not hold while forbidding the
    # fix. Replaced, not dropped, by the three dedicated guards below: the
    # ceiling clears the largest measured answer, the timeout clears the
    # ceiling, and the timeout fits the wall clock the soft deadline leaves.
    # That the constant reaches the provider at all is asserted by
    # ``test_the_ceiling_is_actually_sent_to_the_provider``.
    # NO literal temperature. The production model rejects any value but 1 with
    # a client-side 400, so a literal here would make this salvage fail every
    # time and look exactly like "nothing was gathered" (see llm.sampling).
    assert "temperature" not in seen


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "boom",
    [RuntimeError("provider 500"), TimeoutError(), ValueError("bad response shape")],
)
async def test_a_failed_synthesis_never_raises(boom: BaseException) -> None:
    """Failure costs the salvage, never the turn.

    The caller falls through to the existing deadline handler, so a broken
    synthesis leaves behaviour exactly as it was before this shipped.
    """

    async def _fn(**_: Any) -> Any:
        raise boom

    m = GatheredMaterial()
    m.add("t", "some data")
    out = await synthesize(
        task="q", material=m, partial_text="", settings=_settings(), completion_fn=_fn
    )
    assert out is None


def test_systemexit_does_not_take_down_the_event_loop() -> None:
    """gotcha 7, with the asyncio twist that makes the usual handler insufficient.

    A spaCy-backed dependency calling ``sys.exit(1)`` is a live failure mode
    here. ``except (Exception, SystemExit)`` around an ``asyncio.wait_for`` LOOKS
    like it handles that — and it does return ``None`` — but asyncio ALSO
    re-raises ``SystemExit`` out of the Task into the running loop, so the
    process dies while the handler reports success.

    This test therefore runs its own ``asyncio.run`` and asserts the runner
    itself returns cleanly. Asserting only ``out is None`` inside a
    pytest-asyncio test passes against the broken version — which is exactly
    how this was nearly shipped.
    """

    async def _fn(**_: Any) -> Any:
        raise SystemExit(1)

    async def _main() -> str | None:
        m = GatheredMaterial()
        m.add("t", "some data")
        return await synthesize(
            task="q", material=m, partial_text="", settings=_settings(), completion_fn=_fn
        )

    # No pytest.raises: if SystemExit escapes, this call terminates the test.
    assert asyncio.run(_main()) is None


@pytest.mark.asyncio
async def test_an_empty_model_reply_is_not_an_answer() -> None:
    for empty in (None, "", "   "):

        def _make(value: Any) -> Any:
            async def _fn(**_: Any) -> Any:
                return _reply(value)

            return _fn

        _fn = _make(empty)
        m = GatheredMaterial()
        m.add("t", "data")
        out = await synthesize(
            task="q", material=m, partial_text="", settings=_settings(), completion_fn=_fn
        )
        assert out is None, f"accepted {empty!r} as a synthesized answer"


@pytest.mark.asyncio
async def test_synthesis_is_bounded_by_its_own_timeout() -> None:
    """A hung provider must not eat the remaining 30% of the wall clock."""

    async def _fn(**_: Any) -> Any:
        await asyncio.sleep(3600)

    m = GatheredMaterial()
    m.add("t", "data")
    import helixis.agents.deadline_synthesis as ds

    original = ds.SYNTHESIS_TIMEOUT_S
    ds.SYNTHESIS_TIMEOUT_S = 0.05
    try:
        out = await synthesize(
            task="q", material=m, partial_text="", settings=_settings(), completion_fn=_fn
        )
    finally:
        ds.SYNTHESIS_TIMEOUT_S = original
    assert out is None


# ── the ceiling and the clock are ONE decision (battery v9, 2026-08-27) ─────
#
# v9 lost a question to ``empty_completion:length``: 1_500 tokens exhausted
# before any text block completed. The named fix in #923's taxonomy is "raise
# the ceiling" — but raising it alone converts a truncated answer into a
# TIMED-OUT one, which is the same zero characters reported as ``call_failed``
# instead. The two constants have to move together, and the timeout in turn has
# to fit inside the wall clock the soft deadline actually leaves.
#
# ⚠ These read the REAL constants from the three modules that own them rather
# than restating numbers, because the failure mode is precisely that one of
# them moves and the prose next to the others goes stale (gotcha 48's shape:
# a justification cannot fail, so only a corpus/constant comparison notices).


#: The largest deadline-rescue this project has actually produced: 2,932
#: characters (battery v9, 2026-08-27), which at the usual ~4 chars/token is
#: ~733 tokens. Every bound below is expressed against THIS, because it is the
#: one number here that was measured rather than chosen.
LARGEST_MEASURED_SYNTHESIS_TOKENS = 733

#: Tokens per second, from v7's empties: 1_500 tokens in ~18s. ⚠ ONE
#: measurement on one day — gotcha 37's rule says a rate characterised from a
#: single day is not a rate. It is used only where being wrong about it makes a
#: guard STRICTER, and halved wherever it is used as a floor.
MEASURED_TOK_PER_S = 83


def test_the_ceiling_clears_the_largest_answer_this_ever_produced() -> None:
    """⚠ THE bug battery v9 found, stated as a property.

    1_500 tokens was ~2x the largest real rescue, which sounds like ample
    headroom and was not: ``finish_reason=length`` arrived with ``content`` as
    ``None``, meaning the budget was consumed before a single text block
    completed. Whatever sits ahead of the text — the mechanism has not been
    measured from here — the ceiling has to leave room for it AND the answer,
    so 2x the answer alone is the wrong bar.

    3x is a judgement call and is labelled as one. What is not a judgement call
    is that the bar must be expressed against a MEASURED answer size, so it
    tightens automatically if rescues get longer.
    """
    import helixis.agents.deadline_synthesis as ds

    assert ds.MAX_SYNTHESIS_TOKENS >= 3 * LARGEST_MEASURED_SYNTHESIS_TOKENS, (
        f"MAX_SYNTHESIS_TOKENS={ds.MAX_SYNTHESIS_TOKENS} leaves under 3x the largest rescue this "
        f"project has produced (~{LARGEST_MEASURED_SYNTHESIS_TOKENS} tokens). At 2x, battery v9 "
        f"got finish_reason=length with NO text block emitted — the budget went somewhere ahead "
        f"of the answer. Shrinking this re-opens that."
    )


def test_a_measured_size_answer_completes_well_inside_the_timeout() -> None:
    """The other side of the same decision: the timeout must not become the
    binding constraint on a NORMAL rescue.

    Deliberately NOT ``MAX_SYNTHESIS_TOKENS / rate <= timeout``. The ceiling is
    a safety valve, not a target — a rescue that genuinely runs to 3_000 tokens
    is already pathological, and asserting the pathological case fits would
    force the ceiling back down to roughly where the v9 bug lives. What must
    fit, with room to spare, is the answer size actually observed — at HALF the
    measured rate, so being wrong about the rate makes this stricter.
    """
    import helixis.agents.deadline_synthesis as ds

    needed_s = LARGEST_MEASURED_SYNTHESIS_TOKENS / (MEASURED_TOK_PER_S / 2)
    assert needed_s * 2 <= ds.SYNTHESIS_TIMEOUT_S, (
        f"a typical rescue (~{LARGEST_MEASURED_SYNTHESIS_TOKENS} tokens) needs ~{needed_s:.0f}s at "
        f"half the measured rate, and SYNTHESIS_TIMEOUT_S={ds.SYNTHESIS_TIMEOUT_S} leaves under 2x "
        f"that. A rescue killed by its own timeout returns zero characters — the same failure "
        f"raising the ceiling was meant to fix, reported as call_failed instead."
    )


def test_the_timeout_fits_inside_the_wall_clock_the_soft_deadline_leaves() -> None:
    """⚠ Reads ``SOFT_DEADLINE_FRACTION`` and the configured deadline, not the
    ``72s of 240s`` written in the constant's own comment. Either can move —
    ``reasoning_loop_deadline_s`` is a SETTING and gotcha 29 records that its
    live value is set by env var and invisible to a config grep — and the
    comment would go on asserting a margin that no longer exists.

    The margin exists to publish the synthesized answer over SSE, which takes
    milliseconds; the generous floor here is for the model call's own variance,
    not for the publish.
    """
    from helixis.agents.call_budget import SOFT_DEADLINE_FRACTION
    from helixis.agents.deadline_synthesis import SYNTHESIS_TIMEOUT_S
    from helixis.settings import Settings

    deadline_s = Settings.model_fields["agent"].default_factory().reasoning_loop_deadline_s  # type: ignore[misc]
    remaining_s = deadline_s * (1 - SOFT_DEADLINE_FRACTION)
    assert SYNTHESIS_TIMEOUT_S < remaining_s, (
        f"the soft deadline fires at {SOFT_DEADLINE_FRACTION:.0%} of {deadline_s}s, leaving "
        f"{remaining_s:.0f}s — but the synthesis call may run {SYNTHESIS_TIMEOUT_S}s. The rescue "
        f"would be killed by the hard deadline it exists to survive."
    )


def test_the_ceiling_is_actually_sent_to_the_provider() -> None:
    """⚠ Both sides (gotcha 28). Raising a constant nothing forwards is a
    change that reads as a fix and is not one."""
    import asyncio as _asyncio

    import helixis.agents.deadline_synthesis as ds

    seen: dict[str, Any] = {}

    async def _capture(**kw: Any) -> Any:
        seen.update(kw)
        return _reply_with("an answer", "stop")

    m = GatheredMaterial()
    m.add("t", "data")
    out = _asyncio.run(
        synthesize(
            task="q", material=m, partial_text="", settings=_settings(), completion_fn=_capture
        )
    )
    assert out == "an answer"
    assert seen.get("max_tokens") == ds.MAX_SYNTHESIS_TOKENS, (
        f"the synthesis call sent max_tokens={seen.get('max_tokens')!r}, not "
        f"MAX_SYNTHESIS_TOKENS={ds.MAX_SYNTHESIS_TOKENS}"
    )


# ── a "which one?" question wants a NAME, not a ranking ─────────────────────
#
# Live chat, 2026-08-25. "Which lease needs attention most urgently, and why?"
# died at the soft deadline (178s) and the salvage answered:
#
#     The lease that needs attention most urgently is the one flagged as:
#     "Zero-payment tenant with two open work orders on file" …
#     **What's missing:** The specific property address/unit identifier for
#     this zero-payment lease was not included in the material gathered.
#
# That is a detector RULE LABEL, not a lease. The reply ranked three unnamed
# candidates confidently and put "I never worked out which lease this is" in a
# closing note — and it did so by following the prompt exactly, which said to
# "give the part it answers and say plainly which part is missing".
#
# For a WHICH-ONE question the identity IS the question, so the missing part
# was the whole of it. The trigger is deterministic rather than left to the
# model's judgement about what "the part it answers" means.


@pytest.mark.parametrize(
    ("question", "noun"),
    [
        ("Which lease needs attention most urgently, and why?", "lease"),
        ("Which unit is behind on rent?", "unit"),
        ("What property has the most open work orders?", "property"),
        ("Which properties are underperforming?", "property"),
        ("Which work orders are stuck?", "work order"),
        ("Which tenants should we contact?", "tenant"),
        ("Who should take the breaker fault at Garden Row 2C?", "person or vendor"),
        ("Who approved this invoice?", "person or vendor"),
    ],
)
def test_identity_questions_are_recognised(question: str, noun: str) -> None:
    assert identity_question_noun(question) == noun


@pytest.mark.parametrize(
    "question",
    [
        "Is Marcus right about the escalation window?",
        "Give me a full portfolio review",
        "What is the balance on 415 S Beretania Unit 1?",
        "Summarise the operations meeting notes",
        "Draft a note to the vendor",
        "",
    ],
)
def test_everything_else_gets_no_opinion(question: str) -> None:
    """``None`` means say nothing, so an unrecognised question yields a
    byte-for-byte unchanged prompt — the same narrowness that lets
    ``classify_effort`` and ``classify_table_need`` ship without a tuning pass."""
    assert identity_question_noun(question) is None


@pytest.mark.asyncio
async def test_the_rule_reaches_the_prompt_for_a_which_one_question() -> None:
    """⚠ Reads BOTH sides of the seam (gotcha 28).

    Testing the classifier alone would pass while the rule never reached the
    model — which is the exact shape of the ``next_steps`` enum bug, where
    producer and consumer were each well covered and nothing exercised the
    join between them.
    """
    seen: dict[str, Any] = {}

    async def _fn(**kwargs: Any) -> Any:
        seen.update(kwargs)
        return _reply("I could not determine which lease.")

    m = GatheredMaterial()
    m.add("detector_findings", "Zero-payment tenant with two open work orders")

    await synthesize(
        task="Which lease needs attention most urgently, and why?",
        material=m,
        partial_text="",
        settings=_settings(),
        completion_fn=_fn,
    )

    prompt = seen["messages"][1]["content"]
    assert "It asks WHICH lease" in prompt
    assert "FIRST sentence" in prompt
    assert "do NOT rank" in prompt, "the ranking prohibition is the whole point"


@pytest.mark.asyncio
async def test_a_non_identity_question_prompt_is_untouched() -> None:
    """The narrowness, as an executable claim: no rule, no drift."""
    seen: dict[str, Any] = {}

    async def _fn(**kwargs: Any) -> Any:
        seen.update(kwargs)
        return _reply("ok")

    m = GatheredMaterial()
    m.add("buildium_agent", "12 properties")

    await synthesize(
        task="Give me a full portfolio review",
        material=m,
        partial_text="",
        settings=_settings(),
        completion_fn=_fn,
    )

    prompt = seen["messages"][1]["content"]
    assert "What this question is actually asking" not in prompt
    assert "do NOT rank" not in prompt


@pytest.mark.parametrize(
    "question",
    [
        # ⚠ Both found by scanning the 130 real questions in evals/golden,
        # NOT by hand. Eight hand-written cases passed while these failed.
        "What resident requests are open right now?",  # 407
        "What resident and owner announcements have been posted in Buildium?",  # 094
        "Which tenant balances are wrong?",
    ],
)
def test_a_modifier_is_not_the_head_noun(question: str) -> None:
    """ "resident requests" asks for a LIST of requests, not the identity of a
    resident. Matching on ``resident`` and demanding a name would answer a
    question nobody asked — and the honest default here is silence, so a
    conservative miss costs nothing while a false fire distorts a real answer."""
    assert identity_question_noun(question) is None


@pytest.mark.parametrize(
    ("question", "noun"),
    [
        ("Which vendor cost us the most this year?", "vendor"),  # 110 / 111
        ("Which property is Whitfield's lease at?", "property"),  # 127
    ],
)
def test_the_real_identity_questions_in_the_golden_corpus_still_fire(
    question: str, noun: str
) -> None:
    """The other half of the same scan: the head-noun guard must not have
    bought its precision by silencing the cases that motivated the rule.

    ``127`` is the ambiguous-tenant case — the run that asserted a false
    tenant→property link. "Name it, or say you could not" is exactly the
    shape that case needs.
    """
    assert identity_question_noun(question) == noun


# ── a null rescue must say WHY (battery v7, 2026-08-27) ──────────────────
#
# Two v7 runs reached synthesis with real gathered material, spent ~18s each,
# and returned nothing. The run then reported the generic stall error and the
# user got zero characters. Nothing in the logs or in ``orchestrator_runs``
# named the cause, and there were THREE candidates needing OPPOSITE fixes:
# nothing gathered (gather more), the provider call blew up (fix the call), or
# the model returned an empty completion (fix the prompt). Only the middle one
# logged.
#
# ⚠ Gotcha 17's class — silent degradation nothing tells you about — sitting on
# the rescue path, which by construction only runs when a turn is ALREADY going
# badly. That is exactly where nobody is watching, so it is the last place an
# unattributed failure is affordable.


@pytest.fixture
def codes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str | None]]:
    """Capture reason codes at the module that emits them."""
    seen: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        ds, "record_reason_code", lambda code, **kw: seen.append((code, kw.get("detail")))
    )
    return seen


def _material() -> GatheredMaterial:
    m = GatheredMaterial()
    m.add("delinquency_report", "20 tenants owe $36,340")
    return m


async def _boom(**_kw: Any) -> Any:
    raise RuntimeError("provider exploded")


async def _blank(**_kw: Any) -> Any:
    return _reply("   ")


@pytest.mark.asyncio
async def test_nothing_gathered_is_attributed(codes: list[tuple[str, str | None]]) -> None:
    out = await synthesize(
        task="anything", material=GatheredMaterial(), partial_text="", settings=_settings()
    )
    assert out is None
    assert ("deadline_synthesis_empty", "nothing_gathered") in codes


@pytest.mark.asyncio
async def test_a_failed_provider_call_is_attributed(codes: list[tuple[str, str | None]]) -> None:
    out = await synthesize(
        task="who is behind on rent",
        material=_material(),
        partial_text="",
        settings=_settings(),
        completion_fn=_boom,
    )
    assert out is None
    assert ("deadline_synthesis_empty", "call_failed") in codes


@pytest.mark.asyncio
async def test_an_empty_completion_is_attributed(codes: list[tuple[str, str | None]]) -> None:
    """⚠ THE ONE THAT WAS SILENT. The call SUCCEEDS and returns no usable text,
    so nothing raises and — before this — nothing logged."""
    out = await synthesize(
        task="who is behind on rent",
        material=_material(),
        partial_text="",
        settings=_settings(),
        completion_fn=_blank,
    )
    assert out is None
    # ⚠ The CAUSE is the stable prefix; the finish reason rides after a colon.
    # Asserting equality here would have to change every time the provider
    # adds an enum value, which is exactly the fragility the prefix avoids.
    assert [d for _c, d in codes if (d or "").startswith("empty_completion")]


@pytest.mark.asyncio
async def test_the_three_causes_are_distinguishable(codes: list[tuple[str, str | None]]) -> None:
    """⚠ The point is not that each logs SOMETHING — it is that a reader can
    tell them apart. A single shared slug would satisfy all three tests above
    and leave the operator exactly as stuck as v7 left us."""
    mat = _material()
    await synthesize(task="q", material=GatheredMaterial(), partial_text="", settings=_settings())
    await synthesize(
        task="q", material=mat, partial_text="", settings=_settings(), completion_fn=_boom
    )
    await synthesize(
        task="q", material=mat, partial_text="", settings=_settings(), completion_fn=_blank
    )
    causes = {(d or "").split(":", 1)[0] for _c, d in codes}
    assert causes == {"nothing_gathered", "call_failed", "empty_completion"}, (
        f"the three exits collapsed to {sorted(causes)} — an operator still cannot "
        f"tell 'gather more' from 'fix the call' from 'fix the prompt'"
    )


@pytest.mark.asyncio
async def test_a_successful_synthesis_records_nothing(codes: list[tuple[str, str | None]]) -> None:
    """⚠ No false positives. A reason code that fires on the happy path is
    noise, and noise on a rare-failure channel is worse than silence — it
    trains the reader to ignore the one signal that matters."""

    async def good(**_kw: Any) -> Any:
        return _reply("the real answer")

    out = await synthesize(
        task="q",
        material=_material(),
        partial_text="",
        settings=_settings(),
        completion_fn=good,
    )
    assert out == "the real answer"
    assert codes == []


# ── the finish reason, and the run row (battery v8, 2026-08-27) ──────────
#
# #922 made the three causes distinguishable and v8 immediately used it: both
# surviving failures reported ``empty_completion`` with ``content`` as
# ``NoneType`` after a SUCCESSFUL ~19s call. So the taxonomy worked and then
# ran out — "the model returned nothing" has at least three sub-causes needing
# opposite fixes, and nothing separated them:
#
#     length          -> truncated before any text block (raise the ceiling)
#     stop            -> deliberately produced nothing (fix the prompt)
#     content_filter  -> refused
#
# ⚠ AND the outcome now reaches the RUN ROW, not only the log. #922's PR body
# claimed ``raw`` would carry it; it did not, and a session spent a round-trip
# looking there. Logs are also not always reachable — the Render MCP was down
# for part of that same session — while the run row always answers.


def _reply_with(text: str | None, finish: str | None) -> Any:
    """A LiteLLM-shaped response carrying a finish reason."""

    class _Msg:
        content = text

    class _Choice:
        message = _Msg()
        finish_reason = finish

    class _Resp:
        choices = [_Choice()]

    return _Resp()


@pytest.mark.asyncio
async def test_the_finish_reason_rides_with_the_cause(
    codes: list[tuple[str, str | None]],
) -> None:
    """⚠ THE question v8 could not answer. ``length`` and ``stop`` are the
    same symptom and opposite fixes."""

    async def truncated(**_kw: Any) -> Any:
        return _reply_with(None, "length")

    out = await synthesize(
        task="q",
        material=_material(),
        partial_text="",
        settings=_settings(),
        completion_fn=truncated,
    )
    assert out is None
    assert ("deadline_synthesis_empty", "empty_completion:length") in codes


@pytest.mark.asyncio
async def test_an_unknown_finish_reason_is_reduced_to_a_slug(
    codes: list[tuple[str, str | None]],
) -> None:
    """⚠ The reason-code contract forbids anything free-form in ``detail`` and
    a provider is free to put a SENTENCE there. Repeating it verbatim would
    both break dashboard grouping and risk carrying content into telemetry."""

    async def odd(**_kw: Any) -> Any:
        return _reply_with(None, "the model declined because the tenant ledger looked odd")

    await synthesize(
        task="q",
        material=_material(),
        partial_text="",
        settings=_settings(),
        completion_fn=odd,
    )
    details = [d for _c, d in codes]
    assert details == ["empty_completion:other"], details
    assert not any("tenant" in (d or "") for d in details), "provider prose reached telemetry"


@pytest.mark.asyncio
async def test_a_missing_finish_reason_is_named_not_guessed() -> None:
    """``absent`` and ``unknown`` are different observations from ``stop``.
    Collapsing them would invent a verdict the response never gave."""
    assert ds._finish_reason(_reply_with(None, None)) == "absent"
    assert ds._finish_reason(object()) == "unknown"


@pytest.mark.asyncio
async def test_the_run_row_learns_what_happened_on_every_exit() -> None:
    """⚠ INCLUDING success. "synthesis never ran" and "synthesis ran and
    produced nothing" are different diagnoses, and only stamping failures
    would leave them indistinguishable in the row — which is the confusion
    that cost battery v7 two runs."""

    async def good(**_kw: Any) -> Any:
        return _reply_with("the answer", "stop")

    ok: dict[str, Any] = {}
    assert (
        await synthesize(
            task="q",
            material=_material(),
            partial_text="",
            settings=_settings(),
            completion_fn=good,
            outcome=ok,
        )
        == "the answer"
    )
    assert ok["cause"] == "ok"
    assert ok["chars"] == len("the answer")
    assert ok["entries"] == 1

    empty: dict[str, Any] = {}
    await synthesize(
        task="q",
        material=_material(),
        partial_text="",
        settings=_settings(),
        completion_fn=_blank,
        outcome=empty,
    )
    assert empty["cause"] == "empty_completion"
    assert "finish_reason" in empty

    skipped: dict[str, Any] = {}
    await synthesize(
        task="q",
        material=GatheredMaterial(),
        partial_text="",
        settings=_settings(),
        outcome=skipped,
    )
    assert skipped["cause"] == "nothing_gathered"


@pytest.mark.asyncio
async def test_the_outcome_dict_is_optional_and_never_raises() -> None:
    """Every existing caller passes nothing. And a broken dict must cost the
    telemetry, never the salvage — same rule as the reason code."""

    async def good(**_kw: Any) -> Any:
        return _reply_with("still fine", "stop")

    for outcome in (None, {}):
        assert (
            await synthesize(
                task="q",
                material=_material(),
                partial_text="",
                settings=_settings(),
                completion_fn=good,
                outcome=outcome,
            )
            == "still fine"
        )
