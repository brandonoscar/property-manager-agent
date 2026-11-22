"""How long a property's public-data intel is good for.

`helixis_property_intel` caches answers from FEMA, USFS, EPA and HUD against a
property's coordinates. It was written EXACTLY ONCE per property and never
again: `intel_backfill._PENDING_SQL`'s work-list sentinel is *"this property
has no flood row"*, so the moment one exists the property leaves the list
forever. `fetched_at` was written by the upsert and read by nothing — the
column that would have implemented a refresh, sitting unused beside the pass
that should have used it. Third instance of that exact shape in three days
(gotcha 88's `handled_at`, gotcha 92's partial buffer, this).

MEASURED on helixis-test before any of this existed (2026-09-10, deliberately
cross-tenant — a refresh cadence is a property of the product, not of one
company's books): **109 rows across two sources, every one stamped
2026-08-17..08-21**, nothing re-fetched in the twenty days since, and by
construction nothing ever would be.

⚠ **STALE DATA IS STILL THE BEST ANSWER AVAILABLE — this module dates it, it
does not suppress it.** A flood zone from fourteen months ago beats no flood
zone, and hiding it would be a fabricated absence (gotchas 26/55/57). The
failure being fixed is a confident answer with no age on it, not the answer.

⚠ **The re-crawl cadence is COMPUTED from these values, never typed twice.**
`intel_backfill` re-queries every source for a property in one pass, so the
crawl has to run at the shortest of them or the sharpest source ages past its
window while a longer-lived one holds the property off the work-list. Deriving
it means adding a shorter-lived source automatically tightens the crawl — the
`maintenance_status.OPEN_STATUSES` precedent (gotcha 50).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

#: Per-source refresh windows.
#:
#: ⚠ These are CHOSEN, conservatively, and only one is derived from a
#: publication schedule — saying which is which matters, because a number
#: presented as a vendor's cadence when it was picked by us is the kind of
#: claim that gets believed and then built on (gotcha 47).
#:
#: * ``fmr`` — HUD publishes Fair Market Rents **annually, effective October
#:   1**. That is the one real schedule here, and it is also the sharpest
#:   source: a stale FMR is a wrong number about money, and it goes wrong on a
#:   known date. Set well inside the year so a property is re-crawled before
#:   the figure it holds is superseded rather than after.
#: * ``flood`` — FEMA revises maps continuously through Letters of Map
#:   Revision; there is no single release date to key on, so this is a chosen
#:   annual sweep.
#: * ``wildfire`` / ``superfund`` — periodic republication with no fixed
#:   calendar. Chosen, not derived.
#: * ``radon`` — the EPA zone map is a static 1993 product and our copy is a
#:   BUNDLED table (`intel._radon_zone_table`), so the value changes when this
#:   repo ships a new table, not when a server does. The long window says that
#:   out loud rather than pretending a fetch could learn something new.
REFRESH_AFTER: dict[str, timedelta] = {
    "fmr": timedelta(days=180),
    "flood": timedelta(days=365),
    "wildfire": timedelta(days=365),
    "superfund": timedelta(days=180),
    "radon": timedelta(days=1095),
}

#: What an unrecognised source gets. Deliberately the SHORTEST window rather
#: than the longest: a source nobody has classified is one nobody has reasoned
#: about, and re-fetching it too often costs an HTTP call while trusting it too
#: long costs a wrong answer about a hazard.
DEFAULT_REFRESH_AFTER = min(REFRESH_AFTER.values())


def refresh_after(source: str) -> timedelta:
    return REFRESH_AFTER.get(source, DEFAULT_REFRESH_AFTER)


#: How often a property is re-crawled. One pass re-queries every source, so
#: this is the shortest window of the set — COMPUTED, so it cannot drift from
#: the table above.
RECRAWL_AFTER = min(REFRESH_AFTER.values())


def is_stale(source: str, fetched_at: datetime | None, *, now: datetime | None = None) -> bool:
    """Is this row past the window its source was given?

    ⚠ A missing timestamp counts as STALE, and that direction is deliberate.
    A row we cannot date is one we cannot vouch for, and the cost of being
    wrong is one re-fetch of public data — against a hazard answer presented
    as current when nothing knows how old it is. The opposite of
    `company_clock`'s fail-open, because here refusing to act is the harm
    rather than the safe default.
    """
    if fetched_at is None:
        return True
    instant = fetched_at if fetched_at.tzinfo else fetched_at.replace(tzinfo=UTC)
    return (now or datetime.now(UTC)) - instant > refresh_after(source)


def stale_sources(rows: dict[str, datetime | None], *, now: datetime | None = None) -> list[str]:
    """Which of these sources are due a refresh, in a stable order.

    Sorted so the sentence a caller builds from it does not reorder itself
    between two reads of the same unchanged data — a footer that reshuffles
    reads as new information.
    """
    return sorted(s for s, at in rows.items() if is_stale(s, at, now=now))


def describe_age(fetched_at: datetime | None, *, now: datetime | None = None) -> str:
    """Age in the unit a reader can act on.

    ⚠ **Days, never hours.** `mirror_tools._as_of_line` renders everything past
    two hours as `f"{minutes // 60}h ago"`, which is right for the Buildium
    mirror (webhooks, seconds to a day) and wrong here by three orders of
    magnitude: measured against the live table, a twenty-day-old FEMA row
    rendered **"updated 552h ago"** and a year-old one would read "9600h ago".
    Nobody reads 9600h as "over a year", so the number was technically true and
    functionally a way of not saying it.
    """
    if fetched_at is None:
        return "at an unrecorded time"
    instant = fetched_at if fetched_at.tzinfo else fetched_at.replace(tzinfo=UTC)
    days = max(0, ((now or datetime.now(UTC)) - instant).days)
    if days == 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 60:
        return f"{days} days ago"
    months = days // 30
    if months < 24:
        return f"about {months} months ago"
    return f"about {days // 365} years ago"


__all__ = [
    "DEFAULT_REFRESH_AFTER",
    "RECRAWL_AFTER",
    "REFRESH_AFTER",
    "describe_age",
    "is_stale",
    "refresh_after",
    "stale_sources",
]
