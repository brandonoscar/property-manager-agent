"""Apply-orchestration for Tier-2 proposed actions (Inc.4b).

This is the layer that turns a reviewed proposal into applied writes — and the
single guarantee it enforces is: **a rejected action can never reach a write.**
Only actions the guard (``action_guard.review_proposed_actions``) marks allowed
are handed to ``dispatch``; everything the guard refuses (money-writes,
irreversible, fair-housing, comms, bulk-overflow, unknown) is reported, never
dispatched.

``dispatch`` is dependency-injected — a ``Callable[[str, dict], Awaitable]``
that performs ONE allowed write through its existing confirm-gated tool (that
tool owns the per-write approval + role floor). Keeping it injected makes this
orchestration pure and exhaustively testable without touching real tools; the
concrete per-tool adapters land in Inc.4c.

Per-action isolation: one action failing (or being declined at its confirm
gate) never aborts the rest — each outcome is recorded independently, the same
crash-isolation posture as the detector runner.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from helixis.codeexec.action_guard import ReviewedAction, review_proposed_actions

logger = logging.getLogger(__name__)

#: A dispatcher performs ONE allowed action through its gated tool and returns a
#: small JSON-friendly outcome (or raises to signal failure / a declined gate).
Dispatch = Callable[[str, dict], Awaitable[Any]]


@dataclass
class AppliedOutcome:
    """The result of attempting ONE allowed action."""

    action: str
    ok: bool
    outcome: Any = None
    error: str | None = None


@dataclass
class ApplyReport:
    """Structured result of applying a proposal."""

    applied: list[AppliedOutcome] = field(default_factory=list)
    #: Actions the guard refused — these were NEVER dispatched.
    rejected: list[ReviewedAction] = field(default_factory=list)
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "applied": [
                {"action": a.action, "ok": a.ok, "outcome": a.outcome, "error": a.error}
                for a in self.applied
            ],
            "rejected": [{"action": r.action, "reason": r.reason} for r in self.rejected],
            "truncated": self.truncated,
        }


async def apply_proposed_actions(actions: object, dispatch: Dispatch) -> ApplyReport:
    """Review ``actions`` and apply only the allowed ones via ``dispatch``.

    Total + isolating: never raises for a single action's failure, and NEVER
    dispatches an action the guard refused.
    """
    review = review_proposed_actions(actions)
    report = ApplyReport(rejected=review.rejected, truncated=review.truncated)

    for act in review.allowed:
        try:
            outcome = await dispatch(act.action, act.args)
            report.applied.append(AppliedOutcome(action=act.action, ok=True, outcome=outcome))
        except Exception as exc:  # noqa: BLE001 — isolate each action; report, never abort
            logger.exception("codeexec apply: action %s failed", act.action)
            report.applied.append(
                AppliedOutcome(
                    action=act.action, ok=False, error=str(exc) or exc.__class__.__name__
                )
            )

    return report


__all__ = ["AppliedOutcome", "ApplyReport", "Dispatch", "apply_proposed_actions"]
