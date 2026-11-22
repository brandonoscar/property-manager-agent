"""The demo cut's zoom pass — the maths, and one real run over real frames.

⚠ **I cannot watch the output.** The captured frames live on the founder's
Windows machine and this container has none, so "does it look good" is not a
question any test here answers. What these DO answer is every way the pass can
be wrong in a manner nobody notices until 25 clips have been re-cut: a rect at
the wrong aspect (text stretched horizontally, reads as a broken encoder), a
rect off the edge (black bars, reads as letterboxing), a zoom that upscales
(soft text, which is the exact thing 2x capture exists to prevent), and a crop
riding a 16x segment (unwatchable, and the part nobody is meant to look at).

The end-to-end test generates its own frames and runs the real pipeline. A pure
maths test would pass against a `render()` that never applied the rect — this
repo's most repeated defect is a correct helper nothing calls (gotchas 62/66),
and the zoom is exactly that shape: an edl field, a resolver, and one call site.
"""

from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


zoom = _load("_demo_zoom")

try:
    rd = _load("record_demo")
except (Exception, SystemExit) as exc:  # pragma: no cover - playwright absent
    pytest.skip(f"record_demo not importable here: {exc}", allow_module_level=True)

FRAME_W, FRAME_H = rd.VIEWPORT["width"] * rd.DEVICE_SCALE, rd.VIEWPORT["height"] * rd.DEVICE_SCALE


# ── the rect ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("region", sorted(zoom.REGIONS))
@pytest.mark.parametrize("z", [1.2, 1.5, 1.8, 2.0])
def test_the_rect_always_carries_the_output_aspect(region: str, z: float) -> None:
    """⚠ The failure this prevents looks like a broken encoder, not a bad crop.

    A rect at any other ratio stretches text horizontally when it is resized to
    the output, and from a finished video that reads as an ffmpeg problem.
    """
    rect = zoom.crop_rect(zoom.Focus(region, z), 1.0, FRAME_W, FRAME_H, rd.OUT_W, rd.OUT_H)
    if rect is None:  # "full" is a legitimate no-op
        assert region == "full"
        return
    _, _, w, h = rect
    assert abs((w / h) - (rd.OUT_W / rd.OUT_H)) < 0.01, f"{w}x{h} is not {rd.OUT_W}:{rd.OUT_H}"


@pytest.mark.parametrize("region", sorted(zoom.REGIONS))
def test_the_rect_never_leaves_the_frame(region: str) -> None:
    """⚠ `composer` sits at y=0.88, so its rect runs off the bottom before
    clamping — this is the case that actually exercises it, not a hypothetical.
    Pillow would return black bars rather than raise."""
    rect = zoom.crop_rect(zoom.Focus(region, 2.0), 1.0, FRAME_W, FRAME_H, rd.OUT_W, rd.OUT_H)
    if rect is None:
        return
    left, top, w, h = rect
    assert left >= 0 and top >= 0, f"rect starts outside the frame: {rect}"
    assert left + w <= FRAME_W and top + h <= FRAME_H, f"rect runs past the frame: {rect}"


def test_zoom_up_to_the_capture_scale_never_upscales() -> None:
    """The whole reason this is worth doing at all.

    A crop at exactly the headroom is one output pixel per captured pixel.
    Anything at or below it is a DOWNSCALE and stays sharp; past it the text
    goes soft, which is the "blurry demo" a larger capture buys away.

    ⚠ The headroom is a MEASURED frame width now, not `DEVICE_SCALE`. This
    test used to pass `rd.DEVICE_SCALE` and assert 2.0 — true of the constants
    and false of the frames, which is exactly how the defect survived.
    """
    limit = zoom.max_lossless_zoom(FRAME_W, rd.OUT_W)
    rect = zoom.crop_rect(zoom.Focus("composer", limit), 1.0, FRAME_W, FRAME_H, rd.OUT_W, rd.OUT_H)
    if rect is None:  # headroom 1.0 — no crop is the correct answer
        assert limit == pytest.approx(1.0)
        return
    _, _, w, h = rect
    assert w >= rd.OUT_W and h >= rd.OUT_H, (
        f"a {limit}x crop is {w}x{h}, smaller than the {rd.OUT_W}x{rd.OUT_H} output — "
        "it would be upscaled, which is the softness this capture resolution buys away"
    )


