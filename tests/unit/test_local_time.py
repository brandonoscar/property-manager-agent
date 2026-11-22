"""The recipient's local clock — the input the Leasing lane never had.

``send_sms`` reads an IANA zone off the consent row. A CRM lead has no consent
row, so the Leasing lane had no clock and no quiet-hours check at all: it would
text a lead at 3am, which is a per-message statutory penalty.

The two properties that matter here are opposites, and a test of only one is
how this goes wrong:

* a KNOWN area code must give that recipient their real 08:00-21:00 — the
  whole point, otherwise every customer is stuck with the safest hour;
* an UNKNOWN area code must never be guessed — it falls back to the window
  that holds in every contiguous-US zone at once.
"""

from __future__ import annotations

from datetime import UTC, datetime
from datetime import time as dtime
from zoneinfo import ZoneInfo

import pytest

from helixis.comms.local_time import (
    _NPA_BY_STATE,
    NPA_STATES,
    NPA_ZONES,
    area_code,
    is_nanp,
    window_for,
    within_quiet_hours,
    zone_for,
)


def _at(hour: int, minute: int = 0, *, zone: str) -> datetime:
    """A UTC instant that reads as ``hour`` in ``zone``. Fixed date (a
    Wednesday in June) so DST is unambiguous rather than whatever today is."""
    local = datetime(2026, 6, 17, hour, minute, tzinfo=ZoneInfo(zone))
    return local.astimezone(UTC)


# ── the table itself ──────────────────────────────────────────────────


def test_no_area_code_appears_in_two_zones() -> None:
    """⚠ The flattening is a dict comprehension, so a duplicate NPA is
    swallowed silently and whichever state came last wins. That is a coin flip
    about somebody's legal texting hours, decided by dict ordering."""
    grouped = sum(len(npas.split()) for _zone, npas in _NPA_BY_STATE.values())
    assert len(NPA_ZONES) == grouped, (
        "an area code is listed under two states — the flattened map silently kept only one of them"
    )


def test_every_entry_is_a_plausible_npa() -> None:
    """Three digits, never starting 0 or 1 (the NANP forbids it). A typo like
    a two-digit or four-digit entry would simply never match, so the map would
    look right and quietly stop covering that state."""
    for npa in NPA_ZONES:
        assert len(npa) == 3 and npa.isdigit() and npa[0] not in "01", npa


def test_every_zone_name_resolves() -> None:
    """A typo'd zone fails CLOSED at runtime (nobody in that state can be
    texted, ever) and nothing else would surface it."""
    for zone in set(NPA_ZONES.values()):
        assert ZoneInfo(zone)


def test_the_two_deliberate_exclusions_are_absent() -> None:
    """⚠ Florida's 850 straddles Eastern/Central and Texas's 915 is Mountain
    in an otherwise-Central state. Both are LEFT OUT so they fall back to the
    safe window; adding either "to be complete" reintroduces the exact class
    of error the whole-state rule exists to avoid."""
    assert "850" not in NPA_ZONES
    assert "915" not in NPA_ZONES
    # And their neighbours ARE present, so the exclusion is surgical rather
    # than the whole state having been forgotten.
    assert NPA_ZONES["305"] == "America/New_York"
    assert NPA_ZONES["512"] == "America/Chicago"


# ── reading the number ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("phone", "expected"),
    [
        ("+12125551234", "212"),
        ("+15125550143", "512"),
        ("2125551234", ""),  # not E.164 — we do not guess
        ("+442071234567", ""),  # ⚠ UK. "442" is not a US area code.
        # ⚠ France, and the ONE that actually pins the rule. A naive
        # "drop the country digit, take three" yields "312" here — Chicago —
        # so a Paris number would be judged on a Central-time clock. The UK
        # example above happens to be a digit longer and survives that bug by
        # accident, which is exactly how a length check gets mistaken for a
        # country check.
        ("+33123456789", ""),
        ("+1212555123", ""),  # too short
        ("", ""),
    ],
)
def test_area_code_reads_only_nanp_e164(phone: str, expected: str) -> None:
    """⚠ **The +33 case is the one that matters, and +44 is the trap.**

    Slicing three digits off an international number yields something that may
    well BE a US area code, and then we judge somebody in Paris on a Chicago
    clock. But `+44 20 7123 4567` is one digit too long for that bug to show:
    the naive version returns `""` for it too, for the wrong reason. A battery
    built only on the UK number passes against the defect.

    This is the general shape, not a detail of this function: a negative case
    that rejects the input on a length check proves nothing about a rule that
    is supposed to be about the COUNTRY CODE. Found by mutation, not review.
    """
    assert area_code(phone) == expected


