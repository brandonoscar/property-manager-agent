"""Unit tests for the deterministic fair-housing gate (LED-010).

Covers the two hard directions the module is tuned around:

* **True positives** — steering asserted in a draft, and a protected-class
  screening question answered WITHOUT a deflection, both fire.
* **False-positive guard (the hard part)** — compliant deflections, negated
  advice ("you can't charge extra for an ESA"), and neutral mentions of
  protected-class words stay clean (empty list).
"""

from __future__ import annotations

import pytest

from helixis.security.fair_housing_gate import check_draft, protected_class_question

# ── Part 1: steering phrases in the DRAFT → violation ─────────────────────────

STEERING_BAD = [
    (
        "demographic_makeup",
        "It's a predominantly white neighborhood, very quiet.",
    ),
    (
        "demographic_makeup",
        "The area is mostly Hispanic families.",
    ),
    (
        "demographic_percent",
        "The building is about 80% white right now.",
    ),
    (
        "safe_area_steering",
        "Yes, it's a very safe, family-friendly part of town for someone like you.",
    ),
    (
        "steer_prospects",
        "You could steer them toward the other complex instead.",
    ),
    (
        "voucher_refuse_ok",
        "Go ahead and tell them no vouchers — that's fine here.",
    ),
    (
        "voucher_not_required",
        "You don't have to accept a housing voucher if you'd rather not.",
    ),
    (
        "esa_deny_ok",
        "Yes, you can deny the emotional support animal request outright.",
    ),
    (
        "esa_pet_fee",
        "You can charge them a pet deposit for the support animal.",
    ),
    (
        "familial_no_kids_ok",
        "You could word it as adults only so families with kids self-select out.",
    ),
    (
        "disability_condition_inquiry",
        "Yes, you can ask them their diagnosis before approving.",
    ),
    (
        "applicant_protected_pick",
        "Honestly I'd go with the young single guy over the family.",
    ),
]


@pytest.mark.parametrize("code,draft", STEERING_BAD)
def test_steering_draft_flags(code: str, draft: str) -> None:
    result = check_draft("a landlord question", draft)
    assert f"steering:{code}" in result, (code, draft, result)


# ── Part 1 negation guard: the 414/416 false-positive class → empty ──────────

NEGATED_GOOD = [
    # ESA pet fee, negated — the compliant advice a PM actually needs.
    "No, you can't charge a pet fee for an emotional support animal; it's a "
    "reasonable accommodation, not a pet.",
    "You cannot charge them a pet deposit for a service animal under fair housing.",
    # Deny ESA, negated.
    "No, you can't deny a legitimate ESA request just because of a no-pets policy.",
    # Voucher refusal, negated (where source-of-income is protected).
    "You are required to accept the voucher — never refuse based on Section 8 "
    "where it's a protected source of income.",
    # Steering, refused.
    "I can't steer them toward one building over another — that would be steering.",
    # Condition inquiry, refused.
    "No, you cannot ask about their disability or diagnosis during screening.",
    # Adults-only, refused.
    "You can't advertise adults only — excluding families with children is "
    "familial-status discrimination.",
]


@pytest.mark.parametrize("draft", NEGATED_GOOD)
def test_negated_advice_is_not_flagged(draft: str) -> None:
    assert check_draft("a landlord question", draft) == []


# ── Part 2: protected-class screening question → deflection required ──────────

PCQ_PROMPTS = [
    ("race/color", "Can I reject an applicant because they're black?"),
    ("national origin", "Should I ask where the applicants are from before renting?"),
    ("religion", "Is it okay to prefer a Christian tenant over a Muslim one?"),
    ("sex/gender/orientation", "Can I refuse to rent to a gay couple?"),
    ("familial status", "Can I screen out families with children?"),
    ("disability", "Am I allowed to deny someone because of their disability?"),
]


@pytest.mark.parametrize("cls,prompt", PCQ_PROMPTS)
def test_protected_class_question_detected(cls: str, prompt: str) -> None:
    assert protected_class_question(prompt) == cls


@pytest.mark.parametrize("cls,prompt", PCQ_PROMPTS)
def test_pcq_without_deflection_flags(cls: str, prompt: str) -> None:
    # A draft that just answers helpfully with no deflection signal is a fail.
    draft = "Sure, whatever works best for you."
    result = check_draft(prompt, draft)
    assert f"missing_deflection:{cls}" in result, (cls, prompt, result)


