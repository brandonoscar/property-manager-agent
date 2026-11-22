"""A connector write card the person did not approve ends that system's writes
for the rest of the run (``run_pms.pms_write_tool_enabled``, recorded by
``tool_registries.connector_tools``).

Evals 531 and 534 failed their safety gate on 2026-10-06 (#1092, #1093): after
"Okay, I didn't create the work order", the model called the same write again.
In production that is a second card in front of someone who just turned the
first one down. No prompt line bounds it (rule 13, gotcha 209); hiding the
system's writes for the rest of the run does.

These drive what the eval harness drives: each write case's own mocks, run
binding and switches (``agent_flow.run_agent_flow_case``, with the model
replaced), the agent's real tool list, and the write called once through the
SDK's ``on_invoke_tool`` inside a Task, the way the SDK runs a tool. Model-free.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from agents import RunContextWrapper
from agents.tool_context import ToolContext

from helixis.agents import run_pms
from helixis.evals import agent_flow
from helixis.pms import registry
from helixis.pms.gated_write import is_not_approved_reply

GOLDEN = Path(__file__).resolve().parents[2] / "src" / "helixis" / "evals" / "golden"

#: The write cases whose card is declined or never answered, and the ones
#: approved. Named here, not found by their mocks' words: finding them with the
#: recogniser under test would let it choose its own cases.
NOT_APPROVED = [
    "521_rentmanager_create_work_order_declined",
    "522_rentmanager_create_work_order_never_answered",
    "524_rentmanager_status_change_declined",
    "531_propertyware_create_work_order_declined",
    "532_propertyware_create_work_order_never_answered",
    "534_propertyware_edit_work_order_declined",
]
APPROVED = [
    "520_rentmanager_create_work_order_approved",
    "523_rentmanager_status_change_approved",
    "530_propertyware_create_work_order_approved",
    "533_propertyware_edit_work_order_approved",
]

#: Arguments each write's schema accepts. The mocks ignore them; the SDK
#: validates them before the tool runs.
ARGS: dict[str, dict[str, Any]] = {
    "rentmanager_create_work_order": {
        "property": "Synthetic Cedar Court",
        "title": "Rattling exhaust fan",
        "unit": "C-102",
    },
    "rentmanager_update_work_order_status": {"work_order_id": 880000701, "status": "Closed"},
    "propertyware_create_work_order": {
        "property": "Larkspur Cottage",
        "description": "Broken gate latch",
        "priority": "High",
    },
    "propertyware_update_work_order": {
        "work_order_id": 990700001,
        "description": "The leak is under the bathroom sink.",
    },
}


def _case(stem: str) -> dict[str, Any]:
    case: dict[str, Any] = json.loads((GOLDEN / f"{stem}.json").read_text(encoding="utf-8"))
    return case


def _write_of(case: dict[str, Any]) -> str:
    """The write the case caps at one card: the one its card is about."""
    (write,) = case["expected"]["approval_card_count_max"]
    return str(write)


async def _offered(agent: Any) -> set[str]:
    return {t.name for t in await agent.get_all_tools(RunContextWrapper(context=None))}


async def _call_once(agent: Any, write: str) -> Any:
    """``write`` called once through the SDK's invoke, inside a Task as the
    SDK runs a tool: a ``ContextVar.set`` in there would never reach the next
    turn, so this is what proves the record crosses that boundary."""
    tools = await agent.get_all_tools(RunContextWrapper(context=None))
    (tool,) = [t for t in tools if t.name == write]
    args = json.dumps(ARGS[write])
    ctx = ToolContext(context=None, tool_name=write, tool_call_id="c1", tool_arguments=args)
    return await asyncio.create_task(tool.on_invoke_tool(ctx, args))


async def _one_turn(stem: str, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The case's run as the harness sets it up, with the model replaced by one
    call to the case's write: what is offered before it, its reply, and what is
    offered on the next turn."""
    case = _case(stem)
    write = _write_of(case)
    seen: dict[str, Any] = {"write": write, "pms": case["input"]["pms"]}

    class _StopError(Exception):
        pass

    async def fake_run(agent: Any, *, input: Any, **_kw: Any) -> Any:  # noqa: A002
        seen["before"] = await _offered(agent)
        seen["reply"] = await _call_once(agent, write)
        seen["after"] = await _offered(agent)
        raise _StopError("probe")

    monkeypatch.setattr(agent_flow.Runner, "run", fake_run)
    result = await agent_flow.run_agent_flow_case(case)
    assert result.error == "_StopError: probe", result.error
    return seen


def _writes_of(pms: str, names: set[str]) -> set[str]:
    return {name for name in names if name.startswith(f"{pms}_")}


@pytest.mark.parametrize("stem", NOT_APPROVED)
async def test_a_card_not_approved_ends_that_systems_writes_for_the_run(
    stem: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = await _one_turn(stem, monkeypatch)
    assert seen["write"] in seen["before"], "the case's write was never offered"
    assert is_not_approved_reply(seen["reply"], registry.name_of(seen["pms"])), seen["reply"]
    assert _writes_of(seen["pms"], seen["after"]) == set(), seen["after"]
    # Only that system's writes go: everything the model answers with stays.
    assert seen["after"] == seen["before"] - _writes_of(seen["pms"], seen["before"])


@pytest.mark.parametrize("stem", APPROVED)
async def test_an_approved_card_leaves_the_writes_offered(
    stem: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = await _one_turn(stem, monkeypatch)
    assert seen["write"] in seen["before"]
    assert not is_not_approved_reply(seen["reply"], registry.name_of(seen["pms"]))
    assert seen["after"] == seen["before"]


async def test_the_next_run_offers_the_writes_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """Per run, never per company or per process: the person may ask again."""
    first = await _one_turn("531_propertyware_create_work_order_declined", monkeypatch)
    second = await _one_turn("531_propertyware_create_work_order_declined", monkeypatch)
    assert first["write"] not in first["after"]
    assert second["write"] in second["before"]


def test_outside_a_bound_run_nothing_is_recorded() -> None:
    """A tool called from a test or a script records nothing and never raises,
    and a run starts with an empty record that its end takes away."""
    run_pms.note_write_not_approved("propertyware")
    assert run_pms._RUN_NOT_APPROVED.get() is None
    token = run_pms.bind_run_pms("propertyware")
    try:
        assert run_pms._RUN_NOT_APPROVED.get() == set()
    finally:
        run_pms.reset_run_pms(token)
    assert run_pms._RUN_NOT_APPROVED.get() is None


def test_another_systems_record_hides_nothing_here() -> None:
    shown = run_pms.pms_write_tool_enabled("propertyware")
    with agent_flow._switched_on(["propertyware"]):
        token = run_pms.bind_run_pms("propertyware")
        try:
            assert shown(None, None)
            run_pms.note_write_not_approved("rentmanager")
            assert shown(None, None)
            run_pms.note_write_not_approved("propertyware")
            assert not shown(None, None)
        finally:
            run_pms.reset_run_pms(token)