# ── the window ────────────────────────────────────────────────────────


def test_a_known_area_code_gets_its_own_eight_to_nine() -> None:
    """The point of the table. An Eastern lead is reachable at 8am Eastern,
    not at whatever hour is safe for Hawaii."""
    ny = "+12125551234"
    assert not within_quiet_hours(ny, _at(7, 59, zone="America/New_York"))
    assert within_quiet_hours(ny, _at(8, 0, zone="America/New_York"))
    assert within_quiet_hours(ny, _at(20, 59, zone="America/New_York"))
    assert not within_quiet_hours(ny, _at(21, 0, zone="America/New_York"))


def test_a_pacific_number_is_judged_on_pacific_time() -> None:
    """8am Pacific is 11am Eastern. Reading the sender's clock instead of the
    recipient's is the bug this module exists to prevent, in both directions:
    an 8am Pacific send must be allowed, and a 7am Pacific one must not."""
    la = "+14155551234"
    assert within_quiet_hours(la, _at(8, 0, zone="America/Los_Angeles"))
    assert not within_quiet_hours(la, _at(7, 30, zone="America/Los_Angeles"))
    # …and the same instant is fine for a New York number.
    assert within_quiet_hours("+12125551234", _at(7, 30, zone="America/Los_Angeles"))


def test_arizona_does_not_move_with_daylight_saving() -> None:
    """⚠ Why the table carries IANA names and not UTC offsets. In June,
    Phoenix is an hour behind Denver; an offset written down in winter would
    be wrong for half the year, silently."""
    phoenix = "+16025551234"
    june = datetime(2026, 6, 17, 14, 30, tzinfo=UTC)  # 07:30 MST, 08:30 MDT
    assert not within_quiet_hours(phoenix, june), "Arizona was given Denver's clock"
    assert within_quiet_hours("+13035551234", june), "Denver lost its DST hour"


def test_an_unknown_area_code_uses_the_window_that_holds_everywhere() -> None:
    """⚠ Never guessed, on TWO axes at once. 850 (the Florida panhandle) is
    deliberately unmapped, so neither its zone NOR its state is known — it must
    satisfy every statute (9:00-20:00, the intersection) in Eastern AND Pacific
    simultaneously. That works out to 12:00-20:00 Eastern.

    ⚠ This window is TIGHTER than it was: the old code applied the federal
    8am-9pm everywhere, which is a per-message penalty in the four states with
    a stricter rule — and an unplaceable number cannot rule any of them out.
    """
    unknown = "+18505551234"
    assert not within_quiet_hours(unknown, _at(9, 0, zone="America/New_York")), (
        "sent at 9am Eastern to an unmapped number — 6am on the west coast"
    )
    assert not within_quiet_hours(unknown, _at(11, 0, zone="America/New_York")), (
        "11am Eastern is 8am Pacific, before the strictest start"
    )
    assert within_quiet_hours(unknown, _at(12, 0, zone="America/New_York"))
    assert within_quiet_hours(unknown, _at(19, 59, zone="America/New_York"))
    assert not within_quiet_hours(unknown, _at(20, 30, zone="America/New_York")), (
        "20:30 is legal federally and an FTSA/OTSA/CEMA violation — an "
        "unplaceable number cannot prove it is in none of those states"
    )


def test_a_non_north_american_number_is_refused_outright() -> None:
    """CORRECTED 2026-09-09. This test used to assert the opposite, and its
    reasoning — "hours that cannot be wrong" — was true of an unmapped US
    number and false of this one.

    The fallback asks "is it inside 9-20 in every CONTIGUOUS US zone", which
    is the strictest reading available *for an American*. Applied to London it
    means nothing: Eastern is the binding constraint at the top of the window,
    so 19:59 Eastern is 00:59 BST and the old code called that a legal hour to
    text somebody in the UK.

    Refusing is also the honest product answer rather than merely the safe
    one: the A2P 10DLC campaign is a US programme and Twilio's geo permissions
    are off elsewhere, so the message was never going to arrive.
    """
    uk = "+442071234567"
    assert not within_quiet_hours(uk, _at(14, 0, zone="America/New_York"))
    assert not within_quiet_hours(uk, _at(19, 59, zone="America/New_York")), (
        "19:59 Eastern is 00:59 in London — the hour the old fallback allowed"
    )


