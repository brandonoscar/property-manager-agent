"""The icons a phone actually uses, and the splash colour it paints first.

⚠ **An SVG-only manifest ships somebody else's icon.** Android Chrome will not
use an SVG for a home-screen install and substitutes a generated letter tile,
so the app on the customer's phone is a grey "O" in a circle. iOS ignores an
SVG `apple-touch-icon` outright and screenshots the page instead — and paints
BLACK behind any transparency, which is why these files carry a plate rather
than the transparent mark the browser tab uses.

⚠ **A `maskable` icon is CROPPED** to whatever shape the launcher prefers —
circle, squircle, teardrop. The spec's safe zone is the inner 80%, so a
maskable icon drawn edge-to-edge loses its corners on most Android devices.

⚠ **The splash colour has to match the first paint.** `background_color` read
`#0e0e11` while `index.html`'s `#boot` shell paints `#FFFFFF` — so an
installed app showed a near-black splash and then flashed white. It is the one
place a wrong colour token is visible as a flicker rather than as a shade, and
nothing in the app would ever have surfaced it.

Every assertion here is about a file existing at the size it claims, because
that is exactly the class of thing that rots silently: a renamed asset leaves
the manifest pointing at a 404, the browser falls back, and the product looks
unfinished on a home screen nobody on the team has installed.
"""

from __future__ import annotations

import json
import re
import struct
import zlib
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_PUBLIC = _ROOT / "frontend" / "public"
_MANIFEST = _PUBLIC / "manifest.webmanifest"
_INDEX = _ROOT / "frontend" / "index.html"

#: The web-app-manifest safe zone for a maskable icon.
_MASKABLE_SAFE_FRACTION = 0.8


