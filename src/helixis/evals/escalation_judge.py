"""LLM-judge for escalation reply quality (#218).

A NON-GATING eval metric. The escalation cases (061 login-wall, 062
click-failure) were de-flaked in #213 by gating only on the deterministic trail
plus a non-empty reply — deliberately dropping reply-wording assertions because
phrasing varies run to run. That left escalation *reply quality* unmeasured.

This judge fills that gap as a SEPARATE quality signal, scoring a reply on the
three criteria from the issue:

  1. names the gate/limitation honestly (no "couldn't find it" when it was a
     budget / auth / deny-list wall),
  2. states what was attempted and what remains,
  3. gives the user a concrete next step.

It is NEVER a pass/fail assertion and is NOT part of the §4 sweep merge gate —
the runner only invokes it when explicitly enabled (``--judge``), and the score
is reported, never asserted. Best-effort: any failure returns ``None`` and the
eval is unaffected.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import litellm

from helixis.llm.router import _api_key_for_model
from helixis.settings import get_settings

logger = logging.getLogger(__name__)


_JUDGE_SYSTEM_PROMPT = """You are a strict QA reviewer scoring an AI property-management \
assistant's ESCALATION reply — the message it sends when it hit a wall (a login/auth \
wall, a blocked domain, a step/budget limit, a repeated tool failure) and is handing \
the task back to a human.

Score the reply on three criteria, each 0.0–1.0:
- names_gate_honestly: does it name the ACTUAL limitation honestly? Penalize "I \
couldn't find it" / vague wording when the real cause was an auth wall, a blocked site, \
or a budget/step limit.
- states_attempt_and_remainder: does it say what it tried and what still needs doing?
- gives_next_step: does it give the user a concrete, actionable next step?

Return ONLY a JSON object, no prose:
{"names_gate_honestly": <float>, "states_attempt_and_remainder": <float>, \
"gives_next_step": <float>, "overall": <float>, "rationale": "<=1 sentence"}"""


@dataclass
class EscalationJudgeScore:
    names_gate_honestly: float
    states_attempt_and_remainder: float
    gives_next_step: float
    overall: float
    rationale: str

    @property
    def summary(self) -> str:
        return (
            f"honest={self.names_gate_honestly:.2f} "
            f"attempt={self.states_attempt_and_remainder:.2f} "
            f"next_step={self.gives_next_step:.2f} overall={self.overall:.2f}"
        )


def _clamp01(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


async def judge_escalation_reply(
    *,
    user_message: str,
    reply: str,
    scenario: str = "",
    completion_fn: Any | None = None,
) -> EscalationJudgeScore | None:
    """Score an escalation reply. Returns ``None`` on any failure (best-effort).

    An empty reply scores all-zero (the worst escalation is one that says
    nothing). ``scenario`` is an optional one-line description of the gate that
    was hit, giving the judge ground truth for the honesty criterion.
    """
    if reply is None or not reply.strip():
        return EscalationJudgeScore(0.0, 0.0, 0.0, 0.0, "reply was empty")

    settings = get_settings()
    llm = settings.llm
    models = llm.models or {}
    reasoning = models.get("reasoning") or {}
    model_name = reasoning.get("model") or llm.default_model
    api_key = _api_key_for_model(settings, model_name)

    user_block = (
        (f"Scenario (the wall that was hit): {scenario}\n\n" if scenario else "")
        + f"User's original request: {user_message}\n\n"
        + f"Assistant's escalation reply:\n{reply}"
    )
    kwargs: dict[str, Any] = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": _JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": user_block},
        ],
        "response_format": {"type": "json_object"},
        # Claude-5 only supports temperature=1 — a temp=0 request 422s with
        # litellm.UnsupportedParamsError, and the broad except below turned that
        # into a silent None (the judge scored NOTHING on the prod model). We
        # don't lose determinism: like the rest of the suite post-Gemini-exit,
        # it comes from the K-of-N / quarantine machinery, not a temperature the
        # model can't honor. `drop_params` keeps a FUTURE model that rejects some
        # other param degrading gracefully instead of silently returning None.
        "temperature": 1.0,
        "drop_params": True,
    }
    if api_key:
        kwargs["api_key"] = api_key

    fn = completion_fn or litellm.acompletion
    try:
        response = await fn(**kwargs)
        content = response.choices[0].message.content
        data = json.loads(content)
    except Exception:  # noqa: BLE001 — a non-gating metric must never break the eval
        # WARNING, not INFO: a swallowed judge failure is invisible otherwise
        # (no score line prints), which is exactly how this stayed dead on the
        # prod model. Surface it in the eval output while still never raising.
        logger.warning("escalation judge call/parse failed (non-fatal)", exc_info=True)
        return None

    if not isinstance(data, dict):
        return None
    return EscalationJudgeScore(
        names_gate_honestly=_clamp01(data.get("names_gate_honestly")),
        states_attempt_and_remainder=_clamp01(data.get("states_attempt_and_remainder")),
        gives_next_step=_clamp01(data.get("gives_next_step")),
        overall=_clamp01(data.get("overall")),
        rationale=str(data.get("rationale") or "")[:200],
    )


__all__ = ["EscalationJudgeScore", "judge_escalation_reply"]
