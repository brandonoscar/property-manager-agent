"""Deterministic release-safety gates (#72) — the RED gates that hard-fail a PR.

The model-driven golden cases (routing / should-not-act) are quarantined —
a quality regression only warns, it does not gate (gotcha 10). These tests are
the deterministic complement: they turn on the AGENT'S WIRING, not the model's
judgement, so they hard-gate every PR with zero live-LLM spend. They catch the
scariest regressions the quarantined cases can miss:

  * a NEW write / action tool exposed to the agent without review;
  * a DARK, irreversible tool (renewal / applicant-status / bill) accidentally
    registered;
  * the free Ollama stress net (safety_checks) silently ceasing to flag a live
    write tool — which would make the whole free battery lie.

Loaded via importlib for the stress module (scripts/ is not an importable
package), mirroring test_ollama_stress.py.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from helixis.agents.tool_registries import write_registry_tools

_STRESS_DIR = Path(__file__).resolve().parents[2] / "scripts" / "ollama_stress"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, _STRESS_DIR / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sc = _load("safety_checks")


def _tool_name(tool: object) -> str:
    return getattr(tool, "name", None) or getattr(tool, "__name__", None) or str(tool)


def _registered() -> set[str]:
    # The agent's registered write-capable surface: the Buildium registry PLUS
    # the SMS Stage-4 comms registry (send_text_message rides the Buildium
    # agent via sms_tools(), deliberately outside buildium_tools() — see
    # test_negative_should_not_act.py for why the registries stay separate),
    # Rentvine's, and each connector built on helixis.pms, listed once in
    # agents/tool_registries.py. A new write still fails the test below until
    # its name is added to EXPECTED_WRITE_TOOLS: that stays the review gate.
    return {_tool_name(t) for t in write_registry_tools()}


# The EXACT set of write/action tools intended to be live on the Buildium
# agent (Buildium writes + the Stage-4 SMS comms write). Changing the live
# write surface must be a deliberate edit HERE — that's the review gate.
EXPECTED_WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "buildium_create_to_do_task",
        "buildium_create_work_order",
        "buildium_update_task_status",
        "buildium_close_work_order",
        "buildium_update_work_order",
        "buildium_post_charge",
        "buildium_record_move_out",
        # Reviewed 2026-08-29 — the reversal `record_move_out` was shipped on.
        # Its docstring justifies a confirm-only gate (no Manager+ floor) with
        # "REVERSIBLE via Leases.undo_move_out", and until now that undo was
        # reachable from nothing: a move-out filed against the wrong tenant
        # could only be repaired in the Buildium UI. Confirm-gated, and
        # deliberately NOT role-floored — flooring the correction while the
        # mistake needs only a confirm makes damage easier than repair.
        "buildium_undo_move_out",
        "buildium_record_payment",
        "buildium_add_task_note",
        "buildium_add_work_order_note",
        "buildium_add_vendor_note",
        "buildium_upload_document",
        "buildium_share_file",
        # SMS Stage 4 — the agent-facing text send (agents/sms_tools.py).
        # Reviewed 2026-08-18: dark-flag first, consent-ledger fail-closed,
        # templates-only render, confirm gate with the rendered text, and a
        # run_guarded idempotency claim; send_sms re-runs the Stage-1 guards.
        "send_text_message",
        # Phase 6 — the FIRST write into a second property-management system
        # (agents/rentvine_tools.py). Reviewed 2026-09-17: deployment dark-flag
        # first, per-company credentials with no env fallback, a Manager+
        # fail-closed floor (Rentvine's reversibility is @unverified, so an
        # unprobed undo is treated as no undo), a READ-MERGE-WRITE body because
        # the update endpoint declares 38 fields and requires none, a confirm
        # card carrying the ACTUAL body, a run_guarded idempotency claim, and a
        # read-back verify asserting the field the tool is named for.
        "rentvine_update_work_order_status",
        # The four Rentvine creates, founder decision 2026-10-03
        # (docs/decisions.md). Reviewed 2026-10-03: shown on Rentvine runs only,
        # the same dark-flag-first and no-env-fallback client, a confirm card
        # labelled from the SERVER's records, a run_guarded idempotency claim, a
        # POST that is never retried, and a read-back (the work order re-read,
        # the lease's own transaction list for the money, the upload's own
        # attachment where no GET exists). The two money writes are Manager+ and
        # permanent (no documented void) and say so on the card; neither moves
        # money (terms rule 15). No write mails anybody.
        "rentvine_create_work_order",
        "rentvine_upload_file",
        "rentvine_post_charge",
        "rentvine_record_payment",
        # Rent Manager's two work-order writes, design R4 (agents/
        # rentmanager_tools.py). Reviewed 2026-10-05: shown only on a Rent
        # Manager run with its writes switched on (tool_registries copies);
        # each impl refuses while switched off, opens a client on THIS
        # company's credentials (no env fallback), and makes ONE
        # pms.gated_write call: switch, company, Manager+ floor, a plan read
        # from Rent Manager (names on the card are the server's), the money
        # refusal, a confirm card carrying the ACTUAL body, a run_guarded
        # claim, a re-read before the send (the status change sends nothing
        # if what the approver saw moved), ONE POST with no override ever
        # (saveOptions is refused in any letter case), and a read-back. The
        # create posts a list holding exactly one object; a 409 on the status
        # change is resent once only if nothing the approver saw moved. No
        # note write (RM-15 unsettled), no DELETE, no money.
        "rentmanager_create_work_order",
        "rentmanager_update_work_order_status",
        # Propertyware's two work-order writes, design W4 (agents/
        # propertyware_tools.py). Reviewed 2026-10-05: shown only on a
        # Propertyware run with Propertyware's writes switched on (the registry
        # sets it), and sent only through pms.gated_write: switch first, this
        # company's credentials with no env fallback, a Manager+ floor, a card
        # built from Propertyware's own records just before it, an idempotency
        # claim, a re-read before the edit's send, ONE send never retried, and
        # a read-back (the new work order re-read; the edit's fields re-read
        # and every other field unchanged). The create sends both portal
        # flags false; the edit's PATCH carries only changed fields, never a
        # null. Neither can set a status or a vendor; a 403 is worded by the
        # operation's beta marker. No money, no delete, no bulk.
        "propertyware_create_work_order",
        "propertyware_update_work_order",
    }
)

# Deliberately NOT offered to the agent (dark): renewal creates an irreversible
# lease term (gotcha 22); applicant-status is the FHA/SafeRent screening
# exposure (#56); bill-create is off by product decision (2026-07-06).
DARK_TOOLS: frozenset[str] = frozenset(
    {
        "buildium_renew_lease",
        "buildium_update_applicant_status",
        "buildium_create_bill",
    }
)

# Non-Buildium comms tools whose NAMES the stress net + the 32x negative-safety
# cases rely on being flagged as writes. A checker that stopped flagging these
# would silently pass an exfil / unauthorized-send.
COMMS_WRITE_TOOL_NAMES: tuple[str, ...] = (
    "send_tenant_message",
    "reply_to_email",
    "send_task_to_teammate",
    # SMS Stage 4: the agent text send must trip the verb net ("send" token) —
    # if it ever stops, an unauthorized text in a stress/32x run would pass.
    "send_text_message",
)


def test_dark_tools_are_not_registered() -> None:
    """An irreversible / FHA / off-by-policy tool must never reach the agent."""
    leaked = DARK_TOOLS & _registered()
    assert not leaked, (
        f"DARK tool(s) accidentally registered in buildium_tools(): {sorted(leaked)}. "
        "These are irreversible / FHA-exposed / off-by-policy — un-register them."
    )


def test_live_write_surface_is_exactly_the_reviewed_set() -> None:
    """The tools the stress verb-net flags as writes must EXACTLY equal the
    reviewed set. A new write tool grows the flagged set → this fails until it's
    added here (the review gate); a read tool that trips the net is also caught."""
    registered = _registered()
    flagged = {n for n in registered if sc.find_forbidden_tool_calls([n])}
    new_unreviewed = flagged - EXPECTED_WRITE_TOOLS
    assert not new_unreviewed, (
        f"tool(s) flagged as writes but not in the reviewed set: {sorted(new_unreviewed)}. "
        "If intended, add to EXPECTED_WRITE_TOOLS after confirming a confirm-gate + role "
        "floor guards them; if a read tool tripped the verb net, tighten the tool name."
    )
    missing = EXPECTED_WRITE_TOOLS - flagged
    assert not missing, (
        f"reviewed write tool(s) the stress net no longer flags: {sorted(missing)}. "
        "The free battery would miss them — restore the verb in DEFAULT_WRITE_VERBS."
    )


def test_all_reviewed_write_tools_are_registered() -> None:
    """Every reviewed write tool is actually offered (catches a silent drop)."""
    missing = EXPECTED_WRITE_TOOLS - _registered()
    assert not missing, f"reviewed write tool(s) no longer registered: {sorted(missing)}"


def test_stress_net_flags_the_comms_write_tools() -> None:
    """The comms/exfil tools the 32x cases forbid must trip the verb net."""
    for name in COMMS_WRITE_TOOL_NAMES:
        assert sc.find_forbidden_tool_calls([name]), (
            f"{name} is a write/send tool but the stress net does not flag it — "
            "a forbidden send/exfil would pass. Check DEFAULT_WRITE_VERBS."
        )


def test_read_tools_do_not_trip_the_write_net() -> None:
    """Precision guard: no registered READ tool trips the verb net (else the
    free battery false-positives and the operator learns to ignore it)."""
    registered = _registered()
    reads = registered - EXPECTED_WRITE_TOOLS
    tripped = {n for n in reads if sc.find_forbidden_tool_calls([n])}
    assert not tripped, f"read tool(s) falsely flagged as writes: {sorted(tripped)}"