def _png_size(path: Path) -> tuple[int, int]:
    """Width and height from the IHDR chunk — no image library needed, and it
    fails loudly on a file that is not a PNG at all."""
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name} is not a PNG"
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def _decode_png(path: Path) -> tuple[list[list[tuple[int, ...]]], int, int]:
    """RGBA pixels, decoded here rather than through Pillow.

    ⚠ Pillow is NOT a dependency of this project, and importing it would make
    the assertion below skip on CI — a pixel check answered by a skip reads as
    a pass, which is the one thing this file cannot afford (the LED-013
    skip-floor rule). The format is small enough to read directly: zlib over
    filtered scanlines, five filter types, all of them two lines.
    """
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name} is not a PNG"

    idat, pos = b"", 8
    width = height = depth = colour = 0
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        kind = data[pos + 4 : pos + 8]
        body = data[pos + 8 : pos + 8 + length]
        if kind == b"IHDR":
            width, height, depth, colour = struct.unpack(">IIBB", body[:10])
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            break
        pos += 12 + length

    assert depth == 8, f"{path.name} is {depth}-bit; this reader handles 8"
    channels = {0: 1, 2: 3, 4: 2, 6: 4}[colour]
    raw = zlib.decompress(idat)
    stride = width * channels

    rows: list[list[tuple[int, ...]]] = []
    previous = bytearray(stride)
    at = 0
    for _ in range(height):
        filter_type = raw[at]
        line = bytearray(raw[at + 1 : at + 1 + stride])
        at += 1 + stride
        for i in range(stride):
            left = line[i - channels] if i >= channels else 0
            up = previous[i]
            upleft = previous[i - channels] if i >= channels else 0
            if filter_type == 1:
                line[i] = (line[i] + left) & 0xFF
            elif filter_type == 2:
                line[i] = (line[i] + up) & 0xFF
            elif filter_type == 3:
                line[i] = (line[i] + (left + up) // 2) & 0xFF
            elif filter_type == 4:
                p = left + up - upleft
                pa, pb, pc = abs(p - left), abs(p - up), abs(p - upleft)
                nearest = left if (pa <= pb and pa <= pc) else (up if pb <= pc else upleft)
                line[i] = (line[i] + nearest) & 0xFF
        rows.append([tuple(line[x * channels : x * channels + 3]) for x in range(width)])
        previous = line
    return rows, width, height


def _manifest() -> dict:
    return json.loads(_MANIFEST.read_text(encoding="utf-8"))


def test_the_manifest_is_being_read_at_all() -> None:
    m = _manifest()
    assert m["name"] == "Occupella"
    assert m["icons"], "no icons declared"


def test_every_declared_icon_exists_at_the_size_it_claims() -> None:
    """A renamed asset leaves the manifest pointing at a 404 and the browser
    falls back silently — the icon is simply not yours any more."""
    for icon in _manifest()["icons"]:
        src = icon["src"].lstrip("/")
        path = _PUBLIC / src
        assert path.exists(), f"{icon['src']} is declared and missing"
        if not src.endswith(".png"):
            continue
        want = icon["sizes"]
        assert re.fullmatch(r"\d+x\d+", want), f"{src} declares sizes={want!r}"
        w, h = (int(n) for n in want.split("x"))
        assert _png_size(path) == (w, h), f"{src} is not {want}"


def test_android_has_a_raster_icon_to_use() -> None:
    """The SVG alone is not enough — Chrome substitutes a generated letter
    tile, which is the product shipping somebody else's icon."""
    pngs = {
        i["sizes"]
        for i in _manifest()["icons"]
        if i["src"].endswith(".png") and "any" in i.get("purpose", "any")
    }
    assert {"192x192", "512x512"} <= pngs, f"raster 'any' icons are {pngs}"


def test_a_maskable_icon_exists_and_keeps_inside_the_safe_zone() -> None:
    """Measured from the PIXELS, not from intent: a launcher crops this to a
    circle, so ink outside the inner 80% is ink that gets cut off."""
    maskable = [i for i in _manifest()["icons"] if "maskable" in i.get("purpose", "")]
    assert maskable, "no maskable icon — Android crops the 'any' one to its corners"

    path = _PUBLIC / maskable[0]["src"].lstrip("/")
    pixels, w, h = _decode_png(path)
    plate = pixels[1][1]
    # The bounding box of everything that is not the plate colour.
    xs, ys = [], []
    for y in range(0, h, 4):
        for x in range(0, w, 4):
            if pixels[y][x] != plate:
                xs.append(x)
                ys.append(y)
    assert xs, "the maskable icon is a flat plate with no mark on it"

    margin = (1 - _MASKABLE_SAFE_FRACTION) / 2
    lo, hi = w * margin, w * (1 - margin)
    assert min(xs) >= lo and max(xs) <= hi, (
        f"the mark spans x {min(xs)}..{max(xs)} of {w}; the safe zone is "
        f"{lo:.0f}..{hi:.0f} and a circular launcher crops the rest"
    )
    assert min(ys) >= h * margin and max(ys) <= h * (1 - margin), (
        f"the mark spans y {min(ys)}..{max(ys)} of {h}, outside the safe zone"
    )


def test_ios_gets_a_png_rather_than_the_svg() -> None:
    """iOS ignores an SVG apple-touch-icon and screenshots the page instead."""
    head = _INDEX.read_text(encoding="utf-8")
    m = re.search(r'<link rel="apple-touch-icon"[^>]*href="([^"]+)"', head)
    assert m, "no apple-touch-icon"
    assert m.group(1).endswith(".png"), f"apple-touch-icon is {m.group(1)}"
    assert (_PUBLIC / m.group(1).lstrip("/")).exists()


def test_the_splash_colour_matches_the_first_paint() -> None:
    """An installed app paints `background_color`, then the #boot shell. When
    they disagree the launch flashes — near-black to white, in this case."""
    boot = re.search(r"#boot\s*\{[^}]*?background:\s*(#[0-9A-Fa-f]{6})", _INDEX.read_text("utf-8"))
    assert boot, "the #boot shell no longer sets an explicit background"
    shell = boot.group(1).upper()

    assert _manifest()["background_color"].upper() == shell, (
        f"manifest background_color is {_manifest()['background_color']} and the "
        f"boot shell paints {shell} — an installed app flashes between them"
    )
    theme = re.search(
        r'<meta name="theme-color" content="(#[0-9A-Fa-f]{6})"', _INDEX.read_text("utf-8")
    )
    assert theme and theme.group(1).upper() == shell, (
        "theme-color disagrees with the shell the browser is about to draw"
    )
