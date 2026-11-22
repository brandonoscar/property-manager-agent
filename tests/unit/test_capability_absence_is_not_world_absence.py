"""Four places where the product stated something it had not established.

All four are the same defect wearing different clothes, and it is the one this
repo keeps shipping (gotchas 17, 26, 42, 46, 51, 55, 57, and the memory pair
fixed earlier today): **a lookup that could not run, rendered as a fact about
the world.**

* ``places_nearby`` returns ``[]`` when the provider has no POI search — and
  ``census`` IS the keyless default — so "find a plumber near X" rendered as
  *"No plumber found within 5 miles"* on every call. Not an edge case: the
  default configuration, every time.
* ``_truncate_footer`` said only "… and N more not shown" for the five list
  tools that share it, while ``buildium_list_tasks`` carried a full
  anti-absence warning inline. The one list already burned by the bug was
  protected; the other five were not.
* ``render_template`` enforced the SET of placeholders and never the CONTENT,
  so ``account_notice``'s ``{notice}`` was an unbounded free-form blank on an
  A2P-registered number — while two docstrings called templates-only "a
  compliance guarantee".
* ``sms_tools`` quoted a hardcoded "8:00 AM-9:00 PM" quiet-hours window months
  after the real window became state-aware.

⚠ Every test here asserts BOTH directions. Marking every empty result as a
failure is a fresh false claim about every genuinely-empty case (gotcha 38),
and it is the mistake that makes a guard worse than none.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from helixis.agents.buildium_tools import _truncate_footer
from helixis.comms import local_time
from helixis.comms.sms import TEMPLATES, render_template
from helixis.geo.places import places_supported
from helixis.settings import Settings

# ── the truncation footer ───────────────────────────────────────────────────


def test_a_truncated_list_says_it_is_truncated() -> None:
    footer = _truncate_footer(180, 25, noun="tenant")
    assert footer is not None
    assert "TRUNCATED" in footer, "the model needs the word, not just a count"
    assert "do NOT conclude" in footer
    assert "tenant" in footer, "name what might be wrongly called absent"
    assert "25 of 180" in footer, "say how much was actually seen"


def test_an_untruncated_list_gets_no_warning() -> None:
    """The other direction. A warning on a COMPLETE list would teach the model
    to discount the warning everywhere, which costs more than it buys."""
    assert _truncate_footer(10, 25) is None
    assert _truncate_footer(25, 25) is None, "exactly at the limit is not truncated"


def test_the_footer_does_not_promise_a_search_parameter() -> None:
    """Four of the five callers take NO arguments.

    ``buildium_list_tasks`` can honestly say "re-call with search=" because it
    has one. Putting that in the SHARED footer would send the model to invent a
    parameter on the other four — a fabricated capability, which is the same
    class of lie one level down.
    """
    footer = _truncate_footer(180, 25, noun="tenant")
    assert footer is not None
    assert "search=" not in footer


# ── places: capability absence vs world absence ─────────────────────────────


def _settings_with(provider: str, api_key: str) -> Settings:
    s = Settings()
    s.maps.provider = provider
    s.maps.api_key = api_key
    return s


@pytest.mark.parametrize(
    ("provider", "api_key", "supported"),
    [
        ("census", "", False),  # the keyless DEFAULT — no POI data at all
        ("census", "k", False),  # a key does not give census POIs
        ("mapbox", "k", False),  # wired for static maps, not places
        ("google", "k", True),
        ("google", "", False),  # provider can, deployment cannot
    ],
)
def test_places_support_is_reported_honestly(provider: str, api_key: str, supported: bool) -> None:
    assert places_supported(_settings_with(provider, api_key)) is supported


def test_the_default_provider_cannot_search_places() -> None:
    """Pins the fact that made this a live bug rather than a corner.

    If this ever fails because the default gained POI search, the tool's
    branch is not wrong — but the reason it exists has changed and the message
    should be re-read.
    """
    assert not places_supported(Settings()), (
        "the default maps provider now supports places — re-read "
        "places_tools' empty-result branch before relying on it"
    )


# ── SMS: the free-form blank ────────────────────────────────────────────────


def test_a_normal_notice_still_sends() -> None:
    body = render_template("account_notice", {"notice": "Your lease renewal offer is ready."})
    assert body.startswith("Occupella: ")
    assert "Reply STOP to cancel" in body


@pytest.mark.parametrize(
    ("notice", "why"),
    [
        ("Reply START to keep getting our deals!", "forges opt-IN instructions"),
        ("Text STOP to unsubscribe from this list", "forges a second opt-out"),
        ("Great news!\nSecond line pretending to be a new message", "line break"),
        ("x" * 400, "renders past two segments"),
    ],
)
def test_an_unsendable_notice_is_refused(notice: str, why: str) -> None:
    with pytest.raises(ValueError):
        render_template("account_notice", {"notice": notice})


def test_the_opt_out_footer_is_never_the_thing_that_trips_the_check() -> None:
    """The template's OWN footer contains "Reply STOP to cancel".

    The check runs on the parameter, not the rendered body — if it ran on the
    body it would reject every single message, which is the false-positive
    direction that gets a guard deleted rather than fixed.
    """
    assert "Reply STOP" in TEMPLATES["account_notice"]
    assert render_template("account_notice", {"notice": "Roof work starts Monday."})


# ── SMS: the quiet-hours window ─────────────────────────────────────────────

_NOON_UTC = datetime(2026, 9, 2, 18, 0, tzinfo=UTC)  # a Wednesday
_SUNDAY_UTC = datetime(2026, 9, 6, 18, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("phone", "when", "expected"),
    [
        ("+15125550100", _NOON_UTC, "9:00 AM-9:00 PM"),  # TX SB 140 start
        ("+13055550100", _NOON_UTC, "8:00 AM-8:00 PM"),  # FL ends at 20:00
        ("+15555550100", _NOON_UTC, "9:00 AM-8:00 PM"),  # unknown → intersection
        ("+15125550100", _SUNDAY_UTC, "12:00 PM-9:00 PM"),  # TX Sunday noon rule
    ],
)
def test_the_described_window_matches_the_statute(
    phone: str, when: datetime, expected: str
) -> None:
    """The description and the decision must come from one table.

    A refusal that quotes a window we did not apply is worse than one that
    quotes none: it tells the customer to come back at a time we will refuse
    again.
    """
    assert local_time.describe_window(phone, when) == expected


def test_the_unknown_window_is_the_intersection_not_the_federal_default() -> None:
    """Gotcha 55's decline-to-assert, at the statute level. An unplaceable
    number must get the window legal EVERYWHERE, not the permissive federal
    floor — omission is cheap and a guess is a per-message penalty."""
    unknown = local_time.describe_window("+15555550100", _NOON_UTC)
    federal = f"{local_time.QUIET_HOURS_START.hour}:00"
    assert not unknown.startswith(federal), (
        "an unplaceable number fell back to the federal default; TX starts at 9"
    )


# ── the call site ───────────────────────────────────────────────────────────


def test_the_places_tool_actually_consults_places_supported() -> None:
    """⚠ Added because a mutation SURVIVED the first version of this file.

    Deleting the tool's capability branch left every test above green: they all
    drive ``places_supported`` directly, and a helper that is correct in
    isolation says nothing about a caller that ignores it. That is gotcha 62's
    rule — pair every behavioural test of a helper with a guard on its call
    site — and gotcha 69, where four route-level mutations were missed the same
    way.

    AST rather than a substring scan: the module docstring quotes the old
    wrong message in order to explain it, so a grep would match the
    explanation (gotchas 40/49).
    """
    import ast
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[2] / "src" / "helixis" / "agents" / "places_tools.py"
    ).read_text(encoding="utf-8")
    called = {
        node.func.id
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "places_supported" in called, (
        "places_tools no longer asks whether place search is possible, so an "
        "empty result is once again rendered as 'No <keyword> found within N "
        "miles' — a statement about the world, on the keyless default provider "
        "that has no POI data at all"
    )
