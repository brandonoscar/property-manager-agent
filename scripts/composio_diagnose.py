#!/usr/bin/env python3
"""Dump every Composio connection for a given user_id.

Mechanises Phase 1 of the Composio onboarding runbook
(``docs/operations/composio-setup.md``): instead of clicking
through every row in the Composio dashboard, this script asks
the v3 API directly for all of a user's connections and prints:

* a per-connection table (toolkit / status / created / connection_id / error),
* status × toolkit cross-tabulation, and
* a full attribute dump of one representative inactive connection
  so any field we didn't explicitly probe is still visible.

Usage::

    HELIXIS_COMPOSIO__API_KEY=... python scripts/composio_diagnose.py <user_id>
    HELIXIS_COMPOSIO__API_KEY=... python scripts/composio_diagnose.py <user_id> --json

``--json`` emits a single ``json.dumps`` payload to stdout (and nothing
else) so you can pipe into ``jq`` or a follow-up tool. Without the flag,
output is the human-readable layout above.

Exit codes::

    0  — connections enumerated (including the empty case)
    1  — Composio API call raised
    2  — env / install precondition not met
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import UTC, datetime
from typing import Any


def _format_relative(when: Any) -> str:
    """Best-effort ``X<unit> ago`` for an ISO timestamp."""
    if not when:
        return "?"
    if not isinstance(when, str):
        return str(when)
    try:
        parsed = datetime.fromisoformat(when.replace("Z", "+00:00"))
    except ValueError:
        return when
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    seconds = int((datetime.now(UTC) - parsed).total_seconds())
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _toolkit_slug(account: Any) -> str:
    """Pull the toolkit slug off a v3 ConnectedAccount."""
    t = getattr(account, "toolkit", None)
    if isinstance(t, str):
        return t
    if t is not None:
        return getattr(t, "slug", None) or getattr(t, "name", None) or "?"
    return "?"


def _probe(obj: Any, name: str) -> Any:
    """getattr that never raises — Composio SDK shapes drift between versions."""
    try:
        return getattr(obj, name, None)
    except Exception:  # noqa: BLE001 — diagnostic probe
        return None


_INACTIVE_STATUSES = frozenset({"dropped", "expired", "failed", "revoked"})


def _build_inactive_sample(sample: Any) -> dict[str, str]:
    """Capture every public, non-callable attribute as ``attr -> repr(value)``.

    ``repr`` is captured here (gather time) rather than emitted at print
    time so the JSON output is a flat dict of strings AND the pretty
    output stays byte-identical (we render the captured repr string
    directly instead of re-repring).
    """
    out: dict[str, str] = {}
    for attr in sorted(set(dir(sample))):
        if attr.startswith("_"):
            continue
        val = _probe(sample, attr)
        if callable(val):
            continue
        out[attr] = repr(val)
    return out


def gather_connections(user_id: str) -> tuple[int, dict[str, Any] | None]:
    """Hit the Composio API and assemble the data dict.

    Returns ``(exit_code, data)``. ``data`` is None on precondition /
    API failure so the caller knows not to render anything.

    Exit codes match the script-level contract::

        0  — success (data is the populated dict, even on empty connections)
        1  — Composio API call raised
        2  — env / install precondition not met
    """
    api_key = os.environ.get("HELIXIS_COMPOSIO__API_KEY", "").strip()
    if not api_key:
        print("ERROR: HELIXIS_COMPOSIO__API_KEY is not set.", file=sys.stderr)
        return 2, None

    try:
        from composio import Composio
    except ImportError:
        print(
            'ERROR: composio package not installed. Run `pip install -e ".[connectors]"`.',
            file=sys.stderr,
        )
        return 2, None

    client = Composio(api_key=api_key)

    try:
        response = client.connected_accounts.list(user_ids=[user_id])
    except Exception as exc:  # noqa: BLE001 — surface any API failure
        print(
            f"ERROR calling connected_accounts.list: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1, None

    items_attr = getattr(response, "items", None)
    items = (
        list(items_attr)
        if items_attr is not None
        else (response if isinstance(response, list) else [])
    )

    connections: list[dict[str, Any]] = []
    for c in items:
        err = _probe(c, "last_error") or _probe(c, "error") or ""
        err_str = str(err).splitlines()[0][:60] if err else ""
        connections.append(
            {
                "toolkit": _toolkit_slug(c),
                "status": str(_probe(c, "status") or "?"),
                "created_at": _probe(c, "created_at"),
                "connection_id": str(_probe(c, "id") or _probe(c, "connection_id") or "?"),
                "error": err_str,
            }
        )

    # Pre-compute cross-tabs. Counter preserves insertion order; converting
    # to a dict here keeps the same ordering for both pretty and JSON output.
    by_status = dict(Counter(c["status"] for c in connections))
    by_toolkit = dict(Counter(c["toolkit"] for c in connections))
    cross: Counter[tuple[str, str]] = Counter()
    for c in connections:
        cross[(c["toolkit"], c["status"])] += 1
    by_toolkit_status = [
        {"toolkit": t, "status": s, "count": n} for (t, s), n in sorted(cross.items())
    ]

    inactive_indices = [
        i for i, c in enumerate(connections) if c["status"].lower() in _INACTIVE_STATUSES
    ]
    inactive_sample = (
        _build_inactive_sample(items[inactive_indices[0]]) if inactive_indices else None
    )

    data: dict[str, Any] = {
        "user_id": user_id,
        "total": len(connections),
        "connections": connections,
        "cross_tab": {
            "by_status": by_status,
            "by_toolkit": by_toolkit,
            "by_toolkit_status": by_toolkit_status,
        },
        "inactive_count": len(inactive_indices),
        "inactive_sample": inactive_sample,
    }
    return 0, data


def render_pretty(data: dict[str, Any]) -> str:
    """Render the data dict as the human-readable layout.

    Byte-identical to the pre-refactor script for any given gather
    output — pretty mode does no API work of its own, it only reads
    from ``data``. The unit tests pin a representative dict to this
    exact string.
    """
    lines: list[str] = []
    lines.append(f"# Composio connections for user_id={data['user_id']}")
    lines.append(f"# Total: {data['total']}")
    lines.append("")

    connections = data["connections"]
    if not connections:
        return "\n".join(lines) + "\n"

    header = f"{'TOOLKIT':<20} {'STATUS':<14} {'CREATED':<14} {'CONNECTION_ID':<40} ERROR"
    lines.append(header)
    lines.append("-" * len(header))
    for c in connections:
        created = _format_relative(c["created_at"])
        row = (
            f"{c['toolkit']:<20} {c['status']:<14} {created:<14} "
            f"{c['connection_id']:<40} {c['error']}"
        )
        lines.append(row)

    cross_tab = data["cross_tab"]
    lines.append("")
    lines.append("## Summary")
    lines.append(f"Status counts:  {cross_tab['by_status']}")
    lines.append(f"Toolkit counts: {cross_tab['by_toolkit']}")

    lines.append("")
    lines.append("## Status × Toolkit")
    for row in cross_tab["by_toolkit_status"]:
        lines.append(f"  {row['toolkit']:<20} {row['status']:<14} {row['count']}")

    sample = data["inactive_sample"]
    if sample is not None:
        n = data["inactive_count"]
        plural = "s" if n != 1 else ""
        lines.append("")
        lines.append(f"## Full attribute dump (first of {n} inactive connection{plural})")
        for attr, repr_str in sample.items():
            lines.append(f"  {attr}: {repr_str}")

    return "\n".join(lines) + "\n"


def diagnose(user_id: str, *, as_json: bool) -> int:
    exit_code, data = gather_connections(user_id)
    if data is None:
        return exit_code

    if as_json:
        # Single payload to stdout, nothing else. No trailing extras —
        # makes piping into ``jq`` clean.
        sys.stdout.write(json.dumps(data, indent=2, default=str))
        sys.stdout.write("\n")
    else:
        sys.stdout.write(render_pretty(data))
    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Diagnose Composio connections for a user_id.",
    )
    parser.add_argument("user_id", help="Composio user_id (entity ID) to inspect")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a single JSON payload to stdout instead of the pretty layout.",
    )
    args = parser.parse_args()
    return diagnose(args.user_id, as_json=args.json)


if __name__ == "__main__":
    sys.exit(main())
