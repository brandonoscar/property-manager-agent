"""No runbook or script help tells the founder to TYPE a secret on a line.

Windows PowerShell 5.1 (the founder's shell, CLAUDE.md §4) saves every typed
line to PSReadLine's history file in plain text, so ``$env:X_SECRET = "..."``
leaves the secret on disk after the run: a Rentvine API secret, the
helixis-test database URL, ``HELIXIS_DATA_KEY``. Every runbook asks instead:

    $s = Read-Host "..." -AsSecureString
    $env:X_SECRET = [Net.NetworkCredential]::new("", $s).Password

Reads every place a founder copies a command from: the fenced blocks of every
Markdown file, every string in ``scripts/`` and ``src/`` Python (docstrings and
the lines a script prints), and every line of ``scripts/*.ps1``. Markdown is
read by its fences and Python by its AST, never by matching source text (rule
17), so prose that names the old pattern is left alone.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build"}
_SECRET_NAME = r"[A-Z0-9_]*(?:SECRET|PASSWORD|TOKEN|DB_URL|DATA_KEY|API_KEY|PRIVATE_KEY)[A-Z0-9_]*"
#: A line that sets a secret-named variable from a string the founder types.
_TYPED = re.compile(rf"^\s*(?:#+\s*)?\$env:({_SECRET_NAME})\s*=\s*[\"']")
_FENCE = re.compile(r"^\s*(```|~~~)")


def _files(pattern: str, *roots: str) -> list[pathlib.Path]:
    out = []
    for root in roots:
        for path in sorted((REPO / root).rglob(pattern)):
            if not _SKIP_DIRS.intersection(path.relative_to(REPO).parts):
                out.append(path)
    return out


def _fenced_lines(text: str) -> list[str]:
    """The lines inside fenced code blocks; prose outside them is not a command."""
    inside, lines = False, []
    for line in text.splitlines():
        if _FENCE.match(line):
            inside = not inside
            continue
        if inside:
            lines.append(line)
    return lines


def _python_string_lines(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        line
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        for line in node.value.splitlines()
    ]


def _typed(lines: list[str]) -> list[str]:
    return [line.strip() for line in lines if _TYPED.match(line)]


def _where_typed() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in _files("*.md", "."):
        hits = _typed(_fenced_lines(path.read_text(encoding="utf-8")))
        if hits:
            found[path.relative_to(REPO).as_posix()] = hits
    for path in _files("*.py", "scripts", "src"):
        hits = _typed(_python_string_lines(path))
        if hits:
            found[path.relative_to(REPO).as_posix()] = hits
    for path in _files("*.ps1", "scripts"):
        hits = _typed(path.read_text(encoding="utf-8").splitlines())
        if hits:
            found[path.relative_to(REPO).as_posix()] = hits
    return found


def test_no_runbook_or_script_has_you_type_a_secret() -> None:
    found = _where_typed()
    assert not found, (
        "these tell the founder to type a secret on a line, which PowerShell 5.1 "
        "keeps in its history file. Ask for it instead: "
        '$s = Read-Host "<what it is>" -AsSecureString, then '
        '$env:NAME = [Net.NetworkCredential]::new("", $s).Password. '
        f"{found}"
    )


@pytest.mark.parametrize(
    ("line", "typed"),
    [
        ('$env:RENTVINE_API_SECRET   = "<secret>"', True),
        ("$env:HELIXIS_DATA_KEY = '<the data key>'   # never rotate it", True),
        ('## $env:HELIXIS_RENTVINE_CHECK_TOKEN = "<a JWT>"', True),
        ('  $env:HELIXIS_SUPABASE__DB_URL = "<the helixis-test pooler URL>"', True),
        ('$env:RENTVINE_API_SECRET = [Net.NetworkCredential]::new("", $s).Password', False),
        ('$s = Read-Host "Rentvine API secret" -AsSecureString', False),
        ('$env:RENTVINE_ACCOUNT_CODE = "<subdomain>"', False),
        ('$env:HELIXIS_RENTVINE__ENABLED = "true"', False),
    ],
)
def test_the_check_catches_a_typed_secret_and_spares_a_prompt(line: str, typed: bool) -> None:
    assert bool(_typed([line])) is typed


def test_a_fence_is_what_makes_a_line_a_command() -> None:
    """Before the fence, inside it, and after it closes: only the middle counts."""
    text = (
        '$env:A_SECRET = "prose before"\n'
        '```powershell\n$env:X_SECRET = "s"\n```\n'
        '$env:B_SECRET = "prose after"\n'
    )
    assert _typed(_fenced_lines(text)) == ['$env:X_SECRET = "s"']


def test_the_scan_reads_the_places_it_should() -> None:
    """Rule 10: a scan that finds nothing must prove it looked. The prompts it
    accepts have to be visible to it in each kind of file."""
    rentvine = _fenced_lines((REPO / "docs/runbooks/rentvine.md").read_text(encoding="utf-8"))
    assert any("Read-Host" in line and "-AsSecureString" in line for line in rentvine)
    probe = _python_string_lines(REPO / "scripts/rentvine_probe.py")
    assert any("Read-Host" in line for line in probe)
    replay = (REPO / "scripts/replay_webhooks.ps1").read_text(encoding="utf-8").splitlines()
    assert any("-AsSecureString" in line for line in replay)
