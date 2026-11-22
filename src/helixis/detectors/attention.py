"""Attention budget — the volume-control layer over detector findings.

Eight-plus detectors writing into one findings ledger will bury the signal by
week three unless delivery is governed. This is that governor, and it is PURE
(no DB, no I/O) so it is exhaustively unit-testable; the repository layer
(``db/repositories/noticed.py``) feeds it rows and the endpoint / known-state
channel render what it returns.

The five rules from the ambient-detector spec:

1. **Hard daily cap** — at most ``daily_cap`` non-urgent items are *delivered*
   ("Occupella noticed"); the rest are ``overflow`` (still queryable, not
   pushed).
2. **Severity tiers, different paths** — ``critical`` (habitability / money
   already gone) is **urgent**: delivered immediately and NEVER counted against
   the cap. Everything else rides the capped digest.
3. **Suppression** — the caller passes only ``status='new'`` findings; a
   dismissed finding never returns and an acknowledged (``confirmed``) one is
   excluded upstream, so this layer only ranks what's genuinely unseen.
4. **Cross-detector dedup** — findings on the SAME entity collapse to one item
   (highest-priority wins; the rest ride along as ``related``), so one root
   cause (a lease that's both under-charged and 90-days-late) is one line, not
   five.
5. **Per-detector dismissal-rate** — ``detector_calibration`` flags any detector
   dismissed more than ``miscalibrated_rate`` of the time (with enough samples)
   so a noisy detector gets reported, not left quietly spamming.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# critical is delivered immediately and never capped.
URGENT_SEVERITIES: frozenset[str] = frozenset({"critical"})
DEFAULT_DAILY_CAP = 8

# Lower rank = higher priority.
_SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}


@dataclass
class NoticedFinding:
    """One open finding fed into the budget (a row from ``helixis_risk_flags``)."""

    id: str
    rule_key: str
    severity: str
    title: str
    detail: str
    amount: float | None = None
    property_id: int | None = None
    entity_type: str | None = None
    entity_id: int | None = None
    last_seen: Any = None  # datetime | str; only used as a sort tiebreaker


@dataclass
class NoticedItem:
    """A delivered item — the winning finding for an entity, plus any others
    on that same entity that collapsed into it."""

    finding: NoticedFinding
    related: list[NoticedFinding] = field(default_factory=list)


@dataclass
class NoticedDigest:
    urgent: list[NoticedItem] = field(default_factory=list)
    noticed: list[NoticedItem] = field(default_factory=list)
    overflow_count: int = 0

    @property
    def delivered_count(self) -> int:
        return len(self.urgent) + len(self.noticed)


@dataclass
class CalibrationReport:
    rule_key: str
    total: int
    dismissed: int
    dismissal_rate: float
    miscalibrated: bool


def _severity_rank(sev: str) -> int:
    return _SEVERITY_RANK.get(sev, 3)


def _group_key(f: NoticedFinding) -> tuple[Any, Any]:
    # Entity-less findings (e.g. a promise keyed by email, entity_id=None) never
    # merge with each other — dedup only collapses findings on the SAME entity.
    if f.entity_id is None:
        return ("__id__", f.id)
    return (f.entity_type or "", f.entity_id)


def _priority(f: NoticedFinding) -> tuple[int, float, Any]:
    # Sort key (ascending): severity rank, then larger amount first (negate),
    # then most-recent last_seen first. ``last_seen`` may be None.
    amount = f.amount if f.amount is not None else 0.0
    # Represent last_seen so that "more recent = earlier"; fall back to empty.
    ls = f.last_seen
    ls_key = ls.isoformat() if hasattr(ls, "isoformat") else (str(ls) if ls is not None else "")
    return (_severity_rank(f.severity), -amount, _NegStr(ls_key))


class _NegStr:
    """Reverse-order string wrapper so a plain tuple sort puts the
    lexicographically-largest (most recent ISO timestamp) first."""

    __slots__ = ("s",)

    def __init__(self, s: str) -> None:
        self.s = s

    def __lt__(self, other: _NegStr) -> bool:
        return self.s > other.s

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _NegStr) and self.s == other.s


def select_noticed(
    findings: list[NoticedFinding],
    *,
    daily_cap: int = DEFAULT_DAILY_CAP,
    urgent_severities: frozenset[str] = URGENT_SEVERITIES,
) -> NoticedDigest:
    """Apply the attention budget to a company's open findings.

    Returns urgent items (uncapped), the capped ``noticed`` digest, and the
    count of everything that didn't make the cut (``overflow``).
    """
    # 1. Cross-detector dedup: collapse per entity, highest priority wins.
    groups: dict[tuple[Any, Any], list[NoticedFinding]] = {}
    for f in findings:
        groups.setdefault(_group_key(f), []).append(f)

    items: list[NoticedItem] = []
    for members in groups.values():
        members.sort(key=_priority)
        items.append(NoticedItem(finding=members[0], related=members[1:]))

    # 2. Split urgent vs. the rest, each already priority-ordered.
    items.sort(key=lambda it: _priority(it.finding))
    urgent = [it for it in items if it.finding.severity in urgent_severities]
    rest = [it for it in items if it.finding.severity not in urgent_severities]

    # 3. Hard cap on the non-urgent digest; the tail is overflow.
    cap = max(0, daily_cap)
    noticed = rest[:cap]
    overflow_count = len(rest) - len(noticed)

    return NoticedDigest(urgent=urgent, noticed=noticed, overflow_count=overflow_count)


def detector_calibration(
    counts: dict[str, tuple[int, int]],
    *,
    miscalibrated_rate: float = 0.5,
    min_samples: int = 5,
) -> list[CalibrationReport]:
    """Given ``{rule_key: (dismissed, total)}``, report each detector's
    dismissal rate and flag the miscalibrated ones (dismissed more than
    ``miscalibrated_rate`` of the time, with at least ``min_samples`` findings).
    Sorted worst-first.
    """
    reports: list[CalibrationReport] = []
    for rule_key, (dismissed, total) in counts.items():
        rate = (dismissed / total) if total else 0.0
        reports.append(
            CalibrationReport(
                rule_key=rule_key,
                total=total,
                dismissed=dismissed,
                dismissal_rate=round(rate, 3),
                miscalibrated=(total >= min_samples and rate > miscalibrated_rate),
            )
        )
    reports.sort(key=lambda r: (-r.dismissal_rate, -r.total))
    return reports


__all__ = [
    "DEFAULT_DAILY_CAP",
    "URGENT_SEVERITIES",
    "CalibrationReport",
    "NoticedDigest",
    "NoticedFinding",
    "NoticedItem",
    "detector_calibration",
    "select_noticed",
]
