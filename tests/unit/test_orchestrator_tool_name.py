"""Tool-name extraction from the OpenAI Agents SDK stream events (PR #72).

Pre-PR-#72 the orchestrator read ``item.raw_item.name`` on the
``tool_output`` event. That attribute doesn't exist on output items —
``raw_item`` is a ``ResponseFunctionToolCallOutput`` carrying ``call_id``
only. The name lives on the *call* event's raw_item, populated earlier
in the stream.

The fix routes both events through two helpers:

- :func:`register_tool_call` stashes the (call_id, name) pair on the
  ``tool_called`` event.
- :func:`resolve_tool_output_name` looks it up on the matching
  ``tool_output`` event, or falls back to ``"unknown_tool"`` when the
  shape drifts so the bug surfaces in logs rather than silently
  corrupting persisted history.

These tests pin the helpers' behaviour for the SDK shape the
orchestrator runs against today, plus the resilience paths for shape
drift.
"""

from __future__ import annotations

from dataclasses import dataclass

from helixis.agents.orchestrator import (
    register_tool_call,
    resolve_tool_output_name,
)


@dataclass
class _FakeCallRaw:
    """Mirrors the ``raw_item`` on a ``tool_called`` event — has both
    ``call_id`` and ``name``."""

    call_id: str
    name: str


@dataclass
class _FakeOutputRaw:
    """Mirrors the ``raw_item`` on a ``tool_output`` event — has
    ``call_id`` only. This is the shape that broke pre-PR-#72."""

    call_id: str


@dataclass
class _FakeStreamItem:
    """Wraps either ``raw_item`` shape; the helper unwraps it via
    ``getattr(item, "raw_item", None)``."""

    raw_item: object | None


def test_register_tool_call_stores_name_keyed_by_call_id() -> None:
    call_names: dict[str, str] = {}
    item = _FakeStreamItem(
        raw_item=_FakeCallRaw(call_id="abc", name="buildium_list_open_work_orders"),
    )

    register_tool_call(item, call_names)

    assert call_names == {"abc": "buildium_list_open_work_orders"}


def test_register_tool_call_ignores_missing_call_id() -> None:
    """Missing call_id means we can't look the name up later — skip the
    registration rather than persisting a junk entry."""
    call_names: dict[str, str] = {}
    item = _FakeStreamItem(
        raw_item=_FakeCallRaw(call_id="", name="some_tool"),
    )
    register_tool_call(item, call_names)
    assert call_names == {}


def test_register_tool_call_ignores_missing_name() -> None:
    """Symmetric — if the SDK gives us a call_id with no name, don't
    register an empty value. The output-side resolver will fall back
    to ``unknown_tool``."""
    call_names: dict[str, str] = {}
    item = _FakeStreamItem(raw_item=_FakeCallRaw(call_id="abc", name=""))
    register_tool_call(item, call_names)
    assert call_names == {}


def test_register_tool_call_handles_id_alias() -> None:
    """Some SDK paths expose the call id as ``.id`` rather than
    ``.call_id``. Cover both attribute names."""

    @dataclass
    class _IdOnlyRaw:
        id: str
        name: str

    call_names: dict[str, str] = {}
    item = _FakeStreamItem(raw_item=_IdOnlyRaw(id="def", name="other_tool"))
    register_tool_call(item, call_names)
    assert call_names == {"def": "other_tool"}


def test_register_tool_call_no_raw_item_is_noop() -> None:
    call_names: dict[str, str] = {}
    item = _FakeStreamItem(raw_item=None)
    register_tool_call(item, call_names)
    assert call_names == {}


def test_tool_name_recovered_from_call_id_mapping() -> None:
    """Happy path — the exact sequence the orchestrator runs at
    streaming time: register on call, look up on output."""
    call_names: dict[str, str] = {}
    register_tool_call(
        _FakeStreamItem(
            raw_item=_FakeCallRaw(call_id="abc", name="buildium_list_open_work_orders")
        ),
        call_names,
    )
    output_item = _FakeStreamItem(raw_item=_FakeOutputRaw(call_id="abc"))

    name = resolve_tool_output_name(output_item, call_names)

    assert name == "buildium_list_open_work_orders"


