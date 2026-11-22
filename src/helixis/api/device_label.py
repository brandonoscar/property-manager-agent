"""Turn a User-Agent string into something a property manager recognises.

The sessions list exists so somebody can look at it and say *"that MacBook is
the one I left at the office"*. A raw UA string cannot answer that — nobody
reads ``Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36``
and thinks "my laptop" — and a list you cannot act on is worse than no list,
because it implies a control it does not give you.

⚠ **Deliberately crude, and deliberately WRONG-SILENT rather than
WRONG-CONFIDENT.** UA parsing is a bottomless pit: every browser lies about
being every other browser for historical reasons, which is why "Chrome" ships
inside Edge's UA and "Safari" inside Chrome's. So the order of the checks
below is load-bearing — the liars are tested BEFORE the browsers they
impersonate — and anything unrecognised renders as "Unknown device" instead of
a guess. Somebody deciding whether to revoke a session is making a security
decision; a plausible wrong label is the one failure that actually costs them
something.

⚠ No third-party UA library. They are large, they need updating to stay
accurate, and being 100% right here buys nothing over being roughly right and
honest about the rest.
"""

from __future__ import annotations

# Browser checks, MOST SPECIFIC FIRST. Every entry below Chrome is a browser
# whose UA also contains "Chrome"; every entry below Safari also contains
# "Safari". Reordering this list silently relabels real devices.
_BROWSERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Edge", ("Edg/", "Edge/", "EdgA/", "EdgiOS/")),
    ("Opera", ("OPR/", "Opera")),
    ("Samsung Internet", ("SamsungBrowser",)),
    ("Brave", ("Brave",)),
    ("Firefox", ("Firefox/", "FxiOS/")),
    ("Chrome", ("Chrome/", "CriOS/", "Chromium/")),
    ("Safari", ("Safari/",)),
)

# Platform checks, also most-specific first: an iPhone UA contains "like Mac
# OS X", so testing for Mac before iOS labels every phone a Mac. Android is
# tested before Linux for the same reason.
_PLATFORMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("iPhone", ("iPhone",)),
    ("iPad", ("iPad",)),
    ("Android", ("Android",)),
    ("Windows", ("Windows NT", "Windows")),
    ("macOS", ("Macintosh", "Mac OS X")),
    ("Linux", ("Linux", "X11")),
)

UNKNOWN = "Unknown device"


def _first_match(user_agent: str, table: tuple[tuple[str, tuple[str, ...]], ...]) -> str | None:
    for label, needles in table:
        if any(needle in user_agent for needle in needles):
            return label
    return None


def describe_device(user_agent: str | None) -> str:
    """A short human label, or ``"Unknown device"``.

    Never raises and never returns an empty string — this feeds a settings
    row, and a blank cell there reads as a rendering bug rather than as
    missing data.
    """
    if not user_agent or not user_agent.strip():
        return UNKNOWN

    browser = _first_match(user_agent, _BROWSERS)
    platform = _first_match(user_agent, _PLATFORMS)

    if browser and platform:
        return f"{browser} on {platform}"
    if browser:
        return browser
    if platform:
        return platform
    return UNKNOWN
