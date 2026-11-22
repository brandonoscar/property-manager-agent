"""Anthropic prompt caching (#27 cost lever) — behavior-neutral billing marks."""

from __future__ import annotations

import pytest

from helixis.llm import prompt_cache
from helixis.llm.prompt_cache import _cache_mark, inject_cache_control

#: Derived, never hardcoded. The mark now carries a TTL (see
#: test_prompt_cache_ttl — the default lifetime is why 76% of a measured turn
#: was cache WRITES), and a literal here would have to be edited in lockstep
#: with every lifetime change. What these tests assert is WHERE the mark goes,
#: not what it contains; the TTL's own contract is pinned in its own file.
_MARK = _cache_mark()


def _anthropic_kwargs() -> dict:
    return {
        "model": "anthropic/claude-sonnet-5",
        "messages": [
            {"role": "system", "content": "You are the Buildium Agent."},
            {"role": "user", "content": "list open work orders"},
        ],
        "tools": [
            {"type": "function", "function": {"name": "a", "parameters": {}}},
            {"type": "function", "function": {"name": "b", "parameters": {}}},
        ],
    }


def test_marks_last_tool_and_system_message() -> None:
    # ⚠ Reads the RETURN value. This used to assert on the argument, which
    # worked only because the function happened to write through to the
    # caller's dict — the undocumented coupling that made prompt caching one
    # honest refactor away from silently switching off (2026-08-26). Same
    # property, measured through the channel the wrapper actually uses.
    marked = inject_cache_control(_anthropic_kwargs())

    assert "cache_control" not in marked["tools"][0]
    assert marked["tools"][-1]["cache_control"] == _MARK
    system = marked["messages"][0]
    assert system["content"][-1]["cache_control"] == _MARK
    assert system["content"][-1]["text"] == "You are the Buildium Agent."


def test_the_conversation_tail_is_marked_too() -> None:
    """⚠ CHANGED 2026-08-26. This assertion used to read "the user turn is
    untouched — only stable prefix chunks get marks", and that reasoning is
    what made history uncacheable.

    Anthropic caches the prefix up to the LAST breakpoint. With marks only on
    the tools tail and the system message, every message was fresh input at the
    full rate on every call — history, the question, every tool result, the
    company-knowledge and fair-housing blocks. ``chat_history`` measured its own
    slice at 24,519 uncached tokens per turn.

    The premise was wrong: within one agent run the message list IS a growing
    prefix (the SDK appends the call and its result and re-sends everything), so
    a mark at the tail on turn N is a cache HIT for that whole span on turn N+1.
    Only the delta is written.
    """
    marked = inject_cache_control(_anthropic_kwargs())
    tail = marked["messages"][-1]
    assert tail["role"] == "user"
    assert tail["content"][-1]["cache_control"] == _MARK
    assert tail["content"][-1]["text"] == "list open work orders"


def test_never_more_than_anthropics_four_breakpoints() -> None:
    """⚠ Anthropic accepts at most FOUR ``cache_control`` marks and rejects the
    whole request past that — the fan-out shape of gotcha 27, where one bad
    per-request field 400s every call in the suite. We use three; the fourth is
    deliberately spare."""
    marked = inject_cache_control(_anthropic_kwargs())

    def _count(blocks: object) -> int:
        if isinstance(blocks, list):
            return sum(1 for b in blocks if isinstance(b, dict) and "cache_control" in b) + sum(
                _count(b.get("content")) for b in blocks if isinstance(b, dict)
            )
        return 0

    total = _count(marked.get("tools")) + _count(marked.get("messages"))
    assert total <= 4, f"{total} cache breakpoints — Anthropic rejects the request above 4"
    assert total == 3, f"expected tools + system + tail = 3, got {total}"


def test_a_lone_system_message_gets_no_tail_mark() -> None:
    """The tail mark needs a real conversation. Marking a system message twice
    would spend a breakpoint on the block that already has one."""
    kwargs = _anthropic_kwargs()
    kwargs["messages"] = [{"role": "system", "content": "only me"}]
    marked = inject_cache_control(kwargs)
    assert len(marked["messages"]) == 1
    assert marked["messages"][0]["content"][-1]["cache_control"] == _MARK


def test_non_anthropic_models_pass_through_untouched() -> None:
    for model in ("gemini/gemini-3.5-flash", "openai/gpt-4o", None, 42):
        kwargs = _anthropic_kwargs()
        kwargs["model"] = model
        before = repr(kwargs)
        assert repr(inject_cache_control(kwargs)) == before


def test_idempotent_no_double_marks() -> None:
    once = inject_cache_control(_anthropic_kwargs())
    assert repr(inject_cache_control(once)) == repr(once)