def test_an_unmapped_us_number_still_sends_in_the_conservative_window() -> None:
    """⚠ The property the rewritten test above was protecting, kept separate.

    The NPA table deliberately omits split-timezone states (850 is Florida),
    and blocking those outright would silently break texting for everyone the
    table misses — which is what the old test feared. That fear was right; it
    was just attached to the wrong input. `is_nanp` splits the two cases, so
    an unmapped +1 number keeps the conservative window and only a genuinely
    foreign number is refused.
    """
    unmapped_us = "+18505551234"
    assert within_quiet_hours(unmapped_us, _at(12, 0, zone="America/New_York"))
    assert not within_quiet_hours(unmapped_us, _at(9, 0, zone="America/New_York"))


def test_a_caller_holding_a_real_zone_still_wins() -> None:
    """The refusal is a FALLBACK, not a ban on the number.

    A consent row recording `Europe/London` for a London number is better
    information than the country code, and the branch that honours it runs
    first. Losing that would make the fix "never text anyone we cannot place
    from an area code", which is a different and worse rule.
    """
    uk = "+442071234567"
    assert within_quiet_hours(uk, _at(14, 0, zone="Europe/London"), zone="Europe/London")
    assert not within_quiet_hours(uk, _at(3, 0, zone="Europe/London"), zone="Europe/London")


def test_zone_for_says_unknown_rather_than_defaulting() -> None:
    """``None`` is a real answer the caller must handle. Returning Eastern
    here would make every unmapped number look like a New Yorker."""
    assert zone_for("+12125551234") == "America/New_York"
    assert zone_for("+18505551234") is None
    assert zone_for("") is None


# ── per-state statutes: the window is federal-or-STRICTER ─────────────


def test_the_state_map_covers_exactly_the_zone_map() -> None:
    """⚠ Both are flattened from ONE table, so a number that resolves to a zone
    must resolve to a state. A mismatch means an NPA is judged on one state's
    clock and another state's statute — or on no statute at all, which is the
    permissive direction."""
    assert set(NPA_STATES) == set(NPA_ZONES)


def test_florida_ends_an_hour_before_the_federal_rule() -> None:
    """⚠ THE BUG THIS REPLACES. The old code applied 8am-9pm everywhere; the
    FTSA ends at 8pm and carries a private right of action, so every 20:30 text
    to a Florida number was a per-message exposure."""
    fl = "+13055551234"
    assert within_quiet_hours(fl, _at(19, 59, zone="America/New_York"))
    assert not within_quiet_hours(fl, _at(20, 30, zone="America/New_York"))
    # …and the same instant is still legal for a New York number, which is why
    # this is per-state rather than one blunt window for the whole product.
    assert within_quiet_hours("+12125551234", _at(20, 30, zone="America/New_York"))


def test_texas_starts_an_hour_after_the_federal_rule() -> None:
    """The other end. SB 140 begins at 9am, so 8:30 is federally fine and
    $5,000/violation in Texas — a window wrong at ONE end is still wrong."""
    tx = "+12145551234"
    assert not within_quiet_hours(tx, _at(8, 30, zone="America/Chicago"))
    assert within_quiet_hours(tx, _at(9, 0, zone="America/Chicago"))
    assert within_quiet_hours(tx, _at(20, 59, zone="America/Chicago"))


def test_texas_sunday_starts_at_noon() -> None:
    """SB 140's weekday rule. 2026-09-06 is a Sunday."""
    tx = "+12145551234"
    sunday = datetime(2026, 9, 6, 10, 0, tzinfo=ZoneInfo("America/Chicago")).astimezone(UTC)
    assert not within_quiet_hours(tx, sunday)
    noon = datetime(2026, 9, 6, 12, 0, tzinfo=ZoneInfo("America/Chicago")).astimezone(UTC)
    assert within_quiet_hours(tx, noon)


