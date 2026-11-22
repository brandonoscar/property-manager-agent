"""PR #92 — summarizer module tests.

Covers:
  - Sanitizer rejects ID-shaped references; passes clean text through
    unchanged.
  - Prompt builder concatenates payload + enrichment + timestamp
    cleanly; degrades when enrichment is None.
  - Summarizer happy path: returns SummaryResult with text, version,
    model, duration_ms.
  - Summarizer retries on litellm.Timeout and succeeds on retry.
  - Summarizer exhausts retries → raises SummarizerError with the
    underlying transient cause set on ``__cause__`` so the worker
    can classify it.
  - Summarizer truncates over-long output to SUMMARY_TEXT_MAX_CHARS.
  - Summarizer rejects (immediately, non-retryable) when the LLM
    output contains a Buildium ID-shape.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import litellm
import pytest

from helixis.agent.summarizer import (
    SUMMARY_PROMPT_VERSION,
    Summarizer,
    SummarizerError,
    SummarySanitizationError,
    redact_id_references,
    sanitize_summary_text,
)
from helixis.agent.summarizer.prompt import build_user_prompt
from helixis.agent.summarizer.types import SUMMARY_TEXT_MAX_CHARS

# ── Sanitizer ──────────────────────────────────────────────────────


def test_sanitize_passes_clean_text_through_unchanged() -> None:
    text = "Sarah Chen at 123 Main St unit 4B reported a kitchen sink leak this afternoon."
    assert sanitize_summary_text(text) == text


def test_sanitize_passes_summary_with_unit_number_but_no_id() -> None:
    """Unit numbers like ``#4B`` are legitimate user-facing content
    — sanitizer must not flag them as ID leaks."""
    text = "Marcus Wright at 88 Pine Ave #3 renewed his lease for 12 months."
    assert sanitize_summary_text(text) == text


def test_sanitize_passes_currency_and_dates() -> None:
    """Numbers that are clearly money / dates must not trip the
    bare-large-integer pattern."""
    text = "Lina Park paid $1,950 in rent on May 14."
    assert sanitize_summary_text(text) == text


@pytest.mark.parametrize(
    "bad_text",
    [
        "Task #17435 was completed.",
        "Tenant 9001 (Sarah Chen) reported a leak.",
        "Tenant Id: 9001 reported a leak.",
        "TaskId 17435 was completed.",
        "ID 17435 was assigned to maintenance.",
    ],
)
def test_sanitize_rejects_id_shaped_references(bad_text: str) -> None:
    with pytest.raises(SummarySanitizationError, match="ID-shaped reference"):
        sanitize_summary_text(bad_text)


# ── PR #100 — Sanitizer V2 contract ────────────────────────────────
#
# PR #99 shipped a summarizer prompt that requires unit numbers as
# narrative anchors ("Unit 204", "unit 4B"). The old sanitizer
# regex (\d{2,} threshold + "unit" in the banned-noun list) rejected
# every legitimate V2 summary that mentioned a unit number, leaving
# the Inbox detail pane stuck on V1. PR #100 fixes the sanitizer to
# match the prompt's contract: unit numbers are allowed, the digit
# threshold rises to \d{3,}, "property" only rejects when followed
# by an explicit ID qualifier.


ALLOWED_V2_SAMPLES = [
    # Unit numbers — the V2 anchor case the original bug rejected.
    "Clifton Munoz at 160 East End Avenue Unit 204 reported a leak.",
    "Sarah Chen at 123 Main St unit 4B reported a kitchen sink issue.",
    "Lina Park at 42 Oak Lane unit 3 paid $1,950 in rent on May 19.",
    "Tenant in #120 reported a loud crashing sound.",
    "Unit 1 has an open work order.",
    "Unit #120 is overdue for inspection.",
    # Street numbers in addresses — must not trigger property rule.
    "The unit at 160 East End Avenue is showing two prior incidents.",
    "Property at 31 Main Street — annual inspection due.",
    "Jimmy at 42 Oak Lane created the task.",
    # Bare narrative numbers — not IDs.
    "20 prior tickets on this unit in the last 90 days.",
    "Annual revenue: $84,000; vacancy rate 3%.",
    "Task created on 2026-05-19 at 17:24.",
    "Inspection scheduled for May 24, 2026.",
    # Phone-shape numbers (not preceded by ID nouns).
    "Tenant phone on file: 555-0142.",
    # Two-digit numbers after non-banned nouns — must not match.
    "Bedroom 2 had a broken window.",
    "Floor 3 corridor lighting is out.",
]


@pytest.mark.parametrize("text", ALLOWED_V2_SAMPLES)
def test_sanitizer_allows_v2_anchor_samples(text: str) -> None:
    """Sanitizer must return these unchanged — they are the shapes
    the V2 prompt's four-anchor contract instructs the LLM to
    produce. Each one was a false-positive under the pre-PR-#100
    regex."""
    assert sanitize_summary_text(text) == text


REJECT_SAMPLES = [
    # Explicit Buildium-style IDs — the original deny-list.
    "Task #17435 was created on May 19.",
    "Tenant 9001 has an overdue balance.",
    # ``User 42`` from the brief is intentionally bumped to a 4-digit
    # ID — under the new \d{3,} threshold a bare two-digit "User 42"
    # is no longer an ID-shape match, but a real Buildium user ID is
    # always multi-digit.
    "User 9001 last logged in yesterday.",
    "Lease 1234 is up for renewal.",
    "Owner 567 has not paid this quarter's distribution.",
    "Vendor 890 invoice is overdue.",
    # ID-qualified property — must still reject even though we
    # relaxed bare "property" matching.
    "Property ID 31 needs roof repair.",
    "Property #1234 had a fire alarm test.",
    "Property number 78 has two open work orders.",
    # Bare digit IDs after explicit qualifier.
    "ID 17435 is the task in question.",
    "#17435 is overdue.",
    # Case variants.
    "TENANT 9001 owes back rent.",
    "task id: 17435 was created.",
    # ── Verbatim from LIVE Inbox cards, 2026-08-25 ──────────────────────
    # Every string below shipped to a real card before the noun list, the
    # bare-property rule and the schema-name rule existed. They are kept
    # verbatim rather than paraphrased: the exact casing and spacing are
    # what walked past the validator.
    #
    # "work order" / "bill" were absent from the noun list entirely, so the
    # whole maintenance vocabulary was unguarded.
    'Work order 9023 ("Water heater leaking into unit below") has no link.',
    "work order 9023 has been sitting in New status for 29 days.",
    "Bill 44821 is past due.",
    # Bare "property" + a long number. Previously allowed, to protect
    # addresses -- but a five-digit number after "property" is an id.
    "unit 101 at property 33378 shows an unusual negative entry.",
    "Unit 1A at property 33375 hits its final lease day.",
    # Internal FIELD names. Not ids at all -- the schema itself, in prose a
    # property manager is meant to read.
    "Work order 9023 has property_id and unit_id both null.",
    "unit 101 shows a payments_3mo figure of -900 against 3,600 in charges.",
    "This task has no unit_id or property_id set.",
    "Task#17435 is open.",
]


@pytest.mark.parametrize("text", REJECT_SAMPLES)
def test_sanitizer_rejects_id_shapes(text: str) -> None:
    """ID-shaped references must still raise after the V2 relaxation.
    Anything matching <banned-noun> + optional qualifier + \\d{3,},
    or <property> + explicit qualifier + digits, or bare ID/# +
    \\d{4,} should be rejected with a useful error message."""
    with pytest.raises(SummarySanitizationError) as exc_info:
        sanitize_summary_text(text)
    assert "ID-shaped reference" in str(exc_info.value)


def test_sanitizer_allows_addresses_with_long_street_numbers() -> None:
    r"""The bare-property rule must not eat an address.

    "property 33378" is an id; "the property at 1600 Pennsylvania Avenue" is
    where the building is. The digits alone cannot tell them apart, so the
    rule leans on the qualifier slot: "at" is not one, so the number never
    lands where the pattern looks.

    ⚠ The first cut guarded this with a ``(?<!at\s)`` lookbehind, which sat
    before the word "property" and therefore exempted "at property 33378" --
    the leak itself. Both directions are pinned here.
    """
    for text in (
        "The property at 160 East End Avenue needs a roof inspection.",
        "The property at 1600 Pennsylvania Avenue has two open work orders.",
        "3 similar tickets at this property this quarter.",
    ):
        assert sanitize_summary_text(text) == text

    with pytest.raises(SummarySanitizationError):
        sanitize_summary_text("unit 101 at property 33378 is overdue.")


def test_redaction_repairs_every_shape_the_validator_rejects() -> None:
    """The fail-safe must be able to clean anything the validator catches.

    ``Summarizer.summarize`` redacts and re-validates when the model keeps
    leaking after its correction nudge. A pattern the validator rejects but
    the redactor cannot repair sends that path to the generic failsafe
    summary -- a card that says nothing -- so the two tables must stay in
    step. This is the pairing the single-spec refactor exists to keep.
    """
    for text in REJECT_SAMPLES:
        cleaned = redact_id_references(text)
        # Must now pass, and must not have been emptied to nothing.
        assert sanitize_summary_text(cleaned) == cleaned
        assert cleaned.strip(), f"redaction emptied {text!r}"


def test_redaction_keeps_the_noun_and_humanizes_the_schema() -> None:
    """Redaction has to leave readable prose, not holes.

    Dropping the noun as well would turn "task 39503 is still open" into
    " is still open". And a schema name becomes English rather than
    vanishing, so the sentence still carries its meaning.
    """
    assert redact_id_references("task 39503 is still status New") == ("task is still status New")
    assert redact_id_references("has no property_id or unit_id") == ("has no property or unit")
    assert redact_id_references("a payments_3mo figure") == "a payments 3mo figure"


def test_sanitizer_allows_the_loud_crashing_anchor_string() -> None:
    """Regression: the V2 summary for the Loud Crashing event was
    rejected with 'Unit 204'. Lock in that this exact shape passes
    now so we never accidentally re-break it.
    """
    text = (
        "Clifton Munoz at 160 East End Avenue Unit 204 reported a "
        "loud crashing sound; Jimmy created a Buildium task this "
        "afternoon, currently unassigned."
    )
    assert sanitize_summary_text(text) == text


@pytest.mark.parametrize(
    "dirty",
    [
        "Maple Court has 12 units, owner Jane Doe, ID 4610, and 2 open work orders.",
        "Follow up on Task #17435 for tenant 9001 about the leak.",
        "Property ID 31 needs review; unit 4B is fine.",
        "Close out lease #24188 for the tenant.",
    ],
)
def test_redact_id_references_produces_validator_clean_text(dirty: str) -> None:
    """The retry-exhaust redactor strips ID-shaped refs so the result passes the
    (unchanged) validator, while keeping legitimate anchors like unit numbers."""
    redacted = redact_id_references(dirty)
    assert sanitize_summary_text(redacted) == redacted


def test_redact_id_references_keeps_unit_numbers_and_values() -> None:
    clean = "Unit 204 has $1,950 rent due on 2026-05-19."
    # Nothing to redact — passes through unchanged.
    assert redact_id_references(clean) == clean


# ── Prompt builder ─────────────────────────────────────────────────


def test_build_user_prompt_includes_payload_and_enrichment() -> None:
    payload = {"EventName": "Task.Created", "TaskId": 17435, "Title": "Loud crashing"}
    enrichment = {"task": {"title": "Loud crashing"}, "unit": {"unit_number": "4B"}}
    prompt = build_user_prompt(payload, enrichment, event_time_iso="2026-05-18T18:00:00Z")
    assert "2026-05-18T18:00:00Z" in prompt
    assert "Task.Created" in prompt
    assert "Loud crashing" in prompt
    assert "4B" in prompt
    assert "Enrichment" in prompt


def test_build_user_prompt_drops_enrichment_block_when_none() -> None:
    prompt = build_user_prompt({"EventName": "x"}, None)
    assert "Enrichment" not in prompt
    assert "Raw webhook payload" in prompt


def test_build_user_prompt_omits_timestamp_line_when_absent() -> None:
    prompt = build_user_prompt({"EventName": "x"}, None, event_time_iso=None)
    assert "Event timestamp" not in prompt


# ── Summarizer happy path ──────────────────────────────────────────


def _make_response(text: str) -> Any:
    """Build a LiteLLM-shaped response object."""
    return type(
        "Resp",
        (),
        {
            "choices": [
                type(
                    "Choice",
                    (),
                    {
                        "message": type("Msg", (), {"content": text})(),
                    },
                )()
            ]
        },
    )()


def _settings_stub() -> Any:
    """Minimal Settings-shaped stub for Summarizer.__init__.

    Real Settings construction requires a Supabase URL, env file,
    and a bunch of validators; we only need ``llm.models``,
    ``llm.default_model``, and ``llm.google_api_key`` (consumed by
    ``_api_key_for_model`` when the resolved model is a gemini /
    google / vertex_ai variant)."""
    settings = type("Settings", (), {})()
    llm = type("LLM", (), {})()
    llm.models = {"reasoning": {"model": "gemini/gemini-2.5-flash"}}
    llm.default_model = "gemini/gemini-2.5-flash"
    llm.google_api_key = "test-gemini-key"
    llm.openai_api_key = None
    llm.anthropic_api_key = None
    settings.llm = llm
    return settings


@pytest.mark.asyncio
async def test_summarizer_happy_path_returns_summary_result() -> None:
    text = "Sarah Chen at 123 Main St unit 4B reported a kitchen sink leak this afternoon."
    completion = AsyncMock(return_value=_make_response(text))
    summarizer = Summarizer(settings=_settings_stub(), completion_fn=completion)

    result = await summarizer.summarize(
        payload={"EventName": "Task.Created", "TaskId": 17435},
        enrichment={"task": {"title": "leak"}},
        company_id="company-pr92",
        event_time_iso="2026-05-18T18:00:00Z",
    )

    assert result.text == text
    assert result.prompt_version == SUMMARY_PROMPT_VERSION
    assert result.model_name == "gemini/gemini-2.5-flash"
    assert result.duration_ms >= 0

    # The LLM call carried the right kwargs.
    completion.assert_awaited_once()
    kwargs = completion.await_args.kwargs
    assert kwargs["model"] == "gemini/gemini-2.5-flash"
    assert kwargs["timeout"] == 30.0
    # PR #105 — bumped 280 → 1500 so the gemini-2.5-flash thinking
    # budget doesn't eat the entire output cap, which was producing
    # mid-sentence truncations in prod.
    assert kwargs["max_tokens"] == 1500
    # Note: no response_format kwarg — we want plain text, not JSON.
    assert "response_format" not in kwargs


@pytest.mark.asyncio
async def test_summarizer_truncates_overlong_output() -> None:
    """The model went long despite max_tokens; the post-hoc
    SUMMARY_TEXT_MAX_CHARS cap kicks in and we add an ellipsis."""
    long_text = "x" * (SUMMARY_TEXT_MAX_CHARS + 200)
    completion = AsyncMock(return_value=_make_response(long_text))
    summarizer = Summarizer(settings=_settings_stub(), completion_fn=completion)

    result = await summarizer.summarize(
        payload={"EventName": "x"},
        enrichment=None,
        company_id="company-pr92",
    )

    assert len(result.text) == SUMMARY_TEXT_MAX_CHARS
    assert result.text.endswith("…")


# ── Retry & error paths ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_summarizer_retries_on_litellm_timeout_then_succeeds() -> None:
    """litellm.Timeout on attempt 1, valid response on attempt 2 →
    summarize() returns the second attempt's text."""
    text = "Marcus Wright at 88 Pine Avenue renewed his lease for 12 months."
    completion = AsyncMock(
        side_effect=[
            litellm.Timeout(
                "first attempt slow",
                model="gemini/gemini-2.5-flash",
                llm_provider="gemini",
            ),
            _make_response(text),
        ]
    )
    summarizer = Summarizer(settings=_settings_stub(), completion_fn=completion, max_retries=1)

    result = await summarizer.summarize(
        payload={"EventName": "x"},
        enrichment=None,
        company_id="company-pr92",
    )
    assert result.text == text
    assert completion.await_count == 2


