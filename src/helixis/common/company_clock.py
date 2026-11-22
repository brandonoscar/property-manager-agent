"""One answer to "what day is it for this company".

The product had three, and only one of them was about the customer. This module
is the single home, in the sense gotcha 25 means: `buildium/money.py::bill_amount`
is the precedent — one rule, read by every layer, so two layers cannot drift
into disagreeing about a number a user will act on.

⚠ **The server clock is not any customer's calendar.** No `TZ` is set in
`render.yaml`, the Dockerfile or any workflow, so `date.today()` in the deployed
container is the UTC date. Measured hours per day on which that is not the
manager's date: New York 4, Chicago 5, Denver 6, Los Angeles 7, Anchorage 8,
Honolulu 10 — 17% to 42% of every single day, forever, not an edge case.

⚠ **Which is why "it's only a day" is the wrong reading.** A bill due today
reported as OVERDUE is a false claim about money (gotcha 26's class, pointed at
the thing a property manager acts on first), a severity that flips a day early
cries wolf (gotcha 52), and a statutory deposit deadline computed on the wrong
day is the one in this set with a penalty attached.

⚠ **A bad zone costs the customisation, never the sweep.** `today_in` falls back
to the default rather than raising: a company that somehow stored junk still
gets cards, on the same date it got before this module existed. The API is where
a bad zone is refused, at the point a human can fix it.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

#: The one default, and it is deliberately the value `agents/clock_prompt` was
#: already hardcoding. Migration 0098 gives every existing company this same
#: string, so the day it applies nothing anybody sees changes — what changes is
#: that the reminder producers stop disagreeing with the agent's prompt.
#:
#: ⚠ `clock_prompt` imports this rather than keeping its own copy. Two literals
#: spelling the same zone is how they drift by one character and the product
#: quietly holds two calendars again (gotcha 73's `"approved"` in three modules).
DEFAULT_TIMEZONE = "America/New_York"


def resolve_zone(tz_name: str | None) -> tzinfo:
    """The IANA zone, or the default when it cannot be loaded.

    ⚠ Never raises. `ZoneInfo` throws three different exception types for a
    missing zone depending on how it fails, and a sweep that dies because one
    company stored a typo would take out every OTHER company's cards in the
    same cycle — the failure would be far larger than the defect.
    """
    for candidate in (tz_name, DEFAULT_TIMEZONE):
        if not candidate:
            continue
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError, KeyError, OSError):
            if candidate != DEFAULT_TIMEZONE:
                logger.warning(
                    "unusable company timezone %r — falling back to %s", candidate, DEFAULT_TIMEZONE
                )
    # Only reachable if the tzdata package itself is missing, which would also
    # break comms/local_time. UTC keeps dates being produced.
    logger.warning("no IANA zone database available — dates will be computed in UTC")
    return UTC


def today_in(tz_name: str | None, *, now: datetime | None = None) -> date:
    """Today's date as this company's calendar reads it.

    ``now`` is injectable ON PURPOSE, for `clock_prompt`'s reason: a test that
    computes the answer the same way the code does proves only that two
    identical expressions agree, and passes just as happily against a function
    that ignores the zone entirely. Pinning a FIXED instant is what makes the
    assertion mean something.
    """
    instant = now or datetime.now(UTC)
    if instant.tzinfo is None:
        # A naive datetime is the very bug this module exists to remove, so it
        # is read as UTC rather than as the server's local clock — `astimezone`
        # on a naive value would silently assume local time and reintroduce it.
        instant = instant.replace(tzinfo=UTC)
    return instant.astimezone(resolve_zone(tz_name)).date()


async def company_timezone(pool: Any, company_id: str) -> str:
    """This company's IANA zone, or the default when it cannot be read.

    ⚠ Unreadable is NOT an error here, and that is the opposite of the posture
    gotchas 55/72/89 take on a consent ledger or a role floor. Those refuse
    because acting without the fact is the harm. Here refusing IS the harm: the
    fallback is the exact date the product produced before this column existed,
    so a database blip costs a customisation, while raising would cost the
    company its whole Inbox sweep.
    """
    try:
        # Deliberately company-scoped (gotcha 47) — it reads exactly one row.
        row = await pool.fetchrow(
            "SELECT timezone FROM public.companies WHERE id = $1::uuid", company_id
        )
    except (Exception, SystemExit):
        logger.exception(
            "could not read timezone for company=%s — using %s", company_id, DEFAULT_TIMEZONE
        )
        return DEFAULT_TIMEZONE
    zone = (row or {}).get("timezone") if row is not None else None
    return str(zone) if zone else DEFAULT_TIMEZONE


__all__ = ["DEFAULT_TIMEZONE", "company_timezone", "resolve_zone", "today_in"]