def test_the_sunday_rule_is_read_on_the_recipients_clock() -> None:
    """The weekday comes from the RECIPIENT's clock, not UTC.

    ⚠ **This property is currently UNOBSERVABLE and the test says so rather
    than pretending otherwise** (gotcha 58). Measured across every US zone: the
    two readings disagree only at 20:30 local, and at 20:30 the Texas weekday
    window (9-21) and Sunday window (12-21) both allow — they differ only at
    the START, and 9:00-12:00 local never crosses the UTC date line. So a
    mutation swapping `local.weekday()` for `now_utc.weekday()` changes no
    output today, and writing a test that appeared to catch it would pin a
    distinction that does not exist.

    The local read stays because it is the CORRECT reading and stops being
    unobservable the moment any Sunday window differs at its end, or a zone
    outside UTC-4..-10 appears. What is asserted here is the part that IS
    observable: the Sunday rule fires on the recipient's calendar day.
    """
    tx = "+12145551234"
    sat_evening = datetime(2026, 9, 5, 20, 0, tzinfo=ZoneInfo("America/Chicago")).astimezone(UTC)
    assert sat_evening.weekday() == 6, "fixture no longer straddles the date line"
    assert within_quiet_hours(tx, sat_evening), "Saturday 8pm Central was judged as Sunday"


def test_a_consent_zone_overrides_the_area_code_but_not_the_statute() -> None:
    """``send_sms`` holds the recipient's ACTUAL zone from the consent row,
    which beats an NPA guess. But a zone cannot identify a state —
    America/Chicago covers both Texas and Illinois — so the statute still comes
    from the number, and only one of those two has a Sunday rule."""
    tx = "+12145551234"
    il = "+13125551234"
    eight_thirty = _at(8, 30, zone="America/Chicago")
    assert not within_quiet_hours(tx, eight_thirty, zone="America/Chicago")
    assert within_quiet_hours(il, eight_thirty, zone="America/Chicago")


def test_an_unknown_state_takes_the_intersection_not_the_default() -> None:
    """A guard that cannot see must decline (gotcha 55). Falling back to the
    FEDERAL window here would be the permissive reading of an unknown."""
    assert window_for(None, is_sunday=False) == (dtime(9, 0), dtime(20, 0))
    assert window_for(None, is_sunday=True) == (dtime(12, 0), dtime(20, 0))
    assert window_for("NY", is_sunday=False) == (dtime(8, 0), dtime(21, 0))


# ── reachability, which is a different question from the clock ────────


@pytest.mark.parametrize(
    ("phone", "expected"),
    [
        ("+12125551234", True),
        ("+18505551234", True),  # unmapped US NPA — still reachable
        ("+16045551234", True),  # Vancouver: NANP is US *and* Canada
        ("+442071234567", False),
        ("+33123456789", False),
        ("2125551234", False),  # not E.164 — we do not guess
        ("+1212555123", False),  # ten digits after +1, or it is not NANP
        ("+121255512345", False),
        ("+1212555abcd", False),
        ("", False),
    ],
)
def test_is_nanp_reads_the_country_code_not_the_length(phone: str, expected: str) -> None:
    """⚠ Both halves are load-bearing and a length check alone passes for the
    wrong reason on `+44` (see `test_area_code_reads_only_nanp_e164`). `+33…`
    is nine digits after the country code and `+1…` must be exactly ten, so
    the length half is what rejects a malformed US number and the prefix half
    is what rejects a foreign one.
    """
    assert is_nanp(phone) is expected


def test_is_nanp_is_not_applied_to_matching_an_inbound_number() -> None:
    """⚠ The deliberate absence. Consent governs what we may SEND; it does not
    govern whether we read what somebody chose to send us. An inbound text from
    abroad must still normalise, still match, and still have its STOP honoured
    — so `comms/phone.py` keeps preserving a foreign country code and this rule
    lives only on the outbound paths.
    """
    from helixis.comms.phone import normalize_e164, phone_match_key

    assert normalize_e164("+44 20 7946 0000") == "+442079460000"
    assert phone_match_key("+442079460000") is not None
