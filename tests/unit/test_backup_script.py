"""Unit tests for the backup/restore script's pure core (no DB, no subprocess).

Loaded via ``importlib.util`` (mirrors ``test_rls_guard.py``) so it works under
plain ``pytest tests/`` without ``scripts`` being a package. The live
pg_dump/pg_restore invocation can't run here (no DB egress); these lock the
command construction + safety gating so a regression is caught offline.
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path


def _load_module():
    repo_root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "_helixis_backup", repo_root / "scripts" / "backup.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_mod = _load_module()


# ── filename ─────────────────────────────────────────────────────────


def test_backup_filename_is_utc_sortable() -> None:
    name = _mod.backup_filename(datetime(2026, 7, 12, 14, 3, 55, tzinfo=UTC))
    assert name == "helixis-20260712T140355Z.dump"


def test_backup_filename_normalizes_to_utc() -> None:
    # A non-UTC aware datetime is converted, not taken literally.
    from datetime import timedelta, timezone

    est = timezone(timedelta(hours=-5))
    name = _mod.backup_filename(datetime(2026, 7, 12, 9, 3, 55, tzinfo=est))
    assert name == "helixis-20260712T140355Z.dump"  # 09:03 EST == 14:03 UTC


# ── dsn redaction ────────────────────────────────────────────────────


def test_redact_dsn_masks_password() -> None:
    assert (
        _mod.redact_dsn("postgres://user:s3cr3t@db.host:5432/postgres")
        == "postgres://user:***@db.host:5432/postgres"
    )


def test_redact_dsn_leaves_credential_free_url() -> None:
    assert _mod.redact_dsn("postgres://db.host:5432/postgres") == "postgres://db.host:5432/postgres"
    assert _mod.redact_dsn("not-a-url") == "not-a-url"


# ── command construction ─────────────────────────────────────────────


def test_pg_dump_cmd_is_portable_custom_format() -> None:
    cmd = _mod.build_pg_dump_cmd("postgres://u:p@h/db", "/tmp/out.dump")
    assert cmd[0] == "pg_dump"
    assert "--format=custom" in cmd
    # --no-owner/--no-privileges make it restorable into a fresh project.
    assert "--no-owner" in cmd and "--no-privileges" in cmd
    assert cmd[-2:] == ["--file", "/tmp/out.dump"]
    assert "postgres://u:p@h/db" in cmd  # the real DSN is passed to libpq


def test_verify_cmd_touches_no_database() -> None:
    cmd = _mod.build_verify_cmd("/tmp/out.dump")
    # --list reads the archive TOC only; no --dbname, so it can't hit a DB.
    assert cmd == ["pg_restore", "--list", "/tmp/out.dump"]
    assert "--dbname" not in cmd


def test_restore_cmd_is_clean_replace() -> None:
    cmd = _mod.build_pg_restore_cmd("postgres://u:p@h/db", "/tmp/in.dump")
    assert cmd[0] == "pg_restore"
    assert "--clean" in cmd and "--if-exists" in cmd
    assert "--no-owner" in cmd and "--no-privileges" in cmd


# ── safety gating ────────────────────────────────────────────────────


def test_restore_refuses_without_explicit_confirmation(capsys) -> None:
    rc = _mod._do_restore("/tmp/whatever.dump", confirmed=False)
    assert rc == 2  # refused
    err = capsys.readouterr().err
    assert "DESTRUCTIVE" in err and "--yes-really-restore" in err


def test_verify_missing_file_returns_error(capsys) -> None:
    rc = _mod._do_verify("/tmp/does-not-exist-xyz.dump")
    assert rc == 2
    assert "No such dump" in capsys.readouterr().err
