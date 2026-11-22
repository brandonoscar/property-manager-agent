"""PR #122 — summarizer quality validator + retry-with-nudge.

The summarizer used to ship whatever Gemini returned, as long as it
parsed and didn't leak Buildium IDs. Production row 39bb7547
("Leak at 100 Main Ave") came back with "A task was created this
evening." — syntactically valid, sanitizer-clean, semantically
garbage. We persisted it.

PR #122 adds:

1. ``quality.assess_summary_quality(text, payload, enrichment)`` —
   returns a short reason string when the output is obviously bad,
   else None. Three heuristics: too-short / filler-opener /
   ungrounded.

2. ``quality.build_correction_nudge(reason, payload, enrichment)`` —
   composes the user-role retry message that names the specific
   facts the model has available.

3. ``client.Summarizer.summarize()`` — after sanitization, calls the
   quality check. If it flags AND we have retry budget, append the
   bad output + a correction nudge and retry. If we're out of
   budget, ship the (still-bad) output with a loud warning so ops
   can see the safety net firing.

Tests below cover the validator's three flag cases, the
no-enrichment passthrough, the nudge composition, and the
client-side retry flow (both branches: nudge-then-good and
nudge-then-still-bad-but-shipped).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from helixis.agent.summarizer.client import Summarizer
from helixis.agent.summarizer.quality import (
    _collect_anchor_words,
    assess_summary_quality,
    build_correction_nudge,
)
from helixis.agent.summarizer.sanitizer import sanitize_summary_text

# ── Fixtures ─────────────────────────────────────────────────────


def _leak_enrichment() -> dict[str, Any]:
    """The exact enrichment shape that triggered production row
    39bb7547's failure: rich data, but no requester name on the task."""
    return {
        "task": {
            "title": "Leak at 100 Main Ave",
            "description": (
                "Tenant reports a persistent water leak from the pipe "
                "connection beneath the kitchen sink."
            ),
            "category": "Maintenance Request",
            "requested_by_first_name": None,
            "requested_by_last_name": None,
        },
        "unit": {
            "address_line1": "100 Main Ave",
            "unit_number": "B",
        },
        "active_lease": {
            "tenants": [
                {"first_name": "Clifton", "last_name": "Munoz"},
                {"first_name": "Virginia", "last_name": "Gonzalez"},
            ],
        },
    }


# ── assess_summary_quality ───────────────────────────────────────


