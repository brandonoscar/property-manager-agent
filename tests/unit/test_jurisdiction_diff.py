"""Tests for scripts/jurisdiction_diff.py — the pure diff core only (the CLI
is thin git + argparse glue and stays untested per the script conventions)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "jurisdiction_diff.py"
_spec = importlib.util.spec_from_file_location("jurisdiction_diff", _SCRIPT)
assert _spec and _spec.loader
jurisdiction_diff = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("jurisdiction_diff", jurisdiction_diff)
_spec.loader.exec_module(jurisdiction_diff)

diff_rules = jurisdiction_diff.diff_rules
ABSENT = jurisdiction_diff.ABSENT


def _base() -> dict:
    return {
        "version": "2026-08",
        "federal": {"cares": {"value": 30, "citation": "§4024"}},
        "states": {
            "CA": {
                "deposit": {"cap_months": 1, "return_deadline_days": 21},
                "rates": {"ab1482": {"value": None, "formula": "5% + CPI"}},
            },
            "TX": {"deposit": {"cap_months": None}},
        },
        "overlay_jurisdictions": {"CA": {"places": [{"name": "SF"}]}},
    }


def test_identical_snapshots_diff_empty() -> None:
    assert diff_rules(_base(), _base()) == []


def test_nested_scalar_change_reports_dotted_path_by_jurisdiction() -> None:
    new = _base()
    new["states"]["CA"]["deposit"]["cap_months"] = 2
    changes = diff_rules(_base(), new)
    assert len(changes) == 1
    change = changes[0]
    assert change.jurisdiction == "CA"
    assert change.path == "deposit.cap_months"
    assert change.old == 1 and change.new == 2


def test_rates_value_load_is_a_change() -> None:
    new = _base()
    new["states"]["CA"]["rates"]["ab1482"]["value"] = 8.8
    changes = diff_rules(_base(), new)
    assert [(c.jurisdiction, c.path, c.old, c.new) for c in changes] == [
        ("CA", "rates.ab1482.value", None, 8.8)
    ]


def test_added_and_removed_keys_use_absent_marker() -> None:
    new = _base()
    new["states"]["CA"]["deposit"]["pending_change"] = {"field": "cap_months"}
    del new["states"]["TX"]
    changes = diff_rules(_base(), new)
    by_path = {(c.jurisdiction, c.path): c for c in changes}
    added = by_path[("CA", "deposit.pending_change")]
    assert added.old == ABSENT
    removed = by_path[("TX", "(root)")]
    assert removed.new == ABSENT


def test_federal_overlay_and_version_sections_are_walked() -> None:
    new = _base()
    new["version"] = "2026-09"
    new["federal"]["cares"]["value"] = 60
    new["overlay_jurisdictions"]["CA"]["places"] = [{"name": "SF"}, {"name": "Oakland"}]
    changes = diff_rules(_base(), new)
    keys = {(c.jurisdiction, c.path) for c in changes}
    assert ("meta", "version") in keys
    assert ("federal", "cares.value") in keys
    # Lists compare atomically — one change for the whole places list.
    assert ("overlay:CA", "places") in keys
    assert len(changes) == 3


def test_output_is_deterministic_across_key_order() -> None:
    old = _base()
    shuffled = {
        "version": "2026-08",
        "overlay_jurisdictions": {"CA": {"places": [{"name": "SF"}]}},
        "states": {
            "TX": {"deposit": {"cap_months": None}},
            "CA": {
                "rates": {"ab1482": {"formula": "5% + CPI", "value": None}},
                "deposit": {"return_deadline_days": 21, "cap_months": 1},
            },
        },
        "federal": {"cares": {"citation": "§4024", "value": 30}},
    }
    assert diff_rules(old, shuffled) == []
