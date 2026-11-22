"""Animated zoom for the demo cut — the polish pass, as edl data.

WHY THIS IS A CROP AND NOT A VIDEO EDITOR
-----------------------------------------
A crop only stays sharp while the captured frame is BIGGER than the output.
``max_lossless_zoom`` is that ratio, and ``clamp_to_headroom`` holds every
push-in inside it.

⚠ **This docstring used to assert "every frame on disk is 2880x1800 … zoom is
free up to 2x, measured from the constants". That was FALSE the whole time,
and measured from the wrong thing.** The context sets ``device_scale_factor=2``
and the screencast asks for ``maxWidth = 1440 * 2`` — but that parameter is a
CEILING, not a target, and Chrome delivers at the surface size. ffmpeg reported
the real input on 2026-09-07: **1440x900**, i.e. headroom 1.0 and no free zoom
at all. The consequence was worse than softness — the rect was computed against
2880 and applied to a 1440 image, so the composer centre at 0.60 resolved to
x=1728, clamped to the right edge, and the push-in landed on the wrong region.
So ``build_sequence`` MEASURES a frame (``_frame_size``, reading the JPEG's own
SOF marker) and the zoom disables itself when there is nothing to spend. A
constant multiplied by an assumption is not a measurement (gotcha 47).

⚠ **The crop happens in PYTHON, per frame, before ffmpeg sees anything.** Not
`zoompan`, not `crop` — Playwright's bundled ffmpeg is built
``--disable-everything`` and enables four things: image2pipe, the mjpeg
decoder, the webm muxer and libvpx. A filter graph that works on a full ffmpeg
dies there naming the wrong layer, which is the same trap `render()`'s own
comment records about `-f concat`. Handing ffmpeg pre-cropped mjpeg frames
keeps the one input path that works on BOTH builds.

WHAT MAKES IT LOOK EDITED RATHER THAN CROPPED
----------------------------------------------
Three rules, and each one is a thing that reads as amateur when broken:

1. **Ease, never cut.** A hard jump to a zoomed frame reads as a glitch.
   `smoothstep` ramps in and out; linear interpolation reads mechanical, which
   is its own tell.
2. **Ramp in, hold, ramp out — inside the segment.** A zoom still travelling
   when the segment ends snaps back on the cut. Ramps shrink proportionally on
   a short segment so a two-second beat never snaps.
3. **Never zoom a fast segment.** `think` runs at 16x. A crop moving across a
   16x-sped frame is nauseating, and it is the part of the video nobody is
   meant to look at. `MIN_SPEED_TO_ZOOM` makes that structural rather than a
   note somebody has to remember.

⚠ **The rect is always the output's aspect ratio**, derived from a CENTRE and a
zoom factor rather than specified as a free rectangle. A hand-written rect at
the wrong ratio stretches faces of text horizontally, which looks like a broken
render and is tedious to spot. There is no way to express that here.

⚠ **`answer` deliberately does NOT zoom.** Tables and cards render wide, and a
crop cuts columns off — the one thing the answer segment exists to show. The
typing is the beat worth pushing into; the answer is the beat worth seeing
whole.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Named focus points, as FRACTIONS of the frame (x, y), 0..1.
#:
#: ⚠ Centres, not rectangles. The crop's size comes from the zoom factor and
#: its shape from the output aspect, so a named point cannot express a wrong
#: ratio. Values are read off the 1440x900 layout: the thread rail is ~260px
#: (0.18) and the composer sits at the bottom of the remaining column.
REGIONS: dict[str, tuple[float, float]] = {
    # The composer, bottom of the chat column — where typing happens.
    "composer": (0.60, 0.88),
    # The first reply bubble, upper-centre of the chat column.
    "reply": (0.60, 0.35),
    # The thread rail, for a beat showing history accumulating.
    "threads": (0.18, 0.45),
    # The whole frame. Zoom 1.0, so this is a no-op that can be named.
    "full": (0.50, 0.50),
}

#: Per segment KIND: (region, zoom). `None` means never zoom this kind.
#:
#: ⚠ These are what a re-cut uses with no edl editing at all, so they carry the
#: taste decisions. `answer` is absent ON PURPOSE — see the module docstring.
DEFAULT_FOCUS: dict[str, tuple[str, float] | None] = {
    "type": ("composer", 1.8),
    "think": None,
    "answer": None,
    "hold": None,
    "nav": None,
    "read": None,
    "intro": None,
}

#: Seconds of playback spent ramping in, and again ramping out.
RAMP_S = 0.5

#: A segment slower than this never zooms, whatever the edl says. `think` runs
#: at 0.0625 (16x); a crop travelling across that is unwatchable.
MIN_SPEED_TO_ZOOM = 0.5


def max_lossless_zoom(frame_w: int, out_w: int) -> float:
    """The zoom at which one output pixel is still one captured pixel.

    ⚠ ``frame_w`` is the MEASURED width of a frame on disk, never
    ``VIEWPORT["width"] * DEVICE_SCALE``. This function used to take the
    device scale and multiply — and it was wrong for the whole life of the
    module, because Chrome's screencast ``maxWidth`` is a ceiling rather than
    a target and delivers at the surface size whatever ``device_scale_factor``
    says. It returned 2.0 while the true headroom was 1.0. The signature
    changed deliberately so every caller had to be re-pointed at a
    measurement (gotcha 77's "a required-kwarg change fails the callers
    closed, and that is the gate working").
    """
    return frame_w / out_w


def clamp_to_headroom(focus: Focus | None, headroom: float) -> Focus | None:
    """Never zoom past what the captured pixels can pay for.

    Returns ``None`` — the full frame — when there is no headroom at all,
    which is the honest answer for a 1:1 capture: a crop there is an upscale,
    and soft text on a landing page is worse than no push-in.

    ⚠ It CLAMPS rather than refuses on a partial headroom, so a 1.8x default
    against a 1.5x capture still pushes in as far as it can. Refusing outright
    would silently drop the polish pass the moment the capture size changes by
    any amount; clamping degrades in proportion.
    """
    if focus is None or headroom <= 1.0001:
        return None
    if focus.zoom <= headroom:
        return focus
    return Focus(focus.region, headroom)


@dataclass(frozen=True)
class Focus:
    """Where a segment pushes in to, and how far."""

    region: str
    zoom: float

    @property
    def centre(self) -> tuple[float, float]:
        if self.region not in REGIONS:
            raise KeyError(
                f"unknown focus region {self.region!r} — pick one of "
                f"{sorted(REGIONS)}, or add it to REGIONS with a measured centre"
            )
        return REGIONS[self.region]


def focus_for(kind: str, speed: float, override: str | None) -> Focus | None:
    """The focus a segment gets, or None for the full frame.

    ``override`` is the edl's own ``focus`` field: ``"composer"`` or
    ``"composer@2.0"``. It beats the per-kind default, EXCEPT for the speed
    rule — a zoom on a 16x segment is unwatchable however deliberately it was
    asked for, so that one is not overridable by writing a name in a file.
    """
    if speed < MIN_SPEED_TO_ZOOM:
        return None
    if override is not None:
        if override in ("", "none", "full"):
            return None
        region, _, z = override.partition("@")
        return Focus(region, float(z) if z else 1.8)
    default = DEFAULT_FOCUS.get(kind)
    if default is None:
        return None
    return Focus(default[0], default[1])


def smoothstep(t: float) -> float:
    """Ease in and out. Clamped, because callers pass raw progress."""
    t = 0.0 if t < 0.0 else 1.0 if t > 1.0 else t
    return t * t * (3.0 - 2.0 * t)


def ramp(elapsed: float, duration: float, ramp_s: float = RAMP_S) -> float:
    """How far into the zoom we are at ``elapsed``, 0..1, in PLAYBACK seconds.

    Ramps in, holds, ramps out. ⚠ On a segment shorter than two ramps the
    ramps SHRINK rather than overlapping — otherwise a short beat starts
    zooming out before it finished zooming in, which reads as a wobble.
    """
    if duration <= 0:
        return 0.0
    r = min(ramp_s, duration / 2.0)
    if r <= 0:
        return 1.0
    if elapsed < r:
        return smoothstep(elapsed / r)
    if elapsed > duration - r:
        return smoothstep((duration - elapsed) / r)
    return 1.0


def crop_rect(
    focus: Focus | None,
    amount: float,
    frame_w: int,
    frame_h: int,
    out_w: int,
    out_h: int,
) -> tuple[int, int, int, int] | None:
    """The (left, top, width, height) to cut, or None to use the whole frame.

    ⚠ The rect always carries the OUTPUT's aspect ratio. Anything else
    stretches text horizontally, which looks like a broken encoder and is
    miserable to diagnose from a finished video.

    ⚠ It is also clamped inside the frame. A centre near an edge shifts the
    rect in rather than reading out of bounds — Pillow would happily return
    black bars, which read as a letterboxing bug rather than a bad centre.
    """
    if focus is None or amount <= 0.0 or focus.zoom <= 1.0:
        return None
    # Interpolate the ZOOM, not the rect: easing a rect's corners independently
    # drifts the centre on a non-centred target, so the push-in slides sideways.
    zoom = 1.0 + (focus.zoom - 1.0) * amount
    if zoom <= 1.0001:
        return None

    aspect = out_w / out_h
    # Largest output-shaped box that fits the frame, then divided by the zoom.
    box_w = min(frame_w, int(round(frame_h * aspect)))
    box_h = int(round(box_w / aspect))
    w = max(2, int(round(box_w / zoom)))
    h = max(2, int(round(box_h / zoom)))

    cx, cy = focus.centre
    left = int(round(cx * frame_w - w / 2.0))
    top = int(round(cy * frame_h - h / 2.0))
    left = max(0, min(left, frame_w - w))
    top = max(0, min(top, frame_h - h))
    return left, top, w, h


__all__ = [
    "DEFAULT_FOCUS",
    "MIN_SPEED_TO_ZOOM",
    "RAMP_S",
    "REGIONS",
    "Focus",
    "clamp_to_headroom",
    "crop_rect",
    "focus_for",
    "max_lossless_zoom",
    "ramp",
    "smoothstep",
]
