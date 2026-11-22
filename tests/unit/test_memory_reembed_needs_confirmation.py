"""The memory re-embed workflow cannot write to production on its defaults.

``scripts/reembed_memories.py`` DROPs and rebuilds both memory tables. The
workflow defaulted to ``dry_run: false`` with no gate, so one click on
"Run workflow" did that to the live project, and a run that died after the
DROP once left the tables gone (351f026d, 2026-07-22; audit 2026-09-29). A
GitHub environment reviewer would need GitHub Pro on this private repo, so the
gate is a typed confirmation the run refuses without.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "memory-reembed.yml"


def _load() -> dict:
    data = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    # PyYAML reads the bare key ``on`` as the boolean True.
    data["on"] = data.pop(True, data.get("on"))
    return data


def test_the_default_is_a_dry_run() -> None:
    inputs = _load()["on"]["workflow_dispatch"]["inputs"]
    assert inputs["dry_run"]["default"] is True


def test_a_live_run_without_the_typed_confirmation_stops_before_install() -> None:
    steps = _load()["jobs"]["reembed"]["steps"]
    names = [s.get("name", "") for s in steps]
    gate = next(s for s in steps if "typed confirmation" in s.get("name", ""))
    assert gate["if"].replace(" ", "") == "${{!inputs.dry_run&&inputs.confirm_write!='REEMBED'}}"
    assert "exit 1" in gate["run"]
    # It must run before anything that touches the database.
    assert names.index(gate["name"]) < names.index("Re-embed")
