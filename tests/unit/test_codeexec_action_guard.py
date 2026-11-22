"""The Tier-2 action guard — the forbidden-set boundary.

This is the load-bearing safety test for code-execution writes: the guard must
let the small allow-listed set through AND refuse every money-write, every
irreversible write, fair-housing decisions, tenant comms, unknown actions, and
bulk batches. If any of these regress, "compute never act" leaks into "act".
"""

from __future__ import annotations

import pytest

from helixis.codeexec.action_guard import (
    ALLOWED_ACTIONS,
    MAX_PROPOSED_ACTIONS,
    review_proposed_actions,
)


def _names(reviewed) -> set[str]:
    return {r.action for r in reviewed}


# ── the allow-list passes ────────────────────────────────────────────────


def test_allowed_actions_pass() -> None:
    actions = [{"action": name, "args": {}} for name in sorted(ALLOWED_ACTIONS)]
    review = review_proposed_actions(actions)
    assert _names(review.allowed) == set(ALLOWED_ACTIONS)
    assert review.rejected == []


def test_allowed_action_keeps_its_args() -> None:
    review = review_proposed_actions(
        [{"action": "create_followup_task", "args": {"title": "Inspect 2B"}}]
    )
    assert review.allowed[0].args == {"title": "Inspect 2B"}


# ── the forbidden classes are refused, with reasons ──────────────────────


@pytest.mark.parametrize(
    "action,needle",
    [
        ("buildium_post_charge", "money"),
        ("buildium_record_payment", "money"),
        ("buildium_create_bill", "money"),
        ("buildium_close_work_order", "irreversible"),
        ("buildium_renew_lease", "irreversible"),
        ("buildium_update_applicant_status", "fair-housing"),
        ("send_tenant_message", "communication"),
        ("reply_to_email", "communication"),
    ],
)
def test_forbidden_actions_rejected_with_reason(action: str, needle: str) -> None:
    review = review_proposed_actions([{"action": action, "args": {}}])
    assert review.allowed == []
    assert len(review.rejected) == 1
    assert needle in (review.rejected[0].reason or "")


def test_none_of_the_forbidden_are_on_the_allow_list() -> None:
    forbidden = {
        "buildium_post_charge",
        "buildium_record_payment",
        "buildium_create_bill",
        "buildium_close_work_order",
        "buildium_renew_lease",
        "buildium_update_applicant_status",
        "send_tenant_message",
        "reply_to_email",
    }
    assert forbidden.isdisjoint(ALLOWED_ACTIONS)


def test_unknown_action_is_rejected() -> None:
    review = review_proposed_actions([{"action": "delete_everything", "args": {}}])
    assert review.allowed == []
    assert "not an allowed action type" in (review.rejected[0].reason or "")


def test_property_create_and_delete_are_not_expressible() -> None:
    # There is no allow-listed action for creating properties or deleting
    # anything, so such a proposal simply can't pass.
    review = review_proposed_actions(
        [
            {"action": "buildium_create_property", "args": {}},
            {"action": "buildium_delete_lease", "args": {}},
            {"action": "delete_tenant", "args": {}},
        ]
    )
    assert review.allowed == []
    assert len(review.rejected) == 3


# ── bulk cap ─────────────────────────────────────────────────────────────


def test_bulk_over_cap_is_rejected() -> None:
    actions = [{"action": "add_internal_note", "args": {}}] * (MAX_PROPOSED_ACTIONS + 5)
    review = review_proposed_actions(actions)
    assert len(review.allowed) == MAX_PROPOSED_ACTIONS
    assert len(review.rejected) == 5
    assert review.truncated is True
    assert "bulk" in (review.rejected[0].reason or "")


# ── malformed input never raises / never leaks ───────────────────────────


def test_malformed_inputs_are_rejected_not_raised() -> None:
    review = review_proposed_actions(
        [
            "just a string",
            {"args": {}},  # no action name
            {"action": "   ", "args": {}},  # blank name
            {"action": "add_internal_note"},  # no args → defaults to {}
        ]
    )
    # Only the last (a real allow-listed action) passes; the rest are refused.
    assert _names(review.allowed) == {"add_internal_note"}
    assert len(review.rejected) == 3


def test_non_list_input_yields_empty_review() -> None:
    review = review_proposed_actions({"action": "add_internal_note"})
    assert review.allowed == []
    assert review.rejected == []