class TestAssessSummaryQuality:
    def test_returns_none_for_grounded_full_length_summary(self) -> None:
        text = (
            "Clifton Munoz at 100 Main Ave Unit B reported a worsening "
            "kitchen sink leak this evening — water has been pooling in "
            "the base cabinet for two days. Worth assigning to maintenance "
            "before close of business."
        )
        assert assess_summary_quality(text, {}, _leak_enrichment()) is None

    def test_flags_the_exact_production_failure_string(self) -> None:
        """Row 39bb7547 came back with this exact text."""
        text = "A task was created this evening."
        reason = assess_summary_quality(text, {}, _leak_enrichment())
        assert reason is not None
        # The "too short" check fires first (32 chars < 60 floor).
        assert "too short" in reason

    def test_flags_short_output_with_specific_char_count(self) -> None:
        text = "Short."
        reason = assess_summary_quality(text, {}, _leak_enrichment())
        assert reason is not None
        assert "too short" in reason
        assert "6 chars" in reason

    def test_flags_filler_opener_even_when_long_and_grounded(self) -> None:
        # Long enough + mentions 100 Main Ave (an anchor), but starts
        # with the generic filler stem.
        text = (
            "A task was created this evening for the property at 100 Main "
            "Ave that needs attention from the maintenance team."
        )
        reason = assess_summary_quality(text, {}, _leak_enrichment())
        assert reason is not None
        assert "filler" in reason.lower()

    def test_flags_pr127_production_failure_with_wedged_noun(self) -> None:
        """PR #127 production repro: Garage door row 1ab5cdb8 came back
        with "A new Todo task was created early this morning." The v5
        filler regex required "A new task was" exactly — "Todo" between
        "new" and "task" let this slip through. PR #127 widened the
        regex to allow an optional single noun in that slot."""
        text = "A new Todo task was created early this morning."
        # 47 chars — under the 60-char floor, so the too_short check
        # fires first and that's still acceptable (it's still flagged).
        # But pin BOTH: the regex matches (when text is long enough),
        # AND short text still fails the length check.
        reason = assess_summary_quality(text, {}, _leak_enrichment())
        assert reason is not None
        # Same string padded to length so we exercise the filler path
        # in isolation from the too_short path.
        padded = (
            "A new Todo task was created early this morning concerning "
            "an unspecified matter requiring attention before close of business."
        )
        reason_padded = assess_summary_quality(padded, {}, _leak_enrichment())
        assert reason_padded is not None
        assert "filler" in reason_padded.lower()

    def test_flags_other_wedged_noun_variants(self) -> None:
        """Same regex extension covers other variants we expect to see
        as the LLM iterates on synonyms."""
        variants = [
            "A new maintenance task was opened this afternoon for the property at 100 Main Ave.",
            "A new urgent ticket was added today regarding the building at 100 Main Ave property.",
            "A new tenant note was created earlier today about the residence at 100 Main Ave"
            " Unit B.",
        ]
        for v in variants:
            reason = assess_summary_quality(v, {}, _leak_enrichment())
            assert reason is not None, f"expected filler flag for: {v!r}"
            assert "filler" in reason.lower(), (
                f"reason should mention filler: got {reason!r} for {v!r}"
            )

    def test_flags_pr128_quoted_noun_variant(self) -> None:
        """PR #128 production repro: Exhaust Fan row a9b61f2e came
        back with ``A new "Todo" task was created on May 22, 2026.``
        The PR #127 regex required an unquoted word-character middle
        noun; the model wrapped "Todo" in literal double quotes so
        the optional ``(\\w+\\s+)?`` group didn't bind and the
        outer pattern failed to match. PR #128 allows zero or one
        straight/single quote on each side of the middle noun."""
        # Padded past the too_short floor so we exercise the filler
        # path specifically.
        text = (
            'A new "Todo" task was created on May 22, 2026 concerning '
            "an unspecified matter requiring attention."
        )
        reason = assess_summary_quality(text, {}, _leak_enrichment())
        assert reason is not None
        assert "filler" in reason.lower()

        # Also covers single-quoted variants and other event-shape words.
        variants = [
            "A new 'Maintenance' task was created today regarding the property.",
            'A new "urgent" ticket was opened this morning at the property location.',
            "A new 'general' note was added recently about the building situation.",
        ]
        for v in variants:
            r = assess_summary_quality(v, {}, _leak_enrichment())
            assert r is not None, f"expected filler flag for: {v!r}"
            assert "filler" in r.lower(), f"reason should mention filler: got {r!r} for {v!r}"

    def test_flags_ungrounded_summary(self) -> None:
        # Long enough, no filler opener — but doesn't mention any
        # noun from the enrichment.
        text = (
            "Something noteworthy happened today and the situation needs "
            "to be looked into by someone on the team before the weekend."
        )
        reason = assess_summary_quality(text, {}, _leak_enrichment())
        assert reason is not None
        assert "doesn't mention" in reason
        # Issue #151 fix — _collect_anchor_words now returns an
        # ordered list (was a set), so the first 3 anchors surfaced
        # in the hint are deterministic across PYTHONHASHSEED values.
        # The source-code order in _collect_anchor_words is the
        # implicit semantic priority: title, address, (unit if
        # substantive), reporter_*, tenant first/last names...
        # For _leak_enrichment() the first 3 are reliably:
        #   title="Leak at 100 Main Ave"
        #   address="100 Main Ave"
        #   first tenant first_name="Clifton" (unit_number "B" too
        #     short to anchor; reporter names are None)
        assert "Leak at 100 Main Ave" in reason
        assert "100 Main Ave" in reason
        assert "Clifton" in reason

    def test_passes_thin_summary_when_no_enrichment_available(self) -> None:
        """Pre-migration rows (no enrichment) shouldn't false-positive
        on the ungrounded check — we have nothing to ground against."""
        text = (
            "A maintenance issue was logged today and the team will "
            "follow up shortly with next steps for the tenant involved."
        )
        # Filler opener "A maintenance" is NOT in our stem list (only
        # "a task was created" / "a new event" / etc. are). And there's
        # no enrichment to check ungroundedness against. So passes.
        assert assess_summary_quality(text, {}, None) is None

    def test_handles_missing_enrichment_subkeys_without_crashing(self) -> None:
        """Defensive — backend might supply partial enrichment when
        Buildium calls timed out mid-fetch. Validator must not KeyError."""
        partial: dict[str, Any] = {"task": {}, "unit": None}
        text = (
            "A reasonably long but bland sentence about a maintenance "
            "issue requiring action from someone soon."
        )
        # Should not raise; with no anchors derivable, ungrounded check
        # silently passes.
        result = assess_summary_quality(text, {}, partial)
        # Either None (no anchors to fail against) or a too-short /
        # filler reason — but never a KeyError.
        assert result is None or isinstance(result, str)


# ── _collect_anchor_words (issue #151 regression guard) ──────────


