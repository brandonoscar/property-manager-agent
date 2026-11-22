"""Buildium tool JSON-schema contract test (release-gate Tier-3 #13).

Two things, both model-free and deterministic:

1. The GATE — the live Buildium tools' schemas must match the stored snapshot
   (``tests/snapshots/buildium_tool_schemas.json``). Any drift (a renamed param,
   a dropped field, a type/required/default/description change, a tool
   added/removed) fails CI with the exact changes listed. An intended change is
   a deliberate ``python scripts/buildium_schema_snapshot.py --update`` reviewed
   in the PR diff.
2. The DIFF LOGIC — ``diff_schemas`` is unit-tested on synthetic before/after
   pairs so the gate itself is trustworthy (it catches what it claims to).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]

# Load the snapshot module (scripts/ isn't an importable package).
_spec = importlib.util.spec_from_file_location(
    "buildium_schema_snapshot", _ROOT / "scripts" / "buildium_schema_snapshot.py"
)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
sys.modules["buildium_schema_snapshot"] = _mod
_spec.loader.exec_module(_mod)

collect_tool_schemas = _mod.collect_tool_schemas
diff_schemas = _mod.diff_schemas
load_snapshot = _mod.load_snapshot


# ---- the gate ----------------------------------------------------------------


def test_snapshot_exists() -> None:
    stored = load_snapshot()
    assert stored, (
        "No Buildium tool schema snapshot found. Create it with:\n"
        "  python scripts/buildium_schema_snapshot.py --update"
    )


def test_live_tools_match_snapshot() -> None:
    stored = load_snapshot()
    current = collect_tool_schemas()
    changes = diff_schemas(stored, current)
    assert not changes, (
        "Buildium tool schema contract DRIFT — the live tools no longer match the "
        "stored snapshot:\n  - "
        + "\n  - ".join(changes)
        + "\n\nIf this change is intended, review it in the PR diff and regenerate:\n"
        "  python scripts/buildium_schema_snapshot.py --update"
    )


def test_snapshot_covers_every_registered_tool() -> None:
    # Guards the failure mode where a new tool is registered but never snapshotted.
    stored = load_snapshot()
    current = collect_tool_schemas()
    assert set(stored) == set(current)


# ---- the diff logic (synthetic before/after) ---------------------------------

_BASE: dict[str, dict] = {
    "t_one": {
        "description": "desc one",
        "params_json_schema": {
            "properties": {
                "a": {"type": "integer"},
                "b": {"type": "string", "default": "x"},
            },
            "required": ["a"],
            "additionalProperties": False,
        },
    },
    "t_two": {
        "description": "desc two",
        "params_json_schema": {"properties": {}, "additionalProperties": False},
    },
}


def _clone() -> dict[str, dict]:
    import copy

    return copy.deepcopy(_BASE)


def test_no_diff_when_identical() -> None:
    assert diff_schemas(_clone(), _clone()) == []


def test_detects_tool_added_and_removed() -> None:
    cur = _clone()
    cur["t_three"] = {"description": "new", "params_json_schema": {}}
    del cur["t_two"]
    changes = diff_schemas(_BASE, cur)
    assert "tool ADDED: t_three" in changes
    assert "tool REMOVED: t_two" in changes


def test_detects_param_added_and_removed() -> None:
    cur = _clone()
    cur["t_one"]["params_json_schema"]["properties"]["c"] = {"type": "boolean"}
    del cur["t_one"]["params_json_schema"]["properties"]["b"]
    changes = diff_schemas(_BASE, cur)
    assert any("param ADDED 'c' (boolean)" in c for c in changes)
    assert any("param REMOVED 'b'" in c for c in changes)


def test_detects_type_change() -> None:
    cur = _clone()
    cur["t_one"]["params_json_schema"]["properties"]["a"] = {"type": "string"}
    changes = diff_schemas(_BASE, cur)
    assert any("param 'a' type changed integer -> string" in c for c in changes)


def test_detects_required_flip() -> None:
    cur = _clone()
    cur["t_one"]["params_json_schema"]["required"] = ["a", "b"]
    changes = diff_schemas(_BASE, cur)
    assert any("param 'b' now REQUIRED" in c for c in changes)

    cur2 = _clone()
    cur2["t_one"]["params_json_schema"]["required"] = []
    changes2 = diff_schemas(_BASE, cur2)
    assert any("param 'a' no longer required" in c for c in changes2)


def test_detects_default_change() -> None:
    cur = _clone()
    cur["t_one"]["params_json_schema"]["properties"]["b"]["default"] = "y"
    changes = diff_schemas(_BASE, cur)
    assert any("param 'b' default changed 'x' -> 'y'" in c for c in changes)


def test_detects_description_change() -> None:
    cur = _clone()
    cur["t_one"]["description"] = "desc one CHANGED"
    changes = diff_schemas(_BASE, cur)
    assert "t_one: description changed" in changes


def test_detects_additional_properties_change() -> None:
    cur = _clone()
    cur["t_one"]["params_json_schema"]["additionalProperties"] = True
    changes = diff_schemas(_BASE, cur)
    assert any("additionalProperties changed" in c for c in changes)


def test_anyof_type_is_stable_and_diffable() -> None:
    # Optional params surface as anyOf[type, null] — the type string must be
    # stable (no false drift) yet still catch a real change.
    a = {
        "t": {
            "description": "",
            "params_json_schema": {
                "properties": {"x": {"anyOf": [{"type": "integer"}, {"type": "null"}]}},
            },
        }
    }
    import copy

    same = copy.deepcopy(a)
    assert diff_schemas(a, same) == []

    changed = copy.deepcopy(a)
    changed["t"]["params_json_schema"]["properties"]["x"] = {
        "anyOf": [{"type": "string"}, {"type": "null"}]
    }
    changes = diff_schemas(a, changed)
    assert any("type changed" in c for c in changes)
