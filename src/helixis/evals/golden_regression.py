"""Golden end-state regression engine (release-gate Tier-3 #11) — pure + deterministic.

The spec's regression suite re-runs previously-passing golden tasks, canonicalizes
each end-state (sorted keys, normalized types, ignore-list applied), and diffs it
against a stored golden end-state; any diff is itemized in the report. This module
is the deterministic **engine** — canonicalization + diff over plain dicts/lists —
so it is fully unit-testable WITHOUT a model or the sandbox. Producing the live
end-states to diff (running each task through the agent + reading Buildium back) is
the state-based-scoring layer that needs creds and will call this.

Key design point: **ignore volatile fields.** Timestamps, auto-incrementing ids,
and audit columns change on every run and would swamp a diff with noise, so they're
dropped before comparison. Everything else is compared exactly, at a JSON path, so
a real behavioral change (a different amount, a flipped status, a dropped field)
surfaces as a precise itemized `Diff`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Exact volatile keys dropped before comparison.
_IGNORE_KEYS_EXACT: frozenset[str] = frozenset(
    {
        "id",
        "uuid",
        "guid",
        "timestamp",
        "etag",
        "href",
        "created_at",
        "updated_at",
        "createddatetime",
        "lastupdateddatetime",
        "created_date_time",
        "last_updated_date_time",
        "created_by",
        "updated_by",
    }
)

# Key suffixes that mark a volatile field regardless of prefix (auto-ids, audit
# timestamps): "task_id", "work_order_id", "created_at", "posted_date_time", …
_IGNORE_KEY_SUFFIXES: tuple[str, ...] = ("_id", "_at", "_date_time", "_datetime", "_uuid")


def is_ignored_key(key: str) -> bool:
    """Whether a dict key names a volatile field (auto-id / timestamp / audit)
    that should be dropped before diffing. Case-insensitive."""
    k = key.lower()
    if k in _IGNORE_KEYS_EXACT:
        return True
    return any(k.endswith(suffix) for suffix in _IGNORE_KEY_SUFFIXES)


def _normalize_scalar(value: Any) -> Any:
    """Normalize a scalar so trivially-equal values compare equal:
    ``1`` == ``1.0`` == ``"1"`` for numbers; strings are stripped."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        f = float(value)
        return int(f) if f.is_integer() else f
    if isinstance(value, str):
        s = value.strip()
        # A numeric-looking string normalizes to its number (so "2450" == 2450).
        if re.fullmatch(r"-?\d+(\.\d+)?", s):
            f = float(s)
            return int(f) if f.is_integer() else f
        return s
    return value


def canonicalize(obj: Any, *, extra_ignore: frozenset[str] | None = None) -> Any:
    """Return a canonical copy of ``obj`` for stable comparison: volatile keys
    dropped, dict keys implicitly order-independent (dicts compare by content),
    scalars normalized. Lists keep order (order can be meaningful) but each
    element is canonicalized. Pure — never mutates the input."""
    extra = extra_ignore or frozenset()
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for key in sorted(obj, key=str):
            if is_ignored_key(str(key)) or str(key).lower() in extra:
                continue
            out[str(key)] = canonicalize(obj[key], extra_ignore=extra)
        return out
    if isinstance(obj, list | tuple):
        return [canonicalize(v, extra_ignore=extra) for v in obj]
    return _normalize_scalar(obj)


@dataclass
class Diff:
    path: str  # JSON-ish path, e.g. "work_order.status" or "charges[1].amount"
    kind: str  # "added" | "removed" | "changed" | "type_changed" | "length_changed"
    stored: Any = None
    current: Any = None

    def __str__(self) -> str:
        if self.kind == "added":
            return f"{self.path}: ADDED {self.current!r}"
        if self.kind == "removed":
            return f"{self.path}: REMOVED {self.stored!r}"
        if self.kind == "length_changed":
            return f"{self.path}: length {self.stored} -> {self.current}"
        return f"{self.path}: {self.stored!r} -> {self.current!r}"


def _diff(stored: Any, current: Any, path: str, out: list[Diff]) -> None:
    if type(stored) is not type(current) and not _both_numeric(stored, current):
        out.append(Diff(path or "$", "type_changed", stored, current))
        return
    if isinstance(stored, dict):
        for key in sorted(set(stored) | set(current), key=str):
            child = f"{path}.{key}" if path else str(key)
            if key not in stored:
                out.append(Diff(child, "added", current=current[key]))
            elif key not in current:
                out.append(Diff(child, "removed", stored=stored[key]))
            else:
                _diff(stored[key], current[key], child, out)
        return
    if isinstance(stored, list):
        if len(stored) != len(current):
            out.append(Diff(path or "$", "length_changed", len(stored), len(current)))
        for i in range(min(len(stored), len(current))):
            _diff(stored[i], current[i], f"{path}[{i}]", out)
        return
    if stored != current:
        out.append(Diff(path or "$", "changed", stored, current))


def _both_numeric(a: Any, b: Any) -> bool:
    num = (int, float)
    return (
        isinstance(a, num)
        and isinstance(b, num)
        and not isinstance(a, bool)
        and not isinstance(b, bool)
    )


def diff_states(
    stored: Any, current: Any, *, extra_ignore: frozenset[str] | None = None
) -> list[Diff]:
    """Canonicalize both end-states and return an itemized list of differences
    (empty == no regression). Both sides are canonicalized first, so volatile
    fields never appear in the diff."""
    out: list[Diff] = []
    _diff(
        canonicalize(stored, extra_ignore=extra_ignore),
        canonicalize(current, extra_ignore=extra_ignore),
        "",
        out,
    )
    return out


@dataclass
class RegressionResult:
    task_id: str
    diffs: list[Diff] = field(default_factory=list)

    @property
    def regressed(self) -> bool:
        return bool(self.diffs)


def compare_end_states(
    task_id: str,
    stored: Any,
    current: Any,
    *,
    extra_ignore: frozenset[str] | None = None,
) -> RegressionResult:
    """Diff one task's stored vs current end-state into a labeled result."""
    return RegressionResult(
        task_id=task_id, diffs=diff_states(stored, current, extra_ignore=extra_ignore)
    )


__all__ = [
    "Diff",
    "RegressionResult",
    "canonicalize",
    "compare_end_states",
    "diff_states",
    "is_ignored_key",
]