class TestCollectAnchorWords:
    """Issue #151 — pin the deterministic-ordering contract.

    The function used to return a ``set[str]`` whose iteration order
    varied per process via CPython's PYTHONHASHSEED. ``[:3]`` slicing
    in ``assess_summary_quality`` then surfaced different anchors on
    different pytest invocations (5% test flake) AND different
    production retry-nudge contents. The fix replaces the set with
    a deduped, insertion-ordered list. These tests pin both
    properties — return type AND order — so a future refactor that
    re-introduces a set (silently) OR reorders the anchor-collection
    block (semantically) fails loudly here."""

    def test_returns_list_not_set(self) -> None:
        """Type contract: ``set`` reintroduction would silently
        re-break #151's determinism. Catch it here."""
        result = _collect_anchor_words({}, _leak_enrichment())
        assert isinstance(result, list)
        assert not isinstance(result, set)

    def test_anchor_order_matches_source_code_priority(self) -> None:
        """Order contract: the source-code order in
        ``_collect_anchor_words`` is the implicit semantic priority
        (title > address > unit > reporter > tenants > assignee).
        Pinning the order means a future ``anchors.insert(0, ...)``
        or block reorder fails here rather than silently degrading
        retry-nudge quality.

        For ``_leak_enrichment()``: title + address + (unit_number
        "B" too short, dropped) + (reporter names are None, dropped)
        + tenant 1 first/last + tenant 2 first/last + (no assignee).
        """
        result = _collect_anchor_words({}, _leak_enrichment())
        assert result == [
            "Leak at 100 Main Ave",
            "100 Main Ave",
            "Clifton",
            "Munoz",
            "Virginia",
            "Gonzalez",
        ]

    def test_returns_empty_list_for_no_enrichment(self) -> None:
        """No regression on the pre-migration-row passthrough."""
        result = _collect_anchor_words({}, None)
        assert result == []
        assert isinstance(result, list)

    def test_dedupes_repeated_anchors(self) -> None:
        """``_add`` helper must drop duplicates so a reporter who is
        also a tenant doesn't appear twice in the hint."""
        enrichment: dict[str, Any] = {
            "task": {
                "title": "Plumbing issue",
                "requested_by_first_name": "Clifton",
                "requested_by_last_name": "Munoz",
            },
            "unit": {"address_line1": "100 Main Ave", "unit_number": "B"},
            "active_lease": {
                "tenants": [
                    {"first_name": "Clifton", "last_name": "Munoz"},
                ],
            },
        }
        result = _collect_anchor_words({}, enrichment)
        # "Clifton" and "Munoz" appear in BOTH the reporter slot and
        # the tenant slot — must surface exactly once each.
        assert result.count("Clifton") == 1
        assert result.count("Munoz") == 1


# ── build_correction_nudge ───────────────────────────────────────


class TestBuildCorrectionNudge:
    def test_includes_the_reason_verbatim(self) -> None:
        nudge = build_correction_nudge("too short (32 chars)", {}, _leak_enrichment())
        assert "too short (32 chars)" in nudge

    def test_names_concrete_facts_from_enrichment(self) -> None:
        nudge = build_correction_nudge("too short", {}, _leak_enrichment())
        # The nudge should list at least one anchor noun so the model
        # has actionable guidance, not just "try again".
        assert any(
            anchor in nudge
            for anchor in ("100 Main Ave", "Clifton", "Munoz", "Leak at 100 Main Ave")
        )

    def test_falls_back_gracefully_when_no_enrichment(self) -> None:
        nudge = build_correction_nudge("too short", {}, None)
        # Should still be a coherent nudge; just generic.
        assert "previous response" in nudge.lower()
        assert "enrichment" in nudge.lower()

    def test_repeats_the_2_4_sentence_target(self) -> None:
        """The retry should re-anchor the model on the output target,
        not assume it remembers the system prompt across attempts."""
        nudge = build_correction_nudge("ungrounded", {}, _leak_enrichment())
        assert "2-4 sentence" in nudge


# ── Summarizer client — retry-with-nudge flow ────────────────────


def _fake_completion(*responses: str) -> AsyncMock:
    """Build an AsyncMock that returns ``responses[i]`` on the i-th
    call (LiteLLM acompletion shape: response.choices[0].message.content)."""

    class _Choice:
        def __init__(self, content: str) -> None:
            self.message = type("M", (), {"content": content})()

    class _Response:
        def __init__(self, content: str) -> None:
            self.choices = [_Choice(content)]

    mock = AsyncMock()
    mock.side_effect = [_Response(r) for r in responses]
    return mock


