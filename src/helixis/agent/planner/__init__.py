"""Action planner — turn an enriched event into a 1-4-button surface.

The planner reads :class:`helixis.agent.enrichment.EnrichedEvent`,
prompts the reasoning LLM with a curated tool catalogue, and returns
an :class:`ActionSurface` for the frontend to render. No execution
happens here — Phase A.2 owns the button-click handlers that actually
call Buildium write endpoints.
"""

from helixis.agent.planner.action_planner import (
    PLANNER_PROMPT_VERSION,
    ActionPlanner,
    PlannerError,
)
from helixis.agent.planner.models import (
    ActionSurface,
    DoneSignal,
    FollowupOption,
    FollowupQuestion,
    ProposedAction,
    ToolParameterSchema,
)
from helixis.agent.planner.tool_catalog import TOOL_CATALOG, ToolSpec

__all__ = [
    "ActionPlanner",
    "ActionSurface",
    "DoneSignal",
    "FollowupOption",
    "FollowupQuestion",
    "PLANNER_PROMPT_VERSION",
    "PlannerError",
    "ProposedAction",
    "TOOL_CATALOG",
    "ToolParameterSchema",
    "ToolSpec",
]
