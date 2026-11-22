"""Two cost levers that must not cost accuracy.

**Lookup tier** (``agent.lookup_model``) — ``effort_settings`` only ever lowered
reasoning effort; the answer was still written by the $3/$15 model when $1/$5
would do. This pins that the swap rides the SAME classifier as the effort
downgrade and ships DARK.

**History decay** (``chat_history``) — measured 2026-08-24, 24,519 uncached
input tokens per turn at the full $3/MTok. A 40-turn window where every tool row
can render 2,000 characters is ~80k characters ≈ 20k tokens, and history changes
every turn so none of it caches. Older results decay; recent ones do not.

The shared risk in both is the same, and it is the one gotcha 26 names: making
something smaller must never make a partial thing look complete.
"""

from __future__ import annotations

from typing import Any

import pytest

from helixis.agents import chat_history as ch
from helixis.llm.router import get_lookup_model
from helixis.settings import Settings


def _settings(**agent: Any) -> Settings:
    return Settings(
        env="test",
        llm={"default_model": "anthropic/claude-sonnet-5", "anthropic_api_key": "sk-test"},
        memory={"enabled": False},
        agent=agent,
    )


# ── lookup tier ─────────────────────────────────────────────────────────────


def test_the_tier_swap_ships_dark() -> None:
    """Empty means off, and off must mean no clone at all.

    Unlike the effort downgrade — which only removes thinking budget from a
    question that needs none — this changes WHICH MODEL writes the answer.
    Gotcha 10 is a standing record of Haiku failing a case the production model
    passed minutes earlier, so this is calibrate-then-enable, not on-by-default.
    """
    assert _settings().agent.lookup_model == ""
    assert get_lookup_model(_settings()) is None


def test_configuring_a_tier_returns_a_model_carrying_the_key() -> None:
    model = get_lookup_model(_settings(lookup_model="anthropic/claude-haiku-4-5"))
    assert model is not None
    assert model.model == "anthropic/claude-haiku-4-5"


def test_whitespace_only_is_off_not_a_model_named_space() -> None:
    assert get_lookup_model(_settings(lookup_model="   ")) is None


def test_an_unrecognised_provider_still_returns_a_model() -> None:
    """No Helixis key matches, so LiteLLM falls back to its own env lookup —
    the same degradation ``get_model`` already has. Failing closed here would
    make a typo in one optional setting take out every lookup turn."""
    assert get_lookup_model(_settings(lookup_model="somethingelse/model-x")) is not None


def test_the_swap_is_gated_on_the_effort_classifier() -> None:
    """Source pin: the clone must sit INSIDE the ``effort_settings`` branch.

    A tier swap on a question the classifier did not recognise is precisely the
    failure this must not have — it would silently move hard questions to the
    cheap model. Keeping one classifier and one branch is what makes the
    narrowness auditable (the ``specialist_budget`` argument, gotcha 34).
    """
    import inspect

    from helixis.agents import orchestrator

    src = inspect.getsource(orchestrator.run_agent_streaming)
    gate = src.index("effort_settings(request.task")
    swap = src.index("get_lookup_model(settings)")
    assert gate < swap, "the tier swap escaped the effort-classifier branch"

    between = src[gate:swap]
    assert "\n    if " not in between, (
        "a top-level statement separates the effort gate from the tier swap — "
        "the swap is no longer inside that branch"
    )


# ── history decay ───────────────────────────────────────────────────────────


def _tool_row(result: str) -> dict[str, Any]:
    return {"role": "tool", "content": "", "tool_name": "portfolio_overview", "tool_result": result}


def test_a_recent_result_replays_in_full() -> None:
    body = "x" * 1500
    note = ch._format_tool_note("t", body, budget=ch._TOOL_RESULT_RENDER_BYTES)
    assert body in note
    assert "not replayed" not in note


def test_an_older_result_is_compressed() -> None:
    body = "x" * 5000
    note = ch._format_tool_note("t", body, budget=ch._STALE_TOOL_RESULT_RENDER_BYTES)
    assert len(note) < 1200
    assert "x" * ch._STALE_TOOL_RESULT_RENDER_BYTES in note