@pytest.mark.asyncio
async def test_summarizer_retries_with_nudge_when_first_response_is_low_quality() -> None:
    """The full happy-path safety net: bad output on attempt 0 →
    correction nudge appended → good output on attempt 1 → ship the
    good one."""
    good = (
        "Clifton Munoz at 100 Main Ave Unit B reported a worsening "
        "kitchen sink leak this evening — water pooling in the cabinet "
        "for two days. Worth assigning to maintenance before close of "
        "business."
    )
    completion = _fake_completion("A task was created this evening.", good)
    summarizer = Summarizer(completion_fn=completion, max_retries=1)

    result = await summarizer.summarize(
        payload={"EventName": "Task.Created"},
        enrichment=_leak_enrichment(),
        company_id="co-1",
    )
    # Shipped the GOOD response, not the bad one.
    assert result.text == good
    # The LLM was called twice (initial + retry).
    assert completion.call_count == 2

    # The retry call's messages must include both the bad assistant
    # echo AND the user-role correction nudge.
    second_call_kwargs = completion.call_args_list[1].kwargs
    messages = second_call_kwargs["messages"]
    # 4 messages total: system, user(prompt), assistant(bad), user(nudge).
    assert len(messages) == 4
    assert messages[2]["role"] == "assistant"
    assert messages[2]["content"] == "A task was created this evening."
    assert messages[3]["role"] == "user"
    # The nudge names a concrete fact from the enrichment.
    assert any(anchor in messages[3]["content"] for anchor in ("100 Main Ave", "Clifton"))


@pytest.mark.asyncio
async def test_summarizer_ships_bad_output_when_retry_also_low_quality() -> None:
    """If the safety net fires AND the retry ALSO returns garbage,
    we ship the second response with a loud log — a weak summary is
    more useful than no summary. The contract: never raise when
    output is merely low-quality (only raise on sanitization or
    transient-budget exhaustion)."""
    bad_first = "A task was created this evening."
    bad_second = "An event was reported today."
    completion = _fake_completion(bad_first, bad_second)
    summarizer = Summarizer(completion_fn=completion, max_retries=1)

    result = await summarizer.summarize(
        payload={"EventName": "Task.Created"},
        enrichment=_leak_enrichment(),
        company_id="co-1",
    )
    # Shipped, not raised.
    assert result.text == bad_second
    assert completion.call_count == 2


@pytest.mark.asyncio
async def test_summarizer_does_not_retry_when_first_response_is_good() -> None:
    """No wasted LLM calls when the model gets it right first try."""
    good = (
        "Clifton Munoz at 100 Main Ave Unit B reported a leak this "
        "evening — water pooling for two days. Worth dispatching "
        "maintenance before close of business."
    )
    completion = _fake_completion(good)
    summarizer = Summarizer(completion_fn=completion, max_retries=1)

    result = await summarizer.summarize(
        payload={"EventName": "Task.Created"},
        enrichment=_leak_enrichment(),
        company_id="co-1",
    )
    assert result.text == good
    assert completion.call_count == 1


@pytest.mark.asyncio
async def test_summarizer_does_not_apply_quality_check_when_max_retries_is_zero() -> None:
    """max_retries=0 means single-attempt mode (no retry budget at
    all). The quality check still RUNS — and ships the bad output
    with a warning — but doesn't burn a retry that doesn't exist."""
    bad = "A task was created this evening."
    completion = _fake_completion(bad)
    summarizer = Summarizer(completion_fn=completion, max_retries=0)

    result = await summarizer.summarize(
        payload={"EventName": "Task.Created"},
        enrichment=_leak_enrichment(),
        company_id="co-1",
    )
    assert result.text == bad
    assert completion.call_count == 1


@pytest.mark.asyncio
async def test_summarizer_sanitizer_rejection_retries_then_ships_redacted() -> None:
    """The quality net must not paper over the sanitizer, but summarize() no
    longer HARD-fails on an ID leak: it retries with an ID-avoidance nudge, and
    if the model STILL leaks past the retry budget, ships a redacted, ID-free
    summary rather than failing the row. The sanitizer validator is unchanged."""
    leaky = (
        "Clifton Munoz at 100 Main Ave reported a leak. Tenant 9001 "
        "asked for photos and the plumber is on the way."
    )
    completion = _fake_completion(leaky, leaky)  # both attempts leak the ID
    summarizer = Summarizer(completion_fn=completion, max_retries=1)

    result = await summarizer.summarize(
        payload={"EventName": "Task.Created"},
        enrichment=_leak_enrichment(),
        company_id="co-1",
    )
    # No raise; the shipped text is ID-free (redacted) and passes the validator.
    assert "9001" not in result.text
    sanitize_summary_text(result.text)  # must not raise
    # First attempt + one retry, both leaked → redact + ship (no third call).
    assert completion.call_count == 2
