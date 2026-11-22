"""Payment promises — "will pay the 17th" extraction + the suppression rule.

Rides email ingest (v1.5 of the reminders engine): when a surfaced email
from a known Buildium contact contains a payment promise with a concrete
date, we record it, and the ``rent_missing`` producer stays quiet until
the promised date (+1 grace day) passes. After that the suppression
lapses structurally — the next sweep re-emits the card, which IS the
follow-up ("they said the 17th; it's the 18th and nothing posted").

Extraction is DETERMINISTIC — small regex grammar, no LLM. It only fires
when the text has both a payment word and an explicit near-future date;
anything fuzzier returns None (a missed promise just means the normal
reminder fires — fail-open in the direction of substance). A false
positive costs at most a few days of suppression, and a promise date in
the past never suppresses at all, which naturally ignores "I paid on the
1st" recollections.
"""

from __future__ import annotations

import calendar
import datetime as dt
import re

# A promise only counts when it lands within this window — "next spring"
# isn't an actionable payment plan.
PROMISE_LOOKAHEAD_DAYS = 45

# Suppress through the promised date plus this many days (money takes a
# day to show up); after that the card returns as the follow-up.
PROMISE_GRACE_DAYS = 1

_PAY_WORDS = re.compile(
    r"\b(pay|pays|paying|paid|payment|rent|send|sending|transfer|"
    r"zelle|venmo|deposit|check|cheque)\b",
    re.IGNORECASE,
)

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}  # fmt: skip

_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}  # fmt: skip

# "July 17", "jul 17th", "17 July"
_MONTH_DAY = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b",
    re.IGNORECASE,
)
_DAY_MONTH = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b",
    re.IGNORECASE,
)
# "the 17th", "the 3rd" — day-of-month with an ordinal suffix required,
# so a bare amount ("the 200") never reads as a date.
_THE_NTH = re.compile(r"\bthe\s+(\d{1,2})(?:st|nd|rd|th)\b", re.IGNORECASE)
# US numeric "7/17" or "7/17/2026"
_NUMERIC = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")
_WEEKDAY = re.compile(
    r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.IGNORECASE
)


def _in_window(candidate: dt.date, today: dt.date) -> dt.date | None:
    if today <= candidate <= today + dt.timedelta(days=PROMISE_LOOKAHEAD_DAYS):
        return candidate
    return None


def _safe_date(year: int, month: int, day: int) -> dt.date | None:
    if not 1 <= month <= 12:
        return None
    if not 1 <= day <= calendar.monthrange(year, month)[1]:
        return None
    return dt.date(year, month, day)


def _next_month(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def extract_promise_date(text: str, *, today: dt.date) -> dt.date | None:
    """Return the promised payment date in ``text``, or None.

    Requires a payment word somewhere in the text AND a concrete date
    that lands in [today, today + PROMISE_LOOKAHEAD_DAYS]. Checks the
    most explicit forms first so "I'll pay on July 17, Friday at the
    latest" reads as July 17.
    """
    if not text or not _PAY_WORDS.search(text):
        return None

    m = _MONTH_DAY.search(text)
    if m:
        month, day = _MONTHS[m.group(1).lower()], int(m.group(2))
        for year in (today.year, today.year + 1):
            candidate = _safe_date(year, month, day)
            if candidate and candidate >= today:
                return _in_window(candidate, today)
        return None

    m = _DAY_MONTH.search(text)
    if m:
        day, month = int(m.group(1)), _MONTHS[m.group(2).lower()]
        for year in (today.year, today.year + 1):
            candidate = _safe_date(year, month, day)
            if candidate and candidate >= today:
                return _in_window(candidate, today)
        return None

    m = _NUMERIC.search(text)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        year_raw = m.group(3)
        if year_raw:
            year = int(year_raw)
            if year < 100:
                year += 2000
            candidate = _safe_date(year, month, day)
            return _in_window(candidate, today) if candidate else None
        for year in (today.year, today.year + 1):
            candidate = _safe_date(year, month, day)
            if candidate and candidate >= today:
                return _in_window(candidate, today)
        return None

    m = _THE_NTH.search(text)
    if m:
        day = int(m.group(1))
        candidate = _safe_date(today.year, today.month, day)
        if candidate is None or candidate < today:
            year, month = _next_month(today.year, today.month)
            candidate = _safe_date(year, month, day)
        return _in_window(candidate, today) if candidate else None

    lowered = text.lower()
    if re.search(r"\btomorrow\b", lowered):
        return today + dt.timedelta(days=1)
    if re.search(r"\b(today|tonight)\b", lowered):
        return today
    if re.search(r"\bnext week\b", lowered):
        return today + dt.timedelta(days=7)
    if re.search(r"\b(this|the)\s+weekend\b", lowered):
        return today + dt.timedelta(days=(5 - today.weekday()) % 7 or 7)

    m = _WEEKDAY.search(text)
    if m:
        target = _WEEKDAYS[m.group(1).lower()]
        return today + dt.timedelta(days=(target - today.weekday()) % 7 or 7)

    return None


def promise_pending(promised_date: dt.date | None, *, today: dt.date) -> bool:
    """True while a promise still buys quiet: through the promised date
    plus the grace day. A past promise never suppresses — that's the
    follow-up trigger."""
    if promised_date is None:
        return False
    return today <= promised_date + dt.timedelta(days=PROMISE_GRACE_DAYS)


__all__ = [
    "PROMISE_GRACE_DAYS",
    "PROMISE_LOOKAHEAD_DAYS",
    "extract_promise_date",
    "promise_pending",
]
