"""Did the write DO the thing it is named for? Read it back and check.

**Why this exists.** ``helixis_action_idempotency`` records ``succeeded`` when
Buildium returns 2xx. Until 2026-08-31 that was the harness's only evidence,
and the two disagreed for the first time that night: ``record_move_out``
returned 2xx, logged ``succeeded``, and produced **no move-out** — lease
115938 still read ``Active``, ``GET /leases/{id}/moveouts/{tenantId}`` 404'd,
and the single observable effect was ``LeaseToDate`` moving 2026-09-14 →
2026-08-30. The agent's own read-back then reported the tenant "flagged as a
former tenant… no further action needed": three claims, all contradicted by
the account (gotcha 70).

So a verdict of WROTE means "the request was accepted", never "the record
changed". These verifiers close that gap by re-GETting the record and
asserting the field the tool is **named for**.

⚠ **Each verifier must read the field WHERE IT ACTUALLY SURFACES**, and they
do not all behave the same (gotcha 20, learned from the opposite failure — a
verify reading the wrong field reported every SUCCESSFUL change as
unconfirmed):

* work-order **status** propagates to the WO's top-level ``status``;
* work-order **priority** does NOT — it moves on the NESTED
  ``WorkOrder.Task.Priority`` while the top-level stays frozen at creation;
* the tasks endpoint returns status under **``TaskStatus``**, not ``Status``.

⚠ **Unverifiable is its own answer, never a pass.** A verifier that cannot
resolve the record returns ``None``, and the harness must print that
distinctly — reporting "could not check" as "verified" is the exact failure
this module exists to end (the LED-013 skip-floor rule).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger("write_verify")

#: ``(ok, detail)``. ``ok=None`` means the check could not run — NOT a pass.
Verdict = tuple[bool | None, str]
Verifier = Callable[[Any, dict[str, str]], Awaitable[Verdict]]


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


async def _lease_at(api: Any, fx: dict[str, str]) -> Any | None:
    """The active lease at the fixture property, or None.

    ⚠ Client-side filtering on purpose: production Buildium silently ignores
    ``?propertyids`` on ``GET /leases`` and returns the whole account (gotcha
    24), so a server-filtered read here would resolve a lease at some other
    property and the verifier would confidently check the wrong record.
    """
    prop = _norm(fx.get("property"))
    try:
        leases = await api.leases.list()
    except Exception:  # noqa: BLE001 — an unreadable account is unverifiable
        return None
    for lease in leases:
        unit = getattr(lease, "unit", None)
        addr = _norm(getattr(unit, "address_line1", "") if unit else "")
        name = _norm(getattr(getattr(lease, "property", None), "name", ""))
        if prop and (prop in addr or prop in name or addr in prop):
            return lease
    return None


# ── the one that lied ─────────────────────────────────────────────────


async def verify_record_move_out(api: Any, fx: dict[str, str]) -> Verdict:
    """A move-out must EXIST. This is the check that would have caught it.

    Two assertions, and the second is the one nobody thought to make:

    1. the moveouts collection carries a row for this lease — the tool's
       whole purpose, and it was empty while the ledger said ``succeeded``;
    2. ``lease_to_date`` did NOT move — that was the write's only real
       effect, and it is silent damage, not the named outcome.
    """
    lease = await _lease_at(api, fx)
    if lease is None or not getattr(lease, "id", None):
        return None, "could not resolve the lease at the fixture property"
    try:
        move_outs = await api.leases.list_move_outs(int(lease.id))
    except Exception as exc:  # noqa: BLE001
        return None, f"moveouts read failed: {type(exc).__name__}"

    if not move_outs:
        return (
            False,
            f"NO move-out exists on lease {lease.id} — the write reported success and "
            f"changed nothing it is named for (lease_to_date is now "
            f"{getattr(lease, 'lease_to_date', '?')})",
        )
    return True, f"{len(move_outs)} move-out row(s) on lease {lease.id}"


async def verify_undo_move_out(api: Any, fx: dict[str, str]) -> Verdict:
    """The mirror image: after an undo, the collection must be EMPTY."""
    lease = await _lease_at(api, fx)
    if lease is None or not getattr(lease, "id", None):
        return None, "could not resolve the lease at the fixture property"
    try:
        move_outs = await api.leases.list_move_outs(int(lease.id))
    except Exception as exc:  # noqa: BLE001
        return None, f"moveouts read failed: {type(exc).__name__}"
    if move_outs:
        return False, f"move-out still present on lease {lease.id} after the undo"
    return True, f"no move-out on lease {lease.id} — the undo took effect"


# ── ledger writes ─────────────────────────────────────────────────────


async def _ledger_has(api: Any, fx: dict[str, str], *, positive: bool) -> Verdict:
    """A ledger line of the expected sign appeared. Charges are positive
    ``TotalAmount``, payments negative (gotcha 21)."""
    lease = await _lease_at(api, fx)
    if lease is None or not getattr(lease, "id", None):
        return None, "could not resolve the lease at the fixture property"
    try:
        txns = await api.leases.list_transactions(int(lease.id))
    except Exception as exc:  # noqa: BLE001
        return None, f"transactions read failed: {type(exc).__name__}"
    want = "charge" if positive else "payment"
    for t in txns or []:
        amount = getattr(t, "total_amount", None)
        if amount is None:
            continue
        if (float(amount) > 0) is positive and abs(abs(float(amount)) - 1.00) < 0.005:
            return True, f"$1.00 {want} present on lease {lease.id}"
    return False, f"no $1.00 {want} found on lease {lease.id}"


async def verify_post_charge(api: Any, fx: dict[str, str]) -> Verdict:
    return await _ledger_has(api, fx, positive=True)


async def verify_record_payment(api: Any, fx: dict[str, str]) -> Verdict:
    return await _ledger_has(api, fx, positive=False)


# ── work orders and tasks ─────────────────────────────────────────────


async def _work_orders_at(api: Any, fx: dict[str, str]) -> list[Any]:
    """Work orders at the fixture property, filtered CLIENT-side.

    ⚠ ``WorkOrder.unit_id`` is declared and Buildium never populates it
    (gotcha 55) — filtering on it would turn an over-count into a false
    absence, which reads as "the write did nothing".
    """
    try:
        rows = await api.work_orders.list()
    except Exception:  # noqa: BLE001
        return []
    prop = _norm(fx.get("property"))
    out = []
    for wo in rows or []:
        blob = (
            _norm(getattr(wo, "title", ""))
            + " "
            + _norm(getattr(getattr(wo, "property", None), "name", ""))
        )
        if not prop or prop in blob:
            out.append(wo)
    return out or list(rows or [])


async def verify_close_work_order(api: Any, fx: dict[str, str]) -> Verdict:
    """Status propagates to the WO's TOP-LEVEL ``status`` (gotcha 20)."""
    for wo in await _work_orders_at(api, fx):
        if _norm(getattr(wo, "status", "")) in ("completed", "closed"):
            return True, f"work order {getattr(wo, 'id', '?')} reads Completed"
    return False, "no completed work order found at the fixture property"


