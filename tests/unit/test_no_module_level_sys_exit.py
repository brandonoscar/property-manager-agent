"""A script a test imports must never ``sys.exit`` while being imported.

`scripts/chat_probe.py` did, on a missing optional dependency::

    try:
        from playwright.async_api import Page, async_playwright
    except ImportError:
        sys.exit("playwright is required...")

Four unit files import that module — directly or through `record_demo` — for
its PURE logic: the selector guards, the retime maths, the table policy. None
needs a browser. CI has no playwright.

`SystemExit` is a `BaseException`, so it walks straight through every
`except ImportError` and `except Exception` a caller writes. Raised during
pytest COLLECTION it is not a skipped module — it is an **INTERNALERROR that
aborts the whole session**. The backend job printed ``1 skipped`` and exited 3
while **5,279 collected tests never ran**, on every push, for hours.

⚠ **Invisible on any machine that has playwright.** The import succeeds, the
branch is never taken, the suite is green. That asymmetry is the whole reason
for a static guard: no amount of running the tests locally can see it.

Gotcha 7's rule — "an optional dep that ``sys.exit``s kills the process past
every handler" — arriving in operator tooling rather than in spaCy, and
gotcha 42's "read the summary line, not the absence of the word FAILED".

The fix and this guard say different things, deliberately. The fix makes the
module import cleanly without a browser (bindings become ``None``; ``main``
prints the friendly instructions and returns 2). This asserts that nobody
re-introduces the shortcut.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _ROOT / "scripts"
_TESTS = _ROOT / "tests"


def _scripts_imported_by_tests() -> set[str]:
    """Module names under ``scripts/`` that any test imports.

    ⚠ Includes TRANSITIVE reach through a `scripts` module, because that is
    how this defect actually arrived: no test imports `chat_probe`'s exiting
    line directly — `test_record_demo_signed_in` imports `record_demo`, which
    imports `chat_probe`. A guard that only looked at direct test imports
    would have reported green.
    """
    available = {p.stem for p in _SCRIPTS.glob("*.py")}
    direct: set[str] = set()
    for path in _TESTS.rglob("test_*.py"):
        source = path.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                direct.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                direct.add(node.module.split(".")[0])
        # `test_record_demo_signed_in` loads the module by PATH via importlib
        # rather than by name, so the import graph alone cannot see it.
        for name in available:
            if f"{name}.py" in source:
                direct.add(name)
    reached = {n for n in direct if n in available}

    frontier = set(reached)
    while frontier:
        nxt: set[str] = set()
        for name in frontier:
            tree = ast.parse((_SCRIPTS / f"{name}.py").read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    nxt.update(a.name.split(".")[0] for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    nxt.add(node.module.split(".")[0])
        nxt = {n for n in nxt if n in available} - reached
        reached |= nxt
        frontier = nxt
    return reached


def _module_level_exits(path: Path) -> list[int]:
    """Line numbers of ``sys.exit`` / ``exit`` calls OUTSIDE any function.

    ⚠ Module scope only. `main()` calling `sys.exit` is correct and common —
    what kills an importer is a call that runs while the module body executes.
    Walking the whole tree would flag every well-behaved CLI in the directory
    and the guard would be deleted within a week (gotcha 52).
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits: list[int] = []

    def scan(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                continue  # a call inside a function only runs when called
            if isinstance(child, ast.Call):
                func = child.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name == "exit" or name == "_exit":
                    hits.append(child.lineno)
            scan(child)

    # ⚠ The TOP-LEVEL statements need the same two skips as the children, and
    # leaving them off is a bug this guard shipped with for one run: `scan`
    # only skipped function definitions it found as CHILDREN, so a top-level
    # `def main(): sys.exit(2)` was walked into and `seed.py` /
    # `create_user.py` were both reported as defects. Every one of those calls
    # is correct — inside a function, or under the `__main__` guard.
    #
    # A guard that cries wolf on correct code is one somebody deletes
    # (gotcha 52), so the false positives mattered more than the miss would
    # have. Caught by RUNNING it against the real directory rather than
    # against hand-written inputs (gotcha 45).
    for stmt in tree.body:
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        if isinstance(stmt, ast.If) and "__main__" in ast.unparse(stmt.test):
            continue
        scan(stmt)
    return hits


def test_the_scan_finds_the_scripts_tests_actually_reach() -> None:
    """Floor: a wrong root or a broken import walk would collect nothing and
    report green against the very defect it targets (gotcha 49)."""
    reached = _scripts_imported_by_tests()
    assert "chat_probe" in reached, (
        "the transitive walk no longer reaches chat_probe — it is imported by "
        "record_demo, which several test files load. If the guard cannot see "
        "it, the guard is measuring nothing."
    )


@pytest.mark.parametrize("name", sorted(_scripts_imported_by_tests()))
def test_no_script_a_test_imports_exits_at_module_scope(name: str) -> None:
    lines = _module_level_exits(_SCRIPTS / f"{name}.py")
    assert not lines, (
        f"scripts/{name}.py calls sys.exit at MODULE scope (line(s) {lines}). "
        "A test imports this module, and SystemExit is a BaseException — it "
        "passes through every `except ImportError` / `except Exception` a "
        "caller writes, and during pytest collection it is an INTERNALERROR "
        "that aborts the entire session with zero tests run. Raise, or set the "
        "binding to None and exit from main() instead."
    )


def test_the_guard_can_see_the_defect_it_exists_for(tmp_path: Path) -> None:
    """Mutation, inline: the scan must flag the exact shape that broke CI, and
    must SPARE the two shapes that are correct (gotcha 38 — test what it must
    spare, not only what it must catch)."""
    bad = tmp_path / "bad.py"
    bad.write_text(
        "import sys\ntry:\n    import nope\nexcept ImportError:\n    sys.exit('missing')\n",
        encoding="utf-8",
    )
    assert _module_level_exits(bad), "the scan cannot see a module-level sys.exit"

    good = tmp_path / "good.py"
    good.write_text(
        "import sys\n\n\ndef main() -> int:\n    sys.exit(2)\n\n\n"
        'if __name__ == "__main__":\n    raise SystemExit(main())\n',
        encoding="utf-8",
    )
    assert not _module_level_exits(good), (
        "sys.exit inside main() and `raise SystemExit(main())` under the "
        "__main__ guard are both correct and must not be flagged"
    )
