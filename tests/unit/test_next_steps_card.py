"""The work loop's hand-off — ``suggest_next_steps``.

Answering is not finishing. A manager who just learned who's behind on rent
has a next move, and a product that stops at the answer makes them think of it
themselves every time. These pin the shape of the offer, and the one thing
that must never drift: a step carries the REAL request, so clicking it asks
what the assistant just proposed rather than something vaguer.
"""

from __future__ import annotations

from helixis.agents.visual_tools import (
    _MAX_NEXT_STEPS,
    NEXT_STEPS_HINT,
    build_next_steps_payload,
)


def test_a_step_keeps_its_label_and_its_prompt() -> None:
    payload = build_next_steps_payload(
        [["Draft the late notices", "Draft late notices for the ten 61-90 day accounts"]]
    )
    assert payload["steps"] == [
        {
            "label": "Draft the late notices",
            "prompt": "Draft late notices for the ten 61-90 day accounts",
        }
    ]


def test_a_half_written_step_is_dropped_not_rendered_dead() -> None:
    """A chip with no prompt does nothing when clicked, which is worse than
    not offering it — the user learns the chips are decoration."""
    payload = build_next_steps_payload(
        [["Just a label"], ["", "prompt with no label"], ["Real", "Real request"]]
    )
    assert payload["steps"] == [{"label": "Real", "prompt": "Real request"}]


def test_the_offer_is_capped() -> None:
    """Past a handful this stops being a recommendation and becomes a menu —
    the thing it exists to replace."""
    payload = build_next_steps_payload([[f"Step {i}", f"Do thing {i}"] for i in range(10)])
    assert len(payload["steps"]) == _MAX_NEXT_STEPS


def test_the_note_offer_rides_along() -> None:
    payload = build_next_steps_payload(
        [["Send them", "Send the notices"]],
        note_hint="Note that Whitfield's leak blocks collections",
    )
    assert payload["note_hint"] == "Note that Whitfield's leak blocks collections"


def test_no_note_hint_means_no_key_rather_than_an_empty_one() -> None:
    payload = build_next_steps_payload([["Send them", "Send the notices"]])
    assert "note_hint" not in payload


def test_the_prompt_tells_the_model_to_also_say_it_in_prose() -> None:
    """The founder's ask was explicitly BOTH surfaces. Every other card tells
    the model to stay quiet about it; this one must not, or the answer loses
    its recommendation and becomes a bare chip row."""
    assert "in prose" in NEXT_STEPS_HINT
    assert "suggest_next_steps" in NEXT_STEPS_HINT
    # And the note habit — offer, never write unasked.
    assert "Never" in NEXT_STEPS_HINT and "without being asked" in NEXT_STEPS_HINT


def test_the_tool_is_registered_on_the_agent() -> None:
    from helixis.agents.buildium_agent import create_buildium_agent
    from helixis.settings import Settings

    agent = create_buildium_agent(Settings())
    assert "suggest_next_steps" in {t.name for t in agent.tools if hasattr(t, "name")}
    assert "Finish like a colleague" in agent.instructions


def test_one_row_per_turn_no_matter_how_many_specialists_answer() -> None:
    """A turn that touches Buildium AND Gmail would otherwise stack two chip
    rows — the wallpaper failure the cap prevents, arriving by another door."""
    import asyncio

    from helixis.agents.visual_tools import (
        _suggest_next_steps_impl,
        begin_turn_next_steps,
        drain_pending_cards,
        reset_turn_next_steps,
    )

    async def _run() -> list[tuple[str, dict]]:
        await drain_pending_cards()  # clear anything a prior test queued
        token = begin_turn_next_steps()
        try:
            _suggest_next_steps_impl([["First", "Do the first thing"]])
            second = _suggest_next_steps_impl([["Second", "Do the second thing"]])
            assert "already offered" in second
            return await drain_pending_cards()
        finally:
            reset_turn_next_steps(token)

    cards = asyncio.run(_run())
    assert len([c for c in cards if c[0] == "next_steps"]) == 1


def test_an_unbound_turn_still_works() -> None:
    """Scripts and tests run without the per-run binding. Suppressing a card
    is never worth breaking a caller, so unbound degrades to allow."""
    import asyncio

    from helixis.agents.visual_tools import _suggest_next_steps_impl, drain_pending_cards

    async def _run() -> str:
        await drain_pending_cards()
        out = _suggest_next_steps_impl([["A", "Do A"]])
        await drain_pending_cards()
        return str(out)

    assert "Next steps shown" in asyncio.run(_run())


def test_the_specialists_that_should_offer_next_steps_do() -> None:
    """Buildium, Gmail, Calendar and Drive all write user-facing answers with
    a real next move. Browser, web and document deliberately do NOT — see the
    exclusions noted in the PR: browser's prompt is safety-gated by the 06x
    sweep, web handles untrusted content (a suggested step derived from a
    scraped page is an injection surface), and document extraction has no
    next move of its own — the orchestrator owns what happens to the fields."""
    from helixis.settings import Settings

    settings = Settings()
    from helixis.agents.buildium_agent import create_buildium_agent
    from helixis.agents.calendar_agent import create_calendar_agent
    from helixis.agents.drive_agent import create_drive_agent
    from helixis.agents.gmail_agent import create_gmail_agent

    for build in (
        create_buildium_agent,
        create_gmail_agent,
        create_calendar_agent,
        create_drive_agent,
    ):
        agent = build(settings)
        assert "Finish like a colleague" in agent.instructions, agent.name

    from helixis.agents.web_agent import create_web_agent

    web = create_web_agent(settings)
    assert "suggest_next_steps" not in {t.name for t in web.tools if hasattr(t, "name")}
