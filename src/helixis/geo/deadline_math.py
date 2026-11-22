"""Deterministic deadline arithmetic for jurisdiction rules — pure, no I/O.

Three primitives the jurisdiction layer composes:

* :func:`compute_deadline` — trigger date + a statutory day count + a
  day-count unit → the due date. **The clock starts the day AFTER the
  trigger date** (the trigger day itself is day zero; the first counted
  day is the next one) — the near-universal statutory convention.
* :func:`compliance_completion_date` — the last business day STRICTLY
  BEFORE a due date. For obligations the manager owes (deposit returns),
  never rely on weekend/holiday roll-forward — schedule completion for
  the business day before, not the statutory last minute.
* :func:`apply_pending_changes` — resolve a rule group's dated
  ``pending_change`` against today, so answers always reflect the value
  in force NOW and flag the transition either way.

Business-day math uses the ``holidays`` PyPI package (MIT — verified via
its ``License-Expression`` metadata at pin time) for US federal + state
holidays. The import is lazy and failure-tolerant: a missing or broken
package degrades to weekend-only skipping with an explicit flag — repo
discipline, enhancements degrade and never crash (gotcha 7 class).
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

logger = logging.getLogger(__name__)

UNKNOWN_UNIT_FLAG = (
    "day-count type not encoded — used the longer (tenant-protective) interpretation"
)
COURT_FLAG = "court holidays may extend this — verify locally"
HOLIDAYS_UNAVAILABLE_FLAG = (
    "holiday calendar unavailable — business days counted skipping weekends only; "
    "a holiday could extend this deadline"
)

_COURT_UNITS = frozenset({"court", "judicial"})


@dataclass(frozen=True)
class Deadline:
    """One computed deadline: the due date, which day-count interpretation
    produced it, and any honesty flags the caller must surface."""

    due_date: date
    unit_used: str
    flags: list[str] = field(default_factory=list)


def _holiday_lookup(state: str) -> Callable[[date], bool] | None:
    """A ``date -> is-holiday`` test for US federal + ``state`` holidays,
    or ``None`` when the ``holidays`` package is missing/broken (the
    caller then degrades to weekend-only skipping and flags it).

    Lazy import by design; an unknown state subdivision falls back to the
    federal calendar (still holiday-aware, just not state-specific).
    """
    try:
        import holidays as _holidays

        try:
            calendar = _holidays.country_holidays("US", subdiv=(state or "").strip().upper())
        except Exception:  # noqa: BLE001 — unknown subdiv → federal calendar
            calendar = _holidays.country_holidays("US")

        def _is_holiday(d: date) -> bool:
            try:
                return d in calendar
            except Exception:  # noqa: BLE001 — degrade per-date, never crash
                return False

        return _is_holiday
    except Exception:  # noqa: BLE001 — package missing/broken → weekend-only
        logger.debug("holidays package unavailable — weekend-only business days", exc_info=True)
        return None


def _is_business_day(d: date, is_holiday: Callable[[date], bool] | None) -> bool:
    if d.weekday() >= 5:
        return False
    return not (is_holiday is not None and is_holiday(d))


def _add_business_days(start: date, days: int, is_holiday: Callable[[date], bool] | None) -> date:
    d = start
    remaining = days
    while remaining > 0:
        d += timedelta(days=1)
        if _is_business_day(d, is_holiday):
            remaining -= 1
    return d


def compute_deadline(trigger_date: date, days: int, unit: str | None, *, state: str) -> Deadline:
    """Compute a statutory due date from a trigger date and a day count.

    The clock starts the day AFTER ``trigger_date`` — day 1 is the day
    following the trigger, so a 30-calendar-day deadline triggered on the
    1st is due on the 31st.

    Units: ``"calendar"`` is a straight count; ``"business"`` skips
    weekends and US federal + state holidays; ``"court"``/``"judicial"``
    is business-day math plus a verify-locally flag (court calendars can
    add closures). ``None``/unknown computes BOTH interpretations and
    returns the LATER due date with an explicit flag — the
    tenant-protective reading of an un-encoded day-count type.
    """
    days = max(0, int(days))
    normalized = (unit or "").strip().lower()
    flags: list[str] = []

    if normalized == "calendar":
        return Deadline(trigger_date + timedelta(days=days), "calendar", [])

    if normalized == "business" or normalized in _COURT_UNITS:
        lookup = _holiday_lookup(state)
        if lookup is None:
            flags.append(HOLIDAYS_UNAVAILABLE_FLAG)
        due = _add_business_days(trigger_date, days, lookup)
        if normalized in _COURT_UNITS:
            flags.append(COURT_FLAG)
            return Deadline(due, "court", flags)
        return Deadline(due, "business", flags)

    # None / unknown unit → both interpretations, keep the later.
    calendar_due = trigger_date + timedelta(days=days)
    lookup = _holiday_lookup(state)
    if lookup is None:
        flags.append(HOLIDAYS_UNAVAILABLE_FLAG)
    business_due = _add_business_days(trigger_date, days, lookup)
    flags.append(UNKNOWN_UNIT_FLAG)
    if business_due > calendar_due:
        return Deadline(business_due, "business", flags)
    return Deadline(calendar_due, "calendar", flags)


def compliance_completion_date(due_date: date, *, state: str) -> date:
    """The last business day STRICTLY BEFORE ``due_date``.

    For obligations the manager owes (e.g. a deposit return, §1.6): never
    rely on weekend/holiday roll-forward — plan to be DONE on the business
    day before the statutory due date. Uses the same failure-tolerant
    holiday calendar as :func:`compute_deadline` (weekend-only when the
    package is unavailable).
    """
    lookup = _holiday_lookup(state)
    d = due_date - timedelta(days=1)
    # Bounded walk-back: no US jurisdiction closes for two straight weeks.
    for _ in range(30):
        if _is_business_day(d, lookup):
            return d
        d -= timedelta(days=1)
    return d


def apply_pending_changes(
    group: dict[str, Any], *, today: date
) -> tuple[dict[str, Any], list[str]]:
    """Resolve a rule group's ``pending_change`` against ``today``.

    ``pending_change`` is ``{field, new_value, effective_from, precision,
    citation}`` (rules-file legend). Already effective → return a COPY of
    the group with ``new_value`` applied to ``field`` plus a flag naming
    the change and citation (``precision == "year"`` adds "verify exact
    date" — the in-year date is unverified). Still in the future → keep
    the current value and flag the upcoming change with its date. The
    input group is never mutated.
    """
    pending = group.get("pending_change")
    if not isinstance(pending, dict):
        return group, []
    field_name = pending.get("field")
    raw_effective = pending.get("effective_from")
    if not field_name or not raw_effective:
        return group, []
    try:
        effective = date.fromisoformat(str(raw_effective))
    except ValueError:
        return group, [
            f"pending change to {field_name} carries an unparseable effective date "
            f"({raw_effective!r}) — verify the controlling statute"
        ]
    new_value = pending.get("new_value")
    citation = pending.get("citation")
    cite = f" ({citation})" if citation else ""
    year_precision = str(pending.get("precision") or "").strip().lower() == "year"

    if effective <= today:
        updated = copy.deepcopy(group)
        updated[field_name] = new_value
        if year_precision:
            flag = (
                f"{field_name} changed to {new_value} effective {effective.year} — "
                f"verify exact date{cite}"
            )
        else:
            flag = f"{field_name} changed to {new_value} effective {effective.isoformat()}{cite}"
        return updated, [flag]

    flag = (
        f"upcoming change: {field_name} becomes {new_value} effective {effective.isoformat()}{cite}"
    )
    if year_precision:
        flag += " — exact date unverified"
    return group, [flag]


__all__ = [
    "COURT_FLAG",
    "HOLIDAYS_UNAVAILABLE_FLAG",
    "UNKNOWN_UNIT_FLAG",
    "Deadline",
    "apply_pending_changes",
    "compliance_completion_date",
    "compute_deadline",
]
