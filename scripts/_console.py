"""Make a script's own output survive a Windows console.

WHAT HAPPENED
-------------
#923 added a ``⚠`` marker to the battery's run report. On the next live run
(battery v9, 2026-08-27) the harness crashed with::

    UnicodeEncodeError: 'charmap' codec can't encode character '\\u26a0'
      File "scripts/run_battery.py", line 835, in main
        (args.out / "run-report.md").write_text(report)

**after every question had already executed.** ``Path.write_text`` with no
``encoding=`` uses ``locale.getpreferredencoding()``, which on Windows is
cp1252 and cannot represent ``⚠``. The per-question JSON had landed, so the
data survived — but ``run-report.md`` was left holding the PREVIOUS run's
content, which reads as this run's, and the process exited non-zero on a
battery that had actually succeeded.

⚠ THE SAME BUG HAS A SECOND HALF, AND IT IS QUIETER
----------------------------------------------------
``write_text`` was fixed by passing ``encoding="utf-8"`` at every call site.
That does nothing for ``print``. Python 3.6+ writes to a real Windows console
through ``WindowsConsoleIO``, which is UTF-8 internally — so a ``print("⚠ …")``
looks fine when a human watches it and raises ``UnicodeEncodeError`` the moment
the same command is redirected to a file or piped. ``score_battery.py`` ends
with ``print(f"Wrote report.md … → {dir}")``: on a redirected run it writes all
three outputs correctly and then dies on the line that says it succeeded.

That is a worse shape than the crash above, because the failure appears only
under redirection — which is exactly how an overnight or scheduled run is
invoked, and never how it is tested by hand.

WHY A HELPER AND NOT A RULE
---------------------------
"Remember to pass ``encoding=``" is the kind of instruction gotchas 25/32/34
all record failing. This makes compliance one call, and
``tests/unit/test_scripts_console_encoding.py`` reads the SCRIPTS DIRECTORY —
not this function — so a new script that prints a ``✅`` without calling it
fails a test rather than an overnight run (gotcha 45's rule: a control whose
failure mode is "silently permits more" needs a test that measures what it
permitted).
"""

from __future__ import annotations

import sys


def force_utf8_console() -> None:
    """Reconfigure ``stdout``/``stderr`` to UTF-8 for the life of the process.

    ``errors="replace"`` on purpose: a diagnostic script that dies rather than
    printing a ``?`` has turned a cosmetic problem into a lost run, and losing
    a live battery run costs real money and an hour of wall clock.

    Safe to call unconditionally — a no-op on any platform already writing
    UTF-8, and silently skipped where the streams have been swapped for
    something without ``reconfigure`` (pytest's capture, a StringIO), which is
    why it is wrapped rather than guarded on ``sys.platform``.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            # Not a reconfigurable text stream (captured, redirected to a
            # non-text object, already detached). Nothing to do and nothing
            # worth failing a run over.
            pass


__all__ = ["force_utf8_console"]
