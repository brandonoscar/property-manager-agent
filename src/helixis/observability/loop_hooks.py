"""Reasoning-loop "why" hooks (W4 task #24) — dogfood-prep step 1.

Captures the loop's decision rationale as STRUCTURED metadata on the run's
existing Langfuse trace, so a dogfood failure is a diagnosable event instead of
a vibe. Four captures per loop turn, emitted as one
``reasoning_loop.turn_summary`` event (+ a ``footer_compliant`` score):

* **depth** — deep vs shallow, DERIVED from what the run observably did (any
  specialist consulted → deep). The model's internal signal is not readable
  without a prompt/schema change, so the "why" is the evidence trail: which
  specialists, in what order, and whether known state was checked first.
* **stop_reason** — completed / wall_clock_deadline / stall / max_turns /
  llm_transient_error / error, stamped at the orchestrator's exit paths.
* **specialist_calls** — ordered, with per-sub-run latency and token counts.
  Tokens ride the same mechanism as ``llm/spend.py``'s SpendTracker (LiteLLM
  success callback + contextvars, proven live): each specialist ``.as_tool()``
  invoke is wrapped to label its context, so every LLM completion inside the
  sub-run attributes to that call; unlabeled completions belong to the
  orchestrator itself.
* **footer_status** — emitted / malformed / missing per turn, the compliance
  data that un-parks the REVERSE_REPORT stacking decision (#21).

Everything here is best-effort: attribution never raises (accounting must
never break a call — the SpendTracker rule), and an unbound stats contextvar
makes every hook a no-op, so the handoff path and tests pay nothing.
"""

from __future__ import annotations

import logging
import time
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any

import litellm
from litellm.integrations.custom_logger import CustomLogger

from helixis.agents.synthesis_contract import parse_synthesis_footer, validate_synthesis

logger = logging.getLogger(__name__)


# ── per-run stats ────────────────────────────────────────────────


@dataclass
class SpecialistCall:
    """One specialist-tool invocation inside a loop turn."""

    name: str
    duration_ms: int | None = None  # None while in flight
    tokens: int = 0


@dataclass
class LoopStats:
    """Mutable per-run accumulator. Bound to a contextvar BEFORE the streamed
    run starts (like the confirm emitter), so it propagates by reference into
    every sub-run task and every LiteLLM callback."""

    calls: list[SpecialistCall] = field(default_factory=list)
    orchestrator_tokens: int = 0
    stop_reason: str = "completed"

    def open_call(self, name: str) -> SpecialistCall:
        call = SpecialistCall(name=name)
        self.calls.append(call)
        return call

    @property
    def total_tokens(self) -> int:
        return self.orchestrator_tokens + sum(c.tokens for c in self.calls)


_loop_stats: ContextVar[LoopStats | None] = ContextVar("helixis_loop_stats", default=None)
# The in-flight specialist call for THIS context. Set inside the wrapped tool
# invoke, so parallel tool calls (each its own task, own context copy) label
# their own sub-runs independently while sharing one LoopStats by reference.
_current_call: ContextVar[SpecialistCall | None] = ContextVar(
    "helixis_loop_current_call", default=None
)


def bind_loop_stats(stats: LoopStats) -> Token[LoopStats | None]:
    return _loop_stats.set(stats)


def reset_loop_stats(token: Token[LoopStats | None]) -> None:
    _loop_stats.reset(token)


def current_loop_stats() -> LoopStats | None:
    return _loop_stats.get()


# ── token attribution (SpendTracker mechanism) ───────────────────


def _total_tokens(response_obj: Any) -> int:
    usage = getattr(response_obj, "usage", None)
    if usage is None and isinstance(response_obj, dict):
        usage = response_obj.get("usage")
    if usage is None:
        return 0
    total = getattr(usage, "total_tokens", None)
    if total is None and isinstance(usage, dict):
        total = usage.get("total_tokens")
    try:
        return max(0, int(total or 0))
    except (TypeError, ValueError):
        return 0


class LoopTokenAttributor(CustomLogger):
    """LiteLLM success hook: attribute each completion's tokens to the
    in-flight specialist call (or the orchestrator bucket). Never raises."""

    async def async_log_success_event(
        self, kwargs: Any, response_obj: Any, start_time: Any, end_time: Any
    ) -> None:
        try:
            stats = _loop_stats.get()
            if stats is None:
                return  # not a loop turn — nothing to attribute
            tokens = _total_tokens(response_obj)
            call = _current_call.get()
            if call is not None:
                call.tokens += tokens
            else:
                stats.orchestrator_tokens += tokens
        except (Exception, SystemExit):  # noqa: BLE001 — accounting never breaks a call
            logger.debug("loop token attribution skipped for one call", exc_info=True)


_configured = False


