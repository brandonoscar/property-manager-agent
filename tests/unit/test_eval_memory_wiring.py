"""The eval harness can target the Orchestrator and mock the memory tools.

Guards the wiring added for #341 (the 08x memory cases) without running a live
agent: the Orchestrator must be a registered flow factory, and each memory
tool's mock target path must resolve to a real attribute (a typo there would
only surface as a confusing case-load error against live Gemini).
"""

from __future__ import annotations

import importlib

import pytest

from helixis.evals.agent_flow import _AGENT_FACTORIES, _TOOL_MOCK_TARGETS


def test_orchestrator_is_a_flow_factory() -> None:
    assert "Orchestrator" in _AGENT_FACTORIES
    assert _AGENT_FACTORIES["Orchestrator"].__name__ == "create_orchestrator"


@pytest.mark.parametrize("tool_name", ["search_memory", "store_memory"])
def test_memory_mock_target_resolves(tool_name: str) -> None:
    assert tool_name in _TOOL_MOCK_TARGETS
    target_path, _factory = _TOOL_MOCK_TARGETS[tool_name]
    module_path, attr = target_path.rsplit(".", 1)
    module = importlib.import_module(module_path)
    assert hasattr(module, attr), f"{target_path} does not resolve — mock patch would fail"
