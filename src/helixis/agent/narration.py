"""Hard-coded narration strings for the conversational Inbox (PR #70).

Routine Buildium calls don't need an LLM round-trip to produce a one-
line status — we just want consistent texture. This module maps each
backend tool / read endpoint to a short, present-tense narration the
frontend renders as italic text in the thread (``NARRATION`` events).

Anything that is genuinely interesting — the planner explaining *why*
it's about to do something — goes through ``ActionPlanner._narrate_reasoning``
instead, which streams from Gemini. The split keeps the routine path
fast (<1ms lookup) and the interesting path expressive.

Falling back: if a tool isn't in the table (new endpoint just landed,
catalog hasn't been updated), ``narrate_tool`` returns a generic
"Working on it" rather than raising — better to ship an opaque turn
than a 500.
"""

from __future__ import annotations

TOOL_NARRATION: dict[str, str] = {
    # Buildium read endpoints
    "buildium.tasks.get": "Reading the task from Buildium",
    "buildium.leases.get": "Pulling the lease record",
    "buildium.leases.list": "Listing leases on the unit",
    "buildium.units.get": "Looking up the unit",
    "buildium.tenants.get": "Looking up the tenant",
    "buildium.tenants.list": "Listing tenants on the lease",
    "buildium.properties.get": "Pulling property details",
    "buildium.tasks.history": "Pulling task history",
    "buildium.work_orders.list": "Listing recent work orders",
    "buildium.applicants.get": "Looking up the applicant",
    # Buildium write endpoints (these will be wrapped in CONFIRM_GATE
    # before they actually fire — narration runs at execute time).
    "buildium.tasks.update": "Updating the task in Buildium",
    "buildium.tasks.create": "Creating a follow-up task in Buildium",
    "buildium.notes.create": "Writing a note to Buildium",
    "buildium.messages.send": "Sending the message via Buildium",
    # Email ingest — the drafted reply goes out via Gmail (Composio),
    # not Buildium.
    "gmail.reply.send": "Sending your reply via Gmail",
    # Helixis-internal
    "helixis.enrichment.fanout": "Cross-referencing the situation",
    "helixis.planner.draft": "Drafting suggestions",
}

_GENERIC_FALLBACK = "Working on it"


def narrate_tool(tool_id: str | None) -> str:
    """Resolve a tool id to a narration string.

    Returns ``"Working on it"`` for unknown tool ids — never raises.
    ``tool_id=None`` (e.g. an enrichment fanout that doesn't tag a
    specific tool) also returns the fallback.
    """
    if not tool_id:
        return _GENERIC_FALLBACK
    return TOOL_NARRATION.get(tool_id, _GENERIC_FALLBACK)


__all__ = ["TOOL_NARRATION", "narrate_tool"]
