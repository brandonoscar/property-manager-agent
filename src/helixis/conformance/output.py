"""Pretty + JSON formatting for conformance probe results."""

from __future__ import annotations

import json

from helixis.conformance.checks import ProbeResult

_GLYPH = {"pass": "✓", "fail": "✗", "warn": "!", "skipped": "○"}


def format_pretty(base_url: str, passed: bool, results: list[ProbeResult]) -> str:
    lines = [f"Conformance probe — {base_url}", "─" * 60]
    for r in results:
        glyph = _GLYPH.get(r.status, "?")
        latency = f" [{r.latency_ms}ms]" if r.latency_ms is not None else ""
        lines.append(f" {glyph} {r.name:<34} {r.detail}{latency}")
    lines.append("─" * 60)
    lines.append("RESULT: PASS" if passed else "RESULT: FAIL")
    return "\n".join(lines)


def format_json(base_url: str, passed: bool, results: list[ProbeResult]) -> str:
    return json.dumps(
        {
            "base_url": base_url,
            "passed": passed,
            "probes": [
                {
                    "name": r.name,
                    "status": r.status,
                    "detail": r.detail,
                    "http_status": r.http_status,
                    "latency_ms": r.latency_ms,
                }
                for r in results
            ],
        },
        indent=2,
    )
