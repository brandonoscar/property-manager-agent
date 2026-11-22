"""W2 S5 — routing-harness primitives that close the #676 ``_todo`` list.

Covers: positive trail membership, specialist-call count caps (max=SAFETY,
min=quality), the quality-tier negative reply check, the mechanical
SYNTHESIZE-footer grader, sequenced tool mocks, and the connector-stub client
that gives routing runs a mockable Gmail seam.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from helixis.evals.orchestrator_routing import (
    RoutingRunResult,
    _build_mock_patches,
    _check_routing_assertions,
    _StubComposioClient,
)

_FOOTER_OK = (
    "Bob owes $2,400 and promised to pay by the 25th.\n\n"
    "Sources: buildium: $2,400 past due; gmail: Bob's Jul 14 promise\n"
    "Confidence: likely — promise stated, no payment posted\n"
    "Unverified: none\n"
)


def _named(result: RoutingRunResult, prefix: str):
    return next(a for a in result.assertions if a.name.startswith(prefix))


def _run(trail: list[str], output: str = "", **expected):
    result = RoutingRunResult(last_agent_name="Helixis", handoff_trail=trail)
    result.final_output = output
    _check_routing_assertions(result=result, expected={"must_route_to": "Helixis", **expected})
    return result


# ── trail membership (quality) ───────────────────────────────────


def test_trail_must_contain_passes_and_fails() -> None:
    ok = _run(["buildium_agent", "gmail_agent"], handoff_trail_must_contain=["gmail_agent"])
    assert _named(ok, "handoff_trail_must_contain[0]").passed is True

    miss = _run(["buildium_agent"], handoff_trail_must_contain=["gmail_agent"])
    a = _named(miss, "handoff_trail_must_contain[0]")
    assert a.passed is False
    assert a.critical is False  # quality — dispatch varies run to run


# ── count caps ───────────────────────────────────────────────────


def test_count_max_over_cap_is_safety_critical() -> None:
    r = _run(["a", "b", "c"], specialist_call_count_max=2)
    a = _named(r, "specialist_call_count_max")
    assert a.passed is False
    assert a.critical is True  # over-action class — never averaged


def test_count_min_under_floor_is_quality() -> None:
    r = _run(["buildium_agent"], specialist_call_count_min=2)
    a = _named(r, "specialist_call_count_min")
    assert a.passed is False
    assert a.critical is False


def test_counts_pass_within_bounds() -> None:
    r = _run(["a", "b"], specialist_call_count_max=2, specialist_call_count_min=2)
    assert _named(r, "specialist_call_count_max").passed is True
    assert _named(r, "specialist_call_count_min").passed is True


# ── quality-tier negative reply ──────────────────────────────────


def test_reply_must_not_contain_quality_is_noncritical() -> None:
    r = _run([], output="the tenant definitely paid", reply_must_not_contain_quality=["definitely"])
    a = _named(r, "reply_must_not_contain_quality[0]")
    assert a.passed is False
    assert a.critical is False  # taste prohibition — must not hard-gate a PR


# ── mechanical footer grader (F6/F7) ─────────────────────────────


def test_footer_required_passes_on_compliant_answer() -> None:
    r = _run(["gmail_agent"], output=_FOOTER_OK, synthesis_footer_required=True)
    a = _named(r, "synthesis_footer_required")
    assert a.passed is True and a.critical is False


def test_footer_required_reports_violations() -> None:
    r = _run(["gmail_agent"], output="bare answer, no footer", synthesis_footer_required=True)
    a = _named(r, "synthesis_footer_required")
    assert a.passed is False
    assert "Sources" in (a.reason or "") and "Confidence" in (a.reason or "")


# ── sequenced mocks ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sequenced_mock_returns_successive_then_repeats_last() -> None:
    patches = _build_mock_patches({"buildium_get_tenant": ["first", "second"]})
    assert len(patches) == 1
    with patches[0] as _:
        from helixis.agents.buildium_tools import _get_tenant_impl

        assert isinstance(_get_tenant_impl, AsyncMock)
        assert await _get_tenant_impl("q1") == "first"
        assert await _get_tenant_impl("q2") == "second"
        assert await _get_tenant_impl("q3") == "second"  # last repeats, no StopIteration


def test_sequenced_mock_rejects_empty_list() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        _build_mock_patches({"buildium_get_tenant": []})


# ── connector-stub client ────────────────────────────────────────


def test_stub_client_serves_only_opted_slugs() -> None:
    stub = _StubComposioClient(["GMAIL"])
    gmail = stub.get_tools(toolkits=["GMAIL"], user_id="u")
    assert gmail, "opted GMAIL slug must yield the eval stub tools"
    assert any(getattr(t, "name", "") == "GMAIL_FETCH_EMAILS" for t in gmail)
    # Un-opted toolkits stay empty (specialist boots toolless, as before).
    assert stub.get_tools(toolkits=["GOOGLECALENDAR"], user_id="u") == []
    assert stub.degraded_toolkits("u") == set()