def test_a_default_past_the_headroom_is_clamped_not_shipped() -> None:
    """⚠ The defaults are DECLARED at 1.8x and the real capture may not afford
    it, so the bound cannot be "every default is under the limit" — that would
    fail on a correct 1:1 capture and invite somebody to lower the defaults for
    a reason that has nothing to do with taste.

    What must hold is that nothing SOFT reaches the output: whatever the
    declared zoom, `clamp_to_headroom` returns something the frame can pay for.
    """
    for kind, default in zoom.DEFAULT_FOCUS.items():
        if default is None:
            continue
        for headroom in (1.0, 1.2, 1.8, 2.0, 3.0):
            got = zoom.clamp_to_headroom(zoom.Focus(*default), headroom)
            if got is None:
                assert headroom <= 1.0001, (
                    f"{kind} lost its push-in at {headroom}x headroom, which it could afford"
                )
                continue
            assert got.zoom <= headroom + 1e-9, (
                f"{kind} would render at {got.zoom}x against {headroom}x of headroom — "
                "that is an upscale, i.e. soft text on a landing page"
            )


def test_no_headroom_means_no_crop_at_all() -> None:
    """The 2026-09-07 state, pinned. ffmpeg reported 1440x900 frames against a
    1440x900 output; the module had asserted 2880x1800 and pushed in anyway,
    landing on the wrong region AND upscaling. A 1:1 capture must produce a
    flat video, not a soft one."""
    assert zoom.max_lossless_zoom(rd.OUT_W, rd.OUT_W) == pytest.approx(1.0)
    assert zoom.clamp_to_headroom(zoom.Focus("composer", 1.8), 1.0) is None
    assert (
        zoom.crop_rect(
            zoom.clamp_to_headroom(zoom.Focus("composer", 1.8), 1.0),
            1.0,
            rd.OUT_W,
            rd.OUT_H,
            rd.OUT_W,
            rd.OUT_H,
        )
        is None
    )


# ── the ramp ────────────────────────────────────────────────────────────────


def test_it_starts_and_ends_at_the_full_frame() -> None:
    """A zoom still travelling at the cut snaps back, which reads as a glitch."""
    assert zoom.ramp(0.0, 6.0) == pytest.approx(0.0)
    assert zoom.ramp(6.0, 6.0) == pytest.approx(0.0)
    assert zoom.ramp(3.0, 6.0) == pytest.approx(1.0)


def test_a_short_segment_shrinks_its_ramps_instead_of_wobbling() -> None:
    """⚠ Two 0.5s ramps do not fit in a 0.4s beat. Overlapping them makes the
    zoom start pulling back before it arrived — a visible wobble on exactly the
    short segments a fast typist produces."""
    d = 0.4
    mid = zoom.ramp(d / 2, d)
    assert mid == pytest.approx(1.0), f"a {d}s segment never reaches its zoom (got {mid})"
    assert zoom.ramp(0.0, d) == pytest.approx(0.0)
    assert zoom.ramp(d, d) == pytest.approx(0.0)


def test_the_ease_is_not_linear() -> None:
    """Linear reads mechanical — it is the tell that separates a crop from an
    edit. smoothstep is flat at both ends and steep in the middle."""
    assert zoom.smoothstep(0.5) == pytest.approx(0.5)
    assert zoom.smoothstep(0.25) < 0.25, "no ease-in — the push starts at full speed"
    assert zoom.smoothstep(0.75) > 0.75, "no ease-out — the push stops dead"


