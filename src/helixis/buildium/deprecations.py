"""Buildium surfaces with a KNOWN retirement date, in one place.

WHY THIS MODULE EXISTS
----------------------
Buildium announced that its appliance endpoints retire 2026-10-19. That fact
was recorded in three places — a comment in ``buildium/api.py``, a
`⏳ tracked` row in ``docs/compliance/buildium-api.md``, and a follow-up item
in the same file — and **not one of them can fail**. A comment cannot fail
(gotcha 48); a table cell that says "tracked" tracks nothing. So the whole
mechanism for not being surprised was somebody happening to re-read a comment
in the right month.

The date lives here now, and ``tests/unit/test_buildium_deprecations.py``
turns it into a test that goes RED before the endpoints start 404ing. That is
the difference between a note and an alarm.

⚠ **This is a REGISTRY, not a policy.** Nothing here disables a tool or
changes a request. The registry's only job is to make the deadline arrive
loudly; nothing in it may retire a surface on its own, because doing so would
break a working feature ahead of the vendor and would break it permanently if
the vendor later moved the date.
⚠ That is a rule about what this MODULE does, not about what a maintainer may
decide. Taking a capability out before the vendor's date is a legitimate
answer to an entry — it is one of the two outcomes every ``action`` names —
and it is how the appliance entry was closed. What the rule forbids is a
retirement nobody chose.

⚠ **Ground a retirement date on Buildium's own announcement**, never on an
SDK's deprecation warning or a docs page's tone — the same rule as every other
Buildium fact in this repo (gotchas 15/24/25). Each entry names where its date
came from.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Deprecation:
    """One Buildium surface with an announced end-of-life."""

    #: The attribute on ``BuildiumApi`` this covers — e.g. ``"appliances"``.
    surface: str

    #: The day the endpoints stop working, per Buildium's announcement.
    retires_on: date

    #: Where the date came from. A retirement date with no provenance is the
    #: expired-justification shape gotcha 48 records.
    source: str

    #: What a maintainer has to DO. The failure message prints this, because a
    #: red test that does not say the next action just gets muted.
    action: str

    #: Agent tools that stop working on the date. Pinned against the live
    #: registry by the guard, so renaming a tool cannot silently orphan the
    #: entry and leave the alarm pointing at nothing.
    tools: tuple[str, ...]


#: How long before a retirement the guard starts failing.
#:
#: ⚠ Thirty days is chosen so the alarm lands while there is still room to do
#: the work, not during the week it breaks. It is deliberately NOT a warning:
#: CI output nobody has to act on is read exactly as often as the comment this
#: replaces.
WARNING_DAYS = 30

#: Buildium surfaces with an announced end-of-life that this product still
#: exposes. **Empty is the correct steady state**, not a sign the mechanism
#: rotted.
#:
#: ⚠ The one entry this registry has ever held — ``appliances``, retiring
#: 2026-10-19 — was CLOSED on 2026-09-22 by taking the capability out rather
#: than migrating it: ``BuildiumApi.appliances``, the ``Appliances`` endpoint
#: wrapper, ``RentalAppliance`` and the ``buildium_list_appliances`` tool are
#: all gone. That is the acknowledgement this module's header asks for — the
#: entry goes when the work is done, and the work was "retire it", which the
#: entry's own ``action`` named as one of the two valid outcomes.
#:
#: Adding the next one is the whole job: a surface, a date with its source, an
#: action a maintainer can carry out, and the tools that stop working. The
#: guard turns it into a failing test thirty days out.
DEPRECATIONS: tuple[Deprecation, ...] = ()


def due(today: date, *, within_days: int = WARNING_DAYS) -> list[Deprecation]:
    """Entries retiring within ``within_days`` — including already-past ones.

    ⚠ Past retirements STAY in the list rather than ageing out. An endpoint
    that retired last month is more urgent than one retiring next month, and a
    window that only looks forward would go quiet on the day the thing actually
    broke — the failure mode this whole module exists to prevent.
    """
    return [d for d in DEPRECATIONS if (d.retires_on - today).days <= within_days]


def days_remaining(entry: Deprecation, today: date) -> int:
    """Negative once the date has passed."""
    return (entry.retires_on - today).days


def retired_surface(surface: str, today: date | None = None) -> Deprecation | None:
    """The entry for ``surface`` if its date has PASSED, else ``None``.

    ⚠ ``today`` is an INJECTABLE FALLBACK — the same shape as the three other
    server-clock exemptions in ``test_company_clock``, and it lives here rather
    than at the caller on purpose. The alternative was ``date.today()`` inside
    ``agents/buildium_tools.py``, and that allowlist is FILE-scoped: exempting a
    7,000-line tool module would silently licence every future zone-less clock
    read in it (gotchas 118/122/123's granularity rule). The registry owns the
    date question; a caller asks whether a surface is retired and needs no clock.

    ⚠ This is the one thing here a request path may act on, and it is
    deliberately REACTIVE: a caller consults it only AFTER a call has already
    failed. Refusing to call an endpoint because a date arrived would retire it
    on our side ahead of the vendor — the exact thing this module's header
    forbids — and would break the feature early if Buildium extends the date.
    That contract is also what makes the server clock safe here: a few hours of
    zone slack changes the WORDING of a failure and can never change an answer,
    because there is no answer to change.

    ⚠ **NO PRODUCTION CALLER TODAY, and that is a consequence of the registry
    being empty rather than a sign this is dead weight.** Its one caller was
    the appliance tool, removed with the surface on 2026-09-22. It survives
    because it is the REACTIVE half of the mechanism: the day somebody
    registers the next retirement, the tool reading that surface needs exactly
    this, and the reasoning below is what a fresh author would otherwise have
    to rediscover after shipping the defect once. Tests drive it against a
    synthetic entry, so it is covered rather than merely present.

    WHY A CALLER NEEDS IT. When the endpoints go, the tool's failure must not
    read as an empty portfolio. ``_sanitize_buildium_error``'s 404 branch
    deliberately omits the not-an-absence guard, because for a RECORD lookup
    absence is the correct reading — "that task isn't in Buildium" is a fact.
    For a retired SURFACE it is not: nothing was named, the endpoint itself is
    gone, and "I couldn't find that" is one short step from "you have none of
    those", which is this product's worst failure mode (gotcha 26) aimed at a
    customer's own property. The pattern for a caller is: catch
    ``BuildiumApiError``, ask ``retired_surface(<surface>)``, and when it
    answers, say the endpoint is retired and append ``_NOT_AN_ABSENCE``.

    ⚠ Whether Buildium answers 404 or 410 is NOT settled here — the vendor's
    docs are egress-blocked from this container, and the two statuses take
    different branches (410 falls through to the generic one, which already
    carries the guard; 404 does not). So the caller keys on the DATE rather
    than on a status, which is correct under either.
    """
    on = today or date.today()
    for entry in DEPRECATIONS:
        if entry.surface == surface and (entry.retires_on - on).days <= 0:
            return entry
    return None


__all__ = [
    "DEPRECATIONS",
    "WARNING_DAYS",
    "Deprecation",
    "days_remaining",
    "due",
    "retired_surface",
]
