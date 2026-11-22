"""PR #109 — richer summary body with CONTEXT + RECOMMENDATION sections.

The summarizer v2/v3 prompt was anchors-only: reporter / where /
creator / assignee in 1–3 sentences. Brandon's product critique
asked for a richer paragraph that tells the story end-to-end:
"reporter → what happened → context → what we recommend and why."
The bullets ("WHAT HELIXIS NOTICED") remain the scannable spine;
the summary now carries the narrative.

v4 adds two sections on top of the four anchors:

* CONTEXT — one sentence drawn from ``enrichment.history_unit`` /
  ``enrichment.history_property`` (recurrence pattern) or the task
  ``description`` tone (urgency / frustration). Omits cleanly when
  neither source carries signal.
* RECOMMENDATION — one sentence with a light, data-grounded next
  step. Explicitly forbidden: re-categorize / re-prioritize
  suggestions (the planner handles those). Omits cleanly when no
  genuine recommendation is supported by the data.

Output cap relaxed 1–3 → 2–4 sentences; build_user_prompt's
trailing instruction updated accordingly. Version bumped v3 → v4
so existing rows auto-re-summarize via PR #99's self-heal claim.

Tests pin the prompt SHAPE (string presence) rather than the LLM
output — the prompt itself is the contract under test here. The
runtime behavior (Gemini following the prompt) is exercised
end-to-end on the Vercel preview after deploy.
"""

from __future__ import annotations

from helixis.agent.summarizer.prompt import (
    SUMMARY_PROMPT_VERSION,
    SYSTEM_PROMPT,
    build_user_prompt,
)

# ── Version constant ─────────────────────────────────────────────


def test_summary_prompt_version_is_current() -> None:
    """Pin the constant — bumping it triggers re-summarization of
    every existing row, so the value change should be deliberate
    and visible in code review.

    PR #121 — bumped v4 → v5 to re-summarize rows that hit the
    "no reporter name on the task" failure mode (production row
    39bb7547 silently fell back to "A task was created this
    evening." because the description was 3rd-person passive and
    ``task.requested_by_first_name`` was null).

    PR #127 — bumped v5 → v6 to re-summarize rows that hit a
    stricter variant: no reporter AND no active_lease (because PR
    #124's UnitId validation correctly dropped a mismatched lease).
    Production row 1ab5cdb8 ("Garage door won't close fully" at 33
    Garden Row #3D) regressed to "A new Todo task was created
    early this morning." with the v5 prompt; v6 adds an example
    covering exactly that case (property + symptom + history-only).

    Commit 1 (field strip) — bumped v6 → v7: the detail pane now
    renders priority/category/assignee/due in a field strip beside
    the summary, so the narrative must stop restating those fields
    (dropped the assignment anchor + the "worth assigning"
    recommendation, added a no-restate output rule).
    """
    assert SUMMARY_PROMPT_VERSION == "v7"


# ── CONTEXT section in SYSTEM_PROMPT ─────────────────────────────


def test_system_prompt_introduces_context_section() -> None:
    """The CONTEXT section is the new narrative beat added in v4."""
    assert "CONTEXT" in SYSTEM_PROMPT


def test_system_prompt_grounds_context_in_enrichment_history() -> None:
    """CONTEXT must point the model at the two grounded sources we
    have: history_unit / history_property and description tone."""
    assert "history_unit" in SYSTEM_PROMPT
    assert "history_property" in SYSTEM_PROMPT


def test_system_prompt_requires_exact_recurrence_count() -> None:
    """CONTEXT must instruct the model to state the exact count,
    never a vague 'many' or 'several' — PR #106 catches the
    hallucinated-count case, but the prompt should already steer
    away from vagueness."""
    assert "exact count" in SYSTEM_PROMPT.lower() or "exact" in SYSTEM_PROMPT


def test_system_prompt_allows_context_omission() -> None:
    """When neither history nor description supplies signal, the
    context sentence must be OMITTED — padding is worse than honesty."""
    lowered = SYSTEM_PROMPT.lower()
    assert "omit" in lowered  # the prompt should explicitly authorize omission


# ── RECOMMENDATION section in SYSTEM_PROMPT ──────────────────────


def test_system_prompt_introduces_recommendation_section() -> None:
    assert "RECOMMENDATION" in SYSTEM_PROMPT


def test_system_prompt_recommendation_must_be_grounded() -> None:
    """The RECOMMENDATION section must explicitly require grounding
    in the enrichment — fillers / hallucinations are the failure
    mode to prevent."""
    assert "grounded" in SYSTEM_PROMPT.lower()


def test_system_prompt_forbids_recategorize_or_reprioritize() -> None:
    """The planner owns categorization / prioritization suggestions
    (PR #99 V2 contract). The summarizer must NOT compete with that
    voice — we want one clear recommendation pathway."""
    assert (
        "re-categorizing" in SYSTEM_PROMPT.lower()
        or "re-prioritizing" in SYSTEM_PROMPT.lower()
        or "re-categorize" in SYSTEM_PROMPT.lower()
    )