# ── the rules that are not overridable ──────────────────────────────────────


def test_a_fast_segment_never_zooms_however_the_edl_asks() -> None:
    """⚠ `think` runs at 16x. A crop travelling across that is nauseating, and
    it is the part of the video nobody is meant to watch. Writing a region name
    into the edl must not be able to turn it on."""
    assert zoom.focus_for("think", rd.SPEEDS["think"], "composer@2.0") is None
    assert zoom.focus_for("type", rd.SPEEDS["type"], "composer@2.0") is not None


def test_the_answer_is_seen_whole() -> None:
    """⚠ Deliberate: tables and cards render wide and a crop cuts columns off —
    the one thing the answer segment exists to show. If this ever becomes a
    zoom, it is a taste decision somebody made on purpose."""
    assert zoom.DEFAULT_FOCUS["answer"] is None


def test_an_unknown_region_fails_loudly() -> None:
    """A typo in the edl must not silently render a flat clip — that is
    discovered by watching all 25."""
    with pytest.raises(KeyError, match="unknown focus region"):
        zoom.Focus("compsoer", 1.8).centre


def test_full_and_none_both_mean_no_zoom() -> None:
    for value in ("full", "none", ""):
        assert zoom.focus_for("type", 1.0, value) is None


# ── the pipeline, over real frames ──────────────────────────────────────────


