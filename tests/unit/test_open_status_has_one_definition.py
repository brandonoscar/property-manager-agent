"""Every SQL reader of "open" counts a work order whose status is unknown.

``status NOT IN (closed)`` is never true for NULL, so a work order whose
status a system did not send, or sent in words Occupella does not map, was
dropped by the cockpit, the morning brief, the reminders and seven more
readers, while ``open_work_orders`` (the one reader with an ``IS NULL`` arm)
listed it. ``maintenance_status.is_open(None)`` already said open; the SQL
said closed. ``open_status_sql`` is the one SQL spelling, and this file holds
every reader to it, read from the AST (rule 17).
"""

from __future__ import annotations

import ast
from pathlib import Path

from helixis.buildium import maintenance_status
from helixis.buildium.maintenance_status import CLOSED_STATUSES, is_open, open_status_sql

_SRC = Path(maintenance_status.__file__).resolve().parents[1]
_HOME = Path(maintenance_status.__file__).resolve()
#: The closed list as a SQL literal, the way a hand-copied filter spells it.
_LITERAL = ", ".join(f"'{s}'" for s in CLOSED_STATUSES)


def _docstrings(tree: ast.AST) -> set[int]:
    """Ids of the docstring nodes, which may quote the defect they explain."""
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                found.add(id(body[0].value))
    return found


def _offenders() -> list[str]:
    hits: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        if path.resolve() == _HOME:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        skip = _docstrings(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, (ast.Name, ast.Attribute))
                and (node.func.id if isinstance(node.func, ast.Name) else node.func.attr)
                == "closed_statuses_sql"
            ):
                hits.append(f"{path.relative_to(_SRC)}:{node.lineno} calls closed_statuses_sql")
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in skip
                and _LITERAL in node.value
            ):
                hits.append(f"{path.relative_to(_SRC)}:{node.lineno} spells the closed list")
    return hits


def test_no_reader_builds_its_own_open_filter() -> None:
    assert _offenders() == []


def test_the_guard_reads_the_readers() -> None:
    """The scan reaches the files that use the helper: an empty scan proves
    nothing (gotcha 33's shape)."""
    users = [
        p
        for p in _SRC.rglob("*.py")
        if p.resolve() != _HOME
        and any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "open_status_sql"
            for n in ast.walk(ast.parse(p.read_text()))
        )
    ]
    assert len(users) >= 10


def test_unknown_is_open_in_sql_as_in_python() -> None:
    sql = open_status_sql("wo.status")
    assert sql.startswith("(wo.status IS NULL OR wo.status NOT IN (")
    for status in CLOSED_STATUSES:
        assert f"'{status}'" in sql
    assert is_open(None) and not is_open("Completed")
