"""The favicon, measured against both tab strips it actually lands on.

⚠ **THE MARK CHANGED ON 2026-09-15 AND THIS FILE WAS RE-POINTED, NOT DELETED.**
It used to read a two-tone square grid with a `prefers-color-scheme` swap; the
favicon is now the Occupella wordmark on an opaque white plate, the same word
and colours as the first-paint shell in `index.html`. The STRUCTURE it parsed
is gone. The PROPERTY it guards is not, and an assertion deleted because the
code moved underneath it is a guarantee quietly dropped.

⚠ **The original defect was in LIGHT mode, which is the opposite of where
anyone looks for it, and it is why this file exists at all.** The old mark's
pale tone was `#C3D3E6` — 1.37:1 against Chrome's light tab strip. Sixteen of
its twenty squares were invisible there, so the icon read as four scattered
dots and did not identify the product. On a dark strip the same colour measures
10.56:1 and had always been fine, which is why nothing ever looked wrong to
anybody testing in dark mode. Keep that measurement written down: a future mark
with two tones and no plate walks straight back into it.

**Why the wordmark needs no media query, which is the substantive change.** The
old mark was transparent, so the tab strip WAS its ground and the ground moved
with the theme — hence the swap, and hence a default palette chosen for how it
degraded when a browser ignored the query. A full-bleed plate carries its own
ground, so the ink sits on #FFFFFF on every strip and there is no second case
to get wrong. The media query is not missing; it has nothing left to do.

⚠ So the old `test_the_default_palette_is_the_one_that_degrades_gracefully` is
RETIRED, and this names its replacement rather than leaving a gap: it asserted
which of two palettes a query-less browser should fall back to, and there is
now one palette. What it was really protecting — the mark stays legible on a
strip it was not designed for — is measured directly below, against both
strips, which is strictly stronger than reasoning about a fallback.

⚠ This is pytest and not vitest for the same reason as
`test_palette_contrast.py`: `frontend/public/` is outside the vitest `src` root
and the frontend has no `@types/node`, so a guard written there would read
nothing and pass vacuously. `test_favicon_is_a_real_icon.py` is the sibling
that checks the RASTER `favicon.ico` bytes; this one is about colour.
"""

from __future__ import annotations

import re
from pathlib import Path

_SVG = Path(__file__).resolve().parents[2] / "frontend" / "public" / "favicon.svg"

#: The grounds the icon is composited onto. Chrome's tab strip, both themes.
_LIGHT_STRIP = "#F1F3F4"
_DARK_STRIP = "#202124"

#: A wordmark is read as TEXT, not as a graphic, so it takes WCAG's text bar
#: rather than the 3.0 non-text bar the square grid was held to. The mark
#: clears this by a wide margin; the number is here to catch a future palette
#: edit, not to be scraped past.
_MIN_TEXT = 4.5


def _luminance(hex_colour: str) -> float:
    h = hex_colour.lstrip("#")
    channels = [int(h[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(a: str, b: str) -> float:
    la, lb = _luminance(a), _luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _mark() -> dict[str, str]:
    """The plate colour and the ink colour, read out of the file.

    Read rather than restated — a test that names the colours it expects only
    proves it agrees with itself (gotcha 45).
    """
    svg = _SVG.read_text(encoding="utf-8")
    plate = re.search(r"<rect[^>]*\bfill=\"(#[0-9A-Fa-f]{6})\"", svg)
    ink = re.search(r"<g[^>]*\bfill=\"(#[0-9A-Fa-f]{6})\"", svg)
    assert plate and ink, (
        "the favicon no longer declares a plate <rect fill> and an ink <g fill> "
        "in the shape this reads — re-point it, do not delete it"
    )
    return {"plate": plate.group(1).upper(), "ink": ink.group(1).upper()}


def test_the_favicon_is_being_read_at_all() -> None:
    """A wrong path or a renamed attribute would make every assertion below
    vacuous — the failure mode this whole file exists to catch."""
    assert _SVG.exists(), f"{_SVG} is gone"
    mark = _mark()
    assert set(mark) == {"plate", "ink"}, mark
    assert mark["plate"] != mark["ink"], (
        f"plate and ink are both {mark['plate']} — the favicon is a blank square"
    )


def test_the_word_is_legible_on_its_own_plate() -> None:
    """The one contrast that always applies, because the plate is full-bleed."""
    mark = _mark()
    ratio = contrast(mark["ink"], mark["plate"])
    assert ratio >= _MIN_TEXT, (
        f"ink {mark['ink']} on plate {mark['plate']} is {ratio:.2f}:1 — the word "
        "is not readable at any size, let alone at 16px"
    )


def test_the_mark_separates_from_both_tab_strips_without_a_media_query() -> None:
    """The property the retired fallback test was really protecting.

    ⚠ THE FIRST VERSION OF THIS TEST WAS WRONG AND FAILED AGAINST A CORRECT
    MARK, which is worth keeping because it is the mistake the plate exists to
    prevent. It asserted the INK clears both strips directly — but #0E1620 on
    Chrome's dark strip #202124 is 1.21:1, two near-blacks, and that is not a
    defect: the ink never touches the strip, because the plate is between them.
    A guard that fails against correct code is telling you the property is
    wrong, not the code.

    The property that actually holds, and that a theme change could break:
    on each strip AT LEAST ONE of the two tones separates from it, so the icon
    reads as an object rather than a hole. Which tone does the work flips by
    theme — the ink on a light strip, the plate on a dark one — and that is the
    whole reason one palette can serve both.
    """
    mark = _mark()
    for theme, strip in (("light", _LIGHT_STRIP), ("dark", _DARK_STRIP)):
        ratios = {tone: contrast(colour, strip) for tone, colour in mark.items()}
        assert max(ratios.values()) >= _MIN_TEXT, (
            f"on the {theme} strip {strip} neither tone separates "
            f"({', '.join(f'{t} {r:.2f}:1' for t, r in ratios.items())}) — "
            "the favicon is a hole in the tab bar"
        )


def test_the_plate_is_what_makes_the_dark_strip_safe() -> None:
    """Names the load-bearing half, so removing the plate cannot look tidy.

    Dropping the `<rect>` for a transparent ground is exactly the edit somebody
    makes to 'clean up' a favicon, and it re-creates the original defect in the
    other direction: dark ink on a dark strip, invisible, with nothing failing.
    """
    mark = _mark()
    ratio = contrast(mark["plate"], _DARK_STRIP)
    assert ratio >= _MIN_TEXT, (
        f"the plate {mark['plate']} is only {ratio:.2f}:1 against the dark strip "
        f"{_DARK_STRIP} — it no longer supplies a ground the ink can sit on"
    )
