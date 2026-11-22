"""What time it is where the recipient is — the TCPA window's missing input.

``comms/sms.py`` reads the IANA zone off the consent row. A CRM lead has no
consent row and no stored timezone, so the Leasing lane had no clock at all and
no quiet-hours check: it would text a lead at 3am. The statute is about the
RECIPIENT's local time, so the only signal that is actually about them is their
own number.

**The design, and why it has two halves.**

1. **A known area code gives the real window** — 08:00-21:00 in that zone, the
   same bound the consent lane applies. This is the whole point: an Eastern
   agent texting an Eastern lead should get 8am, not a lowest-common-
   denominator hour.

2. **An unknown area code falls back to the window that is legal EVERYWHERE in
   the contiguous US** — inside 08:00-21:00 in both Eastern and Pacific at
   once, which works out to 11:00-21:00 Eastern. It still sends; it just sends
   in hours that cannot be wrong.

⚠ **The map holds only NPAs whose STATE sits entirely in one zone** (plus two
exclusions below). That is the accuracy decision, and it is the important one:
a split state is exactly where a hand-built table gets an entry wrong, and a
wrong entry here is an illegal text at 6am rather than a missing feature. An
NPA that is absent is not a bug — it degrades to the conservative window, which
is why omission is cheap and a wrong guess is not. **Add an NPA only when its
zone is certain; when in doubt, leave it out.**

The two deliberate exclusions, both large markets where one NPA is the outlier:
Florida is Eastern except **850** (the panhandle straddles), and Texas is
Central except **915** (El Paso is Mountain). Neither is listed, so both fall
through to the safe window rather than being guessed.

⚠ Arizona is ``America/Phoenix`` and Hawaii ``Pacific/Honolulu`` — neither
observes DST, which is the reason to carry IANA zone names rather than UTC
offsets. Offsets are wrong twice a year; the zone database is not.
"""

from __future__ import annotations

import logging
from datetime import datetime
from datetime import time as dtime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

#: The FEDERAL TCPA window, recipient-local (47 CFR § 64.1200(c)(1)).
#: A floor, not the answer — several states are stricter, see ``_WINDOWS``.
QUIET_HOURS_START = dtime(8, 0)
QUIET_HOURS_END = dtime(21, 0)

#: Per-state overrides, each one a different statute. **State law applies by
#: RECIPIENT location, not sender**, so this is keyed on where the number is.
#:
#: ⚠ Federal 8pm-vs-9pm is the trap: the federal window ends at 21:00 and
#: Florida's FTSA, Oklahoma's OTSA and Washington's CEMA all end at 20:00.
#: Shipping the federal number alone is a per-message penalty in three states
#: with a private right of action.
#:
#: ⚠ Texas SB 140 (effective 2025-09-01) is the only one with a WEEKDAY rule:
#: 9am-9pm Mon-Sat, noon-9pm Sunday, at $5,000/violation ($25,000 wilful). It
#: is written for "telephone solicitation", which our four transactional
#: templates arguably are not — but the cost of being wrong about that is four
#: figures per text and the cost of being right is a rent reminder waiting an
#: hour. Not a close call.
_STRICT_EVENING = (dtime(8, 0), dtime(20, 0))
_TEXAS_WEEKDAY = (dtime(9, 0), dtime(21, 0))
_TEXAS_SUNDAY = (dtime(12, 0), dtime(21, 0))

_WINDOWS: dict[str, tuple[dtime, dtime]] = {
    "FL": _STRICT_EVENING,  # FTSA
    "OK": _STRICT_EVENING,  # OTSA
    "WA": _STRICT_EVENING,  # CEMA
    "TX": _TEXAS_WEEKDAY,  # SB 140 — Sunday handled separately
}

#: When the recipient's STATE is unknown, every rule above must hold at once —
#: the intersection, not the federal default. Same posture as the unknown-zone
#: fallback below: a guard that cannot see must take the strictest reading
#: (gotcha 55), because the alternative is an illegal 6am text somebody sues
#: over rather than a message that arrives an hour late.
_UNKNOWN_WEEKDAY = (dtime(9, 0), dtime(20, 0))
_UNKNOWN_SUNDAY = (dtime(12, 0), dtime(20, 0))