@pytest.mark.asyncio
async def test_summarizer_exhausts_transient_retries_raises_with_cause() -> None:
    """Both attempts time out → SummarizerError with the underlying
    litellm.Timeout exposed via ``__cause__`` so the worker's
    ``isinstance(exc.__cause__, _TRANSIENT_FOR_SUMMARY)`` check can
    classify it as transient (→ retrying)."""
    timeout = litellm.Timeout(
        "still slow",
        model="gemini/gemini-2.5-flash",
        llm_provider="gemini",
    )
    completion = AsyncMock(side_effect=[timeout, timeout])
    summarizer = Summarizer(settings=_settings_stub(), completion_fn=completion, max_retries=1)

    with pytest.raises(SummarizerError) as exc_info:
        await summarizer.summarize(
            payload={"EventName": "x"},
            enrichment=None,
            company_id="company-pr92",
        )
    assert "exhausted transient retries" in str(exc_info.value)
    # Worker reads __cause__ to decide retrying vs failed.
    assert isinstance(exc_info.value.__cause__, litellm.Timeout)
    assert completion.await_count == 2


@pytest.mark.asyncio
async def test_summarizer_sanitizer_rejection_retries_with_nudge_then_succeeds() -> None:
    """A sanitizer rejection now triggers a correction-nudge retry (the generic
    prompt rule wasn't enough for this model — e.g. gemini-3.5-flash). A clean
    retry ships normally."""
    bad = _make_response("Task #17435 was completed by Tenant 9001.")
    good = _make_response(
        "Sarah Chen at 123 Main St unit 4B reported a kitchen sink leak this afternoon."
    )
    completion = AsyncMock(side_effect=[bad, good])
    summarizer = Summarizer(settings=_settings_stub(), completion_fn=completion, max_retries=3)

    result = await summarizer.summarize(
        payload={"EventName": "Task.Created", "TaskId": 17435},
        enrichment={"task": {"title": "leak"}},
        company_id="company-pr92",
    )
    assert "17435" not in result.text and "9001" not in result.text
    # Retried once after the sanitizer rejection (was: raise-on-first).
    assert completion.await_count == 2


