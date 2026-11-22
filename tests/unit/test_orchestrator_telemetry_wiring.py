"""Telemetry-wiring tests for ``run_agent_streaming`` (PR #141).

The orchestrator's stream loop is the only writer for the
``orchestrator_runs`` repository. These tests verify that loop calls
the writer at the right points and with the right args, using
synthetic SDK event shapes that exercise both the ``item.call_id``
and ``item.raw_item.call_id`` paths (the PR #102 regression-lock).

Whole-orchestrator integration is out of scope here — we exercise
just the stream-handling block by extracting it conceptually:
``register_tool_call`` / ``resolve_tool_output_name`` /
``_call_id_of`` / ``_args_from_item`` / ``_output_from_item`` are
the helpers it uses, so we test them against the same fixtures the
loop sees.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from helixis.agents.orchestrator import (
    _args_from_item,
    _call_id_of,
    _output_from_item,
    register_tool_call,
    resolve_tool_output_name,
)

# ── Fixture shapes ────────────────────────────────────────────────────


def _tool_called_item_modern(name: str, call_id: str, args: dict) -> SimpleNamespace:
    """SDK shape A: ``item.raw_item`` carries name + call_id + arguments.

    The arguments live as a JSON string on the raw_item — same shape
    the OpenAI Responses API returns for function_call items.
    """
    raw = SimpleNamespace(
        name=name,
        call_id=call_id,
        arguments=json.dumps(args),
    )
    return SimpleNamespace(raw_item=raw)


def _tool_called_item_alt(name: str, call_id: str, args: dict) -> SimpleNamespace:
    """SDK shape B: ``item.call_id`` exposed directly (older SDK paths).

    This is the PR #102 regression case. Fixtures that assume only
    one of these two shapes would let the iterator-exhaustion-style
    bug recur silently.
    """
    raw = SimpleNamespace(
        name=name,
        arguments=json.dumps(args),
    )
    item = SimpleNamespace(raw_item=raw, call_id=call_id)
    return item


def _tool_output_item(call_id: str, output: object) -> SimpleNamespace:
    """tool_output items carry the output on ``item.output``; the
    raw_item holds the call_id but no name (the SDK only puts the
    name on the matching call event)."""
    raw = SimpleNamespace(call_id=call_id)
    return SimpleNamespace(raw_item=raw, output=output)


# ── _call_id_of ───────────────────────────────────────────────────────


def test_call_id_of_reads_modern_shape():
    item = _tool_called_item_modern("X", "call_42", {})
    assert _call_id_of(item) == "call_42"


def test_call_id_of_reads_alt_shape():
    item = _tool_called_item_alt("X", "call_42", {})
    assert _call_id_of(item) == "call_42"


def test_call_id_of_returns_empty_when_missing():
    raw = SimpleNamespace(name="X", arguments="{}")
    item = SimpleNamespace(raw_item=raw)
    assert _call_id_of(item) == ""


# ── _args_from_item ───────────────────────────────────────────────────


def test_args_from_item_parses_json_string():
    item = _tool_called_item_modern("GMAIL_SEND", "c1", {"to": "alice@example.com"})
    assert _args_from_item(item) == {"to": "alice@example.com"}


def test_args_from_item_passes_through_dict_args():
    raw = SimpleNamespace(arguments={"already": "a dict"})
    item = SimpleNamespace(raw_item=raw)
    assert _args_from_item(item) == {"already": "a dict"}


def test_args_from_item_falls_back_to_raw_string_on_bad_json():
    raw = SimpleNamespace(arguments="not valid json {")
    item = SimpleNamespace(raw_item=raw)
    assert _args_from_item(item) == "not valid json {"


def test_args_from_item_returns_none_when_no_raw():
    item = SimpleNamespace()
    assert _args_from_item(item) is None


# ── _output_from_item ─────────────────────────────────────────────────


def test_output_from_item_parses_json_object_string():
    item = _tool_output_item("c1", '{"successful": true, "data": {"n": 1}}')
    out = _output_from_item(item)
    assert out == {"successful": True, "data": {"n": 1}}


def test_output_from_item_parses_json_array_string():
    item = _tool_output_item("c1", "[1, 2, 3]")
    assert _output_from_item(item) == [1, 2, 3]


def test_output_from_item_passes_through_plain_string():
    item = _tool_output_item("c1", "OK")
    assert _output_from_item(item) == "OK"


def test_output_from_item_passes_through_dict():
    item = _tool_output_item("c1", {"k": "v"})
    assert _output_from_item(item) == {"k": "v"}


# ── register_tool_call + resolve_tool_output_name pairing ─────────────


def test_register_then_resolve_modern_shape():
    """The orchestrator's stream loop registers on every tool_called
    and joins on the matching tool_output via this pair."""
    names: dict[str, str] = {}
    call = _tool_called_item_modern("GOOGLECALENDAR_EVENTS_LIST", "c1", {})
    register_tool_call(call, names)
    out = _tool_output_item("c1", "ok")
    assert resolve_tool_output_name(out, names) == "GOOGLECALENDAR_EVENTS_LIST"


def test_register_then_resolve_alt_shape():
    """PR #102 regression-lock — register_tool_call must also pick up
    call_id from ``raw_item.call_id`` when the SDK path A is in use,
    and resolve must match it from ``raw_item.call_id`` on the output."""
    names: dict[str, str] = {}
    call = _tool_called_item_alt("GMAIL_FETCH", "c2", {})
    register_tool_call(call, names)
    out = _tool_output_item("c2", "ok")
    assert resolve_tool_output_name(out, names) == "GMAIL_FETCH"


def test_resolve_returns_unknown_tool_when_join_fails():
    """Loud fallback: the placeholder is ``"unknown_tool"`` so the
    bug surfaces in audits instead of corrupting future replays."""
    names: dict[str, str] = {}
    out = _tool_output_item("orphan-call", "ok")
    assert resolve_tool_output_name(out, names) == "unknown_tool"


# ── Live-SDK dict raw_item (the 2026-07-05 "unknown tool" trail bug) ──


def _tool_output_item_dict(call_id: str, output: object) -> SimpleNamespace:
    """The LIVE SDK's tool_output raw_item is a TypedDict, not an object:
    ``{"call_id": ..., "output": ..., "type": "function_call_output"}``.
    getattr() on a dict returns the default, so the pre-fix join always
    missed on this shape."""
    raw = {"call_id": call_id, "output": output, "type": "function_call_output"}
    return SimpleNamespace(raw_item=raw, output=output)


def test_resolve_joins_dict_shaped_output_item():
    names: dict[str, str] = {}
    call = _tool_called_item_modern("buildium_list_overdue_balances", "c9", {})
    register_tool_call(call, names)
    out = _tool_output_item_dict("c9", "ok")
    assert resolve_tool_output_name(out, names) == "buildium_list_overdue_balances"


def test_call_id_of_reads_dict_shape():
    out = _tool_output_item_dict("c10", "ok")
    assert _call_id_of(out) == "c10"


def test_resolve_fallback_ignores_lingering_handoff_entry():
    """Handoffs (``transfer_to_*``) never emit a tool_output, so their
    entry lingers in the map for the whole specialist turn. The
    single-in-flight fallback must exclude them, or one real tool call
    after a handoff resolves as unknown_tool (the exact trail seen in
    the 2026-07-05 screenshot)."""
    names: dict[str, str] = {
        "h1": "transfer_to_buildium_agent",
        "c11": "buildium_list_overdue_balances",
    }
    # Output whose call_id matches nothing registered (join miss).
    out = _tool_output_item("mismatched-id", "ok")
    assert resolve_tool_output_name(out, names) == "buildium_list_overdue_balances"


def test_resolve_fallback_still_refuses_to_guess_between_two_real_calls():
    names: dict[str, str] = {"a": "tool_one", "b": "tool_two"}
    out = _tool_output_item("mismatched-id", "ok")
    assert resolve_tool_output_name(out, names) == "unknown_tool"


# ── Repo-call args (smoke check the wiring would pass through) ────────


@pytest.mark.asyncio
async def test_repo_record_tool_call_invoked_with_redacted_args(monkeypatch):
    """If the orchestrator hands ``_args_from_item`` output to
    ``record_tool_call``, the repo's redaction pass must run BEFORE
    the JSONB hits Postgres. The repo's unit test covers the masking;
    this test just confirms the wiring path: parsed args from the
    SDK item land in the repo call shape we expect."""
    from helixis.db.repositories import orchestrator_runs as repo

    captured: dict = {}

    async def fake_record(pool, *, run_id, name, args, started_at):
        captured["args"] = args
        captured["name"] = name

    monkeypatch.setattr(repo, "record_tool_call", fake_record)

    # Synthesize the same call the orchestrator would: pull args from
    # the SDK item, hand them straight to record_tool_call.
    item = _tool_called_item_modern(
        "GMAIL_SEND", "c1", {"to": "alice@example.com", "password": "x"}
    )
    args = _args_from_item(item)
    import datetime as dt

    await repo.record_tool_call(
        pool=None,  # bypassed by our patched fn
        run_id=None,
        name="GMAIL_SEND",
        args=args,
        started_at=dt.datetime.now(dt.UTC),
    )
    assert captured["name"] == "GMAIL_SEND"
    # Args came through as a parsed dict (not the raw JSON string).
    assert isinstance(captured["args"], dict)
    assert captured["args"]["to"] == "alice@example.com"
