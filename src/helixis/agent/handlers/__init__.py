"""Phase A.2 — Buildium action executors.

Each module here implements one tool from the Phase A.1 catalog
(``src/helixis/agent/planner/tool_catalog.py``). The execute endpoint
in ``api/routes/buildium.py`` looks up the right handler by
``tool_id`` in :data:`HANDLERS`, deep-merges any
``parameter_overrides`` into the planner-proposed parameters, then
calls the handler with an open ``BuildiumClient`` scoped to the
requesting company.

Read = autonomous (Phase A.1 enrichment / planning).
Write = always human-confirmed (Phase A.2 — this package).
"""

from helixis.agent.handlers.add_internal_note import AddInternalNoteHandler
from helixis.agent.handlers.base import Handler, HandlerError, HandlerResult
from helixis.agent.handlers.create_followup_task import CreateFollowupTaskHandler
from helixis.agent.handlers.reply_to_email import ReplyToEmailHandler
from helixis.agent.handlers.send_tenant_message import SendTenantMessageHandler
from helixis.agent.handlers.update_buildium_task import UpdateBuildiumTaskHandler

# Keyed on the tool ids defined in
# ``helixis.agent.planner.tool_catalog.TOOL_IDS`` so the planner side
# and the executor side cannot drift apart silently — the integration
# test asserts the two key sets match exactly.
HANDLERS: dict[str, Handler] = {
    "update_buildium_task": UpdateBuildiumTaskHandler(),
    "send_tenant_message": SendTenantMessageHandler(),
    "add_internal_note": AddInternalNoteHandler(),
    "create_followup_task": CreateFollowupTaskHandler(),
    # Email-ingest reply — sends via Composio Gmail, not Buildium. The
    # executor passes a None Buildium client + composio/thread context.
    "reply_to_email": ReplyToEmailHandler(),
}


def get_handler(tool_id: str) -> Handler | None:
    """Return the registered handler for a ``tool_id`` or ``None``."""
    return HANDLERS.get(tool_id)


__all__ = [
    "HANDLERS",
    "Handler",
    "HandlerError",
    "HandlerResult",
    "get_handler",
]
