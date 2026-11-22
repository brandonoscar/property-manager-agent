"""Phase 0 of the code-execution safety floor: permission + audit trail.

Two gaps this pins, both found by the 2026-08-22 sandbox audit:

* **The role floor.** ``settings.codeexec.enabled`` was the ONLY gate on
  arbitrary Python. Posting a charge requires Manager+ (gotcha 21); running
  code — a strictly larger capability, since it can compute the numbers a
  charge is based on — required nothing. Any ``member`` could run it the
  moment the flag went true.
* **The audit trail.** The orchestrator's generic tool span recorded
  ``tool:run_computation`` plus 2000 chars of output. Not the code, not the
  approved plan, not the duration, not whether the user clicked approve or
  cancel. "What ran against company X on the 14th" was unanswerable.

The asymmetry between them is deliberate and tested below: the role floor
fails **closed** (unverifiable role → refuse), the audit fails **open** (a
telemetry error must never break a run). Losing a log line is acceptable;
losing a permission check is not.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from helixis.agents import codeexec_tools as ct
from helixis.codeexec import audit
from helixis.observability.context import trace_context
from helixis.settings import Settings

_CODE = "result = 2 + 2"
_PLAN = "Add two numbers together."


def _settings_on():
    """codeexec flipped on — the flag is not the thing under test here, the
    gates behind it are.

    ``substrate="e2b"`` is not decoration: the boot guard refuses a live
    capability on the local subprocess, so a live-shaped config MUST name the
    isolated substrate. Execution itself is patched out in every test below,
    so no sandbox is ever created.
    """
    enabled = Settings(
        env="test",
        memory={"enabled": False},
        codeexec={
            "enabled": True,
            "wall_clock_s": 5.0,
            "substrate": "e2b",
            "e2b_api_key": "e2b_test_key",
        },
    )
    return patch.object(ct, "get_settings", lambda: enabled)


# ── the role floor ────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["member", None])
async def test_non_manager_cannot_run_code(role: str | None) -> None:
    """``None`` is the fail-closed case: "can't verify the role" must never
    mean "allow" for the highest-authority capability in the product."""
    import helixis.agents.buildium_tools as bt

    with _settings_on(), patch.object(bt, "_caller_role", new=AsyncMock(return_value=role)):
        with patch.object(ct, "run_code") as ran:
            out = await ct._run_computation_impl(_PLAN, _CODE)

    assert "manager or admin" in out.lower()
    ran.assert_not_called()


@pytest.mark.asyncio
async def test_the_refusal_lands_before_the_approval_card() -> None:
    """Nobody should be asked to approve a run they aren't permitted to make.
    Gate order is dark-check → permission → confirm (the sms_tools precedent),
    so a denied member never sees a card at all."""
    import helixis.agents.buildium_tools as bt

    with _settings_on(), patch.object(bt, "_caller_role", new=AsyncMock(return_value="member")):
        with patch.object(ct, "confirm_or_abort", new=AsyncMock(return_value=True)) as gate:
            await ct._run_computation_impl(_PLAN, _CODE)

    gate.assert_not_called()


@pytest.mark.asyncio
async def test_manager_and_admin_both_pass_the_floor() -> None:
    import helixis.agents.buildium_tools as bt

    for role in ("manager", "admin"):
        with _settings_on(), patch.object(bt, "_caller_role", new=AsyncMock(return_value=role)):
            with patch.object(ct, "confirm_or_abort", new=AsyncMock(return_value=False)):
                out = await ct._run_computation_impl(_PLAN, _CODE)
        # Reached the gate and was declined there — i.e. the floor let it past.
        assert "cancelled" in out.lower(), role


@pytest.mark.asyncio
async def test_apply_actions_carries_the_same_floor() -> None:
    """``apply_actions`` is the WRITE half of the same capability. Gating
    ``run_computation`` alone would leave the door it guards wide open."""
    import helixis.agents.buildium_tools as bt

    with _settings_on(), patch.object(bt, "_caller_role", new=AsyncMock(return_value="member")):
        out = await ct._apply_actions_impl("Add a note to two tasks.", [])

    assert "manager or admin" in out.lower()


# ── the audit record ──────────────────────────────────────────────────────


def test_the_code_itself_never_reaches_telemetry() -> None:
    """Agent-written Python routinely embeds tenant data as literals. Langfuse
    is third-party telemetry under the security.pii 'no identifiers' contract,
    so the event carries a fingerprint, never the source."""
    secret = "result = 'tenant Jane Doe owes 1400 at jane@example.com'"
    payload = audit.build_audit_payload(
        company_id="c1",
        user_id="u1",
        code=secret,
        plan="Check what Jane owes.",
        approved=True,
        ok=True,
        duration_ms=12,
    )
    flat = str(payload)
    assert "jane@example.com" not in flat
    assert "tenant Jane Doe" not in flat
    assert payload["code_sha256"] == audit.code_fingerprint(secret)
    assert len(payload["code_sha256"]) == 64


def test_the_fingerprint_distinguishes_runs() -> None:
    """The hash has to be load-bearing: it is the only way to tell whether two
    runs executed the same thing, since the source isn't kept."""
    a = audit.code_fingerprint("result = 1")
    b = audit.code_fingerprint("result = 2")
    assert a != b
    assert a == audit.code_fingerprint("result = 1")


