"""REVERSE_REPORT enforcement guardrail (Slice 2b).

Wires the pure :func:`helixis.agents.reverse_report.evaluate_write` decision
into the Agents SDK as a **tool-input guardrail** — a native pre-dispatch
hook that runs *before* a tool executes and can block it.

How it works:

* The guardrail is attached to side-effectful tools (Buildium writes etc.)
  via :func:`attach_reverse_report_guardrail`.
* On each gated tool call it reads the turn's assistant output from
  ``ToolContext.turn_input``, parses the REVERSE_REPORT the model emitted,
  and asks :func:`evaluate_write` whether the write is justified.
* If not, it returns ``reject_content`` — which blocks the write AND feeds
  the violation reasons back to the model (a built-in nudge), rather than
  ``raise_exception`` which would kill the whole run.

Two safety properties, both important because this fires only on live model
runs (it can't be exercised by the offline eval suite without a model):

1. **Runtime flag gate.** The guardrail checks ``settings.agent.reverse_report``
   on every call and allows immediately when off. Buildium tools are
   module-level singletons, so attaching is permanent for the process; gating
   at call time (not attach time) keeps the flag-off default a true no-op
   regardless of attachment lifetime.
2. **Fail-open.** Any unexpected error inside the guardrail logs and ALLOWS
   the tool. A bug in enforcement must never break a real write — the
   contract is a safety net, not a new single point of failure.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from agents import (
    ToolGuardrailFunctionOutput,
    ToolInputGuardrail,
    ToolInputGuardrailData,
)

from helixis.agents.reverse_report import (
    BUILDIUM_WRITE_MIN_CONFIDENCE,
    DEFAULT_WRITE_MIN_CONFIDENCE,
    ReverseReport,
    evaluate_write,
    is_side_effectful_tool,
    parse_reverse_report,
)
from helixis.observability.reason_codes import record_reason_code
from helixis.settings import get_settings

logger = logging.getLogger(__name__)


def _assistant_text(item: Any) -> str:
    """Best-effort extraction of assistant text from a turn-input item.

    Handles both the Responses-API dict shape (``{"role": "assistant",
    "content": "..."}`` or a content-parts list) and object-shaped items.
    Returns ``""`` for non-assistant or unrecognized items.
    """
    role = item.get("role") if isinstance(item, dict) else getattr(item, "role", None)
    if role != "assistant":
        return ""
    content = item.get("content") if isinstance(item, dict) else getattr(item, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                text = part.get("text") or part.get("output_text") or ""
            else:
                text = getattr(part, "text", "") or ""
            if text:
                parts.append(str(text))
        return "\n".join(parts)
    return ""


def extract_latest_reverse_report(
    turn_input: list[Any] | None,
) -> tuple[ReverseReport | None, str | None]:
    """Find + parse the most recent REVERSE_REPORT in the turn's items.

    Walks ``turn_input`` newest-first and returns the first assistant
    message that carries a parseable report. Returns ``(None, error)`` when
    none is found.
    """
    last_error = "no REVERSE_REPORT found in this turn's assistant output"
    for item in reversed(turn_input or []):
        text = _assistant_text(item)
        if not text:
            continue
        report, err = parse_reverse_report(text)
        if report is not None:
            return report, None
        if err:
            last_error = err
    return None, last_error


def _min_confidence_for(tool_name: str) -> float:
    """Buildium writes need >= 0.80 (names aren't unique, IDs are); others 0.6."""
    if tool_name.startswith("buildium_"):
        return BUILDIUM_WRITE_MIN_CONFIDENCE
    return DEFAULT_WRITE_MIN_CONFIDENCE


def _evaluate(data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
    """The guardrail body. Fail-open on any internal error."""
    try:
        if not get_settings().agent.reverse_report:
            return ToolGuardrailFunctionOutput.allow()

        ctx = data.context
        tool_name = str(getattr(ctx, "tool_name", "") or "")
        if not is_side_effectful_tool(tool_name):
            return ToolGuardrailFunctionOutput.allow()

        report, _err = extract_latest_reverse_report(getattr(ctx, "turn_input", None))
        # Writes self-gate on confirmation downstream via ``confirm_or_abort``,
        # so we do not block rule 2 (inferred-requires-confirm) here — pass
        # confirm_present=True and let the existing confirm gate own it.
        decision = evaluate_write(
            report,
            confirm_present=True,
            min_confidence=_min_confidence_for(tool_name),
        )
        if decision.allowed:
            return ToolGuardrailFunctionOutput.allow(output_info={"tool": tool_name})

        message = (
            f"Blocked by the REVERSE_REPORT contract — do NOT retry this {tool_name} "
            "write as-is. Resolve these, then re-decide (ask the user or gather more "
            "observations): " + "; ".join(decision.reasons)
        )
        record_reason_code(
            "rr_write_blocked",
            detail=f"{tool_name}: " + "; ".join(decision.reasons)[:300],
        )
        return ToolGuardrailFunctionOutput.reject_content(
            message=message,
            output_info={"tool": tool_name, "reasons": decision.reasons},
        )
    except Exception as exc:  # noqa: BLE001 — enforcement must never break a write
        logger.exception("reverse_report guardrail error; allowing tool to proceed")
        return ToolGuardrailFunctionOutput.allow(output_info={"guardrail_error": repr(exc)})


# Single shared instance — stateless, safe to attach to shared tool singletons.
REVERSE_REPORT_TOOL_GUARDRAIL: ToolInputGuardrail[Any] = ToolInputGuardrail(
    guardrail_function=_evaluate, name="reverse_report"
)


def attach_reverse_report_guardrail(tools: Sequence[Any]) -> Sequence[Any]:
    """Idempotently attach the guardrail to every side-effectful tool.

    Mutates each tool's ``tool_input_guardrails`` in place (returns the same
    list for convenience). Idempotent: re-attaching is a no-op, so calling
    this on every agent build is safe even though Buildium tools are shared
    singletons. The guardrail itself is inert when the flag is off, so this
    is safe to call unconditionally.
    """
    for tool in tools:
        name = str(getattr(tool, "name", "") or "")
        if not is_side_effectful_tool(name):
            continue
        if not hasattr(tool, "tool_input_guardrails"):
            continue
        existing = list(getattr(tool, "tool_input_guardrails", None) or [])
        if REVERSE_REPORT_TOOL_GUARDRAIL in existing:
            continue
        existing.append(REVERSE_REPORT_TOOL_GUARDRAIL)
        tool.tool_input_guardrails = existing
    return tools


__all__ = [
    "REVERSE_REPORT_TOOL_GUARDRAIL",
    "attach_reverse_report_guardrail",
    "extract_latest_reverse_report",
]