def test_system_prompt_allows_recommendation_omission() -> None:
    """Filler recommendations are worse than none — the prompt must
    explicitly allow omission."""
    # The recommendation section's penultimate paragraph in the prompt
    # contains an explicit OMIT instruction; check it lives in the
    # RECOMMENDATION block.
    rec_block = (
        SYSTEM_PROMPT.split("RECOMMENDATION")[1] if "RECOMMENDATION" in SYSTEM_PROMPT else ""
    )
    assert "OMIT" in rec_block or "omit" in rec_block


# ── Output rules updated ─────────────────────────────────────────


def test_system_prompt_allows_two_to_four_sentences() -> None:
    """v4 relaxed the cap from 1-3 to 2-4 sentences. Pin the new
    range so a future refactor doesn't silently revert."""
    assert "2-4 sentences" in SYSTEM_PROMPT or "2–4 sentences" in SYSTEM_PROMPT


def test_system_prompt_keeps_narrative_anchor_contract() -> None:
    """The human-story anchors must still be present. Commit 1 (field
    strip) dropped the standalone "WHO IS ASSIGNED" anchor — assignment
    now renders in the strip, not the prose — and folded it into a
    "WHO CREATED vs WHO REPORTED" anchor that explicitly forbids
    restating the structured fields."""
    assert "WHO REPORTED IT" in SYSTEM_PROMPT
    assert "WHERE" in SYSTEM_PROMPT
    assert "WHO CREATED" in SYSTEM_PROMPT
    # The summary must NOT restate the strip fields anymore.
    assert "Do NOT restate the structured fields" in SYSTEM_PROMPT


def test_system_prompt_still_forbids_buildium_ids() -> None:
    """PR #100 sanitizer contract — the prompt must still tell the
    model never to emit internal IDs like Task #17435 / Tenant 9001 /
    Unit 412. The sanitizer is the last line of defense; the prompt
    is the first."""
    lowered = SYSTEM_PROMPT.lower()
    assert "buildium internal id" in lowered or "internal buildium id" in lowered


# ── build_user_prompt updated trailing instruction ───────────────


def test_build_user_prompt_asks_for_two_to_four_sentences() -> None:
    """The trailing instruction the user message ends with must
    reflect the new cap, otherwise the LLM gets conflicting signals
    between the system prompt and the user prompt."""
    prompt = build_user_prompt({"EventName": "x"}, None)
    assert "2-4 sentence" in prompt or "2–4 sentence" in prompt


def test_build_user_prompt_still_includes_enrichment_when_supplied() -> None:
    """Back-compat: PR #92's user-prompt contract still holds — the
    enrichment block lands in the user message when present."""
    enrichment = {"task": {"title": "Loud crashing"}, "unit": {"unit_number": "4B"}}
    prompt = build_user_prompt({"EventName": "Task.Created"}, enrichment)
    assert "Enrichment" in prompt
    assert "4B" in prompt


def test_build_user_prompt_drops_enrichment_block_when_none() -> None:
    """Back-compat: missing enrichment still produces a clean prompt."""
    prompt = build_user_prompt({"EventName": "x"}, None)
    assert "Enrichment" not in prompt


# ── PR #121 — v5 no-reporter fallback handling ───────────────────


def test_system_prompt_handles_no_reporter_explicitly() -> None:
    """v5 adds explicit guidance for the case where the task has no
    requested_by name. The v4 prompt left this implicit (the model
    was supposed to "omit that anchor") and Gemini in production
    interpreted that as "produce a one-liner" — row 39bb7547 came
    back as "A task was created this evening." despite rich
    enrichment (full description + active_lease tenants + 21 prior
    tickets on the unit).

    Pin presence of the fallback guidance so a future prompt edit
    can't quietly drop it.
    """
    lowered = SYSTEM_PROMPT.lower()
    assert "fallback when no reporter" in lowered, (
        "v5 prompt must explicitly tell the model how to anchor when "
        "the task lacks a requested_by name"
    )


def test_system_prompt_falls_back_to_property_plus_symptom() -> None:
    """v5 directs the model to lead with PROPERTY + SYMPTOM when no
    reporter is named, instead of bailing to a generic line."""
    lowered = SYSTEM_PROMPT.lower()
    assert "property + symptom" in lowered or "property and symptom" in lowered


def test_system_prompt_uses_active_lease_tenants_for_context_only() -> None:
    """v5 distinguishes active-lease tenants (who LIVE there) from
    the task reporter (who CALLED IT IN) — using tenants as the
    reporter would be a fabrication, which the existing 'never
    fabricate' rule already forbids.

    Pin against a single-line-collapsed copy of the prompt so the
    rule string can wrap across lines in the source without breaking
    the test.
    """
    collapsed = " ".join(SYSTEM_PROMPT.lower().split())
    assert "active_lease" in collapsed or "active-lease" in collapsed
    # And the rule must say "never use them as the reporter" or
    # equivalent so the model doesn't blur the two roles.
    assert "never use them as the reporter" in collapsed


def test_system_prompt_v5_includes_no_reporter_example() -> None:
    """The v5 prompt must include a concrete EVENT / SUMMARY example
    where the task lacks a requester name, so Gemini has a pattern
    to follow rather than reasoning it out from rules alone.
    Examples beat rules — that's exactly why this failure mode
    persisted under v4 (no example covered it)."""
    assert "no reporter named" in SYSTEM_PROMPT.lower()