async def verify_update_work_order(api: Any, fx: dict[str, str]) -> Verdict:
    """Vendor is the ONE meaningfully-mutable WO-put field (gotcha 20).

    ⚠ Priority is deliberately NOT checked on the top level: it moves on the
    NESTED ``WorkOrder.Task.Priority`` while the WO's own stays frozen at
    creation, so a top-level read false-negatives every successful change.
    """
    vendor = _norm(fx.get("vendor"))
    for wo in await _work_orders_at(api, fx):
        v = getattr(wo, "vendor", None)
        if vendor and vendor in _norm(getattr(v, "name", "") if v else ""):
            return True, f"work order {getattr(wo, 'id', '?')} is assigned to {fx.get('vendor')}"
    return False, f"no work order at the fixture property is assigned to {fx.get('vendor')}"


async def verify_create_work_order(api: Any, fx: dict[str, str]) -> Verdict:
    rows = await _work_orders_at(api, fx)
    for wo in rows:
        if "tap" in _norm(getattr(wo, "title", "")):
            return True, f"work order {getattr(wo, 'id', '?')} exists"
    return False, "no work order matching the created title was found"


async def _tasks_at(api: Any, fx: dict[str, str]) -> list[Any]:
    try:
        rows = await api.tasks.list()
    except Exception:  # noqa: BLE001
        return []
    prop = _norm(fx.get("property"))
    scoped = [
        t
        for t in rows or []
        if not prop or prop in _norm(getattr(getattr(t, "property", None), "name", ""))
    ]
    return scoped or list(rows or [])


