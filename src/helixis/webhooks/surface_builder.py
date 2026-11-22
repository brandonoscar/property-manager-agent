"""Build an InboxPage-shaped ``action_surface`` from a routing decision.

PR #82 — the deterministic floor that makes Inbox light up even
when the LLM-backed ``ActionPlanner`` is down. ``route_event`` (PR
#80) decides who the event belongs to; this module turns that
decision into the JSON shape ``InboxActionSurface`` on the frontend
expects.

PR #83 — accept an optional ``EnrichedEvent`` so the floor renders
rich summaries ("Loud crashing — 123 Main St #4B — Reported by
Sarah Chen — Assigned to Vinny Dan · Maintenance") when enrichment
has run. Real Buildium ``Task.Created`` webhook payloads only carry
TaskId / EventName / AccountId / EventDateTime — no title, no
property, no assignee — so the floor needs the enrichment fan-out
to produce anything human-readable.

Hard rules from the original brief (verbatim):

- "No Buildium internal IDs in chat/inbox output, ever. Humans by
  name, places by property name + unit number."
- "This PR does NOT make new Buildium API calls. It re-uses the
  envelope payload Buildium already sends in ``Task.Created``. No
  new field guessing."

Both rules still hold in PR #83: the enrichment was fetched by the
caller (router or backfill), this module is a pure projection. No
network, no DB.

v1 ships ONE action (``noop_view_buildium``). PR #84+ adds Mark
Resolved + Reassign with real Buildium write-backs.
"""

from __future__ import annotations

from typing import Any

from helixis.agent.enrichment.models import EnrichedEvent, EnrichedTask, EnrichedUnit
from helixis.db.repositories.buildium_staff import BuildiumStaffRow
from helixis.webhooks.actions import RoutingDecision

# Bumped from 1 → 2 by PR #82. v1 surfaces had a different action
# vocabulary; the frontend ignores the version today but a future
# renderer can branch on it.
SCHEMA_VERSION = 2

# Buildium's web-app task URL. Doesn't include ``buildium_url`` in
# the rendered summary — only in the action's ``parameters`` so the
# click handler can ``window.open`` it. (Brandon's no-ID-leak rule.)
_BUILDIUM_TASK_URL = "https://app.buildium.com/manager/dashboard/tasks"


def build_action_surface_from_routing(
    *,
    envelope: dict[str, Any],
    event_name: str,
    decision: RoutingDecision,
    staff_cache: dict[int, BuildiumStaffRow],
    enriched: EnrichedEvent | None = None,
) -> dict[str, Any]:
    """Return an InboxPage-shaped ``action_surface`` dict.

    Independent of the LLM planner — pure function over the
    envelope, the routing decision, the staff cache, and (PR #83)
    an optional ``EnrichedEvent``. Safe to call in the webhook
    receiver hot path; no network, no DB.

    ``enriched``:
        - When None, behaves byte-for-byte like PR #82 (back-compat).
        - When provided with an ``EnrichedTask``, the summary picks
          up title, property+unit location, requester name,
          assignee name (resolved via staff_cache), and category.
    """
    if enriched is not None and enriched.task is not None:
        summary = _summary_from_enrichment(enriched, decision, staff_cache, event_name)
    else:
        assignee = _assignee_label(decision, staff_cache)
        title = _extract_title_from_envelope(envelope, event_name)
        summary = _summary_from_envelope(event_name, title, assignee)
    actions = _actions(envelope, event_name)
    return {
        "schema_version": SCHEMA_VERSION,
        "summary": summary,
        "actions": actions,
        "freeform_placeholder": "Reply or take notes…",
    }


# ── Internals ──────────────────────────────────────────────────────


