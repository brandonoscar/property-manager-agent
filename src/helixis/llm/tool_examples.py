"""Anthropic Tool Use Examples for the write surface.

Anthropic's ``input_examples`` attaches concrete demonstrations to a tool
definition; their published measurement is parameter accuracy rising from
72% to 90% on complex parameter handling. Complex parameter handling is
precisely the class that has hurt this product: ``TotalAmount`` versus
``Lines[].Amount``, ``Status`` versus ``TaskStatus``, ``GlAccountId``
versus ``GLAccountId`` — four field-shape bugs in one month, every one of
them found by reading a live production row rather than by a test.

**This works on the current harness.** LiteLLM's Anthropic transform passes
unknown tool keys straight through to the tool block, exactly as it already
does for ``cache_control`` — verified against ``AnthropicConfig._map_tools``.
So the examples ride the same seam :mod:`helixis.llm.prompt_cache` uses, and
no SDK migration is required to get them.

Scope: the WRITE tools only. A wrong parameter on a read costs a retry; a
wrong one on ``record_payment`` is an immutable ledger row (gotcha 21), and
on ``close_work_order`` it is a one-way latch (gotcha 20). Reads are left
alone so the tool block stays small — examples are not free, they ride in
the cached prefix on every call.

Every example is a COMPLETE, valid call: the confirm-gate labels are filled
in, because half the parameters on these tools exist so the approval card
can name a person and a place instead of an id, and an example that omits
them teaches the model they are optional.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: tool name → example inputs. Illustrative ids, not live records.
TOOL_EXAMPLES: dict[str, list[dict[str, Any]]] = {
    # ── the irreversible three ──────────────────────────────────────
    "buildium_record_payment": [
        {
            "lease_id": 41205,
            "amount": 1450.00,
            "gl_account_id": 9001,
            "date": "2026-08-21",
            "payment_method": "Check",
            "memo": "August rent, check #1042",
            "lease_label": "Marcus Whitfield — 600 Dexter Ave, Unit 1",
        }
    ],
    "buildium_post_charge": [
        {
            "lease_id": 41205,
            "amount": 75.00,
            "gl_account_id": 9014,
            "date": "2026-08-21",
            "memo": "Late fee — August rent past the 10th",
            "lease_label": "Marcus Whitfield — 600 Dexter Ave, Unit 1",
            "gl_account_label": "Late Fee Income",
        }
    ],
    "buildium_close_work_order": [
        {
            "work_order_id": 9006,
            "work_order_label": "Water heater replacement — 600 Dexter Ave, Unit 1",
        }
    ],
    # ── routed writes: the field decides the endpoint (gotcha 20) ───
    "buildium_update_work_order": [
        # Vendor goes to the work order itself.
        {
            "work_order_id": 9014,
            "vendor_id": 4471,
            "vendor_label": "Apex Plumbing",
            "priority": "",
            "due_date": "",
            "work_order_label": "Leaking water heater — 600 Dexter Ave, Unit 1",
        },
        # Priority and due date are TASK-derived — same tool, different half.
        # A Priority sent to /workorders returns 200 and silently no-ops.
        {
            "work_order_id": 9014,
            "vendor_id": 0,
            "vendor_label": "",
            "priority": "High",
            "due_date": "2026-08-25",
            "work_order_label": "Leaking water heater — 600 Dexter Ave, Unit 1",
        },
    ],
    "buildium_update_task_status": [
        {
            "task_id": 39431,
            "status": "InProgress",
            "task_label": "URGENT: water heater leaking into the closet",
        }
    ],
    "buildium_record_move_out": [
        {
            "lease_id": 41205,
            "tenant_id": 7710,
            "move_out_date": "2026-09-15",
            "tenant_label": "Marcus Whitfield",
            "lease_label": "600 Dexter Ave, Unit 1",
        }
    ],
    # ── creates ────────────────────────────────────────────────────
    "buildium_create_to_do_task": [
        {
            "title": "Re-check water heater priority",
            "description": (
                "Tenant reports the drip is worsening. Entered at Low by the "
                "previous assistant — confirm the correct priority."
            ),
            "assigned_to_user_id": 229661,
            "property_id": 37948,
            "unit_id": 0,
            "due_date": "2026-08-25",
            "priority": "High",
        }
    ],
    "buildium_create_work_order": [
        {
            "title": "Replace leaking water heater",
            "description": "Active drip into the hall closet; tenant reports it is worsening.",
            "property_id": 37948,
            "vendor_id": 4471,
            "assigned_to_user_id": 229661,
            "priority": "High",
            "unit_id": 5501,
            "entry_allowed": "Yes",
            "property_label": "600 Dexter Ave",
            "vendor_label": "Apex Plumbing",
            "assignee_label": "Dana Reyes",
        }
    ],
    # ── notes: the cheap, reversible writes ────────────────────────
    "buildium_add_task_note": [
        {
            "task_id": 39431,
            "note": "Priority raised to High — active leak, habitability risk.",
            "task_label": "URGENT: water heater leaking into the closet",
        }
    ],
    "buildium_add_work_order_note": [
        {
            "work_order_id": 9006,
            "note": "Vendor confirmed for Thursday AM; tenant notified.",
            "work_order_label": "Water heater replacement — 600 Dexter Ave, Unit 1",
        }
    ],
    "buildium_add_vendor_note": [
        {
            "vendor_id": 4471,
            "note": "Second no-show this quarter. Confirm the day before from now on.",
            "vendor_label": "Apex Plumbing",
        }
    ],
    # ── documents ──────────────────────────────────────────────────
    "buildium_share_file": [
        {
            "file_id": 88213,
            "share_with_tenants": True,
            "share_with_rental_owners": False,
            "file_label": "Signed lease — 600 Dexter Ave, Unit 1",
        }
    ],
}


#: JSON Schema type name → the Python types that satisfy it. ``bool`` is a
#: subclass of ``int``, so booleans are checked before integers everywhere.
_JSON_TYPES: dict[str, Any] = {
    "string": str,
    "boolean": bool,
    "integer": int,
    "number": (int, float),
    "array": list,
    "object": dict,
    "null": type(None),
}


def _matches_type(value: Any, declared: Any) -> bool:
    """One JSON-Schema ``type`` (or a list of them) against one value."""
    if isinstance(declared, list):
        return any(_matches_type(value, d) for d in declared)
    expected = _JSON_TYPES.get(str(declared))
    if expected is None:
        return True  # a type we don't model — don't invent a failure
    if expected is not bool and isinstance(value, bool):
        return False  # True is not an integer, whatever Python says
    return isinstance(value, expected)


def _conforms(value: Any, spec: dict[str, Any]) -> bool:
    """Does one example VALUE satisfy one property's schema?"""
    options = spec.get("anyOf") or spec.get("oneOf")
    if isinstance(options, list):
        return any(isinstance(o, dict) and _conforms(value, o) for o in options)
    enum = spec.get("enum")
    if isinstance(enum, list) and value not in enum:
        return False
    declared = spec.get("type")
    return True if declared is None else _matches_type(value, declared)


