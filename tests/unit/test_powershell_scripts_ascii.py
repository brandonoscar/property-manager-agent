"""Every .ps1 in this repo is ASCII-only, because Windows PowerShell 5.1.

A BOM-less `.ps1` containing non-ASCII is read by Windows PowerShell 5.1 as
CP1252, not UTF-8. An em dash is `E2 80 94` in UTF-8, which becomes three
CP1252 characters ending in **0x94 -- a right double quotation mark, which 5.1
accepts as a STRING TERMINATOR**. The string closes early and the file
cascades into parse errors far from the real cause:

    line  88  Unexpected token 'the' in expression or statement
    line 113  Unexpected token 'safe' ... The hash literal was incomplete
    line 241  The string is missing the terminator: '
    line 306  Missing closing ')' in subexpression

⚠ **No guard INSIDE the file can help.** This happens at parse time, before a
single line executes -- so a `[Console]::OutputEncoding` line at the top, or a
`#requires`, or any runtime check, is already too late. The only fix is that
the bytes never leave ASCII.

WHY THIS TEST EXISTS RATHER THAN A NOTE SOMEWHERE
-------------------------------------------------
It has happened twice. `scripts/replay_webhooks.ps1` was converted to ASCII on
2026-08-25 (be026e8) for exactly this reason, and
`scripts/test_buildium_writes.ps1` reintroduced it four days later with 126
non-ASCII characters -- 92 box-drawing dividers, 21 em dashes, 12 warning
signs and one pound sign. The dry run that was supposed to de-risk a set of
irreversible Buildium writes did not run at all, and nothing about the harness
was proven.

A convention that has to be remembered gets forgotten. This is the same
reasoning as `test_scripts_console_encoding.py`, which guards the *Python*
half of the encoding problem -- and which did not cover `.ps1` at all.

⚠ Non-ASCII in a `.ps1` is not merely a portability nit. `#requires -Version
7.0` does NOT rescue it: `#requires` is honoured only once the file
tokenises, and this defect stops it tokenising. Declaring 7.0 and using
non-ASCII gives you the parse cascade above instead of an honest refusal.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]

#: Every PowerShell script in the repo, wherever it lives -- the last
#: recurrence landed in scripts/ while the previous one was already fixed
#: there, so scanning one directory would have missed it.
_PS1 = sorted(
    p for p in _REPO.rglob("*.ps1") if ".git" not in p.parts and "node_modules" not in p.parts
)


def test_the_scan_actually_finds_the_powershell_corpus() -> None:
    """A bad glob collects nothing and reports green -- the self-check
    `test_scripts_console_encoding` needed for the same reason (gotcha 49)."""
    assert len(_PS1) >= 3, f"only {len(_PS1)} .ps1 files found -- did the glob or CWD change?"


@pytest.mark.parametrize("path", _PS1, ids=lambda p: str(p.relative_to(_REPO)))
def test_powershell_scripts_are_ascii_only(path: Path) -> None:
    raw = path.read_bytes()
    offenders: list[str] = []
    for lineno, line in enumerate(raw.split(b"\n"), start=1):
        bad = {b for b in line if b > 0x7F}
        if bad:
            try:
                text = line.decode("utf-8", errors="replace").strip()
            except Exception:  # pragma: no cover
                text = "<undecodable>"
            offenders.append(f"    line {lineno}: {text[:90]}")

    assert not offenders, (
        f"{path.relative_to(_REPO)} contains non-ASCII bytes:\n"
        + "\n".join(offenders[:12])
        + (f"\n    ... and {len(offenders) - 12} more line(s)" if len(offenders) > 12 else "")
        + "\n\nWindows PowerShell 5.1 reads a BOM-less .ps1 as CP1252, so an em dash ends in "
        "0x94 -- a right double quote, which 5.1 takes as a string terminator. The file fails "
        "to PARSE and nothing runs. Replace them: em dash -> '--', box drawing -> '-', "
        "warning sign -> '!!'. A #requires line does NOT save you; it is only honoured once "
        "the file tokenises."
    )


def test_a_bom_is_not_used_as_the_fix() -> None:
    """A UTF-8 BOM would also make 5.1 read the file correctly, and it is the
    wrong fix: it breaks other tooling, it is invisible in a diff, and it
    leaves the file one careless re-save away from the same failure. ASCII is
    the property worth holding, so a BOM here means someone patched the
    symptom."""
    for path in _PS1:
        assert not path.read_bytes().startswith(b"\xef\xbb\xbf"), (
            f"{path.relative_to(_REPO)} starts with a UTF-8 BOM. That hides the problem "
            "rather than fixing it -- make the file ASCII instead."
        )