def _assignee_label(
    decision: RoutingDecision,
    staff_cache: dict[int, BuildiumStaffRow],
) -> str:
    """Resolve the assignee's display name from the routing decision.

    Falls back to ``"the admin"`` when:
    - no ``assigned_buildium_user_id`` on the decision (admin-fallback
      branch from ``route_event``), OR
    - the user id isn't in ``staff_cache`` yet (sync never ran, or
      the user was removed from Buildium).

    NEVER returns the raw Buildium user id (PR #75 HARD PRESENTATION
    RULES).
    """
    bid = decision.assigned_buildium_user_id
    if bid is None:
        return "the admin"
    row = staff_cache.get(bid)
    if row is None:
        return "the admin"
    return row.display_name


def _extract_title_from_envelope(envelope: dict[str, Any], event_name: str) -> str | None:
    """Pull a human title out of the envelope without an API call.

    Buildium's webhook payload usually does NOT carry a title (real
    ``Task.Created`` is just TaskId + EventName); this helper exists
    for the back-compat path where the caller passes no enrichment.
    PR #83's enriched path uses :func:`_summary_from_enrichment`
    instead.
    """
    root = envelope.get("Title")
    if isinstance(root, str) and root.strip():
        return root.strip()

    event = envelope.get("Event")
    if isinstance(event, dict):
        event_data = event.get("EventData")
        if isinstance(event_data, dict):
            entity_key = _entity_key_for_event(event_name)
            entity = event_data.get(entity_key)
            if isinstance(entity, dict):
                nested = entity.get("Title")
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()

    return None


def _entity_key_for_event(event_name: str) -> str:
    """Map an event name to the entity key Buildium nests under
    ``Event.EventData.{key}``. v1 covers the event families we
    actually surface in Inbox (Task / WorkOrder / ContactRequest /
    Lease); unknowns return ``""`` so the nested-shape lookup
    no-ops and the function returns ``None``."""
    head = event_name.split(".")[0] if event_name else ""
    return {
        "Task": "Task",
        "WorkOrder": "WorkOrder",
        "ContactRequest": "ContactRequest",
        "TenantRequest": "TenantRequest",
        "Lease": "Lease",
    }.get(head, "")


def _summary_from_envelope(event_name: str, title: str | None, assignee: str) -> str:
    """PR #82 back-compat — used when no enrichment is available."""
    head = event_name.split(".")[0] if event_name else ""
    if head == "Task":
        if title:
            return f"Task: {title} — assigned to {assignee}"
        return f"Task created — assigned to {assignee}"
    if head == "WorkOrder":
        if title:
            return f"Work order: {title} — assigned to {assignee}"
        return f"Work order created — assigned to {assignee}"
    if head in ("ContactRequest", "TenantRequest"):
        if title:
            return f"{_humanise(head)}: {title} — assigned to {assignee}"
        return f"{_humanise(head)} — assigned to {assignee}"
    if head == "Lease":
        return f"Lease event — assigned to {assignee}"
    pretty = event_name.replace(".", " ") if event_name else "event"
    return f"{pretty} — assigned to {assignee}"


def _summary_from_enrichment(
    enriched: EnrichedEvent,
    decision: RoutingDecision,
    staff_cache: dict[int, BuildiumStaffRow],
    event_name: str,
) -> str:
    """PR #83 — rich summary composed from EnrichedEvent fields.

    Clause order (only included if the underlying field is populated):

    1. Title (``task.title``) — falls back to the bare event name
       like the PR #82 path when missing.
    2. Location — ``unit.address_line1`` + ``#unit_number`` when
       both present; address-only or unit-only when one is missing;
       clause omitted entirely when neither.
    3. Reporter — ``task.requested_by_first_name + last_name``
       composed; omitted when both empty.
    4. Assignee — resolved via the staff cache from
       ``task.assigned_to_user_id``. Falls back to the routing
       decision's admin label when missing.
    5. Category tail — appended as ``· {Category}`` suffix.

    No Buildium internal IDs ever land in the string. Missing
    fields drop their entire clause — never substituted with the
    numeric id.
    """
    task: EnrichedTask = enriched.task

    parts: list[str] = []

    title = _safe_str(getattr(task, "title", None))
    if title:
        parts.append(title)
    else:
        # Fallback so the row still reads as something — pre-PR-#83
        # behavior is "Task created" without a colon-title.
        head = event_name.split(".")[0] if event_name else ""
        parts.append(f"{head} created" if head == "Task" else (head or "event"))

    location = _format_location(enriched.unit)
    if location:
        parts.append(location)

    reporter = _format_reporter(task)
    if reporter:
        parts.append(f"Reported by {reporter}")

    assignee_clause = _format_assignee_clause(task, decision, staff_cache)
    if assignee_clause:
        parts.append(assignee_clause)

    summary = " — ".join(parts)

    category = _safe_str(getattr(task, "category", None))
    if category:
        summary = f"{summary} · {category}"
    return summary


