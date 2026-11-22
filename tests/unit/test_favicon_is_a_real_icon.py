"""The favicon Google renders beside the product is an actual icon.

``frontend/src/seo.test.ts`` pins the WIRING — that index.html references
``/favicon.ico`` and that the SPA rewrite does not swallow it. It cannot pin
the BYTES: reading a binary through Vite's ``?raw`` mangles it, and the
frontend tsconfig carries no node types, so a ``node:fs`` read there runs
green under vitest and turns ``tsc --noEmit`` red.

So the other half lives here, where Pillow is already a declared dependency.
The failure it exists to stop is the quiet one: a ``favicon.ico`` that is a
renamed PNG, a single 16px frame, or a zero-byte file left by a half-finished
build reads in the repo exactly like a correct one, and the only symptom is
that search results keep showing the generic globe — which is indistinguishable
from Google simply not having recrawled yet.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PIL = pytest.importorskip("PIL.Image", reason="Pillow is a declared dependency")

_FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
_FAVICON = _FRONTEND / "public" / "favicon.ico"
_INDEX = _FRONTEND / "index.html"

#: Google resizes a favicon from a multiple of 48px; browsers ask for 16 and
#: 32. All three are in the file so nothing downstream has to resample.
_REQUIRED_SIZES = {(16, 16), (32, 32), (48, 48)}


def test_the_favicon_exists_and_is_an_ico() -> None:
    assert _FAVICON.exists(), f"{_FAVICON} is missing — Google falls back to a globe"
    with PIL.open(_FAVICON) as img:
        assert img.format == "ICO", f"favicon.ico is a {img.format}, not an ICO"


def test_it_carries_every_size_a_crawler_or_a_tab_asks_for() -> None:
    with PIL.open(_FAVICON) as img:
        sizes = set(img.ico.sizes())
    missing = _REQUIRED_SIZES - sizes
    assert not missing, f"favicon.ico is missing {sorted(missing)}; has {sorted(sizes)}"


def test_the_smallest_frame_is_not_blank() -> None:
    """A 16px frame that downscaled to one flat colour is not a mark.

    The favicon is the Occupella wordmark — the same word, weight and colours
    as the first-paint shell in ``index.html`` — and 16px is where nine
    letters can average themselves into a plain rectangle. That ships as a
    blank tile that identifies nothing and looks like a working icon in every
    listing, which is the failure this whole file exists for.
    """
    with PIL.open(_FAVICON) as img:
        img.size = (16, 16)
        frame = img.convert("RGB")
    # ⚠ getcolors(), not getdata(): the latter is deprecated in Pillow 12 and
    # its replacement does not exist in Pillow 10, which is what pyproject
    # declares as the floor. A guard that warns on every run is one people
    # learn to scroll past.
    distinct = frame.getcolors(maxcolors=100_000) or []
    assert len(distinct) > 4, (
        f"the 16px frame has {len(distinct)} distinct colours — the mark has "
        "flattened into a plain square"
    )


def test_it_uses_the_loading_screen_palette() -> None:
    """ "Like the loading screen" is the requirement, so it is read from it.

    The founder picked the wordmark over the square-grid app icon specifically
    so the tab, the search result and the first paint are one thing. That makes
    the boot shell's own two colours the specification, and they are declared
    once, in ``index.html`` — so this reads them from there rather than
    restating them here, where the two copies could drift with nothing failing
    (the same both-sides rule the SQL default and DEFAULT_TIMEZONE are held to).

    ⚠ THIS REPLACED A CHECK THAT WOULD HAVE PASSED BY COINCIDENCE. The earlier
    version asserted the favicon's corner was a colour present in
    ``icon-512.png``; the wordmark's corner is white, white is in that icon,
    so it would have gone green while measuring nothing about the change.
    """
    boot_css = _INDEX.read_text(encoding="utf-8")

    background = re.search(r"#boot\s*\{[^}]*background:\s*(#[0-9a-fA-F]{6})", boot_css)
    ink = re.search(r"#boot\s\.mark\s*\{[^}]*color:\s*(#[0-9a-fA-F]{6})", boot_css)
    assert background and ink, (
        "index.html no longer declares the boot shell's background and mark "
        "colours in the shape this reads — re-point it, do not delete it"
    )

    def rgb(value: str) -> tuple[int, int, int]:
        v = value.lstrip("#")
        return (int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16))

    with PIL.open(_FAVICON) as img:
        img.size = (48, 48)
        frame = img.convert("RGB")
        corner = frame.getpixel((0, 0))
        palette = {c for _, c in (frame.getcolors(maxcolors=100_000) or [])}

    assert corner == rgb(background.group(1)), (
        f"the favicon's ground is {corner}, the loading screen's is "
        f"{rgb(background.group(1))} — they are meant to be the same surface"
    )
    # Antialiasing means the exact ink tone may not survive a resample, so the
    # check is that the darkest pixel actually reaches the mark's colour rather
    # than that the value appears verbatim.
    darkest = min(palette, key=sum)
    assert sum(darkest) <= sum(rgb(ink.group(1))) + 60, (
        f"the darkest pixel in the favicon is {darkest}, which never gets near "
        f"the loading screen's {rgb(ink.group(1))} — the word has washed out"
    )