#: When the recipient's zone is unknown, the send must be inside the window in
#: EVERY contiguous-US zone at once. Eastern and Pacific are the extremes, so
#: satisfying both satisfies Central and Mountain in between.
_FALLBACK_ZONES = ("America/New_York", "America/Los_Angeles")

_EASTERN = "America/New_York"
_CENTRAL = "America/Chicago"
_MOUNTAIN = "America/Denver"
_ARIZONA = "America/Phoenix"
_PACIFIC = "America/Los_Angeles"
_HAWAII = "Pacific/Honolulu"

#: NPA -> IANA zone, built state by state from single-zone states only.
#: Grouped by state so an entry can be checked against something a reader
#: already knows; a flat list of 300 numbers cannot be reviewed.
_NPA_BY_STATE: dict[str, tuple[str, str]] = {
    # ⚠ NPAs are a whitespace-separated STRING, not a tuple of ints, purely so
    # `ruff format` leaves them alone: as a tuple it reflows to one number per
    # line and a 37-entry state becomes 37 lines nobody will ever read through.
    # A table you cannot review is a table with a wrong entry in it.
    # ── Eastern ──────────────────────────────────────────────────────
    "CT": (_EASTERN, "203 475 860 959"),
    "DC": (_EASTERN, "202"),
    "DE": (_EASTERN, "302"),
    "GA": (_EASTERN, "229 404 470 478 678 706 762 770 912 943"),
    "MA": (_EASTERN, "339 351 413 508 617 774 781 857 978"),
    "MD": (_EASTERN, "240 301 410 443 667"),
    "ME": (_EASTERN, "207"),
    "NC": (_EASTERN, "252 336 704 743 828 910 919 980 984"),
    "NH": (_EASTERN, "603"),
    "NJ": (_EASTERN, "201 551 609 640 732 848 856 862 908 973"),
    "NY": (_EASTERN, "212 315 332 347 516 518 585 607 631 646 680 716 718 838 845 914 917 929 934"),
    "OH": (_EASTERN, "216 220 234 326 330 380 419 440 513 567 614 740 937"),
    "PA": (_EASTERN, "215 223 267 272 412 445 484 570 582 610 717 724 814 878"),
    "RI": (_EASTERN, "401"),
    "SC": (_EASTERN, "803 839 843 854 864"),
    "VA": (_EASTERN, "276 434 540 571 703 757 804 826 948"),
    "VT": (_EASTERN, "802"),
    "WV": (_EASTERN, "304 681"),
    # ⚠ Florida minus 850 — the panhandle straddles Eastern and Central, so
    # that one NPA is deliberately absent and falls back to the safe window.
    "FL": (_EASTERN, "239 305 321 352 386 407 561 656 689 727 754 772 786 813 863 904 941 954"),
    # ── Central ──────────────────────────────────────────────────────
    "AL": (_CENTRAL, "205 251 256 334 659 938"),
    "AR": (_CENTRAL, "479 501 870"),
    "IA": (_CENTRAL, "319 515 563 641 712"),
    "IL": (_CENTRAL, "217 224 309 312 331 447 464 618 630 708 730 773 779 815 847 872"),
    "LA": (_CENTRAL, "225 318 337 504 985"),
    "MN": (_CENTRAL, "218 320 507 612 651 763 952"),
    "MO": (_CENTRAL, "314 417 557 573 636 660 816"),
    "MS": (_CENTRAL, "228 601 662 769"),
    "OK": (_CENTRAL, "405 539 572 580 918"),
    "WI": (_CENTRAL, "262 274 414 534 608 715 920"),
    # ⚠ Texas minus 915 — El Paso is Mountain. Absent, so it falls back.
    "TX": (
        _CENTRAL,
        "210 214 254 281 325 346 361 409 430 432 469 512 682 713 726 737 806 "
        "817 830 832 903 936 940 945 956 972 979",
    ),
    # ── Mountain ─────────────────────────────────────────────────────
    "CO": (_MOUNTAIN, "303 719 720 970 983"),
    "MT": (_MOUNTAIN, "406"),
    "NM": (_MOUNTAIN, "505 575"),
    "UT": (_MOUNTAIN, "385 435 801"),
    "WY": (_MOUNTAIN, "307"),
    # Arizona keeps Mountain STANDARD time all year — a different ZONE, not a
    # different offset, which is why this table carries IANA names.
    "AZ": (_ARIZONA, "480 520 602 623 928"),
    # ── Pacific ──────────────────────────────────────────────────────
    "CA": (
        _PACIFIC,
        "209 213 279 310 323 341 350 408 415 424 442 510 530 559 562 619 626 "
        "628 650 657 661 669 707 714 747 760 805 818 820 831 840 858 909 916 "
        "925 949 951",
    ),
    "WA": (_PACIFIC, "206 253 360 425 509 564"),
    # ── Hawaii ───────────────────────────────────────────────────────
    "HI": (_HAWAII, "808"),
}