def _safe_str(value: Any) -> str:
    """Return ``value`` as a stripped string when it's a real ``str``;
    otherwise return ``""``. Defends the summary against MagicMock
    auto-attributes / non-string envelopes."""
    if isinstance(value, str):
        return value.strip()
    return ""


def _format_location(unit: EnrichedUnit | None) -> str:
    """``"123 Main St #4B"`` / ``"123 Main St"`` / ``"Unit 4B"`` / ``""``.

    No PropertyId or UnitId leakage — only the human-readable
    address/number fields land in the string.

    PR #84 — Buildium occasionally stores the unit suffix baked
    into ``address_line1`` (e.g. ``"160 East End Avenue #120"``
    for what's really ``unit_number = "1"`` of property #120). The
    naive ``f"{addr} #{number}"`` then renders ``"... #120 #1"``
    which reads as nonsense. Detect when ``address_line1`` already
    carries ``unit_number`` as a ``#``-suffix and skip the
    re-append. Comparison is on a token boundary (``#1`` does NOT
    match ``#10``/``#100``) and case-insensitive (``#4b`` matches
    ``"4B"``); the address's original casing is preserved in the
    rendered output.
    """
    if unit is None:
        return ""
    addr = _safe_str(getattr(unit, "address_line1", None))
    number = _safe_str(getattr(unit, "unit_number", None))
    if addr and number:
        if _address_already_has_unit(addr, number):
            return addr
        return f"{addr} #{number}"
    if addr:
        return addr
    if number:
        return f"Unit {number}"
    return ""


_UNIT_IN_ADDRESS_TPL = r"#\s*{unit}(?!\w)"
# Address-level building/property suffix — ends with ``#<3+ digits>``.
# Pure digits only (so alphanumeric ``#4B`` isn't treated as a
# building suffix — it's a unit). The 3-digit threshold separates
# plausible building numbers (``#120``) from plausible unit numbers
# (``#1``, ``#10``); see PR #84 test 1 vs test 3 for the boundary.
_ADDRESS_BUILDING_SUFFIX_RE = r"#\s*\d{3,}\s*$"


def _address_already_has_unit(address: str, unit_number: str) -> bool:
    """True if ``address`` already represents the unit/building so
    appending ``#{unit_number}`` would render as nonsense like
    ``"#120 #1"`` (the PR #84 live regression on row
    ``0fd4e984-…``, ``160 East End Avenue #120`` + ``unit_number=1``).

    Two paths satisfy the check:

    1. **Exact unit-token match** — ``#`` + optional whitespace +
       the exact ``unit_number`` on a token boundary anywhere in
       the address. ``#1`` matches ``"... #1"`` but NOT
       ``"... #10"`` / ``"... #100"``. Whitespace tolerated
       (``"# 3"`` matches ``"3"``). Case-insensitive so ``#4b``
       matches ``"4B"``.
    2. **Building-suffix heuristic** — address ends with
       ``#<3+ pure digits>``. Buildium occasionally bakes the
       building number into ``address_line1`` (``"160 East End
       Avenue #120"`` for property #120); the unit_number on the
       same row may be a small apartment number (``"1"``). Appending
       a second ``#1`` produces ``#120 #1`` which reads as doubled
       unit notation, so we suppress when the address already
       carries the building marker. Alphanumeric suffixes
       (``#4B``) DON'T trigger this — those are unit numbers, not
       building numbers — only pure-digit runs of length ≥ 3.
    """
    import re

    pattern = _UNIT_IN_ADDRESS_TPL.format(unit=re.escape(unit_number))
    if re.search(pattern, address, re.IGNORECASE) is not None:
        return True
    if re.search(_ADDRESS_BUILDING_SUFFIX_RE, address) is not None:
        return True
    return False