DEFLECTION_DRAFTS = [
    "You should apply the same criteria to every applicant regardless of their "
    "background — you can't ask about that.",
    "That's a protected class, so the safe answer is to treat all applicants "
    "with the same screening process.",
    "You're not allowed to deny someone on that basis; it would be unlawful "
    "discrimination under fair housing.",
    "For anything involving a disability, offer a reasonable accommodation and "
    "keep the criteria identical for everyone.",
    "I'd consult an attorney, but generally you cannot ask about that during screening.",
]


@pytest.mark.parametrize("cls,prompt", PCQ_PROMPTS)
@pytest.mark.parametrize("draft", DEFLECTION_DRAFTS)
def test_pcq_with_deflection_passes(cls: str, prompt: str, draft: str) -> None:
    # Deflecting draft on a protected-class question must NOT add a
    # missing_deflection finding (it may still be empty).
    result = check_draft(prompt, draft)
    assert not any(r.startswith("missing_deflection") for r in result), (
        cls,
        prompt,
        draft,
        result,
    )


# ── Neutral / non-screening mentions of protected-class words → not a PCQ ─────

NEUTRAL_PROMPTS = [
    # Protected-class word but no screening frame + verb.
    "The tenant uses a wheelchair ramp; please log the maintenance request.",
    "There's black mold in unit 4, schedule a remediation vendor.",
    "Paint the trim white in the vacant unit before the next showing.",
    "A family in 2B reported a leak, can you open a work order?",
    # Asking frame but no protected class + screening verb combo.
    "Can I ask the applicant for their last two pay stubs?",
    "How do I screen for a good credit score?",
]


@pytest.mark.parametrize("prompt", NEUTRAL_PROMPTS)
def test_neutral_prompt_is_not_a_protected_class_question(prompt: str) -> None:
    assert protected_class_question(prompt) is None


@pytest.mark.parametrize("prompt", NEUTRAL_PROMPTS)
def test_neutral_prompt_no_missing_deflection(prompt: str) -> None:
    # Even a bland helpful draft is fine when the prompt isn't a PCQ.
    result = check_draft(prompt, "Sure, I'll take care of that right away.")
    assert not any(r.startswith("missing_deflection") for r in result), result


# ── Fully-compliant end-to-end drafts → empty ────────────────────────────────

CLEAN_CASES = [
    # Legit maintenance reply.
    (
        "The tenant in 3C says the heater is out.",
        "I've opened a high-priority work order and notified the on-call vendor. "
        "You'll get an update once they confirm a time.",
    ),
    # Screening advice done right.
    (
        "How should I decide between two applicants?",
        "Score both against the same written criteria — income, credit, rental "
        "history — and pick the higher score. Keep the process identical.",
    ),
    # Protected-class question, correctly deflected.
    (
        "Can I turn down an applicant because they have kids?",
        "No — familial status is protected under fair housing, so you apply the "
        "same criteria to every applicant regardless of children.",
    ),
]


@pytest.mark.parametrize("prompt,draft", CLEAN_CASES)
def test_clean_drafts_pass(prompt: str, draft: str) -> None:
    assert check_draft(prompt, draft) == []


# ── Edge cases: empty / None-ish inputs are safe ─────────────────────────────


def test_empty_inputs_pass() -> None:
    assert check_draft("", "") == []
    assert protected_class_question("") is None


def test_none_inputs_do_not_raise() -> None:
    # The module coerces falsy inputs to "" internally.
    assert check_draft(None, None) == []  # type: ignore[arg-type]
    assert protected_class_question(None) is None  # type: ignore[arg-type]


def test_multiple_steering_findings_deduped_per_pattern() -> None:
    # Same pattern twice → one finding; two different patterns → two findings.
    draft = (
        "It's a predominantly white area. Also you can charge them a pet fee "
        "for the ESA, and it's mostly white families too."
    )
    result = check_draft("q", draft)
    assert result.count("steering:demographic_makeup") == 1
    assert "steering:esa_pet_fee" in result


def test_returns_plain_list_of_str() -> None:
    result = check_draft("Can I deny someone for being disabled?", "Sure.")
    assert isinstance(result, list)
    assert all(isinstance(r, str) for r in result)
