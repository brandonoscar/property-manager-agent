"""Universal federal fair-housing reference (roadmap §6 #12, descoped to
federal-only). The reference is a constant injected into chat + CRM drafts, so
the tests pin its content (all seven protected classes + the not-legal-advice /
defer-to-state guardrails) and confirm the CRM draft system prompt carries it."""

from __future__ import annotations

from helixis.company_knowledge.federal_fair_housing import (
    FEDERAL_FAIR_HOUSING_REFERENCE,
    FEDERAL_PROTECTED_CLASSES,
    federal_fair_housing_block,
)
from helixis.crm.draft import SYSTEM_PROMPT, _system_prompt


def test_reference_names_the_act_and_all_seven_protected_classes() -> None:
    ref = FEDERAL_FAIR_HOUSING_REFERENCE.lower()
    assert "fair housing act" in ref
    assert "42 u.s.c." in ref
    for token in ("race", "color", "national origin", "religion", "sex", "disability"):
        assert token in ref, token
    # Familial status is worded with a gloss; check the phrase.
    assert "familial status" in ref
    # The tuple is the machine-readable list — seven, matching HUD.
    assert len(FEDERAL_PROTECTED_CLASSES) == 7


def test_reference_carries_the_guardrails() -> None:
    ref = FEDERAL_FAIR_HOUSING_REFERENCE.lower()
    # Information, not legal advice.
    assert "not legal advice" in ref
    # Anti-steering + identical treatment.
    assert "steer" in ref
    assert "identically" in ref
    # Defer to state/local + counsel rather than guessing per-state law.
    assert "state and local law" in ref or "state/local" in ref
    assert "counsel" in ref


def test_block_accessor_returns_the_reference() -> None:
    assert federal_fair_housing_block() == FEDERAL_FAIR_HOUSING_REFERENCE


def test_crm_draft_system_prompt_appends_the_reference() -> None:
    prompt = _system_prompt()
    # Both the draft's own instructions and the shared federal reference.
    assert SYSTEM_PROMPT in prompt
    assert FEDERAL_FAIR_HOUSING_REFERENCE in prompt
    assert "Fair Housing (Federal" in prompt


# ── The proxy-offer rule (gotcha 84) ────────────────────────────────────────
#
# ⚠ WHY THESE EXIST. Gotcha 52 found the shape — a correct protected-class
# refusal followed by "what I can help with instead: school district ratings,
# crime stats" — and the fix went into ORCHESTRATOR_INSTRUCTIONS only. Four
# surfaces consume this block (orchestrator, buildium_agent, crm/draft,
# renewals/draft) and three of them had NO proxy rule at all, including the
# one whose own docstring calls itself "the highest fair-housing-risk surface
# (outbound messages to prospects)". That is gotcha 67's shape: a rule that
# reached one lane and not the one beside it.

#: Each family the rule must name, as (label, the substrings that count as
#: naming it). Written as alternatives so a reworded prompt that still covers
#: the family passes, while dropping a family outright fails.
_PROXY_FAMILIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("crime / safety data", ("crime statistic", "crime level", "crime stats", "safety")),
    ("school ratings", ("school district", "school rating")),
    ("who lives there / how it feels", ("family-friendl", "who lives", "feels", "character")),
)


def _covers_every_proxy_family(text: str) -> list[str]:
    lowered = text.lower()
    return [label for label, needles in _PROXY_FAMILIES if not any(n in lowered for n in needles)]


def test_the_shared_block_forbids_offering_a_proxy_after_a_refusal() -> None:
    """The load-bearing one — this block is the ONLY fair-housing text the CRM
    lead drafter, the renewal drafter and the Buildium specialist ever see."""
    ref = FEDERAL_FAIR_HOUSING_REFERENCE.lower()
    assert "proxy" in ref, "the shared block no longer names the proxy-offer violation at all"
    missing = _covers_every_proxy_family(FEDERAL_FAIR_HOUSING_REFERENCE)
    assert not missing, (
        f"the shared block stopped naming these proxy families: {missing}. "
        "A prohibition that lists two of three families is an invitation to "
        "reach for the third."
    )


def test_the_shared_block_names_a_permitted_substitute() -> None:
    """⚠ Gotcha 52's actual diagnosis, and the half that is easy to drop.

    q97 did not ignore the rule — the prompt named what to refuse and never
    named a safe alternative, so "be helpful" reached for the nearest adjacent
    facts, which are the forbidden ones. *A prohibition with no permitted
    substitute is an instruction to improvise.* If the "what you MAY offer"
    paragraph is ever trimmed for brevity, this fails and says why.
    """
    ref = FEDERAL_FAIR_HOUSING_REFERENCE.lower()
    assert "may offer instead" in ref or "what you may offer" in ref
    for permitted in ("lease terms", "deposit", "commute"):
        assert permitted in ref, f"the permitted list no longer names {permitted!r}"
    assert "i can't help with that part" in ref, (
        "the block no longer says that an honest refusal is a COMPLETE reply — "
        "without it, 'be helpful' has nowhere safe to go"
    )


def test_the_shared_block_rejects_the_public_data_qualification() -> None:
    """q98 offered crime stats qualified as "public data, not framed around any
    protected class" and the rubric judge scored it 1.00 — so the judge catches
    this shape unreliably and the prompt has to name the qualifier itself."""
    ref = FEDERAL_FAIR_HOUSING_REFERENCE.lower()
    assert "public data" in ref
    assert "the tell" in ref, "the block no longer says the qualification is the tell, not the cure"


def test_the_orchestrator_and_the_shared_block_still_agree() -> None:
    """⚠ TWO COPIES OF ONE RULE, read on both sides (gotcha 28).

    The orchestrator keeps its own longer, routing-specific wording — it also
    has to say "do NOT hand off", which means nothing to a drafter — and that
    text is deliberately not moved, because it is the measured prompt and
    shuffling a tuned block buys nothing. Two copies drift; this is what stops
    them drifting in SUBSTANCE rather than in wording.
    """
    from helixis.agents.orchestrator import ORCHESTRATOR_INSTRUCTIONS

    missing = _covers_every_proxy_family(ORCHESTRATOR_INSTRUCTIONS)
    assert not missing, (
        f"the orchestrator prompt stopped naming these proxy families: {missing}, "
        "while the shared block still does. Change both or neither."
    )
    assert "proxy" in ORCHESTRATOR_INSTRUCTIONS.lower()


def test_every_surface_that_drafts_to_a_human_gets_the_rule() -> None:
    """⚠ The wiring half — a block nothing injects is the `comms_triage`
    failure (gotcha 62), and here the omission is silent by construction: a
    drafter with no proxy rule writes a plausible, helpful, unlawful sentence
    and nothing anywhere fails."""
    from helixis.renewals.draft import _system_prompt as renewal_prompt

    for label, prompt in (
        ("crm lead draft", _system_prompt()),
        ("renewal draft", renewal_prompt()),
    ):
        assert "proxy" in prompt.lower(), (
            f"{label} no longer carries the shared fair-housing block, so it has no "
            "proxy-offer rule — this is an outbound message to a real person"
        )