def _format_reporter(task: EnrichedTask) -> str:
    first = _safe_str(getattr(task, "requested_by_first_name", None))
    last = _safe_str(getattr(task, "requested_by_last_name", None))
    full = " ".join(b for b in (first, last) if b)
    return full


def _format_assignee_clause(
    task: EnrichedTask,
    decision: RoutingDecision,
    staff_cache: dict[int, BuildiumStaffRow],
) -> str:
    """Resolve assignee name via the staff cache. Order:

    1. ``task.assigned_to_user_id`` looked up in the staff cache.
    2. Otherwise the routing decision's assignee (also via cache).
    3. Otherwise the routing decision's reason text (``"the admin"``).

    Always returns a clause prefixed with ``"Assigned to "`` — or
    empty string if every lookup yielded nothing nameable.
    """
    bid = task.assigned_to_user_id
    if isinstance(bid, int):
        row = staff_cache.get(bid)
        if row is not None:
            return f"Assigned to {row.display_name}"
    # Fall back to whatever the routing decision picked.
    routed_bid = decision.assigned_buildium_user_id
    if isinstance(routed_bid, int):
        row = staff_cache.get(routed_bid)
        if row is not None:
            return f"Assigned to {row.display_name}"
    return "Assigned to the admin"


def _humanise(head: str) -> str:
    """``"TenantRequest"`` → ``"Tenant request"``."""
    return (
        "".join(
            (" " + ch.lower() if ch.isupper() and i > 0 else ch.lower() if i == 0 else ch)
            for i, ch in enumerate(head)
        )
        .strip()
        .capitalize()
    )


def _actions(envelope: dict[str, Any], event_name: str) -> list[dict[str, Any]]:
    """Deterministic floor action — a NATIVE in-app action, not an
    external "Open in Buildium" bounce (UI refinement #3 / obs#1: work
    happens inside Helixis). For a Task event we offer an editable
    internal note (``add_internal_note`` injects the task id from
    context, so only ``note_body`` is needed here). For non-Task events
    we emit no floor action — the detail card's free-text "reply / take
    notes" box is the native affordance there. The planner's own
    surface (when it runs) supersedes this floor entirely.
    """
    head = event_name.split(".")[0] if event_name else ""
    task_id = _envelope_int(envelope, "TaskId") or _envelope_int(envelope, "Id")
    if head == "Task" and task_id is not None:
        return [
            {
                "tool_id": "add_internal_note",
                "label": "Add an internal note",
                "intent": "Add a private staff note about this event",
                "parameters": {"note_body": ""},
                "primary_action": {
                    "label": "Add note",
                    "kind": "create_note",
                    "target": {"kind": "internal", "name": None},
                    "draft": "",
                },
            }
        ]
    return []


def _buildium_url(envelope: dict[str, Any], event_name: str) -> str:
    """Construct the Buildium web-app URL for the entity.

    Task is the only event type with a known stable URL shape today.
    Other events fall back to the Buildium dashboard root so the
    "Open in Buildium" action still works (lands the user somewhere
    actionable).
    """
    head = event_name.split(".")[0] if event_name else ""
    if head == "Task":
        task_id = _envelope_int(envelope, "TaskId") or _envelope_int(envelope, "Id")
        if task_id is not None:
            return f"{_BUILDIUM_TASK_URL}/{task_id}"
        return _BUILDIUM_TASK_URL
    return "https://app.buildium.com/manager/dashboard"


def _envelope_int(envelope: dict[str, Any], key: str) -> int | None:
    """Same numeric-coercion rule as ``property_resolver._first_int`` —
    reject bools (which Python treats as int), accept numeric strings."""
    value = envelope.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


__all__ = ["SCHEMA_VERSION", "build_action_surface_from_routing"]