async def verify_create_to_do_task(api: Any, fx: dict[str, str]) -> Verdict:
    for t in await _tasks_at(api, fx):
        if "smoke" in _norm(getattr(t, "title", "")):
            return True, f"task {getattr(t, 'id', '?')} exists"
    return False, "no task matching the created title was found"


async def verify_update_task_status(api: Any, fx: dict[str, str]) -> Verdict:
    """⚠ Read ``TaskStatus``, NOT ``Status``. ``Status`` is NULL on every
    production task row (gotcha 26), so checking it reports every successful
    change as unconfirmed."""
    for t in await _tasks_at(api, fx):
        if _norm(getattr(t, "task_status", "")) == "inprogress":
            return True, f"task {getattr(t, 'id', '?')} reads InProgress"
    return False, "no task at the fixture property reads InProgress"


# ── notes: append-only, so presence is the check ──────────────────────


async def verify_add_vendor_note(api: Any, fx: dict[str, str]) -> Verdict:
    try:
        vendors = await api.vendors.list()
    except Exception as exc:  # noqa: BLE001
        return None, f"vendor read failed: {type(exc).__name__}"
    want = _norm(fx.get("vendor"))
    for v in vendors or []:
        if want and want in _norm(getattr(v, "name", "")):
            try:
                notes = await api.vendors.list_notes(int(v.id))
            except Exception as exc:  # noqa: BLE001
                return None, f"vendor notes read failed: {type(exc).__name__}"
            if notes:
                return True, f"{len(notes)} note(s) on vendor {getattr(v, 'id', '?')}"
            return False, f"no notes on vendor {getattr(v, 'id', '?')}"
    return None, f"could not resolve vendor {fx.get('vendor')}"


async def verify_add_work_order_note(api: Any, fx: dict[str, str]) -> Verdict:
    """The note rides ``WorkDetails`` on a full-replace PUT (gotcha 20)."""
    for wo in await _work_orders_at(api, fx):
        if _norm(getattr(wo, "work_details", "")):
            return True, f"work order {getattr(wo, 'id', '?')} carries WorkDetails text"
    return False, "no work order at the fixture property carries note text"


async def verify_add_task_note(api: Any, fx: dict[str, str]) -> Verdict:
    """The note rides the ``Message`` field and lands as task HISTORY."""
    for t in await _tasks_at(api, fx):
        tid = getattr(t, "id", None)
        if not tid:
            continue
        try:
            history = await api.tasks.list_history(int(tid))
        except Exception:  # noqa: BLE001
            continue
        if history:
            return True, f"task {tid} has {len(history)} history entr(ies)"
    return False, "no task at the fixture property carries history"


#: Keyed by the agent tool name the harness drives. A tool ABSENT here is
#: reported as unverifiable rather than assumed fine — silence is not a pass.
VERIFIERS: dict[str, Verifier] = {
    "buildium_record_move_out": verify_record_move_out,
    "buildium_undo_move_out": verify_undo_move_out,
    "buildium_post_charge": verify_post_charge,
    "buildium_record_payment": verify_record_payment,
    "buildium_close_work_order": verify_close_work_order,
    "buildium_update_work_order": verify_update_work_order,
    "buildium_create_work_order": verify_create_work_order,
    "buildium_create_to_do_task": verify_create_to_do_task,
    "buildium_update_task_status": verify_update_task_status,
    "buildium_add_vendor_note": verify_add_vendor_note,
    "buildium_add_work_order_note": verify_add_work_order_note,
    "buildium_add_task_note": verify_add_task_note,
}


async def verify(tool: str, api: Any, fx: dict[str, str]) -> Verdict:
    """Run ``tool``'s verifier. Never raises — an exception is UNVERIFIABLE,
    which is a distinct outcome from both pass and fail."""
    fn = VERIFIERS.get(tool)
    if fn is None:
        return None, f"no read-back verifier is registered for {tool}"
    try:
        return await fn(api, fx)
    except Exception as exc:  # noqa: BLE001 — a broken check is not a passing one
        logger.warning("verifier for %s raised", tool, exc_info=True)
        return None, f"verifier raised {type(exc).__name__}"


__all__ = ["VERIFIERS", "Verdict", "verify"]
