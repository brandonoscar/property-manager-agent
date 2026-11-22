"""PR #106 — ground LLM-emitted ``signals`` against enrichment.

The V2 planner prompt asks Gemini for a list of 0–4 short
"signals" describing facts about the event ("Three fixture tickets
at this unit in 90 days"). The frontend renders these verbatim in
the "WHAT HELIXIS NOTICED" section of the Inbox detail pane.

Production observation: the LLM occasionally fabricates numeric
claims. The Loud Crashing event at 160 East End Ave #120 had a
signal reading **"20 prior tickets on this unit in the last 90
days"** — but the actual ``EnrichedEvent.history_unit`` carried
only a handful of rows. The number was a confident hallucination.

This module is the **strict-drop** validator: any signal that
makes a numeric claim about a quantity we can verify (currently
prior tickets on the unit / property) gets dropped if the number
doesn't match the ground truth from ``enriched.history_unit`` /
``enriched.history_property``.

Design choices:
- **Drop, don't rewrite.** Replacing "20" with "3" inside the
  signal could change its meaning (the LLM may have inferred a
  pattern from "20"). Dropping is honest — the deterministic
  fallback in the frontend will fill the gap from raw enrichment.
- **Validate only quantities we can ground.** Signals that don't
  mention unit/property history get a pass — we can't refute what
  we can't measure.
- **Pure function, no I/O.** All inputs are in-memory; easy to
  unit-test.
- **Observable.** Each drop is logged at WARNING with the original
  signal + claimed-vs-actual counts so we can monitor LLM
  hallucination rate over time.
"""

from __future__ import annotations

import logging
import re

from helixis.agent.enrichment.models import EnrichedEvent
from helixis.agent.planner.models import ActionSurface
from helixis.observability.reason_codes import record_reason_code

logger = logging.getLogger(__name__)


# Match "N prior tickets" / "5 prior incidents" / "3 prior work
# orders" — any positive integer followed by "prior" + one of the
# common Buildium-noun synonyms for "task." Case-insensitive.
_NUMERIC_PRIOR_CLAIM = re.compile(
    r"\b(\d+)\s+prior\s+"
    r"(?:tickets?|incidents?|complaints?|work[\s-]*orders?|tasks?|issues?|requests?)",
    re.IGNORECASE,
)


def validate_signals(action_surface: ActionSurface, enriched: EnrichedEvent) -> ActionSurface:
    """Return ``action_surface`` with hallucinated signals removed.

    If ``signals`` is None or empty, returns the surface unchanged.
    Otherwise filters the list with :func:`_signal_is_grounded` and
    re-emits the surface (Pydantic creates a new instance so the
    original isn't mutated)."""
    signals = action_surface.signals
    if not signals:
        return action_surface

    actual_unit = len(enriched.history_unit)
    actual_property = len(enriched.history_property)

    kept: list[str] = []
    dropped: list[tuple[str, str]] = []  # (signal, reason)
    for sig in signals:
        outcome = _signal_is_grounded(sig, actual_unit=actual_unit, actual_property=actual_property)
        if outcome is None:
            kept.append(sig)
        else:
            dropped.append((sig, outcome))

    if dropped:
        logger.warning(
            "planner signals dropped as hallucinated",
            extra={
                "event_name": enriched.event_name,
                "task_id": enriched.task.id,
                "actual_history_unit": actual_unit,
                "actual_history_property": actual_property,
                "dropped": [{"signal": s, "reason": r} for s, r in dropped],
            },
        )
        record_reason_code(
            "planner_signal_dropped",
            detail=f"{len(dropped)} dropped: " + "; ".join(r for _, r in dropped)[:300],
        )

    return action_surface.model_copy(update={"signals": kept})


def _signal_is_grounded(
    signal: str,
    *,
    actual_unit: int,
    actual_property: int,
) -> str | None:
    """Return None when ``signal`` passes the ground-truth check,
    otherwise a short reason string explaining why it was dropped.

    Currently checks: numeric "N prior X" claims tied to the unit or
    property scope. Signals without a quantifiable claim pass
    through (we can't refute what we can't measure).
    """
    match = _NUMERIC_PRIOR_CLAIM.search(signal)
    if match is None:
        return None  # no numeric claim → pass

    claimed = int(match.group(1))
    lower = signal.lower()

    # Disambiguate the scope. "this unit" is the strongest tell; if
    # the signal also mentions "property" treat the property claim
    # as the controlling check (broader scope dominates).
    mentions_property = "property" in lower or "building" in lower
    mentions_unit = "unit" in lower or "this apt" in lower or "this apartment" in lower

    if mentions_property:
        if claimed != actual_property:
            return f"claims {claimed} prior on property; actual {actual_property}"
        return None
    if mentions_unit:
        if claimed != actual_unit:
            return f"claims {claimed} prior on unit; actual {actual_unit}"
        return None
    # The signal makes a numeric claim but doesn't tie it to
    # unit/property — we have no ground truth for it. Pass.
    return None