def _write_frames(dirpath: Path, n: int, w: int = 0, h: int = 0) -> list[tuple[str, float]]:
    """Frames with a visible gradient, so a crop is detectable in the bytes.

    ⚠ ``w``/``h`` default to the module constants but are OVERRIDABLE, because
    the whole 2026-09-07 defect was the constants disagreeing with the frames.
    A fixture that can only produce one size cannot exercise that.
    """
    from PIL import Image

    frame_w, frame_h = w or FRAME_W, h or FRAME_H
    dirpath.mkdir(parents=True, exist_ok=True)
    out: list[tuple[str, float]] = []
    for i in range(n):
        img = Image.new("RGB", (frame_w, frame_h))
        # A vertical gradient: the bottom (where the composer is) is distinct
        # from the top, so a bottom-weighted crop changes the mean brightness.
        for y in range(0, frame_h, 8):
            for x in range(0, frame_w, 8):
                img.paste((y * 255 // frame_h, 40, 200 - x * 120 // frame_w), (x, y, x + 8, y + 8))
        name = f"f{i:04d}.jpg"
        img.save(dirpath / name, format="JPEG", quality=92)
        out.append((name, float(i) / 10.0))
    return out


def test_build_sequence_emits_one_rect_per_frame_and_spares_the_fast_segment(
    tmp_path: Path,
) -> None:
    take = rd.Take(session="t")
    take.frames = _write_frames(tmp_path / "frames", 40)
    segments = [
        rd.Segment("type", 0.0, 2.0, label="typing"),
        rd.Segment("think", 2.0, 3.0, label="waiting"),
    ]
    seq, rects, total = rd.build_sequence(take, segments, tmp_path)

    assert len(seq) == len(rects), "a frame without a rect would crop the wrong frame"
    assert total > 0
    assert any(r is not None for r in rects), "the typing segment never zoomed"

    # The think segment runs at 16x, so it is a handful of frames at the END.
    # None of them may carry a rect.
    n_think = sum(1 for r in rects[-2:] if r is not None)
    assert n_think == 0, "a 16x segment was cropped — that is the unwatchable case"


def test_the_render_pipeline_actually_applies_the_crop(tmp_path: Path) -> None:
    """⚠ END TO END, because a correct helper nothing calls is this repo's most
    repeated defect (gotchas 62/66). Every maths test above passes against a
    `render()` that ignores its `rects` argument."""
    from PIL import Image

    _write_frames(tmp_path / "frames", 4)
    raw = (tmp_path / "frames" / "f0000.jpg").read_bytes()

    crop = rd._cropper([None, (0, 0, 1440, 900)])
    assert crop is not None, "Pillow is present here, so a crop must be built"

    untouched = crop(raw, None)
    assert untouched == raw, "a None rect must pass the frame through byte-for-byte"

    cropped = crop(raw, (0, 0, 1440, 900))
    assert cropped != raw, "the rect was ignored — the whole pass is inert"
    img = Image.open(io.BytesIO(cropped))
    assert img.size == (rd.OUT_W, rd.OUT_H), (
        f"cropped frame is {img.size}, not the output size — frames of differing "
        "sizes on one image2pipe stream re-init the decoder mid-stream"
    )


def test_a_flat_edit_builds_no_cropper_at_all(tmp_path: Path) -> None:
    """No zoom anywhere means no Pillow work and no re-encode. A pass that
    re-encodes every frame to change nothing costs quality for free."""
    assert rd._cropper([None, None, None]) is None


def test_the_edl_shows_the_focus_field(tmp_path: Path) -> None:
    """⚠ Written even when it is the default, so it is DISCOVERABLE. A field
    that only appears once you know it exists is a feature nobody finds."""
    import json

    segments = [rd.Segment("type", 0.0, 2.0, label="typing"), rd.Segment("think", 2.0, 3.0)]
    rd.save_edl(segments, tmp_path)
    written = json.loads((tmp_path / "edl.json").read_text(encoding="utf-8"))
    assert written[0]["focus"] == "composer@1.8"
    assert written[1]["focus"] == "full", "the 16x segment must not advertise a zoom"

    # And it survives the round trip, or editing the file does nothing.
    back = rd.load_edl(tmp_path)
    assert back[0].focus == "composer@1.8"


def test_render_pipes_the_cropped_bytes_to_ffmpeg(tmp_path: Path) -> None:
    """⚠ THE HOLE THIS FILE'S OWN DOCSTRING NAMED, AND THEN FELL INTO.

    Every test above passes against a `render()` that accepts `rects` and
    ignores them. There is no ffmpeg in this container, so the first version of
    the end-to-end test exercised `_cropper` directly — proving the helper
    correct while saying nothing about whether the pipeline uses it. That is
    gotchas 62/66 exactly, committed by the author who quoted them.

    A stub standing in for ffmpeg closes it: `render` runs for real, and the
    bytes it writes to stdin are the artifact. The stub also answers
    ``-encoders`` because `has_encoder` probes before choosing a codec.
    """
    from PIL import Image

    _write_frames(tmp_path / "frames", 3)
    seq = [tmp_path / "frames" / f"f{i:04d}.jpg" for i in range(3)]
    sink = tmp_path / "piped.bin"

    stub = tmp_path / "fake_ffmpeg.py"
    stub.write_text(
        "import sys, pathlib\n"
        "if '-encoders' in sys.argv:\n"
        "    print('V..... libx264 H.264'); sys.exit(0)\n"
        f"pathlib.Path({str(sink)!r}).write_bytes(sys.stdin.buffer.read())\n",
        encoding="utf-8",
    )
    shim = tmp_path / ("ffmpeg.cmd" if sys.platform == "win32" else "ffmpeg")
    if sys.platform == "win32":  # pragma: no cover - CI and dev are linux here
        shim.write_text(f'@"{sys.executable}" "{stub}" %*\n', encoding="utf-8")
    else:
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{stub}" "$@"\n', encoding="utf-8")
        shim.chmod(0o755)

    # A rect on EVERY frame, so any byte reaching ffmpeg uncropped is a failure.
    rect = (0, 0, 1440, 900)
    rd.render(seq, tmp_path, "probe", str(shim), [rect, rect, rect])

    piped = sink.read_bytes()
    assert piped, "nothing reached ffmpeg"
    raw = b"".join(p.read_bytes() for p in seq)
    assert piped != raw, (
        "render piped the ORIGINAL frames — it takes `rects` and ignores them, so the "
        "whole zoom pass is inert and every other test in this file still passes"
    )
    # And what arrived is output-sized, not capture-sized.
    first = Image.open(io.BytesIO(piped))
    assert first.size == (rd.OUT_W, rd.OUT_H), f"ffmpeg received {first.size} frames"


# ── the frame size, measured rather than computed ────────────────────────────


def test_the_frame_size_is_read_from_a_real_frame(tmp_path: Path) -> None:
    """⚠ THE 2026-09-07 defect, and it is only visible against a real file.

    `build_sequence` computed `VIEWPORT["width"] * DEVICE_SCALE` and got 2880
    while ffmpeg reported **1440** on the same take — Chrome's screencast
    `maxWidth` is a ceiling, not a target. A test feeding hand-written numbers
    to `max_lossless_zoom` would have agreed with itself forever (gotcha 45);
    what has to be measured is that the reader opens a FRAME.

    The fixture's dimensions match no constant in the module, so a reader that
    quietly went back to arithmetic cannot coincidentally agree with it.
    """
    _write_frames(tmp_path / "frames", 1, w=1234, h=778)
    assert rd._frame_size(tmp_path / "frames") == (1234, 778)


def test_an_unreadable_frame_declines_rather_than_guessing(tmp_path: Path) -> None:
    """None, never a fabricated size. The caller falls back to the old computed
    value — no worse than before — whereas inventing a number would silently
    re-create the defect this exists to close (gotcha 55's decline-to-assert).
    """
    empty = tmp_path / "frames"
    empty.mkdir()
    assert rd._frame_size(empty) is None, "no frames must read as unknown"
    (empty / "f0000.jpg").write_bytes(b"\xff\xd8not-a-jpeg")
    assert rd._frame_size(empty) is None, "a truncated frame must read as unknown"


def test_build_sequence_disables_the_zoom_on_a_one_to_one_capture(tmp_path: Path) -> None:
    """Both sides (gotcha 28): the measurement AND what `build_sequence` does
    with it. A behavioural test of `_frame_size` alone cannot see a caller that
    keeps multiplying constants — which is the shape the bug had (gotcha 62).
    """
    frames = _write_frames(tmp_path / "frames", 6, w=rd.OUT_W, h=rd.OUT_H)
    take = rd.Take(session="t")
    take.frames = frames
    segs = [rd.Segment("type", 0.0, 0.5, speed=1.0, label="q1 typing")]

    _seq, rects, _total = rd.build_sequence(take, segs, tmp_path)
    assert rects, "no frames were resampled — the fixture is wrong, not the code"
    assert all(r is None for r in rects), (
        f"a {rd.OUT_W}x{rd.OUT_H} capture into a {rd.OUT_W}x{rd.OUT_H} output has no "
        f"headroom, so every crop is an upscale — got {[r for r in rects if r][:3]}"
    )


def test_build_sequence_still_zooms_when_the_frames_can_pay_for_it(tmp_path: Path) -> None:
    """⚠ The other direction, and the one that stops this becoming a silent
    'never zoom' (gotcha 38: test what it must catch AND what it must spare).
    Fix the capture to a true 2x and the push-in must come back."""
    frames = _write_frames(tmp_path / "frames", 6, w=rd.OUT_W * 2, h=rd.OUT_H * 2)
    take = rd.Take(session="t")
    take.frames = frames
    segs = [rd.Segment("type", 0.0, 0.5, speed=1.0, label="q1 typing")]

    _seq, rects, _total = rd.build_sequence(take, segs, tmp_path)
    cropped = [r for r in rects if r is not None]
    assert cropped, "a 2x capture affords the push-in and it did not happen"
    for left, top, w, h in cropped:
        assert w >= rd.OUT_W and h >= rd.OUT_H, f"the crop {w}x{h} is smaller than the output"
        assert left + w <= rd.OUT_W * 2 and top + h <= rd.OUT_H * 2, "rect left the frame"
