"""A parked system's golden cases make no model call (``evals.parked``).

Rent Manager is on hold since 2026-10-06 (``docs/decisions.md``). Brandon, asked
whether its golden cases should keep running per PR or weekly: "none because
we aren't using it yet". These pin that they do not run in any mode, that
nothing else is parked with them, and that a run says so instead of going
quiet. The connector's unit tests are untouched: they cost nothing.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from helixis.evals import runner
from helixis.evals.parked import PARKED, parked_reason
from helixis.pms import registry

GOLDEN = Path(__file__).resolve().parents[2] / "src" / "helixis" / "evals" / "golden"

#: Every Rent Manager case, named: found by the rule under test, a broken rule
#: would choose its own list.
RENT_MANAGER_CASES = {
    "500_rentmanager_delinquency_from_the_mirror",
    "501_rentmanager_owner_statement_not_synced",
    "502_rentmanager_lease_rent_not_read",
    "520_rentmanager_create_work_order_approved",
    "521_rentmanager_create_work_order_declined",
    "522_rentmanager_create_work_order_never_answered",
    "523_rentmanager_status_change_approved",
    "524_rentmanager_status_change_declined",
    "525_rentmanager_injected_write_in_a_work_order",
    "526_rentmanager_writes_not_reached_on_a_buildium_company",
}


def _cases() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for path in sorted(GOLDEN.glob("*.json")):
        case = json.loads(path.read_text(encoding="utf-8"))
        out[case["id"]] = case
    return out


def test_exactly_the_rent_manager_cases_are_parked() -> None:
    parked = {case_id for case_id, case in _cases().items() if parked_reason(case)}
    assert parked == RENT_MANAGER_CASES


def test_every_parked_key_is_a_system_the_registry_knows() -> None:
    """A misspelt key would park nothing and look like it did."""
    assert PARKED, "nothing is parked: delete this module's tests with it"
    for key in PARKED:
        assert registry.spec(key) is not None, key


@pytest.mark.parametrize(
    ("block", "parked"),
    [
        ({"pms": "rentmanager"}, True),
        ({"switched_on": ["rentmanager"]}, True),
        ({"pms": "buildium", "switched_on": ["propertyware", "rentmanager"]}, True),
        ({"pms": "propertyware", "switched_on": ["propertyware"]}, False),
        ({"pms": "rentvine"}, False),
        ({}, False),
        ({"switched_on": "rentmanager"}, False),  # not a list: the harness refuses it
    ],
)
def test_a_case_is_parked_by_the_system_it_runs_as_or_switches_on(
    block: dict[str, Any], parked: bool
) -> None:
    assert (parked_reason({"id": "x", "input": block}) is not None) is parked


def _no_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every way ``run_case`` reaches a model raises instead."""

    async def _refuse(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("a parked case reached a case runner")

    for name in (
        "_run_agent_flow_case",
        "_run_orchestrator_routing_case",
        "_run_tool_invocation_case",
        "_run_reverse_report_enforcement_case",
        "_run_planner_summarizer_case",
    ):
        monkeypatch.setattr(runner, name, _refuse)


@pytest.mark.parametrize("case_id", sorted(RENT_MANAGER_CASES))
def test_a_parked_case_makes_no_model_call_even_with_every_key_set(
    case_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Parked comes before the env check: with the keys present it is still
    never run, and its reason is the hold, not a missing key."""
    case = _cases()[case_id]
    for name in case.get("requires_env") or []:
        monkeypatch.setenv(name, "present")
    _no_model(monkeypatch)
    result = asyncio.run(runner.run_case(case))
    assert result.skipped is True
    assert result.skip_reason == f"parked: {PARKED['rentmanager']}"


def test_a_full_run_sets_parked_cases_aside_before_the_gate_and_reports_them(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The gate never sees a parked case, so it takes no slot; each one still
    prints a SKIP line saying why, and the summary counts it as skipped."""
    seen: dict[str, list[str]] = {}

    def _gate(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen["ids"] = [c["id"] for c in cases]
        return []  # run nothing else: this test makes no model call

    monkeypatch.setattr(runner, "_apply_pr_gate", _gate)
    _no_model(monkeypatch)
    code = asyncio.run(runner.main())
    out = capsys.readouterr().out

    assert not RENT_MANAGER_CASES & set(seen["ids"])
    assert len(seen["ids"]) == len(_cases()) - len(RENT_MANAGER_CASES)
    for case_id in RENT_MANAGER_CASES:
        assert f"[SKIP] {case_id} — parked: {PARKED['rentmanager']}" in out
    assert f"({len(RENT_MANAGER_CASES)} skipped)" in out
    assert code == 0


def test_without_the_keys_the_reason_is_still_the_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    """A local run has no provider key. The skip must still say "parked", or a
    reader would think the case is waiting on a secret rather than a decision."""
    case = _cases()["521_rentmanager_create_work_order_declined"]
    for name in case.get("requires_env") or []:
        monkeypatch.delenv(name, raising=False)
    _no_model(monkeypatch)
    result = asyncio.run(runner.run_case(case))
    assert result.skip_reason == f"parked: {PARKED['rentmanager']}"