@pytest.mark.asyncio
async def test_summarizer_sanitizer_exhaust_ships_redacted_failsafe() -> None:
    """When the model keeps leaking an ID past the retry budget, summarize()
    ships a redacted, ID-free summary rather than raising — a clean (terser)
    summary beats failing the row. The sanitizer validator is unchanged."""
    bad = _make_response("Follow up on Task #17435 for Tenant 9001 about the leak.")
    completion = AsyncMock(return_value=bad)
    summarizer = Summarizer(settings=_settings_stub(), completion_fn=completion, max_retries=1)

    result = await summarizer.summarize(
        payload={"EventName": "Task.Created", "TaskId": 17435},
        enrichment={"task": {"title": "leak"}},
        company_id="company-pr92",
    )
    # No raise; the shipped text is ID-free and passes the validator.
    assert "17435" not in result.text and "9001" not in result.text
    sanitize_summary_text(result.text)  # must not raise
    # First attempt + one retry, both leaked → redact + ship (no third call).
    assert completion.await_count == 2


@pytest.mark.asyncio
async def test_summarizer_empty_content_raises() -> None:
    """LLM returned an empty string — we treat that as a non-transient
    error and raise rather than persisting empty text."""
    completion = AsyncMock(return_value=_make_response(""))
    summarizer = Summarizer(settings=_settings_stub(), completion_fn=completion)

    with pytest.raises(SummarizerError):
        await summarizer.summarize(
            payload={"EventName": "x"},
            enrichment=None,
            company_id="company-pr92",
        )
