"""Merge-gate test 3 of 3 (``design/rentvine-terms-rules.md``, "Before you
merge anything"): **evals use synthetic fixtures only.**

Terms rules 17-19. Never use Rentvine data, API responses, or output derived
from them to train, fine-tune, VALIDATE or develop a model, and until Rentvine
says otherwise our evals count as validating. So: no customer data, no exports
from the sandbox, no recorded responses as eval inputs. Hand-built fixtures
shaped from the documented schema only.

Three checks:

1. **The live captures cannot be committed.** Every Rentvine script writes what
   it read from a real account (probe reports, the Phase 8 report, the seed
   manifests, a captured webhook) under ``docs/rentvine/``. Each default output
   path must be git-ignored, and none may be tracked.
2. **No eval input carries a live identifier.** Golden cases, eval fixtures and
   eval fixture code are scanned for the sandbox account code, the seed
   script's record prefix, a captured account URL, and, when the captures
   exist on this machine (the founder's), every name, email and address in
   them.
3. **A Rentvine fixture says it is synthetic.** Any file under a ``rentvine``
   fixture directory carries ``"_synthetic": true``, the marker
   ``design/rentvine-terms-compliance.md`` §3.7 specifies.
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
EVALS = REPO / "src" / "helixis" / "evals"
CAPTURE_DIR = REPO / "docs" / "rentvine"

#: Output files that hold what a live account returned. The derived webhook
#: SCHEME (`webhook-scheme.json`) is excluded on purpose: it is the algorithm,
#: never a payload (`rentvine_webhook_capture.py` refuses to write the capture
#: there).
_NOT_A_CAPTURE = {"docs/rentvine/webhook-scheme.json"}

#: Always-live identifiers: the sandbox account, the seed script's tag on every
#: record it creates in that account, and an account's API host.
_LIVE_PATTERNS = [
    re.compile(r"demoaccount41", re.I),
    re.compile(r"OCC-SEED"),
    re.compile(r"\b[a-z0-9-]+\.rentvine\.com\b", re.I),
]

#: Keys whose values in a capture are a person's or a place's identity.
_IDENTITY_KEYS = {
    "name",
    "firstName",
    "lastName",
    "email",
    "phone",
    "address",
    "address2",
    "payeeName",
    "description",
    "unitName",
    "propertyName",
}


# ── 1. the captures cannot be committed ────────────────────────────────────


def _default_capture_paths() -> list[str]:
    """Every ``docs/rentvine/*.json`` string constant in the Rentvine scripts,
    read from the AST (a comment quoting a path is not a default)."""
    paths: set[str] = set()
    for script in sorted((REPO / "scripts").glob("rentvine_*.py")):
        for node in ast.walk(ast.parse(script.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and re.fullmatch(r"docs/rentvine/[\w.-]+\.json", node.value)
            ):
                paths.add(node.value)
    return sorted(paths - _NOT_A_CAPTURE)


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, check=False)


needs_git = pytest.mark.skipif(
    shutil.which("git") is None or not (REPO / ".git").exists(), reason="not a git checkout"
)


def test_the_scripts_still_write_their_captures_where_this_test_looks() -> None:
    paths = _default_capture_paths()
    assert {
        "docs/rentvine/probe-report.json",
        "docs/rentvine/phase8-report.json",
        "docs/rentvine/seed-manifest.json",
        "docs/rentvine/webhook-capture.json",
    } <= set(paths), paths


@needs_git
def test_every_live_capture_path_is_git_ignored() -> None:
    paths = _default_capture_paths()
    # A finished seed teardown renames its manifest; that copy is live data too.
    paths += ["docs/rentvine/seed-manifest.torn-down-20260924T000000Z.json"]
    result = _git("check-ignore", "--no-index", *paths)
    ignored = set(result.stdout.split())
    assert [p for p in paths if p not in ignored] == []


@needs_git
def test_no_live_capture_is_tracked() -> None:
    tracked = _git("ls-files", "docs/rentvine").stdout.split()
    captures = set(_default_capture_paths())
    offenders = [
        p for p in tracked if p in captures or re.search(r"seed(-smoke)?-manifest.*\.json$", p)
    ]
    assert offenders == []


# ── 2. no eval input carries a live identifier ──────────────────────────────


def _eval_inputs() -> list[Path]:
    """Everything an eval reads as input: golden cases, fixture files, and the
    fixture-building modules. Tests under ``tests/`` test code, not models, and
    are out of scope; ``tests/fixtures`` is in, because evals may load it."""
    files = [p for p in EVALS.rglob("*") if p.is_file() and p.suffix in {".json", ".yaml", ".yml"}]
    files += sorted((EVALS / "fixtures").rglob("*.py"))
    files += [EVALS / "connector_stub_tools.py", EVALS / "live" / "fixtures.py"]
    files += [p for p in (REPO / "tests" / "fixtures").rglob("*") if p.is_file()]
    return sorted({p for p in files if p.exists()})


def _data_strings(path: Path) -> list[str]:
    """The strings a file hands an eval. For Python, string constants that are
    NOT docstrings (a docstring explaining the rule may quote it, rule 17)."""
    if path.suffix != ".py":
        return [path.read_text(encoding="utf-8", errors="replace")]
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    return [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings
    ]


def _identities(value: Any) -> set[str]:
    """Names, emails and addresses in a capture, walked to any depth."""
    found: set[str] = set()
    if isinstance(value, dict):
        for k, v in value.items():
            if k in _IDENTITY_KEYS and isinstance(v, str) and len(v.strip()) >= 4:
                found.add(v.strip())
            found |= _identities(v)
    elif isinstance(value, list):
        for v in value:
            found |= _identities(v)
    return found


def _local_capture_identities() -> set[str]:
    """Identities in the captures on THIS machine. Empty in CI (they are
    ignored, so never checked out); the founder's runs are where they exist."""
    found: set[str] = set()
    for path in sorted(CAPTURE_DIR.glob("*.json")):
        rel = path.relative_to(REPO).as_posix()
        if rel in _NOT_A_CAPTURE:
            continue
        try:
            found |= _identities(json.loads(path.read_text(encoding="utf-8")))
        except (ValueError, OSError):
            continue
    return found


def _offenders(files: list[Path], identities: set[str]) -> list[str]:
    out = []
    for path in files:
        for text in _data_strings(path):
            for pattern in _LIVE_PATTERNS:
                if match := pattern.search(text):
                    out.append(f"{path.relative_to(REPO)}: {match.group(0)!r}")
            out += [
                f"{path.relative_to(REPO)}: {v!r} (from a live capture)"
                for v in identities
                if v in text
            ]
    return sorted(set(out))


def test_the_eval_inputs_are_found() -> None:
    """A scan over nothing passes everything (rule 10)."""
    files = _eval_inputs()
    assert sum(1 for p in files if "golden" in p.parts) >= 100
    assert any(p.name == "480_rentvine_write_not_reached_on_a_buildium_company.json" for p in files)


def test_no_eval_input_carries_a_live_rentvine_identifier() -> None:
    assert _offenders(_eval_inputs(), _local_capture_identities()) == []


def test_the_scan_catches_live_data_in_a_fixture(tmp_path: Path, monkeypatch) -> None:
    """The check must be able to fail, on each kind of leak it claims to catch."""
    monkeypatch.setattr(__import__(__name__, fromlist=["REPO"]), "REPO", tmp_path)
    capture = {"leases": [{"contact": {"name": "Jordan Whitfield", "email": "jw@corp.test"}}]}
    golden = tmp_path / "golden.json"
    golden.write_text(
        json.dumps(
            {
                "prompt": "Is Jordan Whitfield late on rent?",
                "fixture": {"account": "demoaccount41"},
                "note": "OCC-SEED Tenant 001 on https://acme.rentvine.com/api/manager",
            }
        ),
        encoding="utf-8",
    )
    stub = tmp_path / "stub.py"
    stub.write_text(
        '"""Mentions demoaccount41 in prose: allowed."""\nROW = {"name": "OCC-SEED Vendor 1"}\n',
        encoding="utf-8",
    )
    got = _offenders([golden, stub], _identities(capture))
    assert got == sorted(
        {
            "golden.json: 'Jordan Whitfield' (from a live capture)",
            "golden.json: 'OCC-SEED'",
            "golden.json: 'acme.rentvine.com'",
            "golden.json: 'demoaccount41'",
            "stub.py: 'OCC-SEED'",
        }
    )


# ── 3. a Rentvine fixture says it is synthetic ──────────────────────────────


def _rentvine_fixture_files(root: Path) -> list[Path]:
    return sorted(p for d in root.rglob("rentvine") if d.is_dir() for p in d.rglob("*.json"))


def _unmarked(files: list[Path]) -> list[str]:
    out = []
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            out.append(f"{path.name}: not JSON")
            continue
        if not (isinstance(data, dict) and data.get("_synthetic") is True):
            out.append(path.name)
    return out


def test_every_rentvine_fixture_file_is_marked_synthetic() -> None:
    files = _rentvine_fixture_files(REPO / "tests" / "fixtures")
    files += _rentvine_fixture_files(EVALS)
    assert _unmarked(files) == []


def test_the_synthetic_marker_check_can_fail(tmp_path: Path) -> None:
    d = tmp_path / "rentvine"
    d.mkdir()
    (d / "ok.json").write_text(json.dumps({"_synthetic": True, "rows": []}))
    (d / "captured.json").write_text(json.dumps({"rows": [{"leaseID": 1}]}))
    (d / "listy.json").write_text(json.dumps([{"_synthetic": True}]))
    assert _unmarked(_rentvine_fixture_files(tmp_path)) == ["captured.json", "listy.json"]