def test_callers_structures_are_not_mutated() -> None:
    # The SDK / planner reuse their tools + messages lists across retries —
    # marking must copy, not mutate the caller's nested dicts.
    kwargs = _anthropic_kwargs()
    original_tools = kwargs["tools"]
    original_system = kwargs["messages"][0]
    inject_cache_control(kwargs)
    assert "cache_control" not in original_tools[-1]
    assert original_system["content"] == "You are the Buildium Agent."


def test_missing_tools_or_system_is_fine() -> None:
    kwargs = {"model": "anthropic/claude-haiku-4-5", "messages": [{"role": "user", "content": "x"}]}
    marked = inject_cache_control(kwargs)  # no tools, no system — must not raise
    assert "tools" not in marked


@pytest.mark.asyncio
async def test_wrapper_installs_once_and_forwards(monkeypatch: pytest.MonkeyPatch) -> None:
    import litellm

    seen: list[dict] = []

    async def fake_acompletion(**kwargs):
        seen.append(kwargs)
        return "resp"

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    monkeypatch.setattr(prompt_cache, "_original_acompletion", None)

    assert prompt_cache.configure_prompt_caching() is True
    wrapped = litellm.acompletion
    assert prompt_cache.configure_prompt_caching() is True  # idempotent…
    assert litellm.acompletion is wrapped  # …no double-wrap

    result = await litellm.acompletion(**_anthropic_kwargs())
    assert result == "resp"
    assert seen[0]["tools"][-1]["cache_control"] == _MARK

    # Restore module state for other tests (monkeypatch undoes litellm attr).
    monkeypatch.setattr(prompt_cache, "_original_acompletion", None)


def test_boot_chokepoints_opt_in() -> None:
    # Source pins: the retry-config boot path and the eval runner both enable
    # caching — losing either silently forfeits the ~90% cache-read discount.
    import inspect

    from helixis.evals import runner
    from helixis.llm import router

    assert "configure_prompt_caching()" in inspect.getsource(router.configure_litellm_retries)
    assert "configure_prompt_caching()" in inspect.getsource(runner.main)


# ── the accident that was load-bearing (found 2026-08-26) ───────────────────


@pytest.mark.asyncio
async def test_the_marks_reach_the_provider_through_the_wrapper() -> None:
    """⚠ Reads the SEAM, not the helper (gotcha 28).

    ``inject_cache_control`` was always correct and always tested. The wrapper
    DISCARDED its return value and the marks arrived anyway — purely because
    ``inject_tool_examples`` rebinds to the same object, so the later writes
    landed on the caller's dict. Both helpers document themselves as copying.

    So making either one honour its own docstring would have switched prompt
    caching off across the fleet with zero test failures and no error, just a
    ~10x input bill. On the live ledger that day, 89-92%% of prompt tokens were
    cache reads — the accident was carrying most of the bill.

    This asserts on what the PROVIDER receives, which is the only statement
    that cannot be satisfied by an accident.
    """
    import litellm

    from helixis.llm import prompt_cache as pc

    seen: dict[str, object] = {}

    async def _capture(*_a: object, **k: object) -> str:
        seen.update(k)
        return "ok"

    original_acompletion = litellm.acompletion
    original_state = pc._original_acompletion
    litellm.acompletion = _capture
    pc._original_acompletion = None
    try:
        pc.configure_prompt_caching()
        await litellm.acompletion(
            model="anthropic/claude-sonnet-5",
            messages=[{"role": "system", "content": "SYSTEM"}, {"role": "user", "content": "hi"}],
            tools=[
                {"type": "function", "function": {"name": "a", "parameters": {"type": "object"}}},
                {"type": "function", "function": {"name": "b", "parameters": {"type": "object"}}},
            ],
        )
    finally:
        litellm.acompletion = original_acompletion
        pc._original_acompletion = original_state

    tools = seen.get("tools")
    assert isinstance(tools, list) and "cache_control" in tools[-1], (
        "no cache breakpoint on the tool schemas — every call re-pays full input price "
        "for the whole registry"
    )
    system = seen["messages"][0]["content"]  # type: ignore[index]
    assert isinstance(system, list) and "cache_control" in system[-1], (
        "no cache breakpoint on the system prompt"
    )


def test_injection_does_not_depend_on_mutating_the_caller() -> None:
    """The property the wrapper now relies on: the marks are in the RETURN
    value. Pinning it both ways means a future refactor of either helper
    cannot quietly re-create the coupling."""
    from helixis.llm.prompt_cache import inject_cache_control

    original = {
        "model": "anthropic/claude-sonnet-5",
        "messages": [{"role": "system", "content": "SYSTEM"}],
        "tools": [{"type": "function", "function": {"name": "a", "parameters": {}}}],
    }
    marked = inject_cache_control(original)

    assert "cache_control" in marked["tools"][-1]
    assert isinstance(marked["messages"][0]["content"], list)
    # …and the caller's own dict is left alone, as the docstring says.
    assert original["messages"][0]["content"] == "SYSTEM"