def test_tool_name_falls_back_to_unknown_tool_when_call_id_missing() -> None:
    """If the SDK shape drifts and ``call_id`` disappears, we fall
    back to ``"unknown_tool"`` — never the empty string, never the
    literal ``"tool"`` (the pre-PR-#72 bug)."""
    call_names: dict[str, str] = {}
    output_item = _FakeStreamItem(raw_item=_FakeOutputRaw(call_id=""))

    name = resolve_tool_output_name(output_item, call_names)

    assert name == "unknown_tool"
    assert name != ""
    assert name != "tool"


def test_tool_name_falls_back_to_unknown_tool_when_mapping_empty() -> None:
    """Same fallback when call_id is present but no matching
    ``tool_called`` was seen (stream interleaving bug, replay edge
    case)."""
    output_item = _FakeStreamItem(raw_item=_FakeOutputRaw(call_id="orphan-call"))
    name = resolve_tool_output_name(output_item, {})
    assert name == "unknown_tool"


def test_tool_name_falls_back_to_sole_inflight_call_on_call_id_mismatch() -> None:
    """The live SDK symptom: the tool_output's call_id doesn't match the
    one registered on tool_called, so the join misses — but exactly one
    tool is in flight, so its name is unambiguous. Use it instead of
    leaking ``unknown_tool`` into spans + telemetry."""
    call_names: dict[str, str] = {}
    register_tool_call(
        _FakeStreamItem(raw_item=_FakeCallRaw(call_id="real-id", name="buildium_count_units")),
        call_names,
    )
    mismatched_output = _FakeStreamItem(raw_item=_FakeOutputRaw(call_id="mismatched-id"))

    assert resolve_tool_output_name(mismatched_output, call_names) == "buildium_count_units"


def test_tool_name_stays_unknown_with_multiple_inflight_on_mismatch() -> None:
    """With several tool calls in flight and a call_id miss, guessing is
    unsafe — stay with ``unknown_tool``."""
    call_names: dict[str, str] = {}
    register_tool_call(
        _FakeStreamItem(raw_item=_FakeCallRaw(call_id="a", name="tool_a")), call_names
    )
    register_tool_call(
        _FakeStreamItem(raw_item=_FakeCallRaw(call_id="b", name="tool_b")), call_names
    )
    out = _FakeStreamItem(raw_item=_FakeOutputRaw(call_id="mismatch"))

    assert resolve_tool_output_name(out, call_names) == "unknown_tool"


def test_tool_name_uses_direct_name_attribute_when_present() -> None:
    """Future-proof: if a future SDK path exposes ``.name`` directly on
    the output item, prefer that over the fallback."""

    @dataclass
    class _DirectNameRaw:
        call_id: str
        name: str

    # Empty mapping forces the helper down to the direct-attribute
    # fallback path.
    output_item = _FakeStreamItem(raw_item=_DirectNameRaw(call_id="abc", name="direct_name_tool"))
    name = resolve_tool_output_name(output_item, {})
    assert name == "direct_name_tool"


def test_pop_after_resolve_frees_long_run_memory() -> None:
    """Smoke test for the orchestrator's pattern: register N, resolve
    + pop N. The dict stays bounded so long-running sessions don't
    leak. (The actual pop lives in run_agent_streaming; here we just
    exercise the helper interaction.)"""
    call_names: dict[str, str] = {}
    for i in range(50):
        register_tool_call(
            _FakeStreamItem(raw_item=_FakeCallRaw(call_id=f"c-{i}", name=f"tool-{i}")),
            call_names,
        )
    assert len(call_names) == 50
    for i in range(50):
        out = _FakeStreamItem(raw_item=_FakeOutputRaw(call_id=f"c-{i}"))
        assert resolve_tool_output_name(out, call_names) == f"tool-{i}"
        call_names.pop(f"c-{i}", None)
    assert call_names == {}
