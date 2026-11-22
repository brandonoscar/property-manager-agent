"""A golden case can run as a Rentvine workspace, the way production does.

Before 2026-10-03 no eval could: ``run_pms`` is bound only in
``run_agent_streaming``, and both eval harnesses call ``Runner.run`` directly.
So an eval run kept the live Buildium tools visible, hid every Rentvine-only
tool (``rent_roll``, ``lease_payment_history``, the Rentvine writes), and never
saw the Rentvine run note: a "Rentvine" case would have passed or failed as a
Buildium one (gotcha 66's class: the harness must do what production does).

These drive the real ``run_agent_flow_case`` with ``Runner.run`` replaced by a
probe that records what the model would have been handed, then stops.
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import Any

import pytest
from agents import RunContextWrapper

from helixis.agents import run_pms
from helixis.evals import agent_flow

_RENTVINE_ONLY = {"rent_roll", "lease_payment_history"}


class _StopError(Exception):
    """Raised by the probe once it has looked; the harness reports it."""


def _probe(seen: dict[str, Any]):
    async def fake_run(agent: Any, *, input: Any, **_kw: Any) -> Any:  # noqa: A002
        seen["on_rentvine"] = run_pms.run_on_rentvine()
        tools = await agent.get_all_tools(RunContextWrapper(context=None))
        seen["tools"] = {t.name for t in tools}
        seen["instructions"] = str(agent.instructions)
        seen["input"] = input
        raise _StopError("probe")

    return fake_run


def _case(**input_over: Any) -> dict[str, Any]:
    return {
        "id": "rv-probe",
        "input": {
            "agent": "Buildium Agent",
            "user_message": "Show me the rent roll.",
            **input_over,
        },
        "expected": {},
    }


@pytest.mark.asyncio
async def test_a_rentvine_case_runs_as_rentvine(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(agent_flow.Runner, "run", _probe(seen))
    result = await agent_flow.run_agent_flow_case(_case(pms="rentvine"))

    assert result.error == "_StopError: probe"
    assert seen["on_rentvine"] is True
    assert _RENTVINE_ONLY <= seen["tools"]
    assert not any(name.startswith("buildium_") for name in seen["tools"])
    # The specialist is reworded, as the fast route rewords it in production.
    assert "Rentvine Agent" in seen["instructions"]
    assert "Buildium Agent" not in seen["instructions"]
    assert seen["input"] == "Show me the rent roll." + run_pms.RENTVINE_RUN_NOTE
    # And nothing leaks into the next case.
    assert run_pms.run_on_rentvine() is False


@pytest.mark.asyncio
async def test_a_case_that_names_no_pms_is_byte_for_byte_the_old_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(agent_flow.Runner, "run", _probe(seen))
    await agent_flow.run_agent_flow_case(_case())

    assert seen["on_rentvine"] is False
    assert not (_RENTVINE_ONLY & seen["tools"])
    assert "buildium_get_rent_roll" in seen["tools"]
    assert seen["input"] == "Show me the rent roll."


@pytest.mark.asyncio
@pytest.mark.parametrize("pms", ["Rentvine", "appfolio", ""])
async def test_a_misspelt_pms_is_a_case_error_never_a_buildium_run(
    monkeypatch: pytest.MonkeyPatch, pms: str
) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(agent_flow.Runner, "run", _probe(seen))
    result = await agent_flow.run_agent_flow_case(_case(pms=pms))
    assert result.error and "case.input.pms" in result.error
    assert seen == {}, "the case ran anyway"


@pytest.mark.asyncio
async def test_the_rentvine_note_never_reaches_the_judges(monkeypatch: pytest.MonkeyPatch) -> None:
    """The judges score the question the case asked, not the run's wiring."""
    seen: dict[str, Any] = {}

    async def fake_run(agent: Any, *, input: Any, **_kw: Any) -> Any:  # noqa: A002
        seen["input"] = input
        return SimpleNamespace(final_output="Rent roll: 8 active leases.", new_items=[])

    async def fake_score(*, user_message: str, reply: str, context: str) -> None:
        seen["judged"] = user_message

    monkeypatch.setattr(agent_flow.Runner, "run", fake_run)
    import helixis.evals.rubric_judge as rubric

    monkeypatch.setattr(rubric, "score_reply", fake_score)
    result = await agent_flow.run_agent_flow_case(_case(pms="rentvine"), judge=True)
    assert result.error is None and result.final_output == "Rent roll: 8 active leases."
    assert seen["input"].endswith(run_pms.RENTVINE_RUN_NOTE)
    assert seen["judged"] == "Show me the rent roll."


def test_every_rentvine_mock_target_resolves() -> None:
    """A mock key whose target does not exist would patch nothing, and the
    case would call the real tool (gotcha 41's class)."""
    for name in (
        *_RENTVINE_ONLY,
        "rentvine_update_work_order_status",
        "rentvine_create_work_order",
        "rentvine_upload_file",
        "rentvine_post_charge",
        "rentvine_record_payment",
    ):
        target, _factory = agent_flow._TOOL_MOCK_TARGETS[name]
        module, _, attr = target.rpartition(".")
        assert callable(getattr(importlib.import_module(module), attr)), target
