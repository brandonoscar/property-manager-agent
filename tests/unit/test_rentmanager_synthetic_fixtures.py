"""Rent Manager test data is synthetic, and says so (terms rule 16).

Rent Manager data, or anything recorded from an account, may not be used to
develop or evaluate a model, and nobody has read the vendor's own terms yet,
so the strictest reading holds. Every file under a ``rentmanager`` fixture
directory carries ``"_synthetic": true`` (the marker Rentvine's fixtures
use), and its ids sit in the fake range the tests use.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
ROOTS = (REPO / "tests" / "fixtures", REPO / "src" / "helixis" / "evals")

#: A Rent Manager host: a fixture naming one points at a real account.
_LIVE_HOST = re.compile(r"\b[a-z0-9-]+\.api\.rentmanager\.com\b", re.IGNORECASE)


def _fixture_files(root: Path) -> list[Path]:
    return sorted(p for d in root.rglob("rentmanager") if d.is_dir() for p in d.rglob("*.json"))


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


def _ids(value: Any, key_re: re.Pattern[str]) -> list[int]:
    found: list[int] = []
    if isinstance(value, dict):
        for k, v in value.items():
            if key_re.fullmatch(k) and isinstance(v, int) and not isinstance(v, bool):
                found.append(v)
            found += _ids(v, key_re)
    elif isinstance(value, list):
        for v in value:
            found += _ids(v, key_re)
    return found


def test_every_rentmanager_fixture_is_marked_synthetic() -> None:
    files = [p for root in ROOTS for p in _fixture_files(root)]
    assert files, "no Rent Manager fixture found: the scan is pointed at nothing"
    assert _unmarked(files) == []


def test_the_marker_check_can_fail(tmp_path: Path) -> None:
    folder = tmp_path / "rentmanager"
    folder.mkdir()
    (folder / "ok.json").write_text(json.dumps({"_synthetic": True}))
    (folder / "captured.json").write_text(json.dumps({"tenants": [{"TenantID": 1}]}))
    (folder / "listy.json").write_text(json.dumps([{"_synthetic": True}]))
    assert _unmarked(_fixture_files(tmp_path)) == ["captured.json", "listy.json"]


def test_record_ids_sit_in_the_fake_range_and_no_host_is_named() -> None:
    """Large record ids (TenantID, LeaseID, ...) are 880 000 xxx: a real one
    copied in would stand out. Small lookup ids (a status, a location) are
    exempt; they are 1, 2, 3 in every account."""
    key_re = re.compile(
        r"(Tenant|Lease|Unit|Property|Owner|Vendor|ServiceManagerIssue|GLAccount)ID|ID"
    )
    for path in _fixture_files(REPO / "tests" / "fixtures"):
        text = path.read_text(encoding="utf-8")
        assert not _LIVE_HOST.search(text), f"{path.name} names a Rent Manager host"
        ids = _ids(json.loads(text), key_re)
        assert ids, f"{path.name}: no ids found; the check proves nothing"
        stray = sorted({i for i in ids if not 880_000_000 <= i < 881_000_000})
        assert stray == [], f"{path.name}: ids outside the fake range: {stray}"
