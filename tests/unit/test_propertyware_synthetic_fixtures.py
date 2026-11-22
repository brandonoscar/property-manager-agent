"""Propertyware fixtures are synthetic, and say so (terms rule 17).

Never Propertyware data, recorded responses or anything derived from a live
account: hand-built JSON shaped from the official spec's models. Each file
carries ``"_synthetic": true`` (the marker Rentvine's fixtures use), every id
sits in an obviously fake range, every email is an example.com address, and
every phone is a 555-01xx number. Each check is shown able to fail.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from tests.unit._propertyware_fake import FIXTURE_FOR_PATH, FIXTURES

#: The fake id range. 0 is allowed: Propertyware's "no parent" is 0 or null.
ID_RANGE = range(990_000_000, 991_000_000)
_EMAIL = re.compile(r"^[a-z0-9._-]+@([a-z0-9-]+\.)*example\.com$")
_PHONE = re.compile(r"^555-01\d\d$")


def _is_id_key(key: str) -> bool:
    return key == "id" or key.endswith(("ID", "Id"))


def _problems(value: Any, where: str = "") -> list[str]:
    out: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            here = f"{where}.{key}"
            if _is_id_key(key) and isinstance(item, int) and not isinstance(item, bool):
                if item != 0 and item not in ID_RANGE:
                    out.append(f"{here}: id {item} outside the fake range")
            if "email" in key.lower() and isinstance(item, str) and item:
                if not _EMAIL.match(item):
                    out.append(f"{here}: {item!r} is not an example.com address")
            if "phone" in key.lower() and isinstance(item, str) and item:
                if not _PHONE.match(item):
                    out.append(f"{here}: {item!r} is not a 555-01xx number")
            out += _problems(item, here)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            out += _problems(item, f"{where}[{index}]")
    return out


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


def _fixture_files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.json"))


def test_every_propertyware_fixture_is_marked_synthetic() -> None:
    files = _fixture_files(FIXTURES)
    assert files, "no fixtures found: the scan is pointed at nothing"
    assert _unmarked(files) == []


def test_every_fixture_file_is_one_the_tests_serve() -> None:
    assert {p.name for p in _fixture_files(FIXTURES)} == set(FIXTURE_FOR_PATH.values())


def test_ids_emails_and_phones_are_all_invented() -> None:
    problems = []
    for path in _fixture_files(FIXTURES):
        data = json.loads(path.read_text(encoding="utf-8"))
        problems += [f"{path.name}{p}" for p in _problems(data["rows"])]
    assert problems == []


def test_the_marker_check_can_fail(tmp_path: Path) -> None:
    (tmp_path / "ok.json").write_text(json.dumps({"_synthetic": True, "rows": []}))
    (tmp_path / "captured.json").write_text(json.dumps({"rows": [{"id": 1}]}))
    (tmp_path / "listy.json").write_text(json.dumps([{"_synthetic": True}]))
    (tmp_path / "broken.json").write_text("{")
    assert _unmarked(_fixture_files(tmp_path)) == [
        "broken.json: not JSON",
        "captured.json",
        "listy.json",
    ]


def test_the_identity_check_can_fail() -> None:
    leaked = [
        {
            "id": 4471,
            "buildingID": 990100001,
            "parentTxID": 0,
            "email": "jane@realcorp.test",
            "mobilePhone": "212-555-0187",
            "contacts": [{"id": 990400001, "email": "ok@example.com", "homePhone": "555-0101"}],
        }
    ]
    assert _problems(leaked) == [
        "[0].id: id 4471 outside the fake range",
        "[0].email: 'jane@realcorp.test' is not an example.com address",
        "[0].mobilePhone: '212-555-0187' is not a 555-01xx number",
    ]