def test_compression_is_always_announced() -> None:
    """The gotcha-26 rule: a truncated read is never evidence of the whole.

    Cutting an old result to 300 characters without saying so makes a fragment
    read as a complete small result — which is worse than dropping it, because
    the model will answer from it confidently.
    """
    note = ch._format_tool_note("t", "y" * 5000, budget=ch._STALE_TOOL_RESULT_RENDER_BYTES)
    assert "PARTIAL" in note
    assert "re-run the lookup" in note
    # ⚠ "PARTIAL" and "re-run for the rest" are an INVITATION to fetch more.
    # They are not a prohibition on concluding absence from what is visible,
    # and those are different sentences. This matters because compression is
    # HEAD truncation and every safety footer in this product is appended
    # LAST — measured on Blue Ridge, every stored result of every tool that
    # carries one exceeds this budget (unpaid_bills 34/34, shortest 3,332
    # chars; open_tasks 4/4 at 7,220), so the footer is always the part cut.
    # The clause below is the only thing standing in for it on a stale note.
    assert "NOT evidence of absence" in note, (
        "the compressed note no longer carries the anti-absence rule — the "
        "footers it stands in for are cut by this very truncation"
    )


def test_a_result_that_fits_is_not_labelled_partial() -> None:
    """The label must mean something. Stamping every note would train the model
    to ignore it on the notes where it is true."""
    note = ch._format_tool_note("t", "short", budget=ch._STALE_TOOL_RESULT_RENDER_BYTES)
    assert "PARTIAL" not in note


def test_the_default_budget_is_unchanged_for_callers_that_omit_it() -> None:
    big = "z" * 5000
    assert ch._format_tool_note("t", big) == ch._format_tool_note(
        "t", big, budget=ch._TOOL_RESULT_RENDER_BYTES
    )


@pytest.mark.asyncio
async def test_only_the_most_recent_tool_results_keep_full_fidelity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The decay must key on RECENCY, and rows arrive oldest-first.

    Reading the window from the wrong end would compress exactly the results
    the current question is about while replaying stale ones in full — strictly
    worse than not doing this at all, and it would look like it was working.
    """
    n = ch._FULL_FIDELITY_TOOL_NOTES + 3
    rows = [_tool_row(f"{i:03d}" + "q" * 5000) for i in range(n)]

    class _Pool:
        async def fetch(self, *_: Any, **__: Any) -> Any:
            return rows

    monkeypatch.setattr(ch, "get_pool", lambda: _Pool())
    history = await ch.load_session_history("s", "c", user_id="u")
    blob = "\n".join(h["content"] for h in history)

    # The 3 oldest are compressed, the newest _FULL_FIDELITY_TOOL_NOTES are not.
    for i in range(3):
        assert f"{i:03d}" + "q" * 400 not in blob, f"stale result {i} replayed in full"
    for i in range(3, n):
        assert f"{i:03d}" + "q" * 400 in blob, f"recent result {i} was compressed"


@pytest.mark.asyncio
async def test_decay_materially_shrinks_the_replayed_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The point is the token bill, so assert the size actually moved."""
    rows = [_tool_row("q" * 5000) for _ in range(40)]

    class _Pool:
        async def fetch(self, *_: Any, **__: Any) -> Any:
            return rows

    monkeypatch.setattr(ch, "get_pool", lambda: _Pool())
    decayed = sum(len(h["content"]) for h in await ch.load_session_history("s", "c", user_id="u"))

    # What the same window cost before the decay: every row at full budget.
    undecayed = 40 * len(ch._format_tool_note("portfolio_overview", "q" * 5000))
    # Measured at ~31k vs ~94k on this fixture — a ~67% cut on the largest
    # uncached-input line. The bar is set below that with room for the notice
    # wording to move, but high enough that removing the decay fails it.
    assert decayed < undecayed * 0.45, (
        f"decay saved too little: {decayed} vs {undecayed} characters"
    )