#: Flattened once at import, FROM the grouped table so the two cannot
#: disagree. ⚠ A duplicate NPA would be silently swallowed here (last one
#: wins), so `test_local_time.py` counts the flattened map against the sum of
#: the groups — a number that appears in two states is a test failure, not a
#: coin flip about which zone somebody gets.
NPA_ZONES: dict[str, str] = {
    npa: zone for zone, npas in _NPA_BY_STATE.values() for npa in npas.split()
}

#: NPA -> two-letter state, from the SAME table, so a number cannot resolve to
#: one state's zone and another state's statute. The state is what selects the
#: window (``_WINDOWS``); the zone is only what clock it is read on.
NPA_STATES: dict[str, str] = {
    npa: state for state, (_zone, npas) in _NPA_BY_STATE.items() for npa in npas.split()
}


def area_code(phone: str) -> str:
    """The NPA of a NANP number in E.164, or ``''``.

    Only ``+1`` is recognised: an international number's leading digits are
    not an area code, and reading them as one would look up a US timezone for
    somebody in another country.
    """
    digits = (phone or "").strip()
    if not digits.startswith("+1"):
        return ""
    rest = digits[2:]
    return rest[:3] if len(rest) == 10 and rest.isdigit() else ""


def is_nanp(phone: str) -> bool:
    """Is this a North American number — the only kind this product can text?

    ⚠ This is a REACHABILITY question, and it is separate from every other
    guard here. The A2P 10DLC campaign is a US programme, the registered
    consent language and samples are US, and Twilio's geo permissions are off
    for other regions by default. So a ``+44`` number is not "somebody we are
    allowed to text at the wrong hour" — it is somebody this product cannot
    text at all.

    ⚠ Deliberately NOT applied to `comms/phone.py`'s normalisation or its
    match key. An inbound text from abroad must still be logged and its STOP
    still honoured; consent governs what we may SEND, not whether we read what
    somebody chose to send us. Only the outbound paths restrict.

    ``+1`` covers Canada as well as the US. The NPA table here is US-only, so
    a Canadian number gets the conservative unknown-state window rather than a
    wrong one — which is the right answer for a number in the same set of
    zones.
    """
    digits = (phone or "").strip()
    return digits.startswith("+1") and digits[2:].isdigit() and len(digits[2:]) == 10


def zone_for(phone: str) -> str | None:
    """The recipient's IANA zone, or ``None`` when we do not know it.

    ``None`` is a real answer and the caller must handle it — it means "use the
    conservative window", not "assume Eastern".
    """
    return NPA_ZONES.get(area_code(phone))


def window_for(state: str | None, is_sunday: bool) -> tuple[dtime, dtime]:
    """The legal window for a recipient in ``state`` on this day.

    ``None`` means we could not place the number, and takes the intersection
    of every rule rather than the federal default.
    """
    if state is None:
        return _UNKNOWN_SUNDAY if is_sunday else _UNKNOWN_WEEKDAY
    if state == "TX":
        return _TEXAS_SUNDAY if is_sunday else _TEXAS_WEEKDAY
    return _WINDOWS.get(state, (QUIET_HOURS_START, QUIET_HOURS_END))


