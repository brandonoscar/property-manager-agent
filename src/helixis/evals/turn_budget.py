"""Opt-in max-turns override for the live-Gemini eval harnesses.

The ``agent_flow`` and ``orchestrator_routing`` harnesses cap each case's
agent loop at a per-harness ``_DEFAULT_MAX_TURNS`` (12 and 8) — calibrated so
cases like 062 (step-budget exhaustion) still get the room they need. During a
LOCAL dogfood pass those turns are the dominant Gemini cost driver, so a runner
can shrink them via ``HELIXIS_EVAL_MAX_TURNS`` to trade a bit of head-room for
cheaper runs.

Default: env unset → the harness default is returned unchanged, so CI and the
nightly sweep behave EXACTLY as before. This is a local cost knob, not a
behavior change — and it is deliberately NOT lowered globally, because a blanket
cut would break cases calibrated to the current budget (062 asserts a specific
step count). Opt in per-shell when you want cheaper local runs; leave it unset
everywhere else.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

ENV_VAR = "HELIXIS_EVAL_MAX_TURNS"


def resolve_max_turns(default: int) -> int:
    """Return the eval agent-loop turn cap, honoring an opt-in env override.

    ``HELIXIS_EVAL_MAX_TURNS`` — when set to a positive int — REPLACES the
    harness ``default``. Unset, empty, non-numeric, or non-positive values fall
    back to ``default`` and never raise, so a fat-fingered override degrades to
    "no override" rather than breaking the run.
    """
    raw = os.environ.get(ENV_VAR)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        logger.warning("%s=%r is not an integer; using default %d", ENV_VAR, raw, default)
        return default
    if value <= 0:
        logger.warning("%s=%d is not positive; using default %d", ENV_VAR, value, default)
        return default
    return value


__all__ = ["ENV_VAR", "resolve_max_turns"]
