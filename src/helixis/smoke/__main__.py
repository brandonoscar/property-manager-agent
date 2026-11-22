"""CLI entry point — ``python -m helixis.smoke [--json]``."""

from __future__ import annotations

import argparse
import asyncio
import sys

from helixis.smoke.runner import run_smoke

# Windows consoles default to cp1252 / cp437; the box-drawing characters
# in pretty output ("─", "✓", "✗", "○") raise UnicodeEncodeError when
# print()'d to a default stdout. Forcing utf-8 on win32 makes
# ``python -m helixis.smoke`` and ``make smoke-real`` work identically
# across platforms. No-op everywhere else.
if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m helixis.smoke",
        description="Real-services smoke test for Helixis (read-only health checks).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-parseable JSON instead of pretty terminal output (for CI).",
    )
    parser.add_argument(
        "--out",
        default=None,
        metavar="PATH",
        help=(
            "Also write the rendered output directly to PATH. CI parses this "
            "file instead of redirected stdout, so import-time banners or any "
            "other stray stdout writes can never corrupt the machine-readable "
            "result (the 2026-08-18 'missing or malformed' recurrence)."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        passed = asyncio.run(run_smoke(json_output=args.json, out_path=args.out))
        return 0 if passed else 1
    except (Exception, SystemExit) as exc:  # noqa: BLE001
        # Catastrophic failure BEFORE run_smoke could emit its own output —
        # e.g. get_settings()/Settings() raising at boot (a bad model slot,
        # gotcha 18's _assert_anthropic_only), an import error, or a spaCy
        # sys.exit (gotcha 7). Without this guard the process dies with a bare
        # traceback and the smoke workflow reports the opaque "smoke output
        # missing or malformed" with NO signal (issues #686/#706/#723). Always
        # emit ONE parseable failing result carrying the real cause instead.
        from datetime import UTC, datetime

        from helixis.smoke.checks import CheckResult
        from helixis.smoke.output import format_json, format_pretty

        ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        result = CheckResult(
            "smoke_runner",
            "fail",
            0,
            f"smoke crashed before running any check: {type(exc).__name__}: {exc}",
        )
        renderer = format_json if args.json else format_pretty
        rendered = renderer(ts, False, 0.0, [result])
        print(rendered)
        if args.out:
            try:
                from pathlib import Path

                Path(args.out).write_text(rendered + "\n", encoding="utf-8")
            except Exception:  # noqa: BLE001 — the stdout copy above still stands
                pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
