"""A `.Deleted` webhook has to reach the mirror, or the mirror lies forwards.

Sixteen ``*.Deleted`` events are routed and only five removed anything —
applicant, vendor, task, bill, plus the GL/bank prunes. Six entity types with
a real mirror table wrote an activity line SAYING the record was deleted and
left the row in place, including two handlers named for it:
``handle_lease_deleted`` and ``handle_workorder_deleted``.

Every mirror rollup reads those tables. A deleted property stayed in
``portfolio_overview``, a deleted lease in ``delinquency_report``, a deleted
work order in the maintenance counts, a deleted ledger row in the balance
arithmetic behind a late-notice recommendation.

⚠ **That is a false PRESENCE, and it is worse than the false absence this
project already fears (gotcha 26).** An absence sends the manager to go and
look. A presence sends them to act on a record that is not there — chase a
resident for a charge that was reversed, schedule against a work order that
was cancelled. Buildium is the source of truth and the mirror is a cache; a
cache that cannot evict is not a cache.
"""

from __future__ import annotations

import ast
import pathlib
import re

HANDLERS = pathlib.Path(__file__).resolve().parents[2] / "src" / "helixis" / "webhooks" / "handlers"
DISPATCH = HANDLERS.parent / "dispatch.py"

#: Entity types whose `.Deleted` event MUST remove the mirror row, mapped to
#: the repository function that does it. Adding a mirror table means adding a
#: line here, which is the deliberate act this file exists to force.
MUST_DELETE: dict[str, str] = {
    "Rental.Deleted": "delete_property",
    "RentalUnit.Deleted": "delete_unit",
    "Lease.Deleted": "delete_lease",
    "WorkOrder.Deleted": "delete_work_order",
    "LeaseTransaction.Deleted": "delete_lease_transaction",
    "LeaseTenant.Deleted": "delete_lease_tenant_link",
    "Applicant.Deleted": "delete_applicant",
    "Vendor.Deleted": "delete_vendor",
    "Task.Deleted": "delete_task",
    "Bill.Deleted": "delete_bill",
    "GLAccount.Deleted": "prune_gl_accounts",
}

#: `.Deleted` events with NO mirror table of their own — nothing to evict.
#: Listed rather than omitted so the next reader can tell "considered and
#: exempt" from "nobody looked" (gotcha 48: an unexplained absence of a rule
#: is indistinguishable from a forgotten one).
NO_MIRROR_TABLE: dict[str, str] = {
    "BankAccount.Transaction.Deleted": "bank transactions are not mirrored",
    "Bill.Payment.Deleted": "payments are not mirrored; the bill row carries the state",
    "Listing.Deleted": "listings are not mirrored",
    "Task.History.Deleted": "task history is not mirrored",
    "Vendor.Transaction.Deleted": "vendor transactions are not mirrored",
}


def _routed_deletes() -> dict[str, str]:
    """Every ``*.Deleted`` event in the dispatch table → its handler name."""
    return dict(
        re.findall(r'"([A-Za-z.]+\.Deleted)":\s*(\w+)', DISPATCH.read_text(encoding="utf-8"))
    )


def _handler_source(name: str) -> str:
    """The source of one handler function, wherever it lives."""
    for path in HANDLERS.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
                return ast.unparse(node)
    raise AssertionError(f"handler {name!r} not found — the scan is stale, not the code")


def _calls_in(source: str) -> set[str]:
    """Function names called anywhere in a handler, including via a module
    alias (``entities_repo.delete_work_order``)."""
    out: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                out.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                out.add(node.func.attr)
    return out


def test_every_routed_delete_is_classified() -> None:
    """gotcha 28's both-sides rule. A new `.Deleted` route must land in one of
    the two dicts above — must-delete, or exempt-with-a-reason. Silence is how
    six of these sat unnoticed."""
    routed = set(_routed_deletes())
    classified = MUST_DELETE.keys() | NO_MIRROR_TABLE.keys()
    assert routed, "the dispatch scan found no .Deleted routes — it is broken, not clean"
    assert not (routed - classified), (
        f"unclassified .Deleted routes: {sorted(routed - classified)}. Decide "
        f"whether each removes a mirror row or has no table, and say which."
    )


def test_the_classification_has_no_stale_entries() -> None:
    """Both directions (gotcha 38). An entry for a route that no longer exists
    is a rule nobody is applying, and it would silently excuse the route if it
    came back under the same name."""
    routed = set(_routed_deletes())
    stale = (MUST_DELETE.keys() | NO_MIRROR_TABLE.keys()) - routed
    assert not stale, f"classified but no longer routed: {sorted(stale)}"


