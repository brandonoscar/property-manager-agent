"""Template merge helpers — filling a saved message template with a lead's
details (``build_lead_merge_context`` / ``fill_template`` / ``unfilled_tokens``).

These pure helpers seed the CRM's "a lead lands in draft" first reply; they carry
no I/O, so they're tested directly.
"""

from __future__ import annotations

from helixis.crm.template_merge import (
    build_lead_merge_context,
    fill_template,
    unfilled_tokens,
)


def test_fill_template_fills_known_tokens() -> None:
    ctx = build_lead_merge_context(name="Jordan Reyes", unit="Maple Court 4B")
    body = fill_template("Hi {first_name}, {property} is still available.", ctx)
    assert body == "Hi Jordan, Maple Court 4B is still available."


def test_fill_template_leaves_unknown_tokens_intact() -> None:
    ctx = build_lead_merge_context(name="Jordan Reyes")
    body = fill_template("Hi {first_name}, book here: {scheduling_link}.", ctx)
    # Unfilled token is left visible (braces intact) for the PM to complete.
    assert "{scheduling_link}" in body
    assert body.startswith("Hi Jordan,")


def test_unfilled_tokens_reports_blanks_sorted() -> None:
    ctx = build_lead_merge_context(name="Jordan")
    tokens = unfilled_tokens("{scheduling_link} {property} {first_name}", ctx)
    assert tokens == ["property", "scheduling_link"]


def test_merge_context_first_name_and_empty_omitted() -> None:
    ctx = build_lead_merge_context(name="  Priya Nair ", unit=None)
    assert ctx["first_name"] == "Priya"
    assert ctx["name"] == "Priya Nair"
    assert "property" not in ctx  # no unit → token stays unfilled