def describe_window(phone: str, now_utc: datetime, *, zone: str | None = None) -> str:
    """The legal window for this recipient, right now, as human text.

    ⚠ Exists so a REFUSAL can quote the window that was actually applied.
    ``agents/sms_tools`` hardcoded "8:00 AM-9:00 PM" — the pre-2026-09-01
    federal default — and went on saying it after the window became
    state-aware, so a Texan refused at 08:30 was told the rule was 8:00 and a
    Floridian refused at 20:30 was told 9:00. Both wrong, in the direction that
    makes the product look broken rather than careful.

    Derives state and day exactly as ``_inside`` does, so the sentence and the
    decision cannot disagree (gotcha 25: the rule lives once).
    """
    state = NPA_STATES.get(area_code(phone))
    resolved = zone or zone_for(phone)
    is_sunday = False
    if resolved:
        try:
            is_sunday = now_utc.astimezone(ZoneInfo(resolved)).weekday() == 6
        except Exception:  # noqa: BLE001 — describing must never raise
            is_sunday = False
    start, end = window_for(state, is_sunday)

    def _clock(t: dtime) -> str:
        hour = t.hour % 12 or 12
        suffix = "AM" if t.hour < 12 else "PM"
        return f"{hour}:{t.minute:02d} {suffix}"

    return f"{_clock(start)}-{_clock(end)}"


def _inside(now_utc: datetime, zone: str, state: str | None) -> bool:
    try:
        local = now_utc.astimezone(ZoneInfo(zone))
    except Exception:  # noqa: BLE001 — a missing tzdata entry must not send
        logger.warning("unresolvable timezone %r — failing closed", zone)
        return False
    # ⚠ The weekday is read on the RECIPIENT's clock, never UTC. Saturday
    # 23:30 Pacific is already Sunday in UTC, so reading the day there applies
    # the noon rule hours early — and misses it going the other way.
    start, end = window_for(state, local.weekday() == 6)
    return start <= local.time() < end


def within_quiet_hours(phone: str, now_utc: datetime, *, zone: str | None = None) -> bool:
    """May we text this number right now, on the recipient-local clock.

    ⚠ The ONE window rule in this product — ``comms/sms.py`` calls it too,
    passing the consent row's stored zone. Two copies of a statute drift, and
    the drift is invisible until somebody sues (gotcha 25: the rule lives once).

    ``zone`` overrides the NPA-derived guess when the caller holds something
    better; a consent row records the recipient's actual zone. The STATE still
    comes from the area code either way, because a zone cannot identify one —
    ``America/Chicago`` covers both Texas and Illinois and only one of them has
    a Sunday rule.
    """
    state = NPA_STATES.get(area_code(phone))
    resolved = zone or zone_for(phone)
    if resolved is not None:
        return _inside(now_utc, resolved, state)
    # ⚠ FAIL CLOSED for a number we cannot place at all. The fallback below
    # asks "is it inside 9-20 in every CONTIGUOUS US zone", which is the
    # strictest reading available *for an American number* and means nothing
    # for anyone else: a UK recipient judged that way can legally be texted at
    # 01:00 London. `zone_for` returns None for both cases and only this
    # distinguishes them.
    #
    # A caller holding a real zone still wins on the branch above — a consent
    # row recording `Europe/London` for a London number is better information
    # than the number's country code and is honoured.
    if not is_nanp(phone):
        logger.warning(
            "quiet-hours check on a non-NANP number — failing closed; "
            "this product's A2P campaign covers North America only"
        )
        return False
    return all(_inside(now_utc, z, state) for z in _FALLBACK_ZONES)


__all__ = [
    "NPA_STATES",
    "NPA_ZONES",
    "QUIET_HOURS_END",
    "QUIET_HOURS_START",
    "area_code",
    "is_nanp",
    "window_for",
    "within_quiet_hours",
    "zone_for",
]
