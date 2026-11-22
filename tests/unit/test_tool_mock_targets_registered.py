"""Every tool a golden case mocks must be registered — read BOTH sides.

A case naming a mock whose target is absent from ``_TOOL_MOCK_TARGETS`` does
not fail loudly: the runner raises ``unknown tool ... in tool_mocks``, which is
a runner ERROR, which classifies as QUALITY, which for a quarantined case is
NON-GATING. So the case prints a ``[FAIL]`` nobody has to act on and never
executes again. **A case that has never run is not a passing case; it is an
unmonitored one** — and it is worse than a missing case, because the suite
counts it.

This has now happened twice:

* the Maps location tools (099 Lane A / 101 Lane B) — landed referencing mocks
  before the targets were registered, noted in ``agent_flow._TOOL_MOCK_TARGETS``;
* ``buildium_list_open_work_orders`` (099_buildium_auth_failed_honesty) and
  ``buildium_record_move_out`` (126_relative_date_resolved_not_asked), found in
  the PR #882 CI logs on 2026-08-24 — both erroring on every run, silently.

Twice is a pattern, and the pattern is gotcha 28's general rule: *a string that
crosses a producer/consumer boundary needs a test that reads BOTH sides.* The
golden JSON is the producer; the registry is the consumer. Each was individually
fine.

Three traps this file hit while being written, all worth keeping:

* ⚠ **``tool_mocks`` lives under ``input``, not at the top level.** The first
  cut read ``case["tool_mocks"]``, found ZERO mocks in every case, and passed
  cleanly against the exact broken registry it exists to catch. Caught only by
  reverting the fix and watching it stay green — the dead-E2B-sandbox shape
  (gotcha 33): an assertion that examines nothing is trivially satisfied.
  ``test_both_registries_are_actually_exercised`` now fails if that recurs.
* ⚠ **There are TWO registries and they are not interchangeable.** The second
  cut checked routing cases against ``agent_flow._TOOL_MOCK_TARGETS`` and
  reported four healthy cases as broken. A guard that manufactures findings
  gets ignored as fast as one that misses them.
* ⚠ Registration is checked separately from RESOLUTION: a registered name
  pointing at a renamed impl fails exactly as silently as a missing one.
"""

from __future__ import annotations

import importlib
import json

import pytest

from helixis.evals.agent_flow import _TOOL_MOCK_TARGETS
from helixis.evals.runner import GOLDEN_DIR

#: case_type -> the registry THAT type's mocks resolve against. There are two,
#: and they are NOT interchangeable: ``agent_flow`` patches specialist ``*_impl``
#: coroutines via ``agent_flow._TOOL_MOCK_TARGETS``, while
#: ``orchestrator_routing`` has its own ``_MOCK_TARGETS`` keyed partly by
#: Composio SLUG. Checking either against the other's registry manufactures
#: false findings — the first cut of this file did exactly that and reported
#: four healthy routing cases as broken.
#:
#: Both raise ``ValueError: unknown tool ...`` on an unregistered name, so in
#: both families the failure mode is identical: the case errors on every run
#: and never exercises the model.
_REGISTRY_BY_CASE_TYPE: dict[str, str] = {
    "agent_flow": "helixis.evals.agent_flow._TOOL_MOCK_TARGETS",
    "orchestrator_routing": "helixis.evals.orchestrator_routing._MOCK_TARGETS",
}


def _registry(dotted: str) -> dict[str, object]:
    module_path, _, attr = dotted.rpartition(".")
    return dict(getattr(importlib.import_module(module_path), attr))


def _mocked_by_case_type() -> dict[str, dict[str, list[str]]]:
    """case_type -> {tool name -> golden cases that mock it}."""
    out: dict[str, dict[str, list[str]]] = {}
    for path in sorted(GOLDEN_DIR.glob("*.json")):
        case = json.loads(path.read_text(encoding="utf-8"))
        case_type = str(case.get("case_type"))
        for tool_name in _walk_mocks(case):
            out.setdefault(case_type, {}).setdefault(tool_name, []).append(path.stem)
    return out


def _walk_mocks(node: object) -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "tool_mocks" and isinstance(value, dict):
                found.extend(value)
            else:
                found.extend(_walk_mocks(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_walk_mocks(item))
    return found


def test_every_mocked_tool_is_registered_in_its_own_registry() -> None:
    """The guard proper, per case type. A new case naming an unregistered mock
    fails HERE, loudly, instead of erroring into a quarantined [FAIL] forever."""
    problems: list[str] = []
    for case_type, tools in sorted(_mocked_by_case_type().items()):
        dotted = _REGISTRY_BY_CASE_TYPE.get(case_type)
        if dotted is None:
            continue  # a case type with no mock registry mocks nothing
        registry = _registry(dotted)
        for tool_name, cases in sorted(tools.items()):
            if tool_name not in registry:
                problems.append(f"  [{case_type}] {tool_name}: mocked by {', '.join(cases)}")
    assert not problems, (
        "golden cases mock tools absent from their case type's registry — each of these "
        "cases ERRORS on every run and never exercises the model:\n" + "\n".join(problems)
    )


def test_both_registries_are_actually_exercised() -> None:
    """Anti-vacuity, per registry. If either family stops being discovered the
    check above goes quiet for it while still reporting green overall."""
    by_type = _mocked_by_case_type()
    for case_type in _REGISTRY_BY_CASE_TYPE:
        assert len(by_type.get(case_type, {})) >= 5, (
            f"only {len(by_type.get(case_type, {}))} mocked tool(s) found for "
            f"{case_type!r} — the reader has probably stopped finding `tool_mocks` "
            "(it lives under `input`), making this file's checks vacuous."
        )


def _mocked_tools() -> dict[str, list[str]]:
    """tool name -> golden cases that mock it, flattened across ALL case types.

    Used only by the two agent_flow-specific checks below, which do not need
    the per-registry split. Shares ``_walk_mocks`` so the "it lives under
    ``input``" knowledge exists in exactly one place.
    """
    out: dict[str, list[str]] = {}
    for path in sorted(GOLDEN_DIR.glob("*.json")):
        case = json.loads(path.read_text(encoding="utf-8"))
        for tool_name in _walk_mocks(case):
            out.setdefault(tool_name, []).append(path.stem)
    return out


def test_the_registry_is_not_carrying_dead_entries() -> None:
    """The inverse. A registered target no case mocks is not harmful, but it
    makes the registry look more load-bearing than it is — and the browser
    tools genuinely are mocked, so a real orphan is worth a look.

    Informational by design: this asserts nothing, it PRINTS. Failing on an
    orphan would punish adding a target ahead of the case that needs it, which
    is the correct order to do it in.
    """
    orphans = sorted(set(_TOOL_MOCK_TARGETS) - set(_mocked_tools()))
    if orphans:
        print(f"\n[info] {len(orphans)} registered mock target(s) no golden case uses: {orphans}")


@pytest.mark.parametrize("tool_name", sorted(_TOOL_MOCK_TARGETS))
def test_each_registered_target_actually_resolves(tool_name: str) -> None:
    """Registration is a CLAIM; this checks it.

    A target whose impl was renamed fails at patch time with the same silent
    per-case error the missing-registration bug produced — so the registry
    being complete is only half the property.
    """
    target_path, _factory = _TOOL_MOCK_TARGETS[tool_name]
    module_path, _, attr = target_path.rpartition(".")
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"{module_path} not importable in this env: {exc}")
    assert hasattr(module, attr), (
        f"{tool_name!r} is registered as {target_path!r} but {module_path} has no {attr!r}. "
        "A renamed impl breaks the mock exactly as silently as a missing registration."
    )
