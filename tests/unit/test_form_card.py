"""``request_details`` — the assistant asking for the blanks, not writing them.

A vague request ("make a task for the leaky faucet") has real blanks in it,
and the behaviour it replaced was a paragraph of questions the user answered
with a second paragraph. What has to hold here is narrow and mostly about
what the card must NOT do: it must not become a write path, it must not
invent values to look finished, and it must not stack two forms in one turn.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from helixis.agents.visual_tools import (
    _MAX_FORM_FIELDS,
    _request_details_impl,
    begin_turn_form,
    build_form_payload,
    drain_pending_cards,
    reset_turn_form,
)

_SRC = Path(__file__).resolve().parents[2] / "src" / "helixis"


# ── payload shaping ──────────────────────────────────────────────────


def test_a_row_becomes_a_field_the_client_can_render() -> None:
    payload = build_form_payload(
        "New maintenance task",
        [
            ["what", "What needs doing", "text", "Leaky faucet"],
            ["priority", "Priority", "choice", "", "Low|Normal|High"],
        ],
        intro="A couple of details and I can create it.",
        submit_label="Create task",
    )
    assert payload["title"] == "New maintenance task"
    assert payload["submit_label"] == "Create task"
    assert payload["fields"][0] == {
        "name": "what",
        "label": "What needs doing",
        "kind": "text",
        "value": "Leaky faucet",
    }
    assert payload["fields"][1]["options"] == ["Low", "Normal", "High"]


def test_an_unknown_kind_degrades_to_a_text_box_rather_than_vanishing() -> None:
    """⚠ Dropping the field would make the model believe it asked for
    something it did not, and then wait forever for an answer that has no box
    to be typed into. A plain text input always works."""
    payload = build_form_payload("T", [["when", "When", "datetime-local", ""]])
    assert [f["kind"] for f in payload["fields"]] == ["text"]


def test_a_choice_with_no_options_degrades_rather_than_rendering_a_dead_select() -> None:
    payload = build_form_payload("T", [["priority", "Priority", "choice", "", ""]])
    assert payload["fields"][0]["kind"] == "text"
    assert "options" not in payload["fields"][0]


def test_a_form_is_capped_before_it_becomes_paperwork() -> None:
    rows = [[f"f{i}", f"Field {i}"] for i in range(20)]
    payload = build_form_payload("T", rows)
    assert len(payload["fields"]) == _MAX_FORM_FIELDS


def test_duplicate_names_are_dropped() -> None:
    """Two fields with one name means the second silently overwrites the
    first when the answers are composed — one of the questions is asked and
    never heard."""
    payload = build_form_payload("T", [["unit", "Which unit"], ["unit", "Unit again"]])
    assert [f["label"] for f in payload["fields"]] == ["Which unit"]


def test_a_row_without_a_label_is_not_rendered_as_a_nameless_box() -> None:
    payload = build_form_payload("T", [["unit", ""], ["notes", "Notes"]])
    assert [f["name"] for f in payload["fields"]] == ["notes"]


def test_the_button_always_says_something() -> None:
    assert build_form_payload("T", [["a", "A"]])["submit_label"] == "Send"


# ── the tool ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_tool_queues_a_form_and_tells_the_model_to_stop() -> None:
    """The return text is load-bearing. Without "STOP", the model shows the
    form and then asks the same four questions in prose underneath it —
    which is the behaviour the card exists to replace, now duplicated."""
    token = begin_turn_form()
    try:
        await drain_pending_cards()
        out = _request_details_impl("New task", [["what", "What needs doing"]])
        assert "STOP" in out
        assert "guess" in out.lower()

        cards = await drain_pending_cards()
        assert [etype for etype, _ in cards] == ["form"]
        assert cards[0][1]["fields"][0]["name"] == "what"
    finally:
        reset_turn_form(token)


@pytest.mark.asyncio
async def test_a_second_form_in_one_turn_is_refused() -> None:
    """Two stacked forms is a questionnaire, and the second is always the
    model asking again instead of waiting for the first answer."""
    token = begin_turn_form()
    try:
        await drain_pending_cards()
        _request_details_impl("First", [["a", "A"]])
        out = _request_details_impl("Second", [["b", "B"]])

        assert "already shown" in out
        cards = await drain_pending_cards()
        assert len(cards) == 1, "the second form must not reach the queue"
    finally:
        reset_turn_form(token)


@pytest.mark.asyncio
async def test_an_empty_form_is_not_shown() -> None:
    token = begin_turn_form()
    try:
        await drain_pending_cards()
        out = _request_details_impl("New task", [])
        assert "Nothing to show" in out
        assert await drain_pending_cards() == []
    finally:
        reset_turn_form(token)


@pytest.mark.asyncio
async def test_a_refused_form_does_not_spend_the_turn_s_one_slot() -> None:
    """A malformed call must not lock out the real one. Setting the guard
    before validating would let a bad first attempt silence the good second."""
    token = begin_turn_form()
    try:
        await drain_pending_cards()
        _request_details_impl("New task", [])
        _request_details_impl("New task", [["what", "What needs doing"]])
        assert len(await drain_pending_cards()) == 1
    finally:
        reset_turn_form(token)


# ── wiring ───────────────────────────────────────────────────────────


def _orchestrator_source() -> str:
    return (_SRC / "agents" / "orchestrator.py").read_text(encoding="utf-8")


def test_the_tool_is_registered_on_the_router() -> None:
    """A behavioural test of the tool cannot see that nothing calls it —
    gotcha 62's rule, and the `comms_triage` failure it names. Walk the AST
    rather than grepping: the import and the comments both spell the name."""
    tree = ast.parse(_orchestrator_source())
    listed = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id == "request_details"
    }
    assert listed, "request_details is imported but never placed in a tool list"


def test_the_per_turn_guard_is_armed_and_reset_by_the_streaming_loop() -> None:
    """An unreset ContextVar leaks the "already offered" flag into the next
    turn on the same task, so the SECOND vague request of a conversation
    silently gets no form at all."""
    tree = ast.parse(_orchestrator_source())
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "begin_turn_form" in called, "the once-per-turn guard is never armed"
    assert "reset_turn_form" in called, "the once-per-turn guard is never reset"


def test_the_form_hint_reaches_the_router_prompt() -> None:
    """The tool exists whether or not the model knows when to reach for it.
    Four rounds of this repo's history (gotchas 25/32/34/37) say prose alone
    cannot bound a loop — but it is still how a model LEARNS a tool is the
    right shape for a vague ask, and a hint wired to nothing teaches nothing.
    """
    tree = ast.parse(_orchestrator_source())
    used = {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name) and node.id == "FORM_HINT"
    }
    assert used, "FORM_HINT is imported but never appended to the instructions"


def test_the_form_tool_is_not_a_write_path() -> None:
    """⚠ The card must never grow the ability to act on its own answers.

    Submitting sends an ordinary message, so the Buildium write it leads to
    still passes its confirm gate, role floor and idempotency claim. A form
    that wrote directly would be a second write surface with none of those —
    exactly what ``test_write_confirm_gate.py`` exists to prevent, arriving
    through a door that file does not watch.
    """
    source = (_SRC / "agents" / "visual_tools.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    impl = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_request_details_impl"
    )
    called = {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in ast.walk(impl)
        if isinstance(node, ast.Call)
    }
    forbidden = {c for c in called if c.startswith("buildium_") or c in {"run_guarded", "post"}}
    assert not forbidden, (
        f"the form tool calls {sorted(forbidden)} — it must only ASK. "
        f"Anything that writes belongs behind a confirm gate, not behind a card."
    )
