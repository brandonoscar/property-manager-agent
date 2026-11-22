"""Hard-coded narration table + resolver (PR #70).

The dict + resolver function are the routine path for status lines on
the conversational Inbox — when the LLM-driven narration would be
overkill or too slow. These tests pin the public contract:

1. Known tool ids resolve to a non-empty string from ``TOOL_NARRATION``.
2. Unknown tool ids fall back rather than raising.
3. ``None`` tool id falls back the same way (no TypeError).
4. Every entry in ``TOOL_NARRATION`` is a non-empty present-tense
   string — guards against accidental empties / placeholder leftovers
   on future PRs that extend the table.
"""

from __future__ import annotations

from helixis.agent.narration import TOOL_NARRATION, narrate_tool


def test_known_tool_resolves_to_its_table_entry() -> None:
    assert narrate_tool("buildium.tasks.get") == "Reading the task from Buildium"
    assert narrate_tool("buildium.notes.create") == "Writing a note to Buildium"
    assert narrate_tool("buildium.tenants.get") == "Looking up the tenant"


def test_unknown_tool_falls_back_to_generic() -> None:
    assert narrate_tool("definitely.not.a.tool") == "Working on it"


def test_none_tool_falls_back_without_raising() -> None:
    assert narrate_tool(None) == "Working on it"
    assert narrate_tool("") == "Working on it"


def test_every_entry_is_a_nonempty_string() -> None:
    """Defensive — guards against an accidental empty value sneaking
    in on a future PR that adds a tool. A blank narration string is
    worse than a fallback because it disappears in the UI silently."""
    for tool_id, phrase in TOOL_NARRATION.items():
        assert isinstance(phrase, str), f"non-string narration for {tool_id}"
        assert phrase.strip(), f"empty narration for {tool_id}"
        # Sanity: should not be a placeholder like "TODO" or "FIXME".
        assert "TODO" not in phrase.upper(), f"placeholder in narration for {tool_id}"


def test_table_covers_the_executor_tool_ids() -> None:
    """The four planner tools registered in
    ``helixis.agent.handlers.HANDLERS`` map to Buildium writes —
    every one of them needs a narration entry so the user sees a
    real status line when the executor fires, not a generic fallback.
    Pinned here so a future tool added to HANDLERS without a
    narration string fails this test instead of silently dropping to
    "Working on it".
    """
    from helixis.agent.handlers import HANDLERS

    # Map planner tool_id → narration tool_id. Keep the mapping
    # explicit here so the test fails loudly if a handler is renamed.
    expected_keys = {
        "update_buildium_task": "buildium.tasks.update",
        "add_internal_note": "buildium.notes.create",
        "send_tenant_message": "buildium.messages.send",
        "create_followup_task": "buildium.tasks.create",
        "reply_to_email": "gmail.reply.send",
    }
    for handler_id in HANDLERS:
        narration_key = expected_keys.get(handler_id)
        assert narration_key, (
            f"handler {handler_id!r} has no narration mapping in this test — "
            f"add one to TOOL_NARRATION and update expected_keys."
        )
        assert narration_key in TOOL_NARRATION, (
            f"TOOL_NARRATION missing entry for {narration_key!r}"
        )
