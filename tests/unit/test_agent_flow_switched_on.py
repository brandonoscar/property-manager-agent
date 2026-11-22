"""``input.switched_on``: a golden case can switch a connector's writes on for
its own run.

A connector built on ``helixis.pms`` shows its write tools only on its own
system's run AND with its deployment writes switch on
(``tool_registries.connector_tools``), and an eval process leaves every switch
off. Without this key a write case runs with the tools it asks about hidden,
and passes or fails for nothing (gotcha 35).

No connector has tools on ``main`` yet, so a stand-in
``helixis.agents.rentmanager_tools`` module stands for Rent Manager's, loaded
through the real import path (as ``test_tool_registries`` does). The harness
runs for real, with ``Runner.run`` replaced by a probe that records what the
model would have been handed, then stops.
"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any

import pytest
from agents import RunContextWrapper, function_tool

from helixis.agents.tool_registries import tools_module_name
from helixis.evals import agent_flow
from helixis.pms import registry

KEY = registry.RENTMANAGER
WRITE = "rentmanager_create_work_order"


@function_tool
async def rentmanager_create_work_order(title: str) -> str:
    """Stand-in connector write; never called."""
    return title


@pytest.fixture(autouse=True)
def stand_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rent Manager's tools module, as a connector writes it."""
    module = ModuleType(tools_module_name(KEY))
    module.rentmanager_tools = lambda: [rentmanager_create_work_order]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module.__name__, module)


class _StopError(Exception):
    """Raised by the probe once it has looked; the harness reports it."""


def _probe(seen: dict[str, Any]) -> Any:
    async def fake_run(agent: Any, *, input: Any, **_kw: Any) -> Any:  # noqa: A002
        tools = await agent.get_all_tools(RunContextWrapper(context=None))
        seen["tools"] = {t.name for t in tools}
        seen["writes_on"] = registry.writes_enabled(KEY)
        raise _StopError("probe")

    return fake_run


def _case(**input_over: Any) -> dict[str, Any]:
    return {
        "id": "switch-probe",
        "input": {
            "agent": "Buildium Agent",
            "user_message": "Open a work order for the leaking sink in unit 4.",
            "pms": KEY,
            **input_over,
        },
        "expected": {},
    }


@pytest.mark.asyncio
async def test_a_case_that_switches_writes_on_shows_them(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(agent_flow.Runner, "run", _probe(seen))

    result = await agent_flow.run_agent_flow_case(_case(switched_on=[KEY]))

    assert result.error == "_StopError: probe"
    assert seen["writes_on"] is True
    assert WRITE in seen["tools"]
    # For that run only: the next case starts with every switch off again.
    assert registry.writes_enabled(KEY) is False


@pytest.mark.asyncio
async def test_without_it_the_writes_stay_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(agent_flow.Runner, "run", _probe(seen))

    await agent_flow.run_agent_flow_case(_case())

    assert seen["writes_on"] is False
    assert WRITE not in seen["tools"]


@pytest.mark.asyncio
async def test_the_switch_is_not_the_companys_system(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Buildium company's run never shows Rent Manager's writes, switch or no
    switch: the deployment switch and the company's system are separate."""
    seen: dict[str, Any] = {}
    monkeypatch.setattr(agent_flow.Runner, "run", _probe(seen))
    case = _case(switched_on=[KEY])
    del case["input"]["pms"]

    await agent_flow.run_agent_flow_case(case)

    assert seen["writes_on"] is True
    assert WRITE not in seen["tools"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    [KEY, ["buildium"], ["rentvine"], ["RentManager"], [1], {KEY: True}],
    ids=["a bare string", "buildium", "rentvine", "misspelt", "a number", "a mapping"],
)
async def test_anything_but_a_list_of_connectors_is_a_case_error(
    monkeypatch: pytest.MonkeyPatch, value: Any
) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(agent_flow.Runner, "run", _probe(seen))

    result = await agent_flow.run_agent_flow_case(_case(switched_on=value))

    assert result.error and "case.input.switched_on" in result.error
    assert seen == {}, "the case ran anyway"
