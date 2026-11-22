"""The sessions list is a security surface, so its labels have to be honest.

Somebody reads "Chrome on Windows" and decides whether that is them or the
laptop they left behind. A plausible WRONG label is the only failure here that
actually costs anything — it either makes them revoke their own working
session or leave a stranger's alive. So the tests below are mostly about the
liars: every modern browser's User-Agent claims to be several other browsers,
and the check order in `device_label` is what keeps those apart.
"""

from __future__ import annotations

import pytest

from helixis.api.device_label import UNKNOWN, describe_device

# Real strings, not invented ones. Each is here because it contains the NAME
# of a browser it is not — which is exactly what a naive check gets wrong.
CHROME_MAC = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
EDGE_WINDOWS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36 Edg/128.0.0.0"
)
SAFARI_MAC = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15"
)
SAFARI_IPHONE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 "
    "Safari/604.1"
)
CHROME_ANDROID = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Mobile Safari/537.36"
)
FIREFOX_WINDOWS = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0"
OPERA_WINDOWS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36 OPR/113.0.0.0"
)
SAMSUNG_ANDROID = (
    "Mozilla/5.0 (Linux; Android 13; SAMSUNG SM-S918B) AppleWebKit/537.36 "
    "(KHTML, like Gecko) SamsungBrowser/23.0 Chrome/115.0.0.0 Mobile Safari/537.36"
)
FIREFOX_IOS = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) FxiOS/126.0 Mobile/15E148 Safari/605.1.15"
)


@pytest.mark.parametrize(
    ("user_agent", "expected"),
    [
        (CHROME_MAC, "Chrome on macOS"),
        (SAFARI_MAC, "Safari on macOS"),
        (SAFARI_IPHONE, "Safari on iPhone"),
        (CHROME_ANDROID, "Chrome on Android"),
        (FIREFOX_WINDOWS, "Firefox on Windows"),
    ],
)
def test_the_ordinary_cases(user_agent: str, expected: str) -> None:
    assert describe_device(user_agent) == expected


@pytest.mark.parametrize(
    ("user_agent", "expected", "impersonates"),
    [
        (EDGE_WINDOWS, "Edge on Windows", "Chrome"),
        (OPERA_WINDOWS, "Opera on Windows", "Chrome"),
        (SAMSUNG_ANDROID, "Samsung Internet on Android", "Chrome"),
        (CHROME_MAC, "Chrome on macOS", "Safari"),
        (FIREFOX_IOS, "Firefox on iPhone", "Safari"),
    ],
)
def test_the_browsers_that_lie_about_being_other_browsers(
    user_agent: str, expected: str, impersonates: str
) -> None:
    """Order-of-checks regressions land here and nowhere else.

    Each of these UAs literally contains the name of a browser it is not, so
    a check table reordered even slightly relabels real devices — silently,
    and only in production where the strings are real.
    """
    assert impersonates in user_agent, "fixture no longer exercises the ambiguity"
    assert describe_device(user_agent) == expected


def test_an_iphone_is_not_a_mac() -> None:
    """iOS UAs say "like Mac OS X", so a macOS check placed first labels every
    phone on the account a laptop."""
    assert "Mac OS X" in SAFARI_IPHONE
    assert describe_device(SAFARI_IPHONE).endswith("iPhone")


def test_android_is_not_generic_linux() -> None:
    assert "Linux" in CHROME_ANDROID
    assert describe_device(CHROME_ANDROID).endswith("Android")


@pytest.mark.parametrize("value", [None, "", "   ", "\n"])
def test_missing_reads_as_unknown_rather_than_blank(value: str | None) -> None:
    """A blank cell in a settings table reads as a rendering bug; "Unknown
    device" reads as missing data, which is what it is."""
    assert describe_device(value) == UNKNOWN


def test_an_unrecognised_agent_declines_to_guess() -> None:
    assert describe_device("SomeBespokeClient/1.0") == UNKNOWN


def test_a_platform_with_no_known_browser_still_says_something_useful() -> None:
    # curl from a Mac: no browser token, but the platform is real and worth
    # showing — half an answer beats "Unknown device" when half is true.
    assert describe_device("curl/8.4.0 (Macintosh)") == "macOS"


def test_never_raises_and_never_returns_empty() -> None:
    for value in ["\x00", "𝕸𝖔𝖟𝖎𝖑𝖑𝖆", "a" * 5000, "Chrome/", "Windows NT"]:
        result = describe_device(value)
        assert isinstance(result, str) and result
