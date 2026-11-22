"""The first-install order ``scripts/apply_schema.py`` prints and applies.

A new self-hosted database is built from this order, not from the folder
listing: 0002 is historical and fails on an empty database, and
``helixis_entities.sql`` lives outside the folder but must exist before 0020
turns on row-level security for its tables. Measured on 2026-11-22 against an
empty Postgres 16 with pgvector and PostGIS: this order applied all 111 files.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _load():
    spec = importlib.util.spec_from_file_location("apply_schema", REPO / "scripts/apply_schema.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["apply_schema"] = module
    spec.loader.exec_module(module)
    return module


def test_every_migration_but_the_historical_one_runs_once_in_numeric_order() -> None:
    order = _load().install_order()
    migrations = [p.name for p in order if p.parent.name == "migrations"]
    expected = sorted(
        p.name
        for p in (REPO / "supabase/migrations").glob("*.sql")
        if not p.name.startswith("0002_")
    )
    assert migrations == expected
    assert len(migrations) > 100


def test_the_entities_schema_goes_in_right_before_0020() -> None:
    order = _load().install_order()
    names = [p.name for p in order]
    i = names.index("helixis_entities.sql")
    assert names[i + 1].startswith("0020_")
    assert names.count("helixis_entities.sql") == 1


def test_0002_is_skipped_because_it_says_not_to_run_it() -> None:
    """The skip is only right while the file still says so in its header."""
    (historical,) = (REPO / "supabase/migrations").glob("0002_*.sql")
    assert "DO NOT RE-RUN" in historical.read_text(encoding="utf-8")[:400]
    assert historical not in _load().install_order()


def test_only_and_dry_run_print_the_matching_files(capsys) -> None:
    assert _load().main(["--dry-run", "--only", "0025"]) == 0
    out = capsys.readouterr().out.split()
    assert out == ["supabase/migrations/0025_rls_on_runtime_tables.sql"]


def test_a_missing_url_is_refused_before_any_connection(monkeypatch, capsys) -> None:
    monkeypatch.delenv("HELIXIS_SUPABASE__DB_URL", raising=False)
    assert _load().main([]) == 2
    assert "HELIXIS_SUPABASE__DB_URL" in capsys.readouterr().err
