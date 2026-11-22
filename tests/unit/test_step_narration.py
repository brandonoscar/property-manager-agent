"""What the user reads while a turn runs.

A live turn showed "Called buildium agent" five times across three minutes.
That is a machine name split on underscores, and it cannot distinguish
progress from a hang — so the honest reading from outside is "this is broken".

⚠ **The guard reads the LIVE REGISTRY, not hand-written inputs** (gotcha 45).
There is already an older map, ``agent/narration.py::TOOL_NARRATION``, whose
17 keys are dotted legacy ids (``buildium.tasks.get``) while every tool that
reaches ``tool_start`` is named ``buildium_get_property``. It matched NOTHING
on this path and nothing failed to say so. A test feeding this module a few
made-up names would pass just as happily against exactly that defect; what has
to be measured is coverage of the names the product actually emits.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from helixis.agents.buildium_tools import buildium_tools
from helixis.agents.step_narration import narrate_empty, narrate_step

_ROOT = Path(__file__).resolve().parents[2]


def _projection_source(root: Path) -> Path:
    """The store/stream → conversation projection chat AND the Inbox render.

    It lived in ChatThread.tsx until 2026-09-30, when it moved to
    runnerFold.ts so the Inbox could use it too. Re-pointed, not relaxed: the
    consumers are asserted here so a projection nobody calls cannot pass.
    """
    components = root / "frontend" / "src" / "components"
    chat = (components / "ChatThread.tsx").read_text(encoding="utf-8")
    inbox = (components / "InboxPage.tsx").read_text(encoding="utf-8")
    assert "foldMessagesToConversation(" in chat, "ChatThread no longer uses the projection"
    assert "foldInboxStream(" in inbox, "the Inbox no longer uses the projection"
    return components / "conversation" / "runnerFold.ts"


_GENERIC = "Working on it"


def _live_tool_names() -> list[str]:
    names = sorted(t.name for t in buildium_tools())
    assert len(names) > 40, "this scan is not reading the live registry"
    return names


# ── coverage against what actually ships ──────────────────────────────


def test_every_live_buildium_tool_gets_a_real_sentence() -> None:
    """The corpus check. A tool falling through to the generic fallback is a
    step the user cannot follow, and the fallback is the state the OLD map was
    in for every single one of these."""
    unnamed = [n for n in _live_tool_names() if narrate_step(n) == _GENERIC]
    assert not unnamed, (
        f"{len(unnamed)} live tool(s) have no sentence and would read as "
        f"'{_GENERIC}': {unnamed[:10]}"
    )


def test_no_sentence_leaks_a_machine_name() -> None:
    """⚠ The whole point. "buildium_get_property" or "buildium get property"
    on screen is the defect, not a lesser form of it — a reader cannot tell a
    tool name from a stack trace."""
    offenders = []
    for name in _live_tool_names():
        sentence = narrate_step(name)
        if "_" in sentence or sentence.lower().startswith("buildium "):
            offenders.append((name, sentence))
    assert not offenders, f"machine-shaped narration: {offenders[:5]}"


def test_the_specialists_are_named_for_what_they_do() -> None:
    """These are the five lines the three-minute turn actually showed. Each is
    a whole nested agent run, so they are on screen longest and matter most."""
    assert narrate_step("buildium_agent") == "Checking Buildium"
    assert narrate_step("drive_agent") == "Reading company documents"
    assert narrate_step("gmail_agent") == "Checking email"


def test_sentences_are_present_continuous() -> None:
    """The line is on screen WHILE the work happens. Past tense there reads as
    a claim that the step finished."""
    for name in _live_tool_names():
        first = narrate_step(name).split()[0]
        assert first.endswith("ing"), f"{name} -> {narrate_step(name)!r} is not in progress"


# ── the empty case, which must not read as an answer ──────────────────


def test_an_empty_step_says_what_was_empty_and_does_not_call_it_an_answer() -> None:
    """⚠ A step that returned no rows is NOT evidence the record does not
    exist. This product's worst failure mode is a confident absence (gotcha
    26), and narration the user reads while it happens should not seed that
    reading."""
    line = narrate_empty("buildium_list_open_work_orders")
    assert "nothing came back" in line
    assert "no work orders" not in line.lower()
    assert "none" not in line.lower()


def test_unknown_tools_degrade_to_vague_not_to_a_machine_name() -> None:
    """A tool nobody has classified is better described vaguely than
    precisely-and-uselessly."""
    assert narrate_step("some_future_thing_nobody_mapped").startswith(("Checking", "Working"))
    assert narrate_step(None) == _GENERIC
    assert narrate_step("") == _GENERIC


# ── both sides of the boundary (gotcha 28) ────────────────────────────


def test_the_orchestrator_sends_the_sentence_not_the_tool_name() -> None:
    """⚠ AST on the CALL, because the fallback is the bug. `tool_start_event`
    takes a description the client renders; it used to be an f-string of the
    machine name, and a behavioural test of ``narrate_step`` cannot see a
    caller that ignores it (gotcha 62)."""
    src = (_ROOT / "src" / "helixis" / "agents" / "orchestrator.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "tool_start_event"
    ]
    assert calls, "no tool_start_event call found -- this scan is not reading the orchestrator"
    for call in calls:
        assert len(call.args) >= 2, "tool_start_event needs a description"
        described = call.args[1]
        assert isinstance(described, ast.Call) and getattr(described.func, "id", "") == (
            "narrate_step"
        ), (
            "tool_start_event's description is not narrate_step(...) — the user "
            "would read a machine name again"
        )


def test_the_client_prefers_the_backend_sentence() -> None:
    """The other half of the same boundary. The description rode the event all
    along and the client discarded it in favour of re-splitting the raw name."""
    src = _projection_source(_ROOT).read_text(encoding="utf-8")
    assert re.search(r"te\.data\?\.description", src), (
        "runnerFold.ts no longer reads the description off tool_start — it would "
        "fall back to humanising the machine name for every step"
    )
    # The fallback must SURVIVE: a resumed stream can replay events emitted
    # before this landed, and a blank line is worse than a machine name.
    assert "humaniseTool(toolName)" in src, "the pre-description fallback was removed"


def test_the_old_legacy_map_is_left_alone() -> None:
    """⚠ ``agent/narration.py`` is still CORRECT for its own caller (the
    legacy planner path in ``api/routes/buildium.py``). It covers nothing on
    the chat path, which is why this module exists — but deleting it would
    break a live surface that does key on those dotted ids."""
    from helixis.agent.narration import TOOL_NARRATION

    assert TOOL_NARRATION, "the legacy map was emptied"
    assert all("." in key for key in TOOL_NARRATION), (
        "the legacy map now holds underscore names — the two maps have started "
        "to overlap and one of them should be retired deliberately"
    )


def test_the_legacy_map_would_not_have_covered_the_chat_path() -> None:
    """The measurement that identified the defect, kept so the claim in this
    module's docstring cannot quietly become false."""
    from helixis.agent.narration import TOOL_NARRATION

    live = set(_live_tool_names())
    assert not (live & set(TOOL_NARRATION)), (
        "the legacy map now covers live chat tools — re-check which map the "
        "orchestrator should be using before adding entries to this one"
    )


