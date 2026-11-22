"""Latency budgets (SLOs) + over-budget alerting.

Timing already exists across the run path — the orchestrator computes a
per-run ``latency_ms`` and a per-tool ``duration_ms``. What this module adds
is the *budget* and the *alert*: a threshold per operation kind and, when an
operation blows past it, a signal through the observability seams we already
have (structured WARNING log + a Langfuse WARNING event/score) so a slow path
shows up on the dashboards instead of dissolving into an average.

Everything here is **best-effort** and advisory:

* A budget breach NEVER affects the request — it only emits telemetry. The
  caller decides whether to add its own signal (e.g. a PostHog capture) from
  the returned :class:`LatencyBreach`.
* Disabled cleanly when latency monitoring is off in settings (every check a
  no-op), mirroring the ``configure_langfuse`` / ``configure_tracer`` gate.
* Any failure in the alerting itself is swallowed — observability must never
  break the product.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from helixis.settings import Settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LatencyBudgets:
    """Resolved budgets, in milliseconds, for the monitored operation kinds."""

    agent_run_ms: int
    tool_call_ms: int


@dataclass(frozen=True)
class LatencyBreach:
    """Returned when an operation exceeded its budget. ``over_ratio`` is the
    elapsed/budget multiple (2.0 == twice the budget), handy for charting."""

    kind: str
    label: str
    elapsed_ms: int
    budget_ms: int
    over_ms: int
    over_ratio: float


# Active budgets, or None when latency monitoring is off.
_budgets: LatencyBudgets | None = None


def configure_latency(settings: Settings) -> bool:
    """Resolve latency budgets from settings. Returns True when active.

    Mirrors ``configure_tracer``'s gating: off (every check a no-op) when
    disabled in settings.
    """
    global _budgets
    cfg = settings.latency
    if not cfg.enabled:
        _budgets = None
        logger.info("Latency budgets disabled in settings — alerting off")
        return False
    _budgets = LatencyBudgets(
        agent_run_ms=cfg.agent_run_ms,
        tool_call_ms=cfg.tool_call_ms,
    )
    logger.info(
        "Latency budgets active — agent_run=%dms tool_call=%dms",
        cfg.agent_run_ms,
        cfg.tool_call_ms,
    )
    return True


def is_active() -> bool:
    return _budgets is not None


def _budget_for(kind: str) -> int | None:
    if _budgets is None:
        return None
    if kind == "agent_run":
        return _budgets.agent_run_ms
    if kind == "tool_call":
        return _budgets.tool_call_ms
    return None


def check_latency(
    kind: str,
    elapsed_ms: int,
    *,
    trace_id: str = "",
    session_id: str = "",
    company_id: str = "",
    label: str = "",
) -> LatencyBreach | None:
    """Compare an operation's elapsed time to its budget.

    Returns a :class:`LatencyBreach` (and emits the log + Langfuse alert) when
    over budget; returns ``None`` when within budget, when the kind isn't
    monitored, or when latency monitoring is off. Never raises.
    """
    budget = _budget_for(kind)
    if budget is None or budget <= 0 or elapsed_ms <= budget:
        return None

    over = elapsed_ms - budget
    breach = LatencyBreach(
        kind=kind,
        label=label or kind,
        elapsed_ms=elapsed_ms,
        budget_ms=budget,
        over_ms=over,
        over_ratio=round(elapsed_ms / budget, 2),
    )

    logger.warning(
        "latency over budget: kind=%s label=%s elapsed=%dms budget=%dms "
        "over=%dms (%.2f× budget) session=%s company=%s",
        breach.kind,
        breach.label,
        breach.elapsed_ms,
        breach.budget_ms,
        breach.over_ms,
        breach.over_ratio,
        session_id or "-",
        company_id or "-",
    )

    # Best-effort Langfuse signal: a WARNING event to drill into + a numeric
    # score to chart the breach magnitude over time. Imported lazily so the
    # module carries no hard Langfuse dependency and no import cycle.
    try:
        from helixis.observability import tracing

        if trace_id and tracing.is_active():
            tracing.record_event(
                trace_id=trace_id,
                name="latency_over_budget",
                payload={
                    "kind": breach.kind,
                    "label": breach.label,
                    "elapsed_ms": breach.elapsed_ms,
                    "budget_ms": breach.budget_ms,
                    "over_ms": breach.over_ms,
                    "over_ratio": breach.over_ratio,
                },
                level="WARNING",
            )
            tracing.record_score(
                trace_id=trace_id,
                name=f"latency_over_budget:{breach.kind}",
                value=float(breach.over_ms),
                comment=f"{breach.label} {breach.elapsed_ms}ms vs {breach.budget_ms}ms budget",
            )
    except Exception:  # noqa: BLE001 — alerting must never break the run
        logger.debug("latency Langfuse alert failed (non-fatal)", exc_info=True)

    return breach


__all__ = [
    "LatencyBreach",
    "LatencyBudgets",
    "check_latency",
    "configure_latency",
    "is_active",
]
