"""Phone normalization — the match layer that lets a Twilio E.164 number find a
human-format Buildium mirror row. The whole click-to-call feature's correctness
rests on ``phone_match_key`` collapsing every format to one comparable value."""

from __future__ import annotations

import pytest

from helixis.comms.phone import normalize_e164, phone_match_key, phones_match


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("+15551234567", "5551234567"),  # E.164 (what Twilio sends)
        ("(555) 123-4567", "5551234567"),  # human format (what the mirror stores)
        ("555.123.4567", "5551234567"),
        ("555-123-4567", "5551234567"),
        ("1-555-123-4567", "5551234567"),  # 11-digit with US country code
        ("15551234567", "5551234567"),
        ("  +1 (555) 123-4567  ", "5551234567"),
        ("5551234567", "5551234567"),
    ],
)
def test_match_key_collapses_every_us_format(raw: str, expected: str) -> None:
    assert phone_match_key(raw) == expected


@pytest.mark.parametrize("raw", ["", None, "555-1234", "123", "abc", "  "])
def test_match_key_rejects_non_numbers(raw: str | None) -> None:
    # Fewer than 10 national digits can't be a resolvable US number.
    assert phone_match_key(raw) is None


def test_match_key_of_the_two_formats_that_must_meet_is_equal() -> None:
    # The exact failure the feature exists to prevent: E.164 vs. mirror format.
    assert phone_match_key("+15551234567") == phone_match_key("(555) 123-4567")


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("(555) 123-4567", "+15551234567"),
        ("5551234567", "+15551234567"),
        ("1-555-123-4567", "+15551234567"),
        ("+15551234567", "+15551234567"),
        ("+44 20 7946 0000", "+442079460000"),  # international preserved, not forced to +1
    ],
)
def test_normalize_e164(raw: str, expected: str) -> None:
    assert normalize_e164(raw) == expected


@pytest.mark.parametrize("raw", ["", None, "12345", "123", "+123"])
def test_normalize_e164_rejects_junk(raw: str | None) -> None:
    assert normalize_e164(raw) is None


def test_phones_match_across_formats() -> None:
    assert phones_match("+15551234567", "(555) 123-4567") is True
    assert phones_match("555-123-4567", "1 (555) 123 4567") is True


def test_phones_match_is_false_for_different_numbers() -> None:
    assert phones_match("+15551234567", "+15559999999") is False


def test_two_unparseable_numbers_never_match() -> None:
    # An unparseable number matches nothing — including another unparseable one.
    assert phones_match("abc", "abc") is False
    assert phones_match(None, None) is False
