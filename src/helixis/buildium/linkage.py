"""Resolving which property a Buildium record belongs to, once, for every
layer that needs it.

Same shape as :mod:`helixis.buildium.money`: one rule, several call sites that
must share it. They did not share it before, and the layer that got it wrong
was the one nothing tested — the task enricher read a flat ``PropertyId`` that
production never sends, so the planner was told every task was property-less
and wrote that up as a finding on correctly-linked records.

Two facts, both live-verified on account 695763, hold this module together:

* **A task's property is nested.** ``Property.Id`` is present on 90 of 90
  mirrored task rows and the flat ``PropertyId`` on **zero** of them (checked
  2026-08-24 over ``helixis_tasks.raw``, independently of the 179-row check in
  gotcha 26). The flat key is still accepted as a fallback because our own
  write bodies use it — ``endpoints/tasks.py`` converts nested → flat for the
  read-merge-write PUT.
* **A work order has no property of its own.** ``GET /workorders`` sends no
  property linkage at all — not nested, not flat. Its property is whatever
  its backing task's is, reached through ``WorkOrder.Task.Id``. Confirmed by
  joining all 60 mirrored work orders against the task mirror: 60 carry a
  task id, 60 resolve, 60 would gain a property.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def task_property_id(raw: Any) -> int | None:
    """The property a task belongs to.

    Accepts the raw Buildium body, a ``model_dump(by_alias=True)`` of one, or
    anything else with those keys. Prefers the nested ``Property.Id`` that
    production actually sends and falls back to a flat ``PropertyId``.

    Returns ``None`` when neither is present, which means **unknown** — a
    caller that stores this must not let it overwrite a property it already
    knows (see ``upsert_work_order``).
    """
    if not isinstance(raw, Mapping):
        return None
    nested = raw.get("Property")
    if isinstance(nested, Mapping):
        resolved = _to_int(nested.get("Id"))
        if resolved is not None:
            return resolved
    return _to_int(raw.get("PropertyId"))


def work_order_task_id(wo: Any) -> int | None:
    """The id of the to-do task backing a work order.

    This is the ONLY link a work order carries to the rest of the portfolio —
    its own ``PropertyId`` and ``UnitId`` are null on every production row —
    so it is what a property lookup has to go through. Reads the typed
    ``WorkOrder.task.id`` first, then the raw ``Task.Id``, so it works on a
    parsed model and on a stored ``raw`` blob alike.
    """
    if isinstance(wo, Mapping):
        nested = wo.get("Task")
        if isinstance(nested, Mapping):
            return _to_int(nested.get("Id"))
        return None
    task = getattr(wo, "task", None)
    if task is not None:
        return _to_int(getattr(task, "id", None))
    return None


def work_order_property_ids(work_orders: Any, tasks: Any) -> dict[int, int]:
    """``work order id -> property id``, resolved through the backing tasks.

    This is the LIVE-API twin of the join ``upsert_work_order`` does in SQL,
    and it exists because that fix reached the mirror and not the agent tools
    — gotcha 25's rule ("fix a field-shape bug at EVERY layer that reads it")
    applied to the one field a work order does not carry.

    What it cost while only the mirror had it: ``_maintenance_index`` keyed on
    ``WorkOrder.property_id``, which is null on every production row, so the
    portfolio rollup built to kill the per-record N+1 filed all 60 of Blue
    Ridge's open work orders under "unassigned". And ``_only_for_unit`` fell
    back to returning everything (correctly — see ``_only_matching``, a blind
    field must not manufacture an absence), so "the most recent open work
    order at 500 Woodlane St" was handed all 60 in the account. The agent
    then said, twice and at full confidence, that a single-unit property had
    60 open work orders and speculated they were duplicate test data. The
    mirror says 1. Two dry-run cases died on the wall clock over it.

    ⚠ Callers pass tasks they have ALREADY fetched. Resolving one work order
    at a time would re-create the N+1 these rollups exist to remove, which is
    gotcha 32's *removing one fan-out just promotes the next one behind it*.

    A work order with no task, or whose task is not in ``tasks``, is simply
    ABSENT from the result — never mapped to a guessed property. An
    unattributable row that gets attributed is the false claim being removed;
    a missing key lets the caller say "unassigned" honestly.
    """
    by_task: dict[int, int] = {}
    for task in tasks or ():
        task_id = _to_int(getattr(task, "id", None))
        if task_id is None:
            continue
        prop = _to_int(getattr(getattr(task, "property", None), "id", None))
        if prop is None:
            prop = _to_int(getattr(task, "property_id", None))
        if prop is not None:
            by_task[task_id] = prop

    resolved: dict[int, int] = {}
    for wo in work_orders or ():
        wo_id = _to_int(getattr(wo, "id", None))
        if wo_id is None:
            continue
        # A work order's own PropertyId is null on every production row, but
        # other accounts' rows do carry it -- prefer it when present, exactly
        # as the mirror's COALESCE does, so this can never be a regression for
        # an account where the payload is complete.
        own = _to_int(getattr(wo, "property_id", None))
        if own is not None:
            resolved[wo_id] = own
            continue
        task_id = work_order_task_id(wo)
        if task_id is not None and task_id in by_task:
            resolved[wo_id] = by_task[task_id]
    return resolved


def _to_int(value: Any) -> int | None:
    """Buildium sends ids as int or numeric string depending on the source."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = ["task_property_id", "work_order_property_ids", "work_order_task_id"]
