"""Tier-2 action guard — the structural boundary on what a code run may PROPOSE.

The code-execution sandbox itself can never act (it has no write path — see
``executor.py``). Tier 2 lets a run instead return a list of *proposed*
structured actions that the user can review and apply through the EXISTING
confirm-gated write tools. This module is the boundary that decides which
actions are even proposable — and it is a **pure allow-list**, so the founder's
forbidden set is enforced structurally, not by a prompt the model could talk
its way past:

* **Money-writes** (post charge, record payment, create bill) — NOT proposable.
* **Irreversible** writes (close a work order, renew a lease) — NOT proposable.
* **Fair-housing-sensitive** decisions (applicant status) — NOT proposable.
* **Tenant communications** (send message / reply email) — NOT proposable here;
  they route through the drafting + fair-housing flow, not auto-proposal.
* **Bulk** — a per-run cap; a run cannot propose an unbounded batch.
* Anything not on the allow-list (creating properties, ANY delete, …) — there
  is no action type for it, so it cannot be expressed at all.

What *is* proposable is a small set of reversible, non-financial, individually
gated writes (tasks, notes, status, teammate hand-offs). Applying them (Inc.4b)
still goes through each tool's own confirm gate + role floor — this guard only
decides eligibility; it does not itself write anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Reversible, non-financial, individually-gated writes a code run may propose.
#: Add to this list ONLY writes that are safe to undo and carry their own
#: confirm gate at the tool layer.
ALLOWED_ACTIONS: frozenset[str] = frozenset(
    {
        # task lifecycle (reversible)
        "create_followup_task",
        "update_buildium_task",
        "buildium_create_to_do_task",
        "buildium_update_task_status",
        # notes (additive, low blast radius)
        "add_internal_note",
        "buildium_add_task_note",
        "buildium_add_work_order_note",
        "buildium_add_vendor_note",
        # internal hand-off (lands in a teammate's inbox; reversible)
        "send_task_to_teammate",
    }
)

#: Human-readable reasons for the notable forbidden actions, so a rejection
#: explains WHY (better than a generic "not allowed"). An action absent from
#: both sets is rejected as simply not proposable.
_FORBIDDEN_REASONS: dict[str, str] = {
    "buildium_post_charge": "money-write — charges are not reversible",
    "buildium_record_payment": "money-write — payments are immutable",
    "buildium_create_bill": "money-write — accounts payable",
    "buildium_close_work_order": "irreversible — closing a work order is one-way",
    "buildium_renew_lease": "irreversible — a renewal creates a real lease term",
    "buildium_record_move_out": "lease-lifecycle write — not auto-proposable",
    "buildium_update_work_order": "vendor/scheduling write — not auto-proposable",
    "buildium_create_work_order": "dispatch write — not auto-proposable",
    "buildium_update_applicant_status": "fair-housing-sensitive applicant decision",
    "send_tenant_message": "tenant communication — route through the drafting flow",
    "reply_to_email": "tenant communication — route through the drafting flow",
}

#: A single run may not propose more than this many actions (the bulk guard).
MAX_PROPOSED_ACTIONS: int = 25


@dataclass
class ReviewedAction:
    """One proposed action after review."""

    action: str
    args: dict
    allowed: bool
    reason: str | None = None


@dataclass
class ProposalReview:
    """Partitioned result of reviewing a run's proposed actions."""

    allowed: list[ReviewedAction] = field(default_factory=list)
    rejected: list[ReviewedAction] = field(default_factory=list)
    #: True when the proposal exceeded ``MAX_PROPOSED_ACTIONS`` and the overflow
    #: was rejected as bulk.
    truncated: bool = False


def review_proposed_actions(actions: object) -> ProposalReview:
    """Partition ``actions`` into what may be applied vs. what is refused.

    Pure and total: any malformed input, forbidden action, or over-cap overflow
    lands in ``rejected`` with a reason — it never raises and never lets a
    forbidden action through. ``actions`` is expected to be a list of
    ``{"action": str, "args": dict}`` mappings.
    """
    review = ProposalReview()
    if not isinstance(actions, list):
        return review

    for idx, raw in enumerate(actions):
        over_cap = idx >= MAX_PROPOSED_ACTIONS
        if not isinstance(raw, dict):
            review.rejected.append(
                ReviewedAction(action="<malformed>", args={}, allowed=False, reason="not an object")
            )
            continue
        name = raw.get("action")
        args = raw.get("args")
        if not isinstance(args, dict):
            args = {}
        if not isinstance(name, str) or not name.strip():
            review.rejected.append(
                ReviewedAction(
                    action="<malformed>",
                    args=args,
                    allowed=False,
                    reason="missing an `action` name",
                )
            )
            continue
        name = name.strip()

        if over_cap:
            review.truncated = True
            review.rejected.append(
                ReviewedAction(
                    action=name,
                    args=args,
                    allowed=False,
                    reason=(
                        f"exceeds the per-run cap of {MAX_PROPOSED_ACTIONS} "
                        "actions — bulk writes are not allowed"
                    ),
                )
            )
            continue

        if name in ALLOWED_ACTIONS:
            review.allowed.append(ReviewedAction(action=name, args=args, allowed=True))
        else:
            reason = _FORBIDDEN_REASONS.get(name, "not an allowed action type")
            review.rejected.append(
                ReviewedAction(action=name, args=args, allowed=False, reason=reason)
            )

    return review


__all__ = [
    "ALLOWED_ACTIONS",
    "MAX_PROPOSED_ACTIONS",
    "ProposalReview",
    "ReviewedAction",
    "review_proposed_actions",
]
