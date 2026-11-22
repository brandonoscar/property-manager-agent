"""``agents/tool_registries.py``: the agent's tool registries listed once, and
each connector built on ``helixis.pms`` found by itself and shown only where
it may act.

No connector has tools on ``main`` yet, so the real list of connector tools is
empty here and a test over it alone would prove nothing (rule 10). Each rule
is driven through a stand-in ``helixis.agents.rentmanager_tools`` module,
loaded through the real import path.
"""

from __future__ import annotations

import importlib.abc
import importlib.util
import sys
from collections.abc import Iterator
from types import ModuleType
from typing import Any

import pytest
from agents import function_tool

from helixis.agents import run_pms, tool_registries
from helixis.agents.tool_registries import (
    connector_tools,
    tools_module_name,
    write_registry_tools,
)
from helixis.pms import registry, sync_cycle
from helixis.settings import Settings

KEY = registry.RENTMANAGER
NAME = "rentmanager_create_work_order"


@function_tool
async def rentmanager_create_work_order(title: str) -> str:
    """Stand-in connector write; never called."""
    return title


@pytest.fixture
def stand_in(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Rent Manager's tools module, as a connector would write it."""
    module = ModuleType(tools_module_name(KEY))
    module.rentmanager_tools = lambda: [rentmanager_create_work_order]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return module


@pytest.fixture
def switches(monkeypatch: pytest.MonkeyPatch) -> dict[str, bool]:
    """Rent Manager's two switches, read at call time like the real ones."""
    state = {"enabled": True, "writes": True}

    def _settings() -> Settings:
        base = Settings()
        return base.model_copy(
            update={
                "rentmanager": base.rentmanager.model_copy(
                    update={"enabled": state["enabled"], "writes_enabled": state["writes"]}
                )
            }
        )

    monkeypatch.setattr("helixis.settings.get_settings", _settings)
    return state


def _shown_on(run: str | None, tool: Any) -> bool:
    token = run_pms.bind_run_pms(run)
    try:
        return bool(tool.is_enabled(None, None))
    finally:
        run_pms.reset_run_pms(token)


def test_a_connectors_tools_join_as_copies(stand_in: ModuleType) -> None:
    tools = [t for t in connector_tools() if t.name == NAME]
    assert len(tools) == 1
    assert tools[0] is not rentmanager_create_work_order
    # The connector's own object is left as it wrote it.
    assert rentmanager_create_work_order.is_enabled is True


@pytest.mark.parametrize(
    ("run", "enabled", "writes", "shown"),
    [
        ("rentmanager", True, True, True),
        ("rentmanager", True, False, False),
        ("rentmanager", False, True, False),
        ("propertyware", True, True, False),
        ("buildium", True, True, False),
        ("rentvine", True, True, False),
        (None, True, True, False),
    ],
)
def test_a_connector_tool_shows_only_on_its_own_run_with_writes_on(
    stand_in: ModuleType,
    switches: dict[str, bool],
    run: str | None,
    enabled: bool,
    writes: bool,
    shown: bool,
) -> None:
    """Set by the registry, not by the connector: the stand-in's tool is a
    plain ``function_tool`` that would show everywhere on its own."""
    switches.update(enabled=enabled, writes=writes)
    (tool,) = [t for t in connector_tools() if t.name == NAME]
    assert _shown_on(run, tool) is shown


@pytest.mark.parametrize("run", [None, "buildium", "rentvine"])
def test_no_connector_tool_ever_shows_on_a_buildium_or_rentvine_run(
    switches: dict[str, bool], run: str | None
) -> None:
    """Over the REAL connector tools, with every switch on: a Buildium or
    Rentvine customer's prompt never grows (ADR 0010). Empty on ``main``; it
    holds each connector's tools the day they land."""
    assert [t.name for t in connector_tools() if _shown_on(run, t)] == []


def test_the_one_list_holds_every_registry_and_each_connector(stand_in: ModuleType) -> None:
    names = {t.name for t in write_registry_tools()}
    assert {
        "buildium_create_work_order",
        "send_text_message",
        "rentvine_update_work_order_status",
        NAME,
    } <= names


def test_the_cost_census_counts_a_connector_write(stand_in: ModuleType) -> None:
    """``cost_drift`` reads the same list, so a connector's write is filed as a
    write turn, never as a read (the silent failure its comment names)."""
    from helixis.evals.cost_drift import write_tool_names

    assert NAME in write_tool_names()


def test_the_agent_carries_a_connectors_tool_hidden_from_a_buildium_run(
    stand_in: ModuleType, switches: dict[str, bool]
) -> None:
    """The production call site (``create_buildium_agent``), not only the
    helper: a registry nobody wires in is a tool every negative case forbids
    for free (gotcha 35)."""
    from helixis.agents.buildium_agent import create_buildium_agent
    from helixis.settings import get_settings

    carried = [t for t in create_buildium_agent(get_settings()).tools if t.name == NAME]
    assert len(carried) == 1
    # The call budget wraps every tool by copying it; the visibility survives.
    assert _shown_on("buildium", carried[0]) is False
    assert _shown_on(KEY, carried[0]) is True


class _FailingInside(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Finds the tools module, then fails while running it: a dependency the
    module imports is missing."""

    def __init__(self, target: str) -> None:
        self.target = target

    def find_spec(self, fullname: str, path: Any, target: Any = None) -> Any:
        if fullname == self.target:
            return importlib.util.spec_from_loader(fullname, self)
        return None

    def create_module(self, spec: Any) -> None:
        return None

    def exec_module(self, module: ModuleType) -> None:
        raise ModuleNotFoundError("No module named 'vendor_sdk'", name="vendor_sdk")


@pytest.fixture
def failing_inside(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    target = tools_module_name(KEY)
    monkeypatch.delitem(sys.modules, target, raising=False)
    monkeypatch.setattr(sys, "meta_path", [_FailingInside(target), *sys.meta_path])
    yield


def test_a_tools_module_that_fails_inside_raises_never_reads_as_none(
    failing_inside: None,
) -> None:
    """Hiding it would take the connector's writes off the agent in silence."""
    with pytest.raises(ModuleNotFoundError, match="vendor_sdk"):
        connector_tools()


def test_a_tools_module_that_does_not_exist_is_nothing() -> None:
    assert tool_registries._module("no_such_system") is None


def test_a_tools_module_without_its_factory_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    module = ModuleType(tools_module_name(KEY))
    monkeypatch.setitem(sys.modules, module.__name__, module)
    with pytest.raises(TypeError, match="rentmanager_tools"):
        connector_tools()


def test_every_system_the_registry_looks_for_is_a_toolkit_system() -> None:
    """The keys searched are the sync cycle's: an empty list would make every
    test above that reads the real connectors pass for nothing."""
    # Re-pointed 2026-10-06 (rule 19): DoorLoop joined the layer. It has no
    # tools module yet, which ``_module`` reads as nothing.
    assert set(sync_cycle.systems()) == {
        registry.RENTMANAGER,
        registry.PROPERTYWARE,
        registry.DOORLOOP,
    }
    assert tool_registries._module(registry.DOORLOOP) is None