# ── connector slugs must not render as machine names ────────────────────
#
# A live turn showed the user "Checking the GMAIL FETCH EMAILS", forty times.
# The mechanical rule is built for our own lower_snake_case names: strip a
# vendor prefix, read the first word as a verb, and failing that say
# "Checking the <rest>" — which on a SCREAMING_SNAKE Composio slug is the
# machine name with spaces in it.


def test_a_connector_slug_never_reaches_the_user_as_a_machine_name() -> None:
    from helixis.agents.step_narration import narrate_step

    for slug in (
        "GMAIL_FETCH_EMAILS",
        "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID",
        "GOOGLEDRIVE_LIST_FILES",
        "GOOGLECALENDAR_FIND_EVENT",
        "GOOGLESHEETS_BATCH_GET",
    ):
        line = narrate_step(slug)
        assert "_" not in line, f"{slug} rendered with an underscore: {line}"
        assert line.upper() != line, f"{slug} rendered as shouting: {line}"
        # The specific shape of the bug: the slug's own words, spaced out.
        assert slug.split("_", 1)[0] not in line, f"{slug} named its toolkit raw: {line}"


def test_an_unknown_connector_is_described_not_named() -> None:
    """⚠ DERIVED, like the cap. Composio tools are fetched per user at runtime
    — drive alone carries ~186 — so a hand-written table would rot on the next
    toolkit. A connector nobody has mapped still has to read as English."""
    from helixis.agents.step_narration import narrate_step

    line = narrate_step("FRESHBOOKS_LIST_INVOICES")
    assert line == "Checking a connected account", line


def test_our_own_tools_are_unchanged_by_the_connector_branch() -> None:
    # Both directions (gotcha 38): the new branch must not capture anything
    # it was not written for.
    from helixis.agents.step_narration import narrate_step

    assert narrate_step("buildium_get_property") == "Looking up the property"
    assert narrate_step("gmail_agent") == "Checking email"
    assert narrate_step("delinquency_report") == "Pulling who is behind on rent"


# ── the running commentary contract ─────────────────────────────────────
#
# The pipe was already built: pre-tool commentary becomes a `thinking_event`,
# reasoning deltas become `reasoning_event`, and both land on the trail in
# stream order. What was missing was a reason for the model to produce any.
# The instruction existed as one bullet directly under "Be concise. Property
# managers are busy." — so it narrated once and stopped, and a live turn
# showed a single line followed by forty tool names.


def test_the_prompt_asks_for_commentary_before_and_after_every_move() -> None:
    """⚠ Pins the three properties, not the wording.

    A prompt is prose and cannot fail, so the only way to notice this
    regressing is to compare it against what the product needs (gotcha 48).
    Deleting any one of these is a silent return to a blank screen.
    """
    from helixis.agents.orchestrator import ORCHESTRATOR_INSTRUCTIONS

    lowered = ORCHESTRATOR_INSTRUCTIONS.lower()
    # 1. Narrate before acting.
    assert "before every move" in lowered
    # 2. Narrate after, INCLUDING an empty result — "nothing in email" is the
    #    line that separates working from hung.
    assert "after every move" in lowered
    assert "nothing came back" in lowered or "even when nothing" in lowered
    # 3. Say so when the plan changes. A plan that never visibly adapts reads
    #    as a script rather than as reasoning.
    assert "changes your plan" in lowered


def test_the_prompt_forbids_announcing_that_it_is_thinking() -> None:
    """ "Thinking" was the first thing on screen and often the only thing for
    ninety seconds: true, useless, indistinguishable from a hang."""
    from helixis.agents.orchestrator import ORCHESTRATOR_INSTRUCTIONS

    assert "never announce that you are thinking" in ORCHESTRATOR_INSTRUCTIONS.lower()