def configure_loop_token_attribution() -> None:
    """Register the attributor once per process. Cheap and idempotent — called
    lazily from the loop branch so non-loop deployments never register it."""
    global _configured
    if _configured:
        return
    litellm.callbacks = list(getattr(litellm, "callbacks", None) or []) + [LoopTokenAttributor()]
    _configured = True
    logger.info("reasoning-loop token attribution enabled")


# ── specialist instrumentation ───────────────────────────────────


def instrument_specialist_tool(tool: Any) -> Any:
    """Clone a specialist FunctionTool so each invoke opens a labelled
    :class:`SpecialistCall` (ordered trail + wall latency) and labels its
    context for token attribution. Same clone pattern as
    ``tool_gate.fence_tool_error``; exceptions propagate UNCHANGED so the
    ``failure_error_function`` fencing behavior is untouched."""
    from agents import FunctionTool

    original_invoke = getattr(tool, "on_invoke_tool", None)
    if original_invoke is None:
        return tool  # sentinel / non-FunctionTool — nothing to instrument

    async def instrumented_invoke(ctx: Any, payload: str) -> Any:
        stats = _loop_stats.get()
        if stats is None:
            return await original_invoke(ctx, payload)
        call = stats.open_call(tool.name)
        token = _current_call.set(call)
        started = time.monotonic()
        try:
            return await original_invoke(ctx, payload)
        finally:
            _current_call.reset(token)
            call.duration_ms = int((time.monotonic() - started) * 1000)

    clone = FunctionTool(
        name=tool.name,
        description=tool.description,
        params_json_schema=tool.params_json_schema,
        on_invoke_tool=instrumented_invoke,
        strict_json_schema=tool.strict_json_schema,
    )
    # Wiring-test marker (see test_loop_why_hooks).
    clone._loop_instrumented = True  # type: ignore[attr-defined]
    return clone


# ── footer compliance (feeds the #21 stacking decision) ──────────


def classify_footer(text: str) -> str:
    """``emitted`` (well-formed) / ``malformed`` (some contract present, some
    violated) / ``missing`` (no footer at all)."""
    if parse_synthesis_footer(text).well_formed:
        return "emitted"
    violations = validate_synthesis(text)
    missing_all = sum(1 for v in violations if v.startswith("missing")) >= 3
    return "missing" if missing_all else "malformed"


# ── the turn summary ─────────────────────────────────────────────

# Last emitted summary, for in-process harnesses (scripts/loop_baseline.py)
# that need the numbers without round-tripping through Langfuse. Overwritten
# per turn; never read on the request path.
LAST_TURN_SUMMARY: dict[str, Any] | None = None


def turn_summary_payload(
    stats: LoopStats,
    *,
    footer_status: str,
    specialist_names: frozenset[str],
    latency_ms: int | None = None,
) -> dict[str, Any]:
    """Assemble the structured ``reasoning_loop.turn_summary`` event payload.
    Pure — unit-testable without Langfuse."""
    global LAST_TURN_SUMMARY
    real_specialists = [c for c in stats.calls if c.name in specialist_names]
    # DERIVED trail shape (baseline round 1 relabel): the old deep/shallow
    # binary called a correct single-lookup turn "deep" (any specialist call)
    # while a 95k-token single-sub-run cross-table turn would have read
    # "shallow" under a count threshold — the binary carries no information
    # beyond the counts. Descriptive labels instead: none / single_source /
    # multi_source, judged purely by how many distinct gather calls ran
    # (step-zero recall excluded — it's recall, not gathering). Token weight
    # lives in specialist_calls; depth-QUALITY verdicts belong to the evals.
    if not real_specialists:
        depth = "none"
    elif len(real_specialists) == 1:
        depth = "single_source"
    else:
        depth = "multi_source"
    payload: dict[str, Any] = {
        "depth": depth,
        "specialist_count": len(real_specialists),
        "known_state_checked": any(c.name == "check_known_state" for c in stats.calls),
        "specialist_calls": [
            {"name": c.name, "duration_ms": c.duration_ms, "tokens": c.tokens} for c in stats.calls
        ],
        "orchestrator_tokens": stats.orchestrator_tokens,
        "total_tokens": stats.total_tokens,
        "stop_reason": stats.stop_reason,
        "footer_status": footer_status,
    }
    if latency_ms is not None:
        payload["latency_ms"] = latency_ms
    LAST_TURN_SUMMARY = payload
    return payload


__all__ = [
    "LAST_TURN_SUMMARY",
    "LoopStats",
    "LoopTokenAttributor",
    "SpecialistCall",
    "bind_loop_stats",
    "classify_footer",
    "configure_loop_token_attribution",
    "current_loop_stats",
    "instrument_specialist_tool",
    "reset_loop_stats",
    "turn_summary_payload",
]
