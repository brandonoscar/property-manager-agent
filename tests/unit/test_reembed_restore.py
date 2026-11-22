"""reembed_memories --restore-from: the disaster-recovery row loader.

Run #4 taught the lesson: a partially-failed re-embed leaves the tables
dropped, and the ONLY row source left is a prior run's backup artifact. The
loader must round-trip exactly what the script's backup writer produces —
including payloads that json.dumps(default=str) stringified.

Loaded via ``importlib.util`` (mirrors ``test_backup_script.py``) so it works
without installing scripts/ as a package.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _load_module():  # noqa: ANN202
    spec = importlib.util.spec_from_file_location(
        "reembed_memories",
        Path(__file__).resolve().parents[2] / "scripts" / "reembed_memories.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_load_rows_round_trips_backup_shape(tmp_path: Path) -> None:
    mod = _load_module()
    backup = {
        "helixis_memories": [
            {"id": "11111111-1111-1111-1111-111111111111", "payload": {"data": "fact one"}},
            # psycopg jsonb → json.dumps(default=str) can stringify payloads.
            {"id": "22222222-2222-2222-2222-222222222222", "payload": '{"data": "fact two"}'},
        ],
        "helixis_memories_entities": [],
    }
    path = tmp_path / "backup.json"
    path.write_text(json.dumps(backup))

    rows = mod.load_rows_from_backup(path)
    assert [r["payload"]["data"] for r in rows["helixis_memories"]] == ["fact one", "fact two"]
    # Every known table present even when missing/empty in the file.
    assert rows["helixis_memories_entities"] == []
    # Stringified payloads are dicts after loading.
    assert all(isinstance(r["payload"], dict) for r in rows["helixis_memories"])


def test_load_rows_ignores_unknown_tables(tmp_path: Path) -> None:
    mod = _load_module()
    path = tmp_path / "backup.json"
    path.write_text(json.dumps({"surprise_table": [{"id": "x", "payload": {}}]}))

    rows = mod.load_rows_from_backup(path)
    assert set(rows) == set(mod.TABLES)
    assert all(v == [] for v in rows.values())
