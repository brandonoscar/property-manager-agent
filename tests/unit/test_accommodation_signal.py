"""A reasonable-accommodation request must not be answered as a pet question.

This product defends fair housing thoroughly on the OUTPUT side and read
nothing on the INPUT side: `buildium_post_charge` refuses a pet fee for an
assistance animal, `federal_fair_housing_block` is injected into the
write-holding specialist, `fair_housing_gate.check_draft` scans drafted
replies. Every one of those reads text WE produced. Nothing read what the
resident sent, so

    "My doctor prescribed an emotional support animal — can you waive the
     pet deposit?"

arrived as an ordinary message and the drafted reply was about the pet policy.
Under the FHA that message starts an interactive process and the deposit is
not chargeable. Gotcha 86's sentence pointed the other way: a rule enforced
where we WRITE protects the repository; only a rule where we READ protects
the person who sent it.

⚠ **THE ERROR ASYMMETRY IS INVERTED relative to `check_draft`, and every case
below is chosen for it.** There a false positive fires on the best reply in
the corpus and gets the guard switched off (gotchas 52/86). Here a false
negative is an FHA complaint and a false positive is one advisory line. So
this leans permissive — but not unboundedly, because a caveat that fires when
it is not true trains its reader to discount the ones that are (gotchas
57/93). Every firing case has its silent twin.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from helixis.security.accommodation_signal import (
    ACCOMMODATION_SIGNAL,
    accommodation_basis,
    accommodation_floor_signal,
    accommodation_notice,
)

_PLANNER = Path("src/helixis/agent/planner/action_planner.py")
_EMAIL = Path("src/helixis/webhooks/email_triage.py")
_COMMS = Path("src/helixis/webhooks/comms_triage.py")


# ── it fires on the messages that carry a BASIS ──────────────────────────

#: Tier 1 — terms of art. These need no request frame: "I have a service
#: animal" is not a question and still starts the interactive process.
_STRONG = [
    "I'd like to request a reasonable accommodation for my unit.",
    "My doctor prescribed an emotional support animal.",
    "I have a service dog and wanted to let you know before I move in.",
    "This is a reasonable modification request under the Fair Housing Act.",
    "Hi — attaching my ESA letter.",
    "I'm asking about an assistance animal for the apartment.",
]

#: Tier 2 — a disability or medical basis PLUS a request for a change.
_WEAK_PLUS_REQUEST = [
    "I use a wheelchair and need a reserved parking space near the entrance.",
    "Because of my mobility issues, could you install a grab bar in the bathroom?",
    "My doctor says I need to transfer to a ground-floor unit.",
    "I have a chronic illness — requesting an exception to the guest policy.",
    "I'm disabled and would like you to waive the carpet requirement.",
]


@pytest.mark.parametrize("text", _STRONG)
def test_a_term_of_art_fires_on_its_own(text: str) -> None:
    assert accommodation_basis(text) is not None, text


@pytest.mark.parametrize("text", _WEAK_PLUS_REQUEST)
def test_a_basis_beside_a_request_fires(text: str) -> None:
    assert accommodation_basis(text) is not None, text


# ── and stays SILENT on the messages that carry none ─────────────────────

#: ⚠ These are the cases that decide whether the signal survives contact with
#: real traffic. A caveat on ordinary maintenance and pet mail is one people
#: stop reading, and then it is not there on the day it matters (gotcha 52).
_SILENT = [
    # A pet question is not an accommodation request. This is the single most
    # important negative: it is the message the signal is most likely to be
    # confused with, and flagging every one would put an FHA warning on the
    # pet policy inbox.
    "Can I have a dog in the apartment? What's the pet deposit?",
    "What's the pet rent for a cat?",
    # A basis word with NO request — a maintenance report, not a request for
    # a change to a rule or a unit.
    "The wheelchair ramp out front is broken and needs repair.",
    "There's a handicapped parking sign down in the lot.",
    # A request with NO basis.
    "Could you install a second smoke detector in the hallway?",
    "I'd like to transfer to a bigger unit when one opens up.",
    "Requesting an exception to the quiet hours for my birthday party.",
    # Ordinary traffic.
    "The heater in 4B stopped working.",
    "Rent is going to be two days late this month, sorry.",
    "Water pouring through my ceiling — please send someone now.",
]


@pytest.mark.parametrize("text", _SILENT)
def test_an_ordinary_message_stays_silent(text: str) -> None:
    assert accommodation_basis(text) is None, text


def test_empty_input_is_silent_rather_than_raising() -> None:
    assert accommodation_basis(None) is None
    assert accommodation_basis("", None, "   ") is None
    assert accommodation_floor_signal(None, None) is None


def test_a_non_string_field_does_not_take_down_the_card() -> None:
    """⚠ The first cut tested ``str(p).strip()`` and then joined the raw ``p``,
    so anything not already a ``str`` raised `TypeError` out of ``join`` — on
    the LIVE planner path, where an advisory signal must never be the reason a
    card fails to render (gotcha 7: an enhancement is never load-bearing).

    Found by an unrelated planner test whose ``enriched`` is a MagicMock, and
    only by the FULL suite — the file's name carries none of this iteration's
    keywords, so the working selection could not see it (gotchas 105/107)."""
    from unittest.mock import MagicMock

    assert accommodation_basis(MagicMock(), MagicMock()) is None
    assert accommodation_basis(12345, None) is None
    # And a real basis still fires when it arrives beside a non-string field.
    assert accommodation_basis(MagicMock(), "I have a service dog.") is not None


def test_the_parts_are_judged_together_not_separately() -> None:
    """A subject line of "ESA request" with a body that never repeats the term
    is ONE message. Judging the fields separately loses exactly the split that
    a real email has."""
    assert accommodation_basis("Parking", "I'm disabled and need a closer space") is not None
    # The basis in one field and the request in the other — neither fires alone.
    assert accommodation_basis("I have a mobility impairment") is None
    assert accommodation_basis(None, "Can you please install it?") is None
    assert (
        accommodation_basis("I have a mobility impairment", "Can you please install it?")
        is not None
    )


# ── the ESA abbreviation is case-SENSITIVE, deliberately ─────────────────


def test_the_esa_abbreviation_is_uppercase_only() -> None:
    """⚠ Gotcha 38's rule (a two-letter state code under IGNORECASE matches any
    two letters) and gotcha 42's (a bare "drive" reading as Google Drive).
    Lower-cased, "esa" is a Spanish word and a fragment of ordinary text — the
    direction that fires on everything and gets the signal switched off."""
    assert accommodation_basis("Attaching my ESA letter") is not None
    assert accommodation_basis("Dile a esa persona que llame") is None
    assert accommodation_basis("the mesa in the courtyard is cracked") is None


# ── the words the reader actually sees ───────────────────────────────────


def test_the_floor_line_names_the_duty_and_the_two_things_not_to_do() -> None:
    line = accommodation_floor_signal("My doctor prescribed an emotional support animal")
    assert line is not None
    assert "REASONABLE ACCOMMODATION" in line
    assert "Fair Housing" in line
    # The two failures this exists to prevent, in the manager's own terms.
    assert "pet fee" in line
    assert "review it before anything is sent" in line


def test_the_basis_is_a_phrase_and_not_a_boolean() -> None:
    """⚠ A flag a reader has to decode is one they decode wrongly (gotcha 94).
    The line says WHY it fired, so a manager can dismiss a false positive
    in one read instead of distrusting every future one."""
    basis = accommodation_basis("I use a wheelchair and need reserved parking")
    assert isinstance(basis, str) and len(basis) > 10
    assert (
        accommodation_floor_signal("I use a wheelchair and need reserved parking", None) is not None
    )
    assert basis in (
        accommodation_floor_signal("I use a wheelchair and need reserved parking") or ""
    )


def test_the_two_tiers_give_different_reasons() -> None:
    """⚠ Collapsing both tiers onto one phrase reads as a real basis and is a
    false statement about which one fired: a wheelchair-parking request
    labelled "assistance animal language" looks like a bad match and gets
    dismissed, which is the caveat losing its credibility (gotchas 57/93). A
    mutation returning one constant from both branches passed every other
    assertion in this file — the hole was here."""
    strong = accommodation_basis("My doctor prescribed an emotional support animal")
    weak = accommodation_basis("I use a wheelchair and need reserved parking")
    assert strong and weak and strong != weak
    assert "animal" in strong
    assert "disability" in weak or "medical" in weak


def test_the_planner_notice_states_the_duty_and_never_the_answer() -> None:
    """⚠ Telling the planner what the law requires of the manager is a FACT.
    Telling it to grant the request would be advice this product is not
    qualified to give; telling it to refuse would be the violation itself."""
    notice = accommodation_notice("assistance animal language")
    assert "OCCUPELLA NOTE (from the system, not from the sender)" in notice
    assert "REASONABLE ACCOMMODATION" in notice
    # It forbids the three specific things, and promises nothing.
    assert "pet fee" in notice and "pet deposit" in notice
    assert "Do NOT deny, approve or promise an outcome" in notice
    assert "do not ask for a diagnosis or medical records" in notice
    assert "assistance animal language" in notice


def test_the_reason_code_is_stable() -> None:
    """Anything that later counts these reads the constant, not a retyped
    string (gotcha 73: "approved" spelled out in three modules)."""
    assert ACCOMMODATION_SIGNAL == "possible_accommodation_request"


# ── it is wired at all three reading surfaces ────────────────────────────
#
# ⚠ A behavioural test of the helper cannot see a caller that ignores it —
# gotchas 62/66/69/103 are four separate instances of exactly that, and the
# floor-vs-prompt split here means a caller could silently drop EITHER half
# while the other went on working.


def _fn(path: Path, name: str) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {path}")


def test_the_planner_prompt_carries_the_notice() -> None:
    """⚠ It asserts the notice is APPENDED TO ``prompt``, not merely that the
    name appears. A mutation that kept the call and discarded its result —
    ``_ = accommodation_notice(basis)`` — passed the substring version while
    the live prompt lost the notice entirely. Gotcha 40's trap arriving
    inside the guard written to catch this, and the planner has no
    behavioural test to fall back on, so this IS the coverage."""
    fn = _fn(_PLANNER, "_build_user_prompt")
    appended = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.AugAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "prompt"
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "accommodation_notice"
    ]
    assert appended, (
        "the planner prompt no longer APPENDS the accommodation notice, so a "
        "drafted reply can quote a pet fee for an assistance animal"
    )
    assert "accommodation_basis" in ast.unparse(fn)


def test_the_planner_notice_sits_outside_the_untrusted_fence() -> None:
    """⚠ It is an operator-authored instruction ABOUT the message, not part of
    it. Appended inside `_fenced_untrusted_fields`' block it would be
    something a sender could appear to write — golden 435's ingress, and the
    one change that turns this from a defence into an injection surface."""
    fn = _fn(_PLANNER, "_build_user_prompt")
    fence_lines = [
        node.lineno
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_fenced_untrusted_fields"
    ]
    notice_lines = [
        node.lineno
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "accommodation_notice"
    ]
    assert fence_lines and notice_lines
    assert min(notice_lines) > max(fence_lines), (
        "the accommodation notice moved above/into the untrusted fence — it "
        "would then read as sender-authored text"
    )


@pytest.mark.parametrize(
    ("path", "fn_name"),
    [
        (_EMAIL, "_signals_with_floor"),
        (_COMMS, "build_comms_action_surface"),
    ],
)
def test_both_deterministic_floors_carry_the_line(path: Path, fn_name: str) -> None:
    """⚠ A FLOOR, for the reason the comms hazard line is one: the planner is
    an LLM call that times out, hits a spend ceiling (gotcha 62) or returns
    invalid JSON, and the card still renders from the deterministic surface.
    If this signal lived only in the planner prompt, the messages that most
    need it would lose it exactly when the model was unavailable — gotcha 92's
    shape, where the salvage path was blind to the runs it existed for."""
    src = ast.unparse(_fn(path, fn_name))
    assert "accommodation_floor_signal" in src, (
        f"{path.name}::{fn_name} no longer adds the accommodation floor line, "
        "so the card renders with no note when the planner is down"
    )


# ── end to end on both surfaces ──────────────────────────────────────────


def test_an_email_surface_carries_the_line_ahead_of_the_enrichment_signals() -> None:
    from helixis.webhooks.email_triage import InboundEmail, build_email_action_surface

    email = InboundEmail(
        message_id="m1",
        from_email="dana@example.com",
        subject="Pet deposit",
        body_text=(
            "My doctor prescribed an emotional support animal — can you waive the pet deposit?"
        ),
        received_at="2026-09-11T12:00:00Z",
    )
    surface = build_email_action_surface(email, signals=["Rent is current"])
    assert "REASONABLE ACCOMMODATION" in surface["signals"][0]
    assert surface["signals"][-1] == "Rent is current"


def test_an_ordinary_email_surface_gains_nothing() -> None:
    from helixis.webhooks.email_triage import InboundEmail, build_email_action_surface

    email = InboundEmail(
        message_id="m2",
        from_email="dana@example.com",
        subject="Pet policy",
        body_text="Can I have a dog in the apartment? What's the pet deposit?",
        received_at="2026-09-11T12:00:00Z",
    )
    surface = build_email_action_surface(email, signals=["Rent is current"])
    assert surface["signals"] == ["Rent is current"]


def _comms(body: str) -> object:
    from helixis.comms.contact_resolver import ResolvedContact
    from helixis.webhooks.comms_triage import InboundComms

    return InboundComms(
        channel="sms",
        from_number="+15551234567",
        to_number="+15559876543",
        body=body,
        occurred_at="2026-09-11T12:00:00Z",
        contact=ResolvedContact(
            kind="tenant",
            buildium_id=1,
            name="Dana Reyes",
            phone="(555) 123-4567",
            company_id="co-1",
        ),
        sid="SM1",
    )


def test_a_text_surface_carries_the_line() -> None:
    from helixis.webhooks.comms_triage import build_comms_action_surface

    surface = build_comms_action_surface(_comms("I have a service dog moving in with me."))  # type: ignore[arg-type]
    assert any("REASONABLE ACCOMMODATION" in s for s in surface["signals"])


def test_an_ordinary_text_surface_gains_nothing() -> None:
    from helixis.webhooks.comms_triage import build_comms_action_surface

    surface = build_comms_action_surface(_comms("Can I have a dog? What's the pet deposit?"))  # type: ignore[arg-type]
    assert not any("REASONABLE ACCOMMODATION" in s for s in surface["signals"])


def test_a_hazard_still_outranks_the_accommodation_line() -> None:
    """⚠ A gas leak is the one thing that outranks this. The ordering is the
    claim, so it is asserted rather than left to the order of two `if`s."""
    from helixis.webhooks.comms_triage import build_comms_action_surface

    surface = build_comms_action_surface(  # type: ignore[arg-type]
        _comms("I smell gas in the kitchen and I have a service animal in the unit.")
    )
    signals = surface["signals"]
    assert len(signals) >= 2
    assert "REASONABLE ACCOMMODATION" not in signals[0], (
        "the accommodation line displaced the hazard line from the top of the card"
    )
    assert any("REASONABLE ACCOMMODATION" in s for s in signals)
