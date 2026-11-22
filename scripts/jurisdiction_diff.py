#!/usr/bin/env python3
"""Jurisdiction rules changelog — what changed between two rules snapshots.

Changelog discipline for ``src/helixis/geo/data/jurisdiction_rules.json``:
every edit to the rules file should be reviewable as a per-jurisdiction
"path: old -> new" listing, not a raw JSON diff. Pure core
(:func:`diff_rules`) + a thin git-backed CLI.

Usage:
    python scripts/jurisdiction_diff.py --old <git-ref> [--new <path>]

``--old`` resolves the rules file at ``<git-ref>`` via ``git show``;
``--new`` defaults to the working-tree file. Informational only — always
exits 0 (a failed git resolve prints a helpful error instead of a diff).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RULES_REL_PATH = "src/helixis/geo/data/jurisdiction_rules.json"

# Marks a key present on only one side (rendered as "added"/"removed").
ABSENT = "<absent>"


@dataclass
class Change:
    jurisdiction: str
    path: str
    old: Any
    new: Any


def _diff_node(jurisdiction: str, path: str, old: Any, new: Any, out: list[Change]) -> None:
    """Recursive walk: dicts descend key-by-key (union of keys, sorted so
    output is deterministic); everything else — scalars AND lists — compares
    atomically as one change."""
    if isinstance(old, dict) and isinstance(new, dict):
        for key in sorted(set(old) | set(new)):
            child = f"{path}.{key}" if path else str(key)
            if key not in old:
                out.append(Change(jurisdiction, child, ABSENT, new[key]))
            elif key not in new:
                out.append(Change(jurisdiction, child, old[key], ABSENT))
            else:
                _diff_node(jurisdiction, child, old[key], new[key], out)
    elif old != new:
        out.append(Change(jurisdiction, path or "(root)", old, new))


def diff_rules(old: dict, new: dict) -> list[Change]:
    """Every change between two rules snapshots, grouped-ready by
    jurisdiction: ``federal``, each state (``states.<abbr>`` → abbr), each
    overlay block (``overlay:<abbr>``), plus the top-level version. Nested
    structures (deposit / notice / rates / pending_change / ...) are walked
    recursively."""
    out: list[Change] = []
    if old.get("version") != new.get("version"):
        out.append(Change("meta", "version", old.get("version"), new.get("version")))
    _diff_node("federal", "", old.get("federal") or {}, new.get("federal") or {}, out)
    old_states = old.get("states") or {}
    new_states = new.get("states") or {}
    for abbr in sorted(set(old_states) | set(new_states)):
        _diff_node(abbr, "", old_states.get(abbr, ABSENT), new_states.get(abbr, ABSENT), out)
    old_overlay = old.get("overlay_jurisdictions") or {}
    new_overlay = new.get("overlay_jurisdictions") or {}
    for abbr in sorted(set(old_overlay) | set(new_overlay)):
        _diff_node(
            f"overlay:{abbr}",
            "",
            old_overlay.get(abbr, ABSENT),
            new_overlay.get(abbr, ABSENT),
            out,
        )
    return out


def _render(changes: list[Change]) -> str:
    if not changes:
        return "No changes."
    lines: list[str] = []
    by_jurisdiction: dict[str, list[Change]] = {}
    for change in changes:
        by_jurisdiction.setdefault(change.jurisdiction, []).append(change)
    for jurisdiction in sorted(by_jurisdiction):
        lines.append(f"{jurisdiction}:")
        for change in by_jurisdiction[jurisdiction]:
            lines.append(f"  {change.path}: {change.old!r} -> {change.new!r}")
    lines.append(f"{len(changes)} change{'s' if len(changes) != 1 else ''}.")
    return "\n".join(lines)


def _load_from_git(ref: str) -> dict | None:
    """The rules file at ``ref`` via ``git show``, or None (with a helpful
    stderr message) when the ref/file can't be resolved."""
    try:
        result = subprocess.run(
            ["git", "show", f"{ref}:{RULES_REL_PATH}"],
            capture_output=True,
            text=True,
            cwd=ROOT,
            check=False,
        )
    except OSError as exc:
        print(f"error: could not run git ({exc})", file=sys.stderr)
        return None
    if result.returncode != 0:
        print(
            f"error: git could not resolve '{ref}:{RULES_REL_PATH}' — check that "
            "the ref exists and that the rules file exists at that ref "
            f"(git said: {result.stderr.strip() or 'nothing'})",
            file=sys.stderr,
        )
        return None
    try:
        data = json.loads(result.stdout)
    except ValueError as exc:
        print(f"error: rules file at '{ref}' is not valid JSON ({exc})", file=sys.stderr)
        return None
    return data if isinstance(data, dict) else None


def main() -> int:
    ap = argparse.ArgumentParser(description="Diff two jurisdiction-rules snapshots.")
    ap.add_argument("--old", required=True, help="git ref for the OLD snapshot (e.g. main, HEAD~1)")
    ap.add_argument(
        "--new",
        default=os.path.join(ROOT, RULES_REL_PATH),
        help="path to the NEW snapshot (default: the working-tree rules file)",
    )
    args = ap.parse_args()

    old = _load_from_git(args.old)
    if old is None:
        return 0  # informational tool — never gates
    try:
        with open(args.new, encoding="utf-8") as fh:
            new = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"error: could not read new snapshot {args.new!r} ({exc})", file=sys.stderr)
        return 0
    if not isinstance(new, dict):
        print(f"error: new snapshot {args.new!r} is not a JSON object", file=sys.stderr)
        return 0

    print(_render(diff_rules(old, new)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
