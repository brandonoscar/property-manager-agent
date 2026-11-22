"""The Propertyware golden families: 51x, the read side, and 53x, the writes.

510-519 are Propertyware's read-side cases and 530-539 its writes (Rent
Manager has 500-509 and 520-529). Model-free, these pin what makes each case
measure something:

  - it runs AS Propertyware (``input.pms``), with the deployment's
    Propertyware switches on when it is about the writes
    (``input.switched_on``), is quarantined (gotcha 48), and says of every
    assertion whether it is SAFETY or QUALITY, matching how the harness
    builds it;
  - every tool it mocks or asserts on is one the model can see on that run:
    a prohibition on an invisible tool passes for nothing (gotcha 35), and a
    mock of one never runs. The one exception is deliberate and pinned the
    other way: on a Buildium company (538) the Propertyware tools must be
    INVISIBLE, which is the boundary that case is the regression net for;
  - its data is invented and names no other system.

The live run of a case costs money and is the runbook's (``docs/runbooks/
evals.md``); none runs here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from agents import RunContextWrapper

from helixis.agents import run_pms
from helixis.evals import agent_flow
from helixis.propertyware import sync as pw_sync

GOLDEN = Path(__file__).resolve().parents[2] / "src" / "helixis" / "evals" / "golden"
READS = sorted(GOLDEN.glob("51[0-9]_*.json"))
WRITES = sorted(GOLDEN.glob("53[0-9]_*.json"))
FAMILY = READS + WRITES

#: The Propertyware tools: every one is shown only with its writes switched on.
PW_TOOLS = {
    "propertyware_create_work_order",
    "propertyware_update_work_order",
    "propertyware_change_work_order_status_or_vendor",
}

#: How the agent_flow harness builds each assertion (agent_flow
#: _check_agent_flow_assertions): critical=True is SAFETY.
_SAFETY = {
    "tool_call_trail_must_not_contain",
    "tool_call_count_max",
    "approval_card_count_max",
    "reply_must_not_match_regex",
    "reply_must_not_contain",
}
_QUALITY = {
    "tool_call_trail_must_contain",
    "tool_call_count_min",
    "reply_must_contain",
    "reply_must_match_regex",
    "reply_should_not_contain",
    "reply_must_not_be_empty",
}


def _case(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _buildium_run(case: dict[str, Any]) -> bool:
    return case["input"].get("pms") is None


def test_the_families_are_the_propertyware_read_side_and_writes() -> None:
    """Re-pointed from "no 53x yet" when the writes slice filled 530-538."""
    assert [p.name[:3] for p in READS] == ["510", "511", "512"]
    assert [p.name[:3] for p in WRITES] == [f"53{i}" for i in range(9)]
    for path in FAMILY:
        assert _case(path)["id"] == path.stem
        assert "_propertyware_" in path.stem
    # Rent Manager's numbers stay Rent Manager's.
    assert not [p for p in GOLDEN.glob("5[02][0-9]_*.json") if "propertyware" in p.name]


@pytest.mark.parametrize("path", FAMILY, ids=lambda p: p.stem)
def test_each_case_runs_as_propertyware_and_is_quarantined(path: Path) -> None:
    case = _case(path)
    assert case["case_type"] == "agent_flow"
    assert case["input"]["agent"] == "Buildium Agent"
    assert case["single_run_gate"] is False
    assert "gotcha 48" in case["_single_run_gate_note"]
    assert "HELIXIS_LLM__ANTHROPIC_API_KEY" in case["requires_env"]
    if path in READS:
        assert case["input"]["pms"] == "propertyware"
        assert "switched_on" not in case["input"], "a read case runs with the writes off"
    elif path.stem.startswith("538_"):
        assert _buildium_run(case), "538 is a Buildium company"
        assert case["input"]["switched_on"] == ["propertyware"]
    else:
        assert case["input"]["pms"] == "propertyware"
        assert case["input"]["switched_on"] == ["propertyware"]


@pytest.mark.parametrize("path", FAMILY, ids=lambda p: p.stem)
def test_each_assertion_says_safety_or_quality_as_the_harness_builds_it(path: Path) -> None:
    case = _case(path)
    notes = case["_assertion_notes"]
    for key in case["expected"]:
        assert key in _SAFETY | _QUALITY, f"{key} is not an agent_flow assertion"
        label = "SAFETY" if key in _SAFETY else "QUALITY"
        assert notes[key].startswith(label), (key, notes[key][:40])


@pytest.mark.parametrize("path", FAMILY, ids=lambda p: p.stem)
def test_each_case_is_invented_and_names_no_other_system(path: Path) -> None:
    case = _case(path)
    mocks = json.dumps(case["input"]["tool_mocks"], ensure_ascii=False)
    if not _buildium_run(case):
        for other in ("Buildium", "Rentvine", "Rent Manager"):
            assert other not in mocks
    for email in re.findall(r"[\w.+-]+@[\w.-]+", mocks):
        assert email.endswith("@example.com"), email
    for phone in re.findall(r"\b\d{3}-\d{4}\b", mocks):
        assert phone.startswith("555-01"), phone
    for number in re.findall(r"\b\d{6,}\b", mocks + case["input"]["user_message"]):
        assert 990_000_000 <= int(number) < 991_000_000, f"{number} is not in the fake id range"


def _rentvine_status_write_open_on_a_propertyware_run() -> bool:
    from helixis.agents import rentvine_tools

    token = run_pms.bind_run_pms("propertyware")
    try:
        return bool(rentvine_tools.rentvine_update_work_order_status.is_enabled(None, None))
    finally:
        run_pms.reset_run_pms(token)


@pytest.mark.parametrize("path", WRITES, ids=lambda p: p.stem)
def test_rentvines_status_write_is_not_open_on_a_propertyware_write_case(path: Path) -> None:
    """Re-pointed (rule 19). It was on every run, a Propertyware one included,
    so each write case forbade it: a case that did not left the wrong
    system's write open (the coordinator's finding on slice 5). It is hidden
    on a Propertyware run now, and a forbid on a tool the run cannot see is
    vacuous (``test_every_tool_the_case_names_is_one_the_run_can_see``)."""
    assert not _rentvine_status_write_open_on_a_propertyware_run()
    assert _case(path)["expected"]["tool_call_trail_must_not_contain"], "still bounded"


@pytest.mark.parametrize(
    "stem",
    [
        "535_propertyware_mark_complete_refused_nothing_sent",
        "536_propertyware_assign_vendor_refused_nothing_sent",
    ],
)
def test_the_status_and_vendor_cases_forbid_every_write(stem: str) -> None:
    forbidden = set(_case(GOLDEN / f"{stem}.json")["expected"]["tool_call_trail_must_not_contain"])
    assert {"propertyware_create_work_order", "propertyware_update_work_order"} <= forbidden
    # Every write the run can see: Rentvine's status change is hidden on it.
    assert not _rentvine_status_write_open_on_a_propertyware_run()
    assert "propertyware_change_work_order_status_or_vendor" not in forbidden, (
        "that answer sends nothing"
    )


async def _seen(case: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """What the model is given on the case's run: its tools, input and
    instructions, captured where ``Runner.run`` would start."""
    seen: dict[str, Any] = {}

    class _StopError(Exception):
        pass

    async def fake_run(agent: Any, *, input: Any, **_kw: Any) -> Any:  # noqa: A002
        tools = await agent.get_all_tools(RunContextWrapper(context=None))
        seen.update(
            tools={t.name for t in tools},
            input=input,
            instructions=str(agent.instructions),
            pms=run_pms.run_pms_spec(),
        )
        raise _StopError("probe")

    monkeypatch.setattr(agent_flow.Runner, "run", fake_run)
    result = await agent_flow.run_agent_flow_case(case)
    assert result.error == "_StopError: probe", result.error
    return seen


@pytest.mark.parametrize("path", FAMILY, ids=lambda p: p.stem)
async def test_every_tool_the_case_names_is_one_the_run_can_see(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _case(path)
    seen = await _seen(case, monkeypatch)
    named = set(case["input"]["tool_mocks"])
    for key in ("tool_call_trail_must_contain", "tool_call_trail_must_not_contain"):
        named |= set(case["expected"].get(key, []))
    named |= set(case["expected"].get("tool_call_count_max", {}))
    named |= set(case["expected"].get("approval_card_count_max", {}))
    if _buildium_run(case):
        # The boundary 538 is the net for: with Propertyware's writes switched
        # on, a Buildium run still does not see a Propertyware tool.
        assert seen["pms"] is None or seen["pms"].key == "buildium"
        assert not seen["tools"] & PW_TOOLS, sorted(seen["tools"] & PW_TOOLS)
        assert PW_TOOLS & named, "538 must still forbid them"
        named -= PW_TOOLS
    else:
        assert seen["pms"] is not None and seen["pms"].key == "propertyware"
        assert seen["input"] == case["input"]["user_message"] + pw_sync.RUN_NOTE
        assert "Propertyware Agent" in seen["instructions"]
        assert not any(name.startswith("buildium_") for name in seen["tools"])
        # Rentvine's status change is hidden on a Propertyware run, which
        # bounds it more firmly than the forbid these cases used to carry.
        assert "rentvine_update_work_order_status" not in seen["tools"]
        if path in WRITES:
            assert seen["tools"] >= PW_TOOLS, "the writes are switched on for this run"
        else:
            assert not seen["tools"] & PW_TOOLS, "a read case runs with the writes off"
    assert named <= seen["tools"], sorted(named - seen["tools"])


# ── the harness's switches ───────────────────────────────────────────────


def _bare(**input_block: Any) -> dict[str, Any]:
    return {
        "id": "x",
        "case_type": "agent_flow",
        "input": {"user_message": "Hi", "agent": "Buildium Agent", **input_block},
        "expected": {"reply_must_not_be_empty": True},
    }


async def test_switched_on_is_what_shows_the_propertyware_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An eval process leaves every deployment switch off, so without
    ``input.switched_on`` a write case would run with its tools hidden."""
    off = await _seen(_bare(pms="propertyware"), monkeypatch)
    assert not off["tools"] & PW_TOOLS
    on = await _seen(_bare(pms="propertyware", switched_on=["propertyware"]), monkeypatch)
    assert on["tools"] >= PW_TOOLS
    # The switch lasts the run only.
    from helixis.pms import registry

    assert registry.writes_enabled("propertyware") is False


@pytest.mark.parametrize(
    "value", [["rentvine"], ["buildium"], ["propertyware", "nope"], "propertyware", {"a": 1}]
)
async def test_a_switch_the_harness_cannot_set_is_a_case_error(value: Any) -> None:
    result = await agent_flow.run_agent_flow_case(_bare(pms="propertyware", switched_on=value))
    assert result.error is not None and "switched_on" in result.error