def test_each_mirrored_delete_actually_deletes() -> None:
    """THE defect. Two of these handlers are NAMED for deletion and did not
    delete; four more wrote an activity line and moved on."""
    routed = _routed_deletes()
    missing: list[str] = []
    for event, expected in MUST_DELETE.items():
        handler = routed.get(event)
        if handler is None:
            continue  # covered by the stale-entry test above
        if expected not in _calls_in(_handler_source(handler)):
            missing.append(f"{event} -> {handler}() never calls {expected}()")
    assert not missing, (
        "these events tell the mirror a record is gone and leave the row in "
        "place, so every rollup keeps reporting it — a confident false "
        "presence:\n  " + "\n  ".join(missing)
    )


def test_a_delete_never_reaches_another_tenant() -> None:
    """Every delete is company-scoped (gotcha 47). A mirror eviction that
    dropped the filter would delete a DIFFERENT company's row on a Buildium id
    collision — silent, permanent cross-tenant data loss, which is the worst
    outcome in this file by a distance."""
    src = (HANDLERS.parent.parent / "db" / "repositories" / "entities.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(src)
    unscoped: list[str] = []
    for node in tree.body:
        if not (isinstance(node, ast.AsyncFunctionDef) and node.name.startswith("delete_")):
            continue
        body = ast.unparse(node)
        statements = [s for s in re.findall(r"DELETE FROM[^\"']*", body, re.IGNORECASE | re.DOTALL)]
        assert statements, f"{node.name} has no DELETE statement"
        for stmt in statements:
            if "company_id" not in stmt:
                unscoped.append(f"{node.name}: {' '.join(stmt.split())[:90]}")
    assert not unscoped, f"unscoped mirror delete(s): {unscoped}"


def test_the_delete_helpers_all_exist() -> None:
    """Resolution, checked separately from registration — a name in the map
    pointing at a function that was renamed fails just as silently as one that
    was never written (gotcha 35's third trap)."""
    from helixis.db.repositories import entities

    for expected in MUST_DELETE.values():
        assert hasattr(entities, expected), f"{expected} is named here but does not exist"


def _sql_literals(func_name: str) -> list[str]:
    """The SQL string constants inside one repository function, EXCLUDING its
    docstring.

    ⚠ ``ast.unparse`` includes the docstring, and ``delete_lease``'s docstring
    names both tables in order to explain why both are deleted. Asserting
    against the unparsed source therefore passed against a version with the
    link delete removed — the mutation that proved it is why this helper
    exists. Third instance of gotcha 40 in one sitting, and the first two were
    substring scans; this one was inside an AST walk written to avoid them.
    """
    from helixis.db.repositories import entities

    tree = ast.parse(pathlib.Path(entities.__file__).read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == func_name)
    # ⚠ Exclude the docstring by NODE IDENTITY, not by comparing values.
    # ``ast.get_docstring`` returns the CLEANED string (dedented, stripped),
    # while the ``Constant`` in the tree holds the raw literal with its
    # original indentation — so ``c.value != doc`` never matches and the
    # docstring stays in the list. That is exactly how the first version of
    # this helper passed against a ``delete_lease`` with the link delete
    # removed: the docstring names both tables in order to explain them.
    docstring_node = (
        node.body[0].value
        if node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        else None
    )
    return [
        c.value
        for c in ast.walk(node)
        if isinstance(c, ast.Constant) and isinstance(c.value, str) and c is not docstring_node
    ]


def test_deleting_a_lease_takes_its_tenant_links_with_it() -> None:
    """``helixis_lease_tenants`` keys on ``lease_buildium_id`` with no FK
    cascade, so a surviving link points at a lease that is gone — and
    ``contact_lookup`` reads that join to decide whether someone is a current
    resident (gotcha 51's shape, one field over)."""
    sql = " ".join(_sql_literals("delete_lease"))
    assert "helixis_lease_tenants" in sql, "the join rows survive their lease"
    assert "helixis_leases" in sql


def test_the_lease_delete_is_atomic() -> None:
    """Both statements land together or neither. A crash between them leaves
    either orphaned links or a lease nobody can find the tenants for, and the
    second is the shape that makes `contact_lookup` describe a resident who
    has none."""
    from helixis.db.repositories import entities

    tree = ast.parse(pathlib.Path(entities.__file__).read_text(encoding="utf-8"))
    node = next(
        n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "delete_lease"
    )
    withs = [
        ast.unparse(item.context_expr)
        for w in ast.walk(node)
        if isinstance(w, ast.AsyncWith)
        for item in w.items
    ]
    assert any("transaction()" in w for w in withs), "the two deletes are not in one transaction"
