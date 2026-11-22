#!/usr/bin/env python3
"""Portable off-Supabase backup + restore for the Helixis database.

The audit (2026-07-12, Q27/Q32) flagged a company-ending gap: no backup you
control, and no restore ever tested. Supabase's own managed backups / PITR
cover the platform, but they live inside the one account that four surfaces
hard-pin — if that account is lost or a bad migration corrupts data, there was
no independent copy and no rehearsed recovery.

This is that independent copy. It runs ``pg_dump`` against
``HELIXIS_SUPABASE__DB_URL`` into a dated, compressed **custom-format** dump
(restorable with ``pg_restore`` into ANY fresh Postgres), verifies the dump is
readable, and — behind an explicit destructive flag — restores it.

    python scripts/backup.py                       # dump → ./backups/helixis-<UTC>.dump
    python scripts/backup.py --out-dir /mnt/x      # dump elsewhere
    python scripts/backup.py --verify FILE         # confirm a dump is readable
    python scripts/backup.py --restore FILE --yes-really-restore   # DESTRUCTIVE

⚠ Restore is a FULL, destructive replace (``--clean --if-exists``). It is gated
behind ``--yes-really-restore`` and refuses to run without it. Read
``docs/deployment/disaster-recovery.md`` before restoring anything.

This container cannot reach Supabase (egress allowlist) — run this from a
machine or CI job that can. The pure command-building logic here is unit-tested
(``tests/unit/test_backup_script.py``) so a regression is caught without a live
DB.
"""

from __future__ import annotations

import argparse
import os
import subprocess  # noqa: S404 — invoking pg_dump/pg_restore is the whole point
import sys
from datetime import UTC, datetime

_DUMP_PREFIX = "helixis-"
_DUMP_SUFFIX = ".dump"


def backup_filename(now: datetime) -> str:
    """Dated dump filename, e.g. ``helixis-20260712T140355Z.dump``. UTC, second
    resolution, sortable — so ``ls`` orders backups chronologically."""
    return f"{_DUMP_PREFIX}{now.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}{_DUMP_SUFFIX}"


def redact_dsn(dsn: str) -> str:
    """Mask the password in a postgres URL for safe logging:
    ``postgres://user:secret@host/db`` → ``postgres://user:***@host/db``.
    Leaves a DSN without credentials untouched."""
    if "://" not in dsn:
        return dsn
    scheme, rest = dsn.split("://", 1)
    if "@" not in rest:
        return dsn
    creds, host = rest.split("@", 1)
    if ":" in creds:
        user = creds.split(":", 1)[0]
        creds = f"{user}:***"
    return f"{scheme}://{creds}@{host}"


def build_pg_dump_cmd(dsn: str, out_path: str) -> list[str]:
    """``pg_dump`` argv producing a portable custom-format archive.

    ``-Fc`` = custom format (compressed, selective restore, the pg_restore
    input format). ``--no-owner --no-privileges`` strip role/ACL statements so
    the dump restores into a fresh database (a rebuilt Supabase project) whose
    roles differ, instead of erroring on a missing ``postgres``/``supabase``
    role. The DSN is passed via ``--dbname`` (libpq parses the URL).
    """
    return [
        "pg_dump",
        "--dbname",
        dsn,
        "--format=custom",
        "--no-owner",
        "--no-privileges",
        "--file",
        out_path,
    ]


def build_verify_cmd(dump_path: str) -> list[str]:
    """``pg_restore --list`` — reads the archive's table of contents without
    touching any database. Non-zero exit = a truncated/corrupt dump."""
    return ["pg_restore", "--list", dump_path]


def build_pg_restore_cmd(dsn: str, dump_path: str) -> list[str]:
    """``pg_restore`` argv for a FULL destructive replace into ``dsn``.

    ``--clean --if-exists`` drop existing objects before recreating them;
    ``--no-owner --no-privileges`` mirror the dump flags so it lands in a
    fresh project cleanly. Callers MUST gate this behind an explicit human
    confirmation — it overwrites the target database.
    """
    return [
        "pg_restore",
        "--dbname",
        dsn,
        "--clean",
        "--if-exists",
        "--no-owner",
        "--no-privileges",
        dump_path,
    ]


def _dsn_or_exit() -> str:
    dsn = os.environ.get("HELIXIS_SUPABASE__DB_URL", "")
    if not dsn:
        print("HELIXIS_SUPABASE__DB_URL is not set — nothing to back up.", file=sys.stderr)
        raise SystemExit(2)
    return dsn


def _run(cmd: list[str]) -> int:
    """Run a pg_* command, echoing a credential-redacted form. Returns exit code."""
    printable = [redact_dsn(a) for a in cmd]
    print(f"$ {' '.join(printable)}")
    return subprocess.run(cmd, check=False).returncode  # noqa: S603 — args are ours


def _do_backup(out_dir: str, *, now: datetime) -> int:
    dsn = _dsn_or_exit()
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, backup_filename(now))
    print(f"Backing up {redact_dsn(dsn)} → {out_path}")
    rc = _run(build_pg_dump_cmd(dsn, out_path))
    if rc != 0:
        print("pg_dump FAILED — no usable backup written.", file=sys.stderr)
        return rc
    size = os.path.getsize(out_path) if os.path.exists(out_path) else 0
    if size == 0:
        print("pg_dump produced an EMPTY file — treating as failure.", file=sys.stderr)
        return 1
    # Verify the archive is readable before we call it a backup.
    if _run(build_verify_cmd(out_path)) != 0:
        print("Backup wrote but is NOT readable by pg_restore — corrupt dump.", file=sys.stderr)
        return 1
    print(f"OK — {out_path} ({size / 1_048_576:.1f} MiB), verified readable.")
    return 0


def _do_verify(dump_path: str) -> int:
    if not os.path.exists(dump_path):
        print(f"No such dump: {dump_path}", file=sys.stderr)
        return 2
    rc = _run(build_verify_cmd(dump_path))
    print("OK — dump is readable." if rc == 0 else "FAIL — dump is not readable.")
    return rc


def _do_restore(dump_path: str, *, confirmed: bool) -> int:
    if not confirmed:
        print(
            "Refusing to restore: restore is a DESTRUCTIVE full replace.\n"
            "Re-run with --yes-really-restore once you have read "
            "docs/deployment/disaster-recovery.md and confirmed the target DSN.",
            file=sys.stderr,
        )
        return 2
    if not os.path.exists(dump_path):
        print(f"No such dump: {dump_path}", file=sys.stderr)
        return 2
    dsn = _dsn_or_exit()
    print(f"RESTORING {dump_path} → {redact_dsn(dsn)} (destructive, --clean)")
    return _run(build_pg_restore_cmd(dsn, dump_path))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out-dir", default="backups", help="dump destination dir (default ./backups)"
    )
    parser.add_argument("--verify", metavar="FILE", help="check a dump is readable, then exit")
    parser.add_argument(
        "--restore", metavar="FILE", help="restore a dump (needs --yes-really-restore)"
    )
    parser.add_argument(
        "--yes-really-restore",
        action="store_true",
        help="confirm the destructive full-replace restore",
    )
    args = parser.parse_args(argv)

    if args.verify:
        return _do_verify(args.verify)
    if args.restore:
        return _do_restore(args.restore, confirmed=args.yes_really_restore)
    return _do_backup(args.out_dir, now=datetime.now(UTC))


if __name__ == "__main__":
    raise SystemExit(main())