def test_the_plan_is_redacted_not_dropped() -> None:
    """The plan is what the user actually approved, so it has to survive — but
    it's model-written prose about real people, so it goes through redact."""
    payload = audit.build_audit_payload(
        company_id="c1",
        user_id="u1",
        code=_CODE,
        plan="Email carlos@acme.com about the balance at 555-123-4567.",
        approved=True,
        ok=True,
        duration_ms=1,
    )
    assert "carlos@acme.com" not in payload["plan"]
    assert "555-123-4567" not in payload["plan"]
    assert "balance" in payload["plan"]  # the substance is still legible


def test_a_declined_run_is_still_recorded() -> None:
    """A trail that keeps only the happy path is not a trail. 'Someone was
    asked to approve arbitrary code and said no' is exactly what a reviewer
    needs to see."""
    payload = audit.build_audit_payload(
        company_id="c1",
        user_id="u1",
        code=_CODE,
        plan=_PLAN,
        approved=False,
        ok=False,
        duration_ms=40,
    )
    assert payload["approved"] is False
    assert payload["ok"] is False
    assert payload["company_id"] == "c1"


def test_the_substrate_field_exists_before_there_is_a_sandbox() -> None:
    """Recorded from day one so swapping to a real isolated substrate changes
    a VALUE, not the event schema — dashboards built on this survive it."""
    payload = audit.build_audit_payload(
        company_id="c1",
        user_id="u1",
        code=_CODE,
        plan=_PLAN,
        approved=True,
        ok=True,
        duration_ms=1,
    )
    assert payload["substrate"] == "subprocess"
    assert payload["sandbox_id"] is None

    swapped = audit.build_audit_payload(
        company_id="c1",
        user_id="u1",
        code=_CODE,
        plan=_PLAN,
        approved=True,
        ok=True,
        duration_ms=1,
        substrate="e2b",
        sandbox_id="sbx_abc123",
    )
    assert set(swapped) == set(payload)  # identical shape, different values


def test_artifact_names_are_capped_but_the_count_is_exact() -> None:
    """A run that writes 500 files shouldn't blow up the event, but the count
    is the audit-relevant number and must never be the truncated one."""
    payload = audit.build_audit_payload(
        company_id="c1",
        user_id="u1",
        code=_CODE,
        plan=_PLAN,
        approved=True,
        ok=True,
        duration_ms=1,
        artifacts=[f"f{i}.csv" for i in range(500)],
    )
    assert payload["artifact_count"] == 500
    assert len(payload["artifacts"]) <= 25


def test_audit_failure_never_raises_into_the_run() -> None:
    """The asymmetry with the role floor: telemetry fails OPEN. A broken
    Langfuse must not take down a computation the user is waiting on."""
    with trace_context(company_id="c1", session_id="s1", trace_id="t1"):
        with patch.object(audit.tracing, "record_event", side_effect=RuntimeError("langfuse down")):
            audit.record_run(
                company_id="c1",
                user_id="u1",
                code=_CODE,
                plan=_PLAN,
                approved=True,
                ok=True,
                duration_ms=1,
            )  # must not raise


def test_no_trace_context_is_a_quiet_noop() -> None:
    """Scripts, tests and the eval harness run without a trace. That's not an
    error condition — it just has nowhere to attach."""
    with patch.object(audit.tracing, "record_event") as emit:
        audit.record_run(
            company_id="c1",
            user_id="u1",
            code=_CODE,
            plan=_PLAN,
            approved=True,
            ok=True,
            duration_ms=1,
        )
    emit.assert_not_called()


@pytest.mark.asyncio
async def test_a_real_run_emits_exactly_one_record() -> None:
    """End to end through the tool: one run, one audit event, carrying the
    fingerprint of the code that actually executed."""
    import helixis.agents.buildium_tools as bt

    with _settings_on(), patch.object(bt, "_caller_role", new=AsyncMock(return_value="manager")):
        with patch.object(ct, "confirm_or_abort", new=AsyncMock(return_value=True)):
            with patch.object(ct.audit, "record_run") as rec:
                out = await ct._run_computation_impl(_PLAN, _CODE)

    assert '"ok": true' in out.lower()
    rec.assert_called_once()
    kwargs = rec.call_args.kwargs
    assert kwargs["approved"] is True
    assert kwargs["ok"] is True
    assert kwargs["code"] == _CODE
    assert kwargs["duration_ms"] >= 0
