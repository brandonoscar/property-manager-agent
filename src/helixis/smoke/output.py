"""Pretty terminal + JSON formatters for smoke results.

Every ``detail`` string passes through :func:`_redact` before output
so that error messages from downstream libraries (which sometimes
include the credential they used) never spill secrets to logs.
"""

from __future__ import annotations

import json
import re

from helixis.smoke.checks import CheckResult

# Redaction order: longest/most-specific patterns first so that a
# postgres URL's password isn't accidentally consumed by the generic
# hex pattern before the URL pattern can claim it.
_REDACTION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # postgres://user:secret@host/db → postgres://[REDACTED]@host/db
    (re.compile(r"postgres(?:ql)?://[^@\s]+@"), "postgres://[REDACTED]@"),
    # OpenAI-style keys (sk-...) — match 8+ chars to avoid false positives.
    (re.compile(r"sk-[A-Za-z0-9_\-]{8,}"), "sk-[REDACTED]"),
    # JWT shape (eyJ<header>.<payload>.<sig>).
    (re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"), "[REDACTED-JWT]"),
    # Long hex strings (>=32 chars) — covers Fernet keys, hex API tokens, etc.
    (re.compile(r"[a-f0-9]{32,}", re.IGNORECASE), "[REDACTED-HEX]"),
    # Field-name-keyed secrets in error strings ("token=...", "secret=...").
    (re.compile(r"(?i)(key|token|secret)=[^\s,&]+"), r"\1=[REDACTED]"),
]


def _redact(s: str) -> str:
    for pattern, replacement in _REDACTION_PATTERNS:
        s = pattern.sub(replacement, s)
    return s


def _status_icon(status: str) -> str:
    return {"pass": "✓", "fail": "✗", "skipped": "○"}.get(status, "?")


# Column widths chosen so that "composio_calendar" (17 chars) + a few
# spaces lands before the detail column. Widen if a longer check name
# is added.
_NAME_WIDTH = 22
_DETAIL_WIDTH = 10
_RULE_WIDTH = 37


def format_pretty(
    timestamp: str,
    passed: bool,
    duration: float,
    results: list[CheckResult],
) -> str:
    lines: list[str] = [
        f"Helixis smoke — {timestamp}",
        "─" * _RULE_WIDTH,
    ]
    for r in results:
        icon = _status_icon(r.status)
        detail = _redact(r.detail)
        latency = f"{r.latency_ms}ms"
        if r.status == "pass":
            lines.append(f"{icon} {r.name:<{_NAME_WIDTH}} {detail:<{_DETAIL_WIDTH}} {latency:>6}")
        elif r.status == "skipped":
            lines.append(f"{icon} {r.name:<{_NAME_WIDTH}} skipped: {detail}")
        else:
            lines.append(f"{icon} {r.name:<{_NAME_WIDTH}} {detail}")
    lines.append("─" * _RULE_WIDTH)

    green = sum(1 for r in results if r.passed)
    failed_names = [r.name for r in results if r.failed]
    skipped_names = [r.name for r in results if r.skipped]
    total = len(results)

    if passed:
        lines.append(f"PASS — {green}/{total} checks green in {duration:.1f}s")
    else:
        lines.append(
            f"FAIL — {green}/{total} green, {len(failed_names)} broken: {', '.join(failed_names)}"
        )
        lines.append("Exit 1.")

    # ⚠ Named, not just implied by the gap between green and total. A skipped
    # check did not run, and a run where half the checks skipped is not the
    # same evidence as one where they all passed — but "PASS — 12/15 checks
    # green" reads as success to anyone not holding §4's "all 14" bar in mind.
    # Same reasoning as the truncation footer in gotcha 26: state the scope
    # rather than leaving the reader to infer it.
    if skipped_names:
        lines.append(f"{len(skipped_names)} did NOT run: {', '.join(skipped_names)}")

    return "\n".join(lines)


def format_json(
    timestamp: str,
    passed: bool,
    duration: float,
    results: list[CheckResult],
) -> str:
    payload = {
        "timestamp": timestamp,
        "passed": passed,
        "duration_seconds": round(duration, 1),
        "checks": [
            {
                "name": r.name,
                "status": r.status,
                "latency_ms": r.latency_ms,
                "detail": _redact(r.detail),
            }
            for r in results
        ],
        "failed_checks": [r.name for r in results if r.failed],
        # The workflow gates on ``passed`` alone, so without this a run where
        # a check never executed is byte-indistinguishable from one where it
        # passed. Machine-readable so the daily cron can widen its bar later
        # without another schema change.
        "skipped_checks": [r.name for r in results if r.skipped],
    }
    return json.dumps(payload, indent=2)
