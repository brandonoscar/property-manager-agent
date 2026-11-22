"""Saying the task back, without saying anything that was not asked.

The feature is one cheap parallel call that fills the first few seconds after
Enter, before the model has produced anything. The RISK is the whole reason
this file is long: a restatement that gets the request wrong is worse than a
spinner.

That is measured, not asserted. CHI 2026 (N=68) found an incorrect rationale
damages trust more than no rationale; Buell & Norton's fifth experiment found
transparency *reduces* perceived value when the outcome is unfavourable. And
this codebase's own history says it louder — gotcha 41's confident false claim
about a real person's address, gotcha 51's false tenancy claim. "I'll send the
late notice" shown to somebody who asked whether one is warranted is the agent
announcing an action it was not asked to take, in the seconds before they can
check.

So the gates are deterministic and they are the product here. Every one of
them is tested in BOTH directions (gotcha 38: test the thing it must catch and
the thing it must spare) — a gate that rejects everything is as broken as one
that rejects nothing, and only the second kind is obvious.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from typing import Any

from helixis.agents.restatement import (
    invented_digits,
    invented_write_verb,
    is_safe_restatement,
    restate,
    worth_restating,
)
from helixis.common.events import EventType, restatement_event
from helixis.settings import Settings

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


_ORCH = _ROOT / "src" / "helixis" / "agents" / "orchestrator.py"

_ASK = (
    "check whether 4B is behind on rent and whether they have open maintenance "
    "before I decide on a late notice"
)


# ── the dangerous shape: a read restated as an action ─────────────────


def test_a_write_verb_the_user_never_used_is_caught() -> None:
    """⚠ THE failure this feature could cause. The user asked whether a notice
    is warranted; the line says one is being sent."""
    assert invented_write_verb(_ASK, "Sending the late notice to 4B.") == "send"
    assert not is_safe_restatement(_ASK, "Sending the late notice to 4B.")


def test_a_write_verb_the_user_did_use_is_allowed() -> None:
    """⚠ The other direction, and the one that gets forgotten. If the request
    is "draft a late notice for 4B", a restatement that says "Drafting..." is
    correct — rejecting it would make the gate fire on every write request and
    the feature would silently never appear where it is most useful."""
    ask = "draft a late notice for 4B and send it to the tenant"
    assert invented_write_verb(ask, "Drafting a late notice for 4B and sending it.") is None
    assert is_safe_restatement(ask, "Drafting a late notice for 4B and sending it.")


def test_the_verb_match_is_on_stems_not_substrings() -> None:
    """⚠ Both halves matter. "sending" in the line must be caught by "send" in
    the request (or the gate cries wolf on every write request), and "recorded"
    must not be found inside "unrecorded" (or it fires on prose that has
    nothing to do with writing)."""
    assert invented_write_verb("please send a reminder", "Sending a reminder.") is None
    # A stem that only appears INSIDE another word is not a use of that verb.
    assert invented_write_verb("what is unrecorded here", "Looking at what is unrecorded.") is None


# ── invented numbers ──────────────────────────────────────────────────


def test_a_number_the_user_did_not_write_is_caught() -> None:
    """An invented figure is an amount, a unit or a record id the user never
    typed — the identifier class (gotcha 38) arriving through a paraphrase."""
    assert invented_digits(_ASK, "Checking the $1,830 balance on 4B.")
    assert not is_safe_restatement(_ASK, "Checking the $1,830 balance on 4B.")


def test_numbers_the_user_did_write_survive() -> None:
    """Rejecting these would drop the restatement on every question that names
    a unit — which is most of them."""
    assert invented_digits(_ASK, "Checking whether 4B is behind on rent.") == set()
    assert is_safe_restatement(_ASK, "Checking whether 4B is behind on rent.")


# ── the remaining gates ───────────────────────────────────────────────


def test_a_restatement_that_grows_into_a_summary_is_dropped() -> None:
    """Padding is where an addition hides. A line materially longer than the
    request is no longer a restatement of it."""
    assert not is_safe_restatement("is 4B late?", "Looking at " + ("the situation " * 40))


def test_the_models_own_refusal_is_honoured() -> None:
    """The prompt tells it to answer SKIP rather than invent, and a literal
    "SKIP" rendered to the user would be worse than either."""
    assert not is_safe_restatement(_ASK, "SKIP")
    assert not is_safe_restatement(_ASK, "skip — nothing to add")
    assert not is_safe_restatement(_ASK, "")
    assert not is_safe_restatement(_ASK, "   ")


# ── when it should not fire at all ────────────────────────────────────


def test_a_one_fact_lookup_is_not_worth_restating() -> None:
    """⚠ Cost AND noise. These answer in seconds, so there is no wait to fill,
    and "Looking up the rent on 4B" tells the user nothing they did not just
    type. Skipping them also means the cheap path stays free of an extra model
    call, which is the whole point of ``classify_effort``."""
    assert not worth_restating("what's the rent on 4B")
    assert not worth_restating("")
    assert not worth_restating("   ")


def test_a_substantive_question_is() -> None:
    assert worth_restating(_ASK)
    assert worth_restating("compare maintenance spend across my properties this quarter")


# ── the call, and its failure modes ───────────────────────────────────


def _settings() -> Settings:
    return Settings()


def _reply(text: str) -> Any:
    msg = type("M", (), {"content": text})()
    return type("R", (), {"choices": [type("C", (), {"message": msg})()]})()


def test_a_good_line_comes_back() -> None:
    async def fake(**_: Any) -> Any:
        return _reply("Checking whether 4B is behind on rent and what maintenance is open.")

    got = asyncio.run(restate(_ASK, _settings(), completion_fn=fake))
    assert got == "Checking whether 4B is behind on rent and what maintenance is open."


def test_a_line_that_fails_a_gate_returns_nothing_not_the_line() -> None:
    """⚠ Silence is the safe default and always was. The answer is the
    product; this is a nicety, and there is no version of "roughly right" that
    beats not showing it."""

    async def fake(**_: Any) -> Any:
        return _reply("Sending the late notice to 4B now.")

    assert asyncio.run(restate(_ASK, _settings(), completion_fn=fake)) is None


def test_a_provider_failure_costs_the_line_and_nothing_else() -> None:
    async def boom(**_: Any) -> Any:
        raise RuntimeError("provider down")

    assert asyncio.run(restate(_ASK, _settings(), completion_fn=boom)) is None


def test_a_slow_call_is_abandoned_rather_than_waited_on() -> None:
    """⚠ Past its window the turn has already produced real signal, and a
    restatement arriving behind the answer is noise. It must never be able to
    hold anything up."""

    async def slow(**_: Any) -> Any:
        await asyncio.sleep(5)
        return _reply("too late")

    assert asyncio.run(restate(_ASK, _settings(), completion_fn=slow, timeout_s=0.05)) is None


def test_an_unrecognised_response_shape_is_survived() -> None:
    async def weird(**_: Any) -> Any:
        return {"not": "a completion"}

    assert asyncio.run(restate(_ASK, _settings(), completion_fn=weird)) is None


def test_systemexit_does_not_take_down_the_event_loop() -> None:
    """⚠ Runs its OWN ``asyncio.run`` on purpose. Under pytest-asyncio,
    asserting the result is None passes against the BROKEN version too — the
    loop teardown happens outside the test's assertion. asyncio re-raises
    ``SystemExit`` out of a Task into the running loop as well as to the
    awaiter, so it has to be converted inside the coroutine (gotcha 36), and
    only a test that watches the runner itself return can see the difference.
    """

    async def exits(**_: Any) -> Any:
        raise SystemExit(1)

    async def main() -> str | None:
        return await restate(_ASK, _settings(), completion_fn=exits)

    assert asyncio.run(main()) is None


# ── the wire, and the other side of it (gotcha 28) ────────────────────


def test_restatement_is_its_own_event_type() -> None:
    e = restatement_event("Checking whether 4B is behind on rent.")
    assert e.type == EventType.RESTATEMENT
    # Not a narration and not a thought: it is addressed to the USER and
    # renders above the trail at readable size, rather than at 11.5px inside a
    # collapsible pill.
    assert EventType.RESTATEMENT not in (EventType.NARRATION, EventType.REASONING)


def _orch_calls(name: str) -> list[ast.Call]:
    tree = ast.parse(_ORCH.read_text(encoding="utf-8"))
    return [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name
    ]


def test_the_orchestrator_dispatches_it_and_gates_it() -> None:
    """⚠ Both calls. ``restate`` alone would fire on every turn including the
    trivial ones the cheap path exists to keep cheap; ``worth_restating``
    alone would decide and never dispatch."""
    assert _orch_calls("restate"), "nothing dispatches the restatement — the feature is inert"
    assert _orch_calls("worth_restating"), (
        "the restatement is not gated — a one-fact lookup would pay for an "
        "extra model call to be told what it just typed"
    )


def test_the_published_line_goes_through_the_identifier_scrubber() -> None:
    for call in _orch_calls("restatement_event"):
        assert call.args, "restatement_event called with no content"
        arg = call.args[0]
        assert isinstance(arg, ast.Call) and getattr(arg.func, "id", "") == "scrub_reply_text", (
            "the restatement is published unscrubbed — it is model-written "
            "text reaching the screen, like every other channel here"
        )


def test_it_is_dropped_once_the_answer_has_started() -> None:
    """A restatement arriving behind the answer is noise. The flag has to be
    set on the OUTPUT delta — the point at which the user is reading real
    content — not at some earlier stage of the run."""
    src = _ORCH.read_text(encoding="utf-8")
    assert 'answer_started["yes"] = True' in src, (
        "nothing closes the restatement's window — it can now land after the "
        "answer has begun streaming"
    )
    delta_at = src.index('if data_type == "response.output_text.delta"')
    assert 'answer_started["yes"] = True' in src[delta_at : delta_at + 900], (
        "the window is closed somewhere other than the first answer token"
    )
    assert 'not answer_started["yes"]' in src, "the publish does not check the window"


def test_the_client_renders_it_above_the_trail() -> None:
    chat = _projection_source(_ROOT).read_text(encoding="utf-8")
    assert "'restatement'" in chat, "runnerFold.ts drops restatement events on the floor"

    thread = (
        _ROOT / "frontend" / "src" / "components" / "conversation" / "ConversationThread.tsx"
    ).read_text(encoding="utf-8")
    assert "case 'restatement'" in thread, "the renderer has no restatement branch"
    assert "flushNarrationPill();" in thread
