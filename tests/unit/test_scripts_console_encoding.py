"""A script that emits non-ASCII must be able to survive a Windows console.

WHAT HAPPENED
-------------
#923 added a ``⚠`` to the battery's run report. Battery v9 (2026-08-27) then
crashed with ``UnicodeEncodeError: 'charmap' codec can't encode character
'\\u26a0'`` at ``run_battery.py``'s ``write_text(report)`` — **after all twelve
questions had executed**. ``Path.write_text`` with no ``encoding=`` uses
``locale.getpreferredencoding()``, cp1252 on Windows. The per-question JSON had
already landed so no answers were lost, but ``run-report.md`` was left holding
the PREVIOUS run's content (which reads as this run's) and the process exited
non-zero on a battery that had succeeded.

⚠ WHY THIS READS THE DIRECTORY AND NOT A FUNCTION
--------------------------------------------------
Gotcha 45's rule. The fix — ``encoding="utf-8"`` at each call site, plus
``force_utf8_console()`` for ``print`` — is correct and a test feeding it
hand-written inputs would pass forever while the next script quietly reopened
the hole. What has to be measured is the CORPUS: which files emit non-ASCII,
and whether each of them is safe. That is also why the marker set below is
derived from the files themselves rather than listed here; a hardcoded list of
"the emoji we use" goes stale the first time someone picks a new one.

⚠ AND WHY ``print`` IS PART OF IT
----------------------------------
The ``write_text`` half is the loud one. The quiet half is that Python 3.6+
writes to a real Windows console through ``WindowsConsoleIO`` (UTF-8
internally), so ``print("⚠ …")`` works when a human watches it and raises the
moment the command is redirected or piped — which is exactly how a scheduled
or overnight run is invoked, and never how it is checked by hand.

NOT ASSERTED HERE
-----------------
That ``src/`` and ``tests/`` do the same. Application code writes through
loggers and the SSE bus, neither of which resolves an encoding from the
locale, and pytest replaces the streams anyway. This is scoped to the surface
where the bug is real: standalone scripts a human runs at a shell.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

SCRIPTS = pathlib.Path("scripts")

#: The helper every affected script is expected to call.
GUARD_CALL = "force_utf8_console"


def _non_ascii(text: str) -> set[str]:
    return {ch for ch in text if ord(ch) > 127}


def _emitting_scripts() -> list[pathlib.Path]:
    """Scripts whose SOURCE contains characters cp1252 cannot represent.

    Deliberately the whole file, not just string literals: a ``—`` in a
    docstring is fine, but the point of the scan is to find files where a
    non-ASCII character could reach a stream at all, and a docstring is
    printable via ``--help``.
    """
    out = []
    for path in sorted(SCRIPTS.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for ch in _non_ascii(text):
            try:
                ch.encode("cp1252")
            except UnicodeEncodeError:
                out.append(path)
                break
    return out


def _writes_without_encoding(tree: ast.AST) -> list[int]:
    """Line numbers of ``*.write_text(...)`` calls with no ``encoding=``.

    ⚠ AST, not a substring scan (gotcha 40) — and the direction it fails in is
    worth naming, because it is not the obvious one. ``ruff format`` wraps a
    long call as::

        (args.out / "a-very-long-report-name.md").write_text(
            report, encoding="utf-8"
        )

    A ``"write_text(" in line and "encoding=" not in line`` check flags that
    CORRECT call — measured, not assumed. So the substring form does not go
    quietly blind; it cries wolf on every properly-encoded wrapped call, and
    the natural way to shut it up (join lines, strip continuations, skip files)
    is what eventually makes it blind. Walking the tree is exact in both
    directions and needs no line handling at all.
    """
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "write_text"):
            continue
        if not any(kw.arg == "encoding" for kw in node.keywords):
            bad.append(node.lineno)
    return bad


@pytest.mark.parametrize("path", _emitting_scripts(), ids=lambda p: p.name)
def test_a_script_that_emits_non_ascii_writes_files_with_an_explicit_encoding(
    path: pathlib.Path,
) -> None:
    """⚠ THE crash. Every ``write_text`` in an affected script must name its
    encoding — not only the one that happened to blow up (gotcha 25: fix a bug
    at every layer that has it, not at the line that reported it)."""
    bad = _writes_without_encoding(ast.parse(path.read_text(encoding="utf-8")))
    assert not bad, (
        f"{path} contains characters cp1252 cannot encode, and has write_text calls with no "
        f"encoding= at line(s) {bad}. On Windows those raise UnicodeEncodeError at the moment "
        f"of writing — for a report written at the END of a long run, that means the work is "
        f"done, the output file still holds the PREVIOUS run, and the exit code says failure. "
        f'Pass encoding="utf-8".'
    )


#: Scripts that must not depend on the operator getting their shell right.
#:
#: ⚠ SCOPED ON PURPOSE, and the scope is the interesting part. 27 scripts here
#: print non-cp1252 characters; requiring the call in all of them would be 27
#: edits to files this change has no reason to touch, and gotcha 48 records
#: exactly that trade ("a repo-wide convention this file would be inventing and
#: back-filling"). The corpus-wide answer is not 27 edits — it is one
#: environment variable, ``PYTHONUTF8=1``, which puts the interpreter in UTF-8
#: mode and fixes ``print`` AND ``locale.getpreferredencoding()`` for every
#: script at once. That belongs in the runbook (§4) and it is there.
#:
#: These two are listed because they are the ones that run for HOURS and cost
#: real money, so "the operator forgot to set the variable" must not be able to
#: lose the run. Everything else degrades to a crash the operator sees
#: immediately and re-runs for free.
MUST_NOT_DEPEND_ON_THE_SHELL = ("run_battery.py", "score_battery.py")


@pytest.mark.parametrize("name", MUST_NOT_DEPEND_ON_THE_SHELL)
def test_the_long_expensive_scripts_reconfigure_their_own_console(name: str) -> None:
    """⚠ The quiet half — fails only under redirection, which is how a
    scheduled or overnight run is invoked and never how anyone checks by hand.

    Python 3.6+ writes to a real Windows console through ``WindowsConsoleIO``
    (UTF-8 internally), so ``print("⚠ …")`` looks fine to a watching human and
    raises the moment the same command is piped to a file. ``score_battery.py``
    ends by printing ``Wrote report.md … → <dir>``: redirected and unguarded,
    it writes all three outputs correctly and then dies on the line announcing
    that it worked.
    """
    path = SCRIPTS / name
    tree = ast.parse(path.read_text(encoding="utf-8"))
    called = any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == GUARD_CALL
        for n in ast.walk(tree)
    )
    assert called, (
        f"{path} is a long, expensive run whose output is routinely redirected, and it never "
        f"calls {GUARD_CALL}(). Add `from _console import {GUARD_CALL}` after the sys.path "
        f"setup and call it at import time. PYTHONUTF8=1 also fixes it, but a run that costs "
        f"an hour and real money must not depend on the operator remembering."
    )


def test_the_scan_actually_finds_something() -> None:
    """⚠ Assert the probe RAN (gotcha 33). Both tests above are parametrized
    over ``_emitting_scripts()``; if that ever returns empty — a bad glob, a
    wrong CWD, a refactor that moves the scripts — pytest collects ZERO cases
    and reports green while measuring nothing."""
    found = _emitting_scripts()
    assert found, (
        "no script under scripts/ contains non-cp1252 characters. Either the corpus was cleaned "
        "up (delete this file and say why) or the scan is pointed at the wrong place — check the "
        f"working directory: {SCRIPTS.resolve()}"
    )
