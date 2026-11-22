"""Tool Use Examples on the write surface.

Anthropic reports parameter accuracy going 72%→90% on complex parameter
handling with ``input_examples`` attached. That is exactly the class that has
hurt this product — four field-shape bugs in a month, each found by reading a
live production row rather than by a test.

The load-bearing fact these pin: **this works on the current harness.**
LiteLLM's Anthropic transform forwards unknown tool keys into the tool block
the same way it forwards ``cache_control``. The research that recommended this
feature said it required leaving LiteLLM for the native SDK; it does not, and
if a future LiteLLM bump changes that, the round-trip test below is where it
surfaces.
"""

from __future__ import annotations

from typing import Any

from helixis.llm.prompt_cache import _cache_mark, inject_cache_control
from helixis.llm.tool_examples import (
    TOOL_EXAMPLES,
    example_violations,
    inject_tool_examples,
)


def _tool(name: str) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {"name": name, "description": "x", "parameters": {"type": "object"}},
    }


def test_a_write_tool_gets_its_examples() -> None:
    out = inject_tool_examples({"tools": [_tool("buildium_record_payment")]})
    assert out["tools"][0]["input_examples"] == TOOL_EXAMPLES["buildium_record_payment"]


def test_a_read_tool_gets_nothing() -> None:
    """Examples ride the cached prefix on every call. Reads are cheap to get
    wrong and expensive to carry, so the write surface only."""
    out = inject_tool_examples({"tools": [_tool("buildium_list_tenants")]})
    assert "input_examples" not in out["tools"][0]


def test_injection_is_idempotent() -> None:
    once = inject_tool_examples({"tools": [_tool("buildium_post_charge")]})
    twice = inject_tool_examples(once)
    assert twice["tools"][0]["input_examples"] == once["tools"][0]["input_examples"]


def test_non_anthropic_calls_are_untouched() -> None:
    kwargs = {"model": "gpt-4o", "tools": [_tool("buildium_record_payment")]}
    assert "input_examples" not in inject_cache_control(kwargs)["tools"][0]


def test_the_anthropic_seam_attaches_examples_and_the_cache_mark() -> None:
    """Both hints ride one injection, and the examples land INSIDE the cached
    prefix — attaching them after the breakpoint would invalidate it."""
    out = inject_cache_control(
        {
            "model": "anthropic/claude-sonnet-5",
            "tools": [_tool("buildium_list_tenants"), _tool("buildium_close_work_order")],
        }
    )
    assert "input_examples" in out["tools"][1]
    # Derived, not literal: the mark carries a TTL now (see
    # test_prompt_cache_ttl). What THIS test asserts is that examples and the
    # cache breakpoint coexist on the same seam — not what the mark contains.
    assert out["tools"][-1]["cache_control"] == _cache_mark()


def test_litellm_forwards_examples_into_the_anthropic_tool_block() -> None:
    """The claim the whole approach rests on. If a LiteLLM bump ever drops
    unknown tool keys, this fails and the feature silently stops working —
    which is exactly how it would otherwise go unnoticed."""
    from litellm.llms.anthropic.chat.transformation import AnthropicConfig

    injected = inject_tool_examples({"tools": [_tool("buildium_record_payment")]})
    mapped = AnthropicConfig()._map_tools(injected["tools"])
    flat = mapped[0] if isinstance(mapped, tuple | list) else mapped
    block = flat[0] if isinstance(flat, list) else flat
    assert block["name"] == "buildium_record_payment"
    assert block["input_examples"] == TOOL_EXAMPLES["buildium_record_payment"]


def test_every_example_is_a_valid_call_against_the_live_schema() -> None:
    """The gate that matters. Anthropic validates each example against the
    tool's ``input_schema`` and **400s the whole request** when one fails — not
    the tool, the entire call.

    The first cut of this test compared key NAMES only, so
    ``"entry_allowed": True`` sailed through: it is a tri-state STRING
    ("Unknown"/"Yes"/"No") and the schema said so. Every Anthropic call
    carrying the Buildium tool set 400'd, and CI reported it as five unrelated
    eval cases flaking. Names are not enough — types are the failure mode."""
    from helixis.agents.buildium_tools import buildium_tools
    from helixis.agents.sms_tools import sms_tools

    registry = {t.name: t for t in (*buildium_tools(), *sms_tools())}
    for name, examples in TOOL_EXAMPLES.items():
        tool = registry.get(name)
        assert tool is not None, f"{name} has examples but is not registered"
        for example in examples:
            problems = example_violations(example, tool.params_json_schema or {})
            assert not problems, f"{name}: {'; '.join(problems)}"


def test_a_mistyped_example_is_caught_rather_than_sent() -> None:
    """The exact #860 shape: a boolean where the schema says string."""
    schema = {
        "type": "object",
        "properties": {"entry_allowed": {"type": "string"}},
        "required": ["entry_allowed"],
        "additionalProperties": False,
    }
    assert example_violations({"entry_allowed": "Yes"}, schema) == []
    assert example_violations({"entry_allowed": True}, schema)
    assert example_violations({}, schema)
    assert example_violations({"entry_allowed": "Yes", "nope": 1}, schema)


def test_a_drifted_example_costs_its_accuracy_not_the_turn() -> None:
    """If an example ever stops matching its tool, the request must still go
    out. Losing the hint is a regression; losing the turn is an outage."""
    tool = {
        "type": "function",
        "function": {
            "name": "buildium_record_payment",
            "parameters": {
                "type": "object",
                "properties": {"lease_id": {"type": "string"}},
                "additionalProperties": False,
            },
        },
    }
    out = inject_tool_examples({"tools": [tool]})
    assert "input_examples" not in out["tools"][0]


def test_unions_and_enums_are_understood() -> None:
    """The Agents SDK emits ``anyOf`` for every optional parameter, so a
    checker that didn't understand unions would reject half the write surface
    and silently strip the examples it exists to protect."""
    optional_int = {"anyOf": [{"type": "integer"}, {"type": "null"}]}
    schema = {"type": "object", "properties": {"unit_id": optional_int}}
    assert example_violations({"unit_id": 5501}, schema) == []
    assert example_violations({"unit_id": None}, schema) == []
    assert example_violations({"unit_id": "5501"}, schema)

    enum = {"type": "object", "properties": {"p": {"type": "string", "enum": ["Low", "High"]}}}
    assert example_violations({"p": "High"}, enum) == []
    assert example_violations({"p": "Urgent"}, enum)


def test_a_boolean_is_not_an_integer() -> None:
    """``isinstance(True, int)`` is True in Python and False at Anthropic."""
    schema = {"type": "object", "properties": {"n": {"type": "integer"}}}
    assert example_violations({"n": 7}, schema) == []
    assert example_violations({"n": True}, schema)


def test_only_write_tools_carry_examples() -> None:
    """A read with examples is wasted prefix; a write without them is the bug
    class this exists for."""
    write_verbs = ("create", "update", "close", "post", "record", "add", "share", "upload")
    for name in TOOL_EXAMPLES:
        assert any(verb in name for verb in write_verbs), name
