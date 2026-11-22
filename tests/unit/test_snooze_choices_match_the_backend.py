"""The snooze buttons must send a number the snooze ROUTE will accept.

``SnoozeIn.days`` is ``Field(default=2, gt=0, le=MAX_SNOOZE_DAYS)``. A choice
outside that is a 422 — and nothing in the frontend can see it, because the
constant lives in Python and the buttons live in TypeScript. That is gotcha
28's both-sides problem with a language boundary in the middle, and the failure
is silent in the worst direction: the button renders, the click looks normal,
and the only person who meets the error is a customer.

Python reading a frontend file is the established pattern here rather than a
preference (``test_frontend_api_paths_exist.py``, ``test_favicon_contrast.py``)
— ``lib/`` constants are outside the vitest root's reach for a node-free check,
and importing ``node:fs`` from a frontend test runs green under vitest while
turning ``tsc --noEmit`` red (gotcha 97).

⚠ This pins the RELATION, not the numbers (gotcha 96's rule). Raising the cap
or adding a "1 month" button stays a one-line change; shipping one the server
refuses does not.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from helixis.reminders.aging import MAX_SNOOZE_DAYS

_REPO = Path(__file__).resolve().parents[2]
_TODAY_CARD = _REPO / "frontend" / "src" / "components" / "TodayCard.tsx"
_ROUTE = _REPO / "src" / "helixis" / "api" / "routes" / "reminders.py"

_CHOICES_BLOCK = re.compile(
    r"export const SNOOZE_CHOICES[^=]*=\s*\[(?P<body>.*?)\];",
    re.DOTALL,
)
_DAYS = re.compile(r"days:\s*(?P<days>\d+(?:\.\d+)?)")
#: ``days: float = Field(default=2, gt=0, le=MAX_SNOOZE_DAYS)``
_SNOOZE_DEFAULT = re.compile(r"days:\s*float\s*=\s*Field\(\s*default=(?P<default>\d+(?:\.\d+)?)")


def _snooze_choice_days() -> list[float]:
    """The `days` each button sends, read out of the shipped constant.

    ⚠ Raises rather than returning empty. A scan that finds zero reads exactly
    like a corpus with nothing wrong in it, and this guard's whole job is to
    judge values it can only get by finding them (gotchas 49/100/101/114/128).
    """
    source = _TODAY_CARD.read_text(encoding="utf-8")
    block = _CHOICES_BLOCK.search(source)
    if block is None:
        raise AssertionError(
            "SNOOZE_CHOICES not found in TodayCard.tsx — if it was renamed or moved, "
            "re-point this guard; the relation it pins is still real."
        )
    days = [float(m.group("days")) for m in _DAYS.finditer(block.group("body"))]
    if not days:
        raise AssertionError("SNOOZE_CHOICES parsed to zero entries — the scan is broken.")
    return days


def test_the_scan_finds_the_buttons_the_product_ships() -> None:
    # The canary. Everything below is vacuous if this returns nothing.
    assert len(_snooze_choice_days()) >= 1


@pytest.mark.parametrize("days", _snooze_choice_days())
def test_every_snooze_choice_is_inside_the_backend_cap(days: float) -> None:
    # `gt=0, le=MAX_SNOOZE_DAYS` — both ends, because a 0-day button is as
    # rejected as a 90-day one and would look just as plausible on screen.
    assert days > 0, f"a snooze button sends {days} days; the route requires gt=0"
    assert days <= MAX_SNOOZE_DAYS, (
        f"a snooze button sends {days} days and the route caps at {MAX_SNOOZE_DAYS} "
        "(`SnoozeIn.days`, le=MAX_SNOOZE_DAYS) — that click is a 422 in production"
    )


def test_the_shortest_choice_is_the_products_own_default() -> None:
    """TodayCard's comment claims the 2-day option is not a number picked to
    fill a button. Prose cannot fail (gotcha 48), so the claim is checked."""
    route = _ROUTE.read_text(encoding="utf-8")
    match = _SNOOZE_DEFAULT.search(route)
    assert match is not None, (
        "SnoozeIn.days default not found in the reminders route — re-point this "
        "guard rather than dropping it"
    )
    assert min(_snooze_choice_days()) == float(match.group("default")), (
        "the shortest snooze button no longer matches the route's own default, so "
        "TodayCard's 'NOT invented here' comment has gone stale"
    )
