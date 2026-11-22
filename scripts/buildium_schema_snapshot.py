"""Buildium tool JSON-schema contract snapshot (release-gate Tier-3 #13).

Every Buildium tool the agent can call exposes a JSON schema (name +
description + `params_json_schema`) — the contract the LLM sees and, downstream,
the shape that must line up with the real Buildium API. A silent drift there (a
renamed param, a dropped field, a changed type, a required/optional flip) can
break tool-calling or send a malformed request to Buildium with NO test failure
anywhere else.

This module snapshots that contract to ``tests/snapshots/buildium_tool_schemas.json``
and diffs the live tools against it. It is FULLY DETERMINISTIC — no model, no
network, no env — so it gates hard in CI (``tests/unit/test_buildium_schema_contract.py``)
with zero flake risk.

    python scripts/buildium_schema_snapshot.py            # diff live vs snapshot; exit 1 on drift
    python scripts/buildium_schema_snapshot.py --update   # regenerate after an intended change

When the test fails it prints exactly what changed (tool added/removed, param
added/removed, type/required/default/description changed) and the one-line update
command. An intended contract change is a deliberate ``--update`` + review of the
diff in the PR — never an accident.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_PATH = _ROOT / "tests" / "snapshots" / "buildium_tool_schemas.json"


def collect_tool_schemas() -> dict[str, dict[str, Any]]:
    """Return ``{tool_name: {description, params_json_schema}}`` for every
    registered Buildium tool. Deterministic: pure import-time data, sorted."""
    from helixis.agents.buildium_tools import buildium_tools

    schemas: dict[str, dict[str, Any]] = {}
    for tool in buildium_tools():
        schemas[tool.name] = {
            "description": tool.description or "",
            "params_json_schema": tool.params_json_schema or {},
        }
    return dict(sorted(schemas.items()))


def _params(entry: dict[str, Any]) -> dict[str, Any]:
    return entry.get("params_json_schema", {}) or {}


def _properties(entry: dict[str, Any]) -> dict[str, Any]:
    return _params(entry).get("properties", {}) or {}


def _required(entry: dict[str, Any]) -> set[str]:
    return set(_params(entry).get("required", []) or [])


def _param_type(spec: dict[str, Any]) -> str:
    """A stable string for a param's type (handles anyOf/enum/$ref shapes)."""
    if "type" in spec:
        return str(spec["type"])
    if "anyOf" in spec:
        return "anyOf[" + ",".join(_param_type(s) for s in spec["anyOf"]) + "]"
    if "$ref" in spec:
        return f"$ref:{spec['$ref']}"
    if "enum" in spec:
        return "enum"
    return "unknown"


def diff_schemas(stored: dict[str, dict], current: dict[str, dict]) -> list[str]:
    """Itemize every contract change between two snapshots. Pure — the whole
    point is a readable, testable list of what drifted. Empty list == in sync."""
    changes: list[str] = []
    stored_names = set(stored)
    current_names = set(current)

    for name in sorted(current_names - stored_names):
        changes.append(f"tool ADDED: {name}")
    for name in sorted(stored_names - current_names):
        changes.append(f"tool REMOVED: {name}")

    for name in sorted(stored_names & current_names):
        s, c = stored[name], current[name]

        if s.get("description", "") != c.get("description", ""):
            changes.append(f"{name}: description changed")

        s_props, c_props = _properties(s), _properties(c)
        for p in sorted(set(c_props) - set(s_props)):
            changes.append(f"{name}: param ADDED '{p}' ({_param_type(c_props[p])})")
        for p in sorted(set(s_props) - set(c_props)):
            changes.append(f"{name}: param REMOVED '{p}'")
        for p in sorted(set(s_props) & set(c_props)):
            st, ct = _param_type(s_props[p]), _param_type(c_props[p])
            if st != ct:
                changes.append(f"{name}: param '{p}' type changed {st} -> {ct}")
            sd, cd = s_props[p].get("default", ...), c_props[p].get("default", ...)
            if sd != cd:
                changes.append(f"{name}: param '{p}' default changed {sd!r} -> {cd!r}")

        s_req, c_req = _required(s), _required(c)
        for p in sorted(c_req - s_req):
            changes.append(f"{name}: param '{p}' now REQUIRED")
        for p in sorted(s_req - c_req):
            changes.append(f"{name}: param '{p}' no longer required")

        s_add = _params(s).get("additionalProperties")
        c_add = _params(c).get("additionalProperties")
        if s_add != c_add:
            changes.append(f"{name}: additionalProperties changed {s_add!r} -> {c_add!r}")

    return changes


def load_snapshot() -> dict[str, dict]:
    if not SNAPSHOT_PATH.exists():
        return {}
    data: dict[str, dict] = json.loads(SNAPSHOT_PATH.read_text())
    return data


def write_snapshot(schemas: dict[str, dict]) -> None:
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_PATH.write_text(json.dumps(schemas, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="Buildium tool JSON-schema contract snapshot")
    ap.add_argument(
        "--update", action="store_true", help="regenerate the snapshot (after an intended change)"
    )
    args = ap.parse_args()

    current = collect_tool_schemas()
    if args.update:
        write_snapshot(current)
        print(f"Snapshot updated → {SNAPSHOT_PATH.relative_to(_ROOT)} ({len(current)} tools)")
        return 0

    stored = load_snapshot()
    if not stored:
        rel = SNAPSHOT_PATH.relative_to(_ROOT)
        print(f"No snapshot at {rel} — run with --update to create it.")
        return 1

    changes = diff_schemas(stored, current)
    if not changes:
        print(f"Buildium tool schema contract in sync ({len(current)} tools).")
        return 0

    print(f"Buildium tool schema DRIFT — {len(changes)} change(s):")
    for c in changes:
        print(f"  - {c}")
    print("\nIf this change is intended, review the diff and run:")
    print("  python scripts/buildium_schema_snapshot.py --update")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