def example_violations(example: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    """Why this example would be rejected as an ``input_examples`` entry.

    Anthropic validates every example against the tool's own ``input_schema``
    and **400s the entire request** when one fails — not the tool, the whole
    call. So a single mistyped example takes the product down, which is what
    ``"entry_allowed": True`` did on PR #860: it is a tri-state STRING
    ("Unknown"/"Yes"/"No"), the schema said so, and the examples test only
    compared key NAMES. Every Anthropic call carrying the Buildium tool set
    400'd.

    Empty list means conforming. Deliberately conservative — it models the
    parts of JSON Schema the Agents SDK actually emits (flat properties,
    ``anyOf`` unions, enums, ``additionalProperties: false``) and stays quiet
    about anything it does not understand, so it can only ever catch a real
    mismatch.
    """
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return []
    problems: list[str] = []
    for missing in sorted(set(schema.get("required") or []) - set(example)):
        problems.append(f"missing required key {missing!r}")
    if schema.get("additionalProperties") is False:
        for unknown in sorted(set(example) - set(properties)):
            problems.append(f"unknown key {unknown!r}")
    for key, value in example.items():
        spec = properties.get(key)
        if isinstance(spec, dict) and not _conforms(value, spec):
            problems.append(f"{key}={value!r} does not match {spec.get('type') or 'schema'}")
    return problems


def inject_tool_examples(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Attach ``input_examples`` to any known write tool in this request.

    Same posture as :func:`helixis.llm.prompt_cache.inject_cache_control`:
    Anthropic-only, idempotent, copies rather than mutates, and never raises
    — a billing/accuracy hint must not be able to fail a turn.

    Each example is validated against the tool's OWN schema first and dropped
    if it does not conform, because Anthropic rejects the whole request over
    one bad example. A drifted example must cost us its accuracy benefit, not
    the turn.
    """
    tools = kwargs.get("tools")
    if not isinstance(tools, list) or not tools:
        return kwargs

    out: list[Any] = []
    changed = False
    for tool in tools:
        if not isinstance(tool, dict) or "input_examples" in tool:
            out.append(tool)
            continue
        name = ""
        function = tool.get("function")
        if isinstance(function, dict):
            name = str(function.get("name") or "")
        name = name or str(tool.get("name") or "")
        examples = TOOL_EXAMPLES.get(name)
        if not examples:
            out.append(tool)
            continue
        schema = tool.get("input_schema")
        if not isinstance(schema, dict) and isinstance(function, dict):
            schema = function.get("parameters")
        keep: list[dict[str, Any]] = []
        for example in examples:
            problems = example_violations(example, schema) if isinstance(schema, dict) else []
            if problems:
                logger.warning(
                    "tool example for %s does not match its schema (%s) — dropping it; "
                    "Anthropic 400s the whole request over one bad example",
                    name,
                    "; ".join(problems),
                )
                continue
            keep.append(dict(example))
        if not keep:
            out.append(tool)
            continue
        out.append({**tool, "input_examples": keep})
        changed = True

    if changed:
        kwargs["tools"] = out
    return kwargs


__all__ = ["TOOL_EXAMPLES", "example_violations", "inject_tool_examples"]
