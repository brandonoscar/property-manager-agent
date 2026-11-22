"""Abstract base + shared types for Phase A.2 tool executors.

Every concrete handler implements :meth:`Handler.execute` returning a
``HandlerResult``: a small dict with ``buildium_id`` (the entity the
write affected) and ``summary`` (a human-readable one-liner the UI
toasts and the activity log uses for the description).

Handlers raise :class:`HandlerError` on user-facing failures (bad
parameters, Buildium rejecting the write, the account lacking the
required scope). The execute endpoint catches and surfaces these as
HTTP 502 with the error message and the offending ``tool_id``.
Internal-only exceptions (programming errors, asyncio plumbing) are
allowed to propagate so they show up in Sentry.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from helixis.buildium.client import BuildiumClient


class HandlerError(RuntimeError):
    """User-visible execution failure.

    ``tool_id`` is set by the executor at the call site so the wrapping
    HTTP layer can include it in the 502 response without each handler
    having to remember to attach it. ``status_code`` lets a handler
    request a specific upstream HTTP status (e.g. 501 when a Buildium
    feature isn't exposed via API for the company's account scope)."""

    def __init__(
        self,
        message: str,
        *,
        tool_id: str | None = None,
        status_code: int = 502,
    ) -> None:
        super().__init__(message)
        self.tool_id = tool_id
        self.status_code = status_code


HandlerResult = dict[str, Any]
"""Loose alias for the dict shape every handler returns. Always
contains at minimum ``buildium_id: int | None`` and ``summary: str``."""


def guard_fair_housing(text: str, *, tool_id: str, prompt: str = "") -> None:
    """Egress fair-housing gate (#49) — refuse to send tenant/applicant-facing
    text that trips the deterministic steering / missing-deflection check.

    Called at the OUTBOUND boundary of the communication handlers, right before
    the message leaves the system. Every outbound message is human-reviewed
    first, so a block here is a "revise and resend" prompt, not a silent drop —
    the safe fail direction for fair-housing liability. ``check_draft`` is the
    tuned, negation-guarded gate (empty list = pass); ``prompt`` is the original
    request when available (enables the protected-class-question deflection
    check), else "" (steering-in-outbound only). Raises HandlerError(422) on a
    violation; returns None on pass.
    """
    from helixis.security.fair_housing_gate import check_draft

    violations = check_draft(prompt, text)
    if violations:
        raise HandlerError(
            "Blocked by the fair-housing check ("
            + ", ".join(violations)
            + "). Revise the message to remove steering or discriminatory "
            "language and resend.",
            tool_id=tool_id,
            status_code=422,
        )


class Handler(ABC):
    """One executable Phase A.2 tool."""

    tool_id: str
    """Must match the planner catalog's ``ToolSpec.id``."""

    @abstractmethod
    async def execute(
        self,
        client: BuildiumClient,
        params: dict[str, Any],
        *,
        context: dict[str, Any] | None = None,
    ) -> HandlerResult:
        """Run the tool against Buildium.

        ``params`` is the already-merged parameter dict (planner
        proposal + user overrides). ``context`` carries non-parameter
        hints from the executor — currently ``event_task_id`` so
        ``add_internal_note`` can resolve the parent task without
        forcing the planner to include it in every parameter set.
        """
        raise NotImplementedError
