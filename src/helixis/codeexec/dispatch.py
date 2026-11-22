"""Real dispatch adapters for Tier-2 apply (Inc.4c) — the first live write path.

``default_dispatch`` is the concrete ``apply.Dispatch`` that turns an allowed
proposed action into a real write. Each adapter calls the EXISTING self-gating
Buildium tool impl — which owns the per-write confirm gate + idempotency claim
and reads its company scope from the request's ``MemoryContext``. So a code run
never writes directly; it proposes, the guard filters, and the write happens
through the same audited, user-approved path a chat write would.

Scope of this increment: the three additive **note** writes — the safest
possible first real writes (append-only, confirm-gated, effectively reversible).
Other allow-listed actions stay proposable but are not yet wired here; asking to
apply one returns a clear "not available for auto-apply yet" rather than a raw
error. Nothing is exposed to the model in this increment (the capability is
dark); this is the write mechanism, tested, ready for the deliberate on-switch.
"""

from __future__ import annotations

import logging
from typing import Any

from helixis.agents.buildium_tools import (
    _add_note_impl,
    _add_vendor_note_impl,
    _create_to_do_task_impl,
    _update_task_status_impl,
)

logger = logging.getLogger(__name__)


class ActionNotWiredError(Exception):
    """Raised when an allow-listed action has no apply adapter yet."""


def _req_int(args: dict, key: str) -> int:
    val = args.get(key)
    if isinstance(val, bool) or not isinstance(val, int):
        # Accept an int-like string too — the model sometimes stringifies ids.
        try:
            return int(str(val))
        except (TypeError, ValueError):
            raise ValueError(f"'{key}' must be an integer id") from None
    return val


def _req_str(args: dict, key: str) -> str:
    val = args.get(key)
    if not isinstance(val, str) or not val.strip():
        raise ValueError(f"'{key}' is required")
    return val.strip()


def _opt_str(args: dict, key: str) -> str | None:
    val = args.get(key)
    return val.strip() if isinstance(val, str) and val.strip() else None


def _opt_int(args: dict, key: str) -> int | None:
    val = args.get(key)
    if val is None or isinstance(val, bool):
        return None
    try:
        return int(str(val))
    except (TypeError, ValueError):
        return None


async def _apply_task_note(args: dict) -> Any:
    return await _add_note_impl(
        kind="task",
        target_id=_req_int(args, "task_id"),
        note=_req_str(args, "note"),
        label=_opt_str(args, "task_label"),
    )


async def _apply_work_order_note(args: dict) -> Any:
    return await _add_note_impl(
        kind="work_order",
        target_id=_req_int(args, "work_order_id"),
        note=_req_str(args, "note"),
        label=_opt_str(args, "work_order_label"),
    )


async def _apply_vendor_note(args: dict) -> Any:
    return await _add_vendor_note_impl(
        vendor_id=_req_int(args, "vendor_id"),
        note=_req_str(args, "note"),
        vendor_label=_opt_str(args, "vendor_label"),
    )


async def _apply_create_task(args: dict) -> Any:
    return await _create_to_do_task_impl(
        title=_req_str(args, "title"),
        description=_opt_str(args, "description"),
        assigned_to_user_id=_opt_int(args, "assigned_to_user_id"),
        property_id=_opt_int(args, "property_id"),
        unit_id=_opt_int(args, "unit_id"),
        due_date=_opt_str(args, "due_date"),
        priority=_opt_str(args, "priority") or "Normal",
    )


async def _apply_update_task_status(args: dict) -> Any:
    return await _update_task_status_impl(
        task_id=_req_int(args, "task_id"),
        status=_req_str(args, "status"),
        task_label=_opt_str(args, "task_label"),
    )


#: action id → adapter. A KEY here is a subset of action_guard.ALLOWED_ACTIONS
#: that has a live write adapter; allow-listed actions NOT here are proposable
#: but not yet auto-appliable.
_ADAPTERS = {
    "buildium_add_task_note": _apply_task_note,
    "buildium_add_work_order_note": _apply_work_order_note,
    "buildium_add_vendor_note": _apply_vendor_note,
    "buildium_create_to_do_task": _apply_create_task,
    "buildium_update_task_status": _apply_update_task_status,
}


async def default_dispatch(action: str, args: dict) -> Any:
    """Apply ONE allowed action through its existing gated tool impl.

    Raises ``ActionNotWiredError`` for an allow-listed action with no adapter yet,
    and ``ValueError`` for missing/invalid args — both are caught upstream by
    ``apply_proposed_actions`` and reported as that action's failure, never
    aborting the batch.
    """
    adapter = _ADAPTERS.get(action)
    if adapter is None:
        raise ActionNotWiredError(f"'{action}' can't be applied automatically yet.")
    return await adapter(args or {})


__all__ = ["ActionNotWiredError", "default_dispatch"]
