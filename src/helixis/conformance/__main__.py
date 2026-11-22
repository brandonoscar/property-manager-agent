"""CLI entry point — ``python -m helixis.conformance [--json] [--base-url URL]``.

Verifies the deployed backend exposes the ADR 0003 client contract and that
CORS admits the onboarding wizard origin. See
``docs/operations/conformance-check.md``.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from helixis.conformance.checks import DEFAULT_WIZARD_ORIGIN
from helixis.conformance.runner import DEFAULT_BASE_URL, run_conformance

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m helixis.conformance",
        description="ADR 0003 client↔backend contract conformance probe.",
    )
    parser.add_argument(
        "--base-url",
        # `or` (not `.get(..., default)`): a GitHub Actions `vars.*` reference
        # to an unset repo variable resolves to `""`, which lands here as an
        # *empty but set* env var — `.get` would return "" instead of falling
        # back, sending every probe to a URL-less client (#420).
        default=os.environ.get("HELIXIS_CONFORMANCE_BASE_URL") or DEFAULT_BASE_URL,
        help=f"Backend base URL (default: {DEFAULT_BASE_URL}).",
    )
    parser.add_argument(
        "--origin",
        default=os.environ.get("HELIXIS_CONFORMANCE_ORIGIN") or DEFAULT_WIZARD_ORIGIN,
        help="Browser origin to test CORS for (default: the onboarding wizard).",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("HELIXIS_CONFORMANCE_TOKEN"),
        help="Supabase JWT (helixis-test) to run the authenticated bootstrap probe.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-parseable JSON instead of pretty output.",
    )
    parser.add_argument(
        "--credentialed",
        action="store_true",
        help="Also run the STATE-MUTATING Buildium provisioning probe "
        "(PUT credentials → POST /buildium/test → ok:true → read-back). "
        "Needs --token + a Buildium key; run against helixis-test with a SANDBOX key.",
    )
    parser.add_argument(
        "--buildium-client-id",
        default=os.environ.get("HELIXIS_BUILDIUM__CLIENT_ID"),
        help="Buildium client_id for --credentialed (default: HELIXIS_BUILDIUM__CLIENT_ID).",
    )
    parser.add_argument(
        "--buildium-client-secret",
        default=os.environ.get("HELIXIS_BUILDIUM__CLIENT_SECRET"),
        help="Buildium client_secret for --credentialed "
        "(default: HELIXIS_BUILDIUM__CLIENT_SECRET).",
    )
    parser.add_argument(
        "--buildium-environment",
        choices=["sandbox", "production"],
        default=os.environ.get("HELIXIS_BUILDIUM__ENVIRONMENT") or "sandbox",
        help="Which Buildium environment the key targets (default: sandbox).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    passed = asyncio.run(
        run_conformance(
            base_url=args.base_url,
            origin=args.origin,
            token=args.token,
            json_output=args.json,
            credentialed=args.credentialed,
            buildium_client_id=args.buildium_client_id,
            buildium_client_secret=args.buildium_client_secret,
            buildium_environment=args.buildium_environment,
        )
    )
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
