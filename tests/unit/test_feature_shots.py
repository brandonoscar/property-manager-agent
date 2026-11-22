"""The four properties that make a set of capability screenshots usable.

⚠ **This file exists because the first five screenshots were unusable**, and
both defects were invisible to every static guard already in the repo
(`test_chat_probe_feature_shots.py` is 8 AST walks). A screenshot script fails
by producing a FILE — a plausible-looking PNG of the wrong moment or the wrong
region — so nothing errors, nothing is red, and the only signal is a human
looking at the image. That is gotcha 43's lesson at a different surface: a
wait that returns too early and a capture that frames the wrong thing both
look exactly like the correct code.

So the guards RUN the functions — against a fake page for the timing ones, and
against real PNGs read back pixel by pixel for the layout ones:

1. **Settling.** `chat_probe._wait_idle` returns on composer-enabled +
   no-thinking-pill, which is ALSO the state of a fresh thread before the turn
   starts, so it could return instantly and photograph an empty thread.
   `wait_for_settled_answer` must refuse that state, and must refuse a
   mid-stream pause between chunks.
2. **Framing.** `full_page=True` expands the BODY; the conversation is its own
   overflow container, so a full-page shot holds one viewport of an
   auto-scrolled thread — the answer without the question. `shoot` must
   screenshot the THREAD ELEMENT.
3. **Uniform size, without cropping.** Every tile the same size, a long answer
   scaled rather than cut, a short one padded rather than stretched.
4. **Apparent zoom.** The app caps the chat column at 820 CSS px, so a 2x
   element capture is an image of a 780px column at twice its real size. The
   cap is cleared before the shot and the tile is delivered at `OUTPUT_WIDTH`.

Every wait here is bounded by `asyncio.wait_for`: a re-introduced hang has to
fail a test in seconds rather than cost a live run (gotcha 43 again — six
identical timeouts, 42 minutes, zero answers).
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

_SRC = (_SCRIPTS / "feature_shots.py").read_text(encoding="utf-8")


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


cp = _load("chat_probe")
fs = _load("feature_shots")


# --------------------------------------------------------------------------
# A fake page, scripted per poll.
#
# ⚠ It answers by SELECTOR, using the module's own imported constants — a fake
# keyed on call order would agree with whatever the code happened to ask for,
# which is the one thing this must not do.
# --------------------------------------------------------------------------


class _FakeLocator:
    def __init__(self, page: _FakePage, selector: str) -> None:
        self._page = page
        self._sel = selector

    @property
    def first(self) -> _FakeLocator:
        return self

    @property
    def last(self) -> _FakeLocator:
        return self

    async def count(self) -> int:
        if self._sel == cp.THINKING:
            # ⚠ The clock ticks HERE, not on the reply read. `_answer_text`
            # reads the reply locator twice in one round (count, then
            # inner_text), so advancing there would run the script at double
            # speed and make a two-poll pause look like four.
            self._page.advance()
            return self._page.thinking
        if self._sel == cp.REPLY:
            return 1 if self._page.answer else 0
        return 1

    async def inner_text(self) -> str:
        return self._page.answer

    async def is_enabled(self) -> bool:
        return self._page.composer_enabled

    async def click(self) -> None:
        self._page.clicked.append(self._sel)

    async def fill(self, text: str) -> None:
        self._page.filled.append(text)

    async def press(self, key: str) -> None:
        self._page.pressed.append(key)

    async def evaluate(self, script: str) -> int:
        self._page.element_evaluated.append(self._sel)
        return 1

    async def screenshot(self, *, path: str) -> None:
        self._page.element_shots.append((self._sel, path))
        Path(path).write_bytes(b"png")


class _FakePage:
    """A page whose answer text advances one entry per poll."""

    def __init__(
        self,
        script: list[str],
        *,
        composer_enabled: bool = True,
        thinking: int = 0,
    ) -> None:
        self._script = list(script)
        self._i = 0
        self.composer_enabled = composer_enabled
        self.thinking = thinking
        self.clicked: list[str] = []
        self.filled: list[str] = []
        self.pressed: list[str] = []
        self.element_shots: list[tuple[str, str]] = []
        self.page_shots: list[dict[str, Any]] = []
        self.element_evaluated: list[str] = []
        self.page_evaluated: list[str] = []

    @property
    def answer(self) -> str:
        # The last entry repeats forever, so a script that never settles keeps
        # the page in that state rather than falling off the end.
        return self._script[min(self._i, len(self._script) - 1)]

    def advance(self) -> None:
        self._i += 1

    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator(self, selector)

    async def evaluate(self, script: str) -> None:
        self.page_evaluated.append(script)

    async def screenshot(self, **kwargs: Any) -> None:
        self.page_shots.append(kwargs)


@pytest.fixture(autouse=True)
def _fast_polls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Poll on a test clock. The INTERVAL is scaled; the COUNT is not —
    `_STABLE_POLLS` is the property under test and stays untouched."""
    monkeypatch.setattr(fs, "_POLL_S", 0.001)


# --------------------------------------------------------------------------
# 1. Settling
# --------------------------------------------------------------------------


def test_a_thread_with_no_answer_never_counts_as_settled() -> None:
    """The exact state `_wait_idle` returns on: composer enabled, no pill, and
    no answer — which is a fresh thread one instant after Enter.

    ⚠ Driven under its own `asyncio.run` inside an `asyncio.wait_for` so a
    regression that hangs FAILS instead of hanging the suite (gotcha 43).
    """

    async def main() -> str | None:
        page = _FakePage([""])
        with pytest.raises(RuntimeError) as err:
            await asyncio.wait_for(fs.wait_for_settled_answer(page, timeout_s=0.05), timeout=5.0)
        return str(err.value)

    message = asyncio.run(main())
    assert message is not None
    # gotcha 43: a timeout that cannot name what it waited for is not
    # diagnosable — six identical lines cost four wrong theories.
    assert "composer enabled=True" in message
    assert "thinking pills=0" in message


def test_a_pause_between_streamed_chunks_is_not_the_end_of_the_answer() -> None:
    """A streaming answer arrives in bursts, so one unchanged read is an
    ordinary gap — photographing it captures a half-written answer, which is
    exactly the complaint this file answers."""

    async def main() -> str:
        page = _FakePage(["", "abc", "abc", "abcdef", "abcdef", "abcdef", "abcdef"])
        return await asyncio.wait_for(fs.wait_for_settled_answer(page, timeout_s=5.0), timeout=10.0)

    assert asyncio.run(main()) == "abcdef"


def test_a_thinking_pill_still_up_is_not_settled() -> None:
    """Length alone is not enough: a stalled stream holds its text while the
    pill says the turn is live."""

    async def main() -> str:
        page = _FakePage(["done", "done", "done", "done", "done"], thinking=1)
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(fs.wait_for_settled_answer(page, timeout_s=0.05), timeout=5.0)
        page.thinking = 0
        return await asyncio.wait_for(fs.wait_for_settled_answer(page, timeout_s=5.0), timeout=10.0)

    assert asyncio.run(main()) == "done"


# --------------------------------------------------------------------------
# 2. Framing
# --------------------------------------------------------------------------


def test_the_shot_is_of_the_thread_element_not_the_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⚠ `full_page=True` expands the BODY. The conversation scrolls inside its
    own container and auto-scrolls to the bottom, so a page shot frames the
    answer with the question off the top — every image in the first batch."""
    monkeypatch.setattr(fs, "_start_fresh_thread", _noop)

    async def main() -> tuple[dict[str, Any], _FakePage]:
        page = _FakePage(["", "an answer", "an answer", "an answer", "an answer"])
        got = await asyncio.wait_for(fs.shoot(page, cp.FEATURE_SHOTS[0], tmp_path), timeout=10.0)
        return got, page

    got, page = asyncio.run(main())

    assert got["file"] == "01-contextual-memory.png"
    assert got["answer"] == "an answer"
    assert (tmp_path / "01-contextual-memory.png").exists()
    # The fake records BOTH kinds, so a swap to a page-level capture shows up
    # as an element shot that never happened rather than as silence.
    assert [sel for sel, _ in page.element_shots] == [cp.CONVERSATION_THREAD]
    assert page.page_shots == []


def test_the_expansion_is_applied_to_the_thread_and_then_undone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The container is expanded before the capture and restored after — the
    restore matters because a later question is asked on the same page."""
    monkeypatch.setattr(fs, "_start_fresh_thread", _noop)

    async def main() -> _FakePage:
        page = _FakePage(["", "an answer", "an answer", "an answer", "an answer"])
        await asyncio.wait_for(fs.shoot(page, cp.FEATURE_SHOTS[0], tmp_path), timeout=10.0)
        return page

    page = asyncio.run(main())
    assert page.element_evaluated == [cp.CONVERSATION_THREAD]
    assert [cp.CONVERSATION_THREAD] == [s for s, _ in page.element_shots]
    assert page.page_evaluated, "styles were never restored"
    assert not page.page_shots, "a page-level screenshot was taken"


def test_the_question_is_actually_asked_before_anything_is_photographed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shot of a thread nobody typed into is the same class of unusable
    image, arriving from the other end."""
    monkeypatch.setattr(fs, "_start_fresh_thread", _noop)
    question = cp.FEATURE_SHOTS[1]

    async def main() -> _FakePage:
        page = _FakePage(["", "an answer", "an answer", "an answer", "an answer"])
        await asyncio.wait_for(fs.shoot(page, question, tmp_path), timeout=10.0)
        return page

    page = asyncio.run(main())
    assert page.filled == [question]
    assert page.pressed == ["Enter"]


async def _noop(page: Any) -> None:
    return None


# --------------------------------------------------------------------------
# 3. The filename map, both directions (gotcha 63's stale-entry rule)
# --------------------------------------------------------------------------


def test_every_question_has_a_label_and_every_label_is_used() -> None:
    stems = [fs.label_for(q) for q in cp.FEATURE_SHOTS]
    assert len(set(stems)) == len(stems), f"two questions share a filename: {stems}"
    # A stale entry is a licence nobody needs — it makes the map read as
    # covering questions that no longer exist.
    assert set(stems) == set(fs._LABELS.values())


def test_an_unlabelled_question_raises_rather_than_writing_an_unnameable_file() -> None:
    with pytest.raises(KeyError):
        fs.label_for("a question nobody added a label for")


# --------------------------------------------------------------------------
# 4. Wiring
# --------------------------------------------------------------------------


def test_the_script_asks_every_feature_question() -> None:
    """⚠ A correct helper nothing calls is this repo's most repeated defect
    (gotchas 62/66/79), so walk the source for the loop over the shared list
    rather than trusting that `shoot` exists."""
    tree = ast.parse(_SRC)
    loops = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFor | ast.For) and "FEATURE_SHOTS" in ast.dump(n.iter)
    ]
    assert loops, "run() never iterates chat_probe.FEATURE_SHOTS"


def test_no_page_level_screenshot_survives_in_the_source() -> None:
    """The behavioural guard above catches a swap inside `shoot`; this catches
    one added anywhere else in the file. AST, not a substring scan — the
    docstring QUOTES `full_page=True` in order to explain it, and a text
    search fires on the sentence documenting the guard (gotcha 40)."""
    tree = ast.parse(_SRC)
    offenders = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for kw in node.keywords
        if kw.arg == "full_page"
    ]
    assert not offenders, f"full_page= screenshot at line(s) {offenders}"


# --------------------------------------------------------------------------
# 5. Uniform sizing
#
# ⚠ These build REAL PNGs and read the composed pixels back. A test asserting
# on the returned `scale` numbers alone would agree with the arithmetic and
# never notice that the image was cropped, stretched, or written with the
# padding in the wrong colour — the three ways this can look right in a report
# and wrong in an editor.
# --------------------------------------------------------------------------

_BG = (248, 248, 250)
_TOP = (30, 40, 60)
_BOTTOM = (200, 60, 90)

#: Default page border on a fixture, in pixels. ⚠ Deliberately SMALLER than
#: `trim_margins`' own padding, so the default fixture trims to itself and
#: every pre-existing size assertion below still measures what it always did.
#: The trimming tests pass a margin well above the padding on purpose.
_MARGIN = 4


def _png(path: Path, w: int, h: int, *, band: bool = False, margin: int = _MARGIN) -> Path:
    """A shot-shaped PNG: `margin` px of page colour around a content block.

    ⚠ **The margin is not decoration.** A real capture is an answer floating on
    the page background, and `trim_margins` defines background as the top-left
    pixel. A fixture painted edge to edge in the CONTENT colour inverts that —
    with `band=True` the top 60% then reads as background and the trim eats the
    answer, so the test measures its own fixture rather than the code.

    `band` paints the bottom 40% of the CONTENT a second colour, which is what
    distinguishes 'scaled to fit' from 'cropped to fit'.
    """
    from PIL import Image

    img = Image.new("RGB", (w, h), _BG)
    x0, y0 = margin, margin
    x1, y1 = max(x0 + 1, w - margin), max(y0 + 1, h - margin)
    band_from = int(y0 + (y1 - y0) * 0.6) if band else y1
    for y in range(y0, y1):
        colour = _TOP if y < band_from else _BOTTOM
        for x in range(x0, x1):
            img.putpixel((x, y), colour)
    img.save(path)
    return path


def _has_colour(path: Path, rgb: tuple[int, int, int], tol: int = 20) -> bool:
    """Is this colour anywhere in the image? Read as raw RGB bytes — `getdata`
    is deprecated in Pillow 12 and a warning in a guard is noise nobody reads."""
    from PIL import Image

    with Image.open(path) as img:
        buf = img.convert("RGB").tobytes()
    return any(
        all(abs(buf[i + k] - rgb[k]) <= tol for k in range(3)) for i in range(0, len(buf), 3)
    )


def _sizes(paths: list[Path]) -> list[tuple[int, int]]:
    from PIL import Image

    out = []
    for p in paths:
        with Image.open(p) as img:
            out.append(img.size)
    return out


def test_every_tile_comes_out_the_same_size(tmp_path: Path) -> None:
    """The whole point of the pass: five images that sit together."""
    raw = tmp_path / "raw"
    raw.mkdir()
    for i, h in enumerate((80, 140, 300, 95, 110)):
        _png(raw / f"0{i}.png", 120, h)

    fs.compose_uniform(sorted(raw.glob("*.png")), tmp_path)
    sizes = set(_sizes(sorted(tmp_path.glob("*.png"))))
    assert len(sizes) == 1, f"tiles came out at {sizes}"


def test_a_uniform_set_is_not_scaled_at_all(tmp_path: Path) -> None:
    """⚠ The canvas tracks the MEDIAN but is capped by the max, so a set whose
    answers are all similar lengths loses no sharpness. Anchoring on the median
    alone would shrink the tallest of five near-identical shots for nothing."""
    raw = tmp_path / "raw"
    raw.mkdir()
    for i, h in enumerate((100, 105, 110)):
        _png(raw / f"0{i}.png", 120, h)

    laid = fs.compose_uniform(sorted(raw.glob("*.png")), tmp_path)
    assert [r["scale"] for r in laid] == [1.0, 1.0, 1.0]
    assert all(r["crisp"] for r in laid)


def test_a_long_answer_is_scaled_down_and_never_cropped(tmp_path: Path) -> None:
    """⚠ The failure this replaces is an answer cut off mid-sentence, which
    reads as a product bug rather than a layout choice. The bottom band is the
    evidence: a crop-to-fit loses it, a scale-to-fit keeps it."""
    raw = tmp_path / "raw"
    raw.mkdir()
    for i in range(4):
        _png(raw / f"0{i}.png", 120, 80)
    _png(raw / "09-long.png", 120, 300, band=True)

    laid = fs.compose_uniform(sorted(raw.glob("*.png")), tmp_path)
    long_row = next(r for r in laid if r["file"] == "09-long.png")
    assert long_row["scale"] < 1.0, "the outlier was not scaled to fit"
    assert _has_colour(tmp_path / "09-long.png", _BOTTOM), (
        "the bottom of the long answer is missing — it was cropped, not scaled"
    )
    assert _has_colour(tmp_path / "09-long.png", _TOP), "the top of the answer is missing"


def test_a_short_answer_is_padded_and_never_stretched(tmp_path: Path) -> None:
    """⚠ Upscaling a capture past its own pixels is the soft-text failure
    `_demo_zoom.clamp_to_headroom` already records for the video crop. Padding
    costs whitespace; stretching costs the one tile that needed nothing."""
    from PIL import Image

    raw = tmp_path / "raw"
    raw.mkdir()
    _png(raw / "00.png", 60, 40)
    for i in (1, 2):
        _png(raw / f"0{i}.png", 120, 200)

    laid = fs.compose_uniform(sorted(raw.glob("*.png")), tmp_path)
    short = next(r for r in laid if r["file"] == "00.png")
    assert short["scale"] == 1.0, "a shot smaller than the canvas was upscaled"
    with Image.open(tmp_path / "00.png") as tile:
        assert tile.size == tuple(short["size"])
        # The padding is the SOURCE's own background, sampled from its
        # top-left — so it is invisible and needs no palette knowledge here.
        assert all(abs(a - b) <= 2 for a, b in zip(tile.getpixel((0, 0)), _BG, strict=True))


def test_fit_scale_never_exceeds_one_and_always_fits() -> None:
    canvas = (400, 300)
    for size in [(100, 50), (400, 300), (800, 300), (400, 900), (1200, 2400)]:
        s = fs.fit_scale(size, canvas)
        assert 0 < s <= 1.0, f"{size} -> {s}"
        assert size[0] * s <= canvas[0] + 1 and size[1] * s <= canvas[1] + 1


def test_a_wide_capture_is_delivered_at_the_output_width(tmp_path: Path) -> None:
    """⚠ The "too zoomed in" complaint. A 2x capture of a wide column is a
    ~2900px image; handing that over as the deliverable shows every answer at
    twice its on-screen size. Tiles come out at `OUTPUT_WIDTH`."""
    raw = tmp_path / "raw"
    raw.mkdir()
    for i in range(3):
        _png(raw / f"0{i}.png", 2900, 1800)

    laid = fs.compose_uniform(sorted(raw.glob("*.png")), tmp_path, out_width=1600)
    assert {tuple(r["size"])[0] for r in laid} == {1600}
    # Aspect kept: shrinking the width must not squash the height separately.
    assert {tuple(r["size"])[1] for r in laid} == {round(1800 * 1600 / 2900)}
    # Every tile shares the width-driven scale, so none is flagged as the odd
    # one out — an absolute threshold here would have flagged all three.
    assert all(r["crisp"] for r in laid), [r["relative"] for r in laid]


def test_a_capture_narrower_than_the_output_width_is_left_alone(tmp_path: Path) -> None:
    """Delivering at a width is a shrink, never a stretch — the same rule
    `fit_scale` holds one level down."""
    raw = tmp_path / "raw"
    raw.mkdir()
    for i in range(2):
        _png(raw / f"0{i}.png", 400, 300)

    laid = fs.compose_uniform(sorted(raw.glob("*.png")), tmp_path, out_width=1600)
    assert {tuple(r["size"]) for r in laid} == {(400, 300)}


def test_the_odd_one_out_is_flagged_relative_to_its_peers(tmp_path: Path) -> None:
    """⚠ The warning has to mean "smaller than the others", not "below some
    absolute number". All five share a width, so an absolute bar fires on all
    of them or none — and moves the day the viewport changes."""
    raw = tmp_path / "raw"
    raw.mkdir()
    for i in range(3):
        _png(raw / f"0{i}.png", 2900, 1600)
    _png(raw / "09-long.png", 2900, 6000)

    laid = fs.compose_uniform(sorted(raw.glob("*.png")), tmp_path, out_width=1600)
    flagged = {r["file"] for r in laid if not r["crisp"]}
    assert flagged == {"09-long.png"}, [(r["file"], r["relative"]) for r in laid]


def test_the_flag_disagrees_with_an_absolute_bar_in_both_directions(
    tmp_path: Path,
) -> None:
    """⚠ Written because a mutation replacing the relative rule with a fixed
    `scale >= 0.5` SURVIVED the tests above — their fixtures happened to agree
    with both rules (gotcha 65's shape: the gap was in the test, not the code).
    These are the two inputs where they disagree, measured before writing it:

    - a wide capture delivered narrow puts the SHARED baseline at 0.414, so an
      absolute bar cries wolf on every tile — the way a guard gets ignored;
    - a moderately long answer lands at 0.667, which an absolute bar calls
      fine while it is plainly two-thirds the size of its peers.
    """
    wide = tmp_path / "wide"
    (wide / "raw").mkdir(parents=True)
    for i in range(3):
        _png(wide / "raw" / f"0{i}.png", 2900, 1600)
    laid = fs.compose_uniform(sorted((wide / "raw").glob("*.png")), wide, out_width=1200)
    assert all(r["scale"] < 0.5 for r in laid), "fixture no longer straddles the bar"
    assert all(r["crisp"] for r in laid), "a uniform set must never all be flagged"

    odd = tmp_path / "odd"
    (odd / "raw").mkdir(parents=True)
    for i in range(3):
        _png(odd / "raw" / f"0{i}.png", 120, 100)
    _png(odd / "raw" / "09-long.png", 120, 240)
    laid = fs.compose_uniform(sorted((odd / "raw").glob("*.png")), odd)
    long_row = next(r for r in laid if r["file"] == "09-long.png")
    assert long_row["scale"] > 0.5, "fixture no longer straddles the bar"
    assert not long_row["crisp"], "a tile two-thirds its peers' size must be flagged"


def test_the_empty_page_around_the_answer_is_cropped_away(tmp_path: Path) -> None:
    """⚠ This is what makes the text legible. The thread element is as tall as
    the layout leaves it, so a short answer captures with a screenful of empty
    page around it — and the shared canvas then spends its pixels on that
    whitespace, which is exactly how the first batch came out tiny."""
    from PIL import Image

    shot = _png(tmp_path / "wide-margin.png", 400, 400, margin=100)
    with Image.open(shot) as src:
        trimmed = fs.trim_margins(src)
        assert trimmed.size < src.size, "nothing was cropped"
    # Content is 200x200 inset by 100; keeping `_TRIM_PAD` on each side.
    assert trimmed.size == (200 + 2 * fs._TRIM_PAD, 200 + 2 * fs._TRIM_PAD)


def test_the_answer_itself_is_never_cropped(tmp_path: Path) -> None:
    """⚠ The dangerous direction. A trim that eats content turns a layout
    choice into what reads as a product bug — the same asymmetry `_only_matching`
    follows: over-keeping is visible and arguable, over-cropping is silent."""
    from PIL import Image

    shot = _png(tmp_path / "banded.png", 400, 400, band=True, margin=100)
    with Image.open(shot) as src:
        trimmed = fs.trim_margins(src)
        trimmed.save(tmp_path / "out.png")
    assert _has_colour(tmp_path / "out.png", _TOP), "the top of the answer was cropped"
    assert _has_colour(tmp_path / "out.png", _BOTTOM), "the bottom of the answer was cropped"


def test_a_shot_that_is_all_background_is_returned_untouched(tmp_path: Path) -> None:
    """⚠ Fails OPEN. A uniform image has no bounding box, and an image trimmed
    to nothing is worse than one with wide margins — decline to act rather than
    return an empty frame (gotcha 55's posture, pointed at pixels)."""
    from PIL import Image

    blank = Image.new("RGB", (300, 200), _BG)
    assert fs.trim_margins(blank).size == (300, 200)


def test_the_canvas_is_measured_after_trimming(tmp_path: Path) -> None:
    """⚠ Order matters, and getting it wrong is invisible in the report.
    Sizing the shared canvas on the UNTRIMMED shots budgets the frame for
    margin that is about to be discarded, so every tile scales down to fit
    whitespace and the numbers still read 1.0."""
    raw = tmp_path / "raw"
    raw.mkdir()
    for i in range(3):
        _png(raw / f"0{i}.png", 600, 600, margin=200)

    laid = fs.compose_uniform(sorted(raw.glob("*.png")), tmp_path)
    # 200px content + 24px padding each side. Measured on the untrimmed 600px
    # shots the canvas would be 600x600 instead.
    assert {tuple(r["size"]) for r in laid} == {(248, 248)}


def test_the_padding_is_still_the_page_colour_after_a_trim(tmp_path: Path) -> None:
    """The background is sampled from the TRIMMED image, and that was checked
    rather than assumed: the crop origin is `max(0, edge - pad)`, so the new
    top-left is either the old one or a pixel outside the content box.

    ⚠ Sampling the untrimmed source instead cannot change any output — a
    distinction that does not exist is not worth pinning (gotcha 58)."""
    from PIL import Image

    raw = tmp_path / "raw"
    raw.mkdir()
    _png(raw / "00.png", 400, 200, margin=100)
    _png(raw / "01.png", 400, 600, margin=100)

    fs.compose_uniform(sorted(raw.glob("*.png")), tmp_path)
    with Image.open(tmp_path / "00.png") as tile:
        assert all(abs(a - b) <= 2 for a, b in zip(tile.getpixel((0, 0)), _BG, strict=True)), (
            "the tile was padded in the answer's own colour"
        )


def test_the_thread_width_cap_is_cleared_before_the_shot() -> None:
    """⚠ The root of the zoom: the app caps the chat column at 820 CSS px, so
    a 2x element capture is a 1560px image of a 780px column. Clearing it is
    also what shortens long answers, which is what the canvas rule then has
    less of to absorb. A behavioural test cannot see this — it is a string
    handed to the browser."""
    assert "maxWidth" in fs._EXPAND_JS
    assert "maxHeight" in fs._EXPAND_JS, "the scroll-cap clear must survive too"


def test_the_contact_sheet_holds_every_tile_side_by_side(tmp_path: Path) -> None:
    """⚠ "Open them all" is not something an OS does — `start` opens one, and
    a Photos slideshow shows them one at a time, which is the wrong view for a
    question that is entirely comparative. So the set gets its own image."""
    raw = tmp_path / "raw"
    raw.mkdir()
    for i in range(5):
        _png(raw / f"0{i}.png", 300, 500)

    laid = fs.compose_uniform(sorted(raw.glob("*.png")), tmp_path)
    sheet = fs.contact_sheet([tmp_path / r["file"] for r in laid], tmp_path)

    assert sheet is not None
    w, h = sheet["size"]
    tile_w, tile_h = _sizes([tmp_path / laid[0]["file"]])[0]
    # Five tiles plus four gaps, all at one scale — so the sheet is wider than
    # any tile and never taller than one.
    assert w > tile_w * 4
    assert h <= tile_h
    assert (tmp_path / "all-five.png").exists()


def test_the_contact_sheet_is_capped_and_never_enlarges(tmp_path: Path) -> None:
    """Five portrait tiles side by side is ~8,000px — a strip nothing opens
    usefully. And a small set must not be blown up to fill the cap."""
    wide = tmp_path / "wide"
    wide.mkdir()
    for i in range(5):
        _png(wide / f"0{i}.png", 1600, 2600)
    big = fs.contact_sheet(sorted(wide.glob("*.png")), wide)
    assert big is not None and big["size"][0] <= fs._SHEET_MAX_WIDTH

    small = tmp_path / "small"
    small.mkdir()
    for i in range(2):
        _png(small / f"0{i}.png", 100, 80)
    got = fs.contact_sheet(sorted(small.glob("*.png")), small)
    assert got is not None
    assert got["size"] == [2 * 100 + fs._SHEET_GAP, 80], "a small set was upscaled"


def test_one_tile_gets_no_contact_sheet(tmp_path: Path) -> None:
    """A contact sheet of one is a copy under a name that implies otherwise."""
    _png(tmp_path / "only.png", 200, 300)
    assert fs.contact_sheet([tmp_path / "only.png"], tmp_path) is None
    assert not (tmp_path / "all-five.png").exists()


def test_finish_writes_the_contact_sheet(tmp_path: Path) -> None:
    """⚠ A correct helper nothing calls (gotchas 62/66/79) — and here the
    helper is the ONLY artifact the founder actually opens first."""
    raw = tmp_path / "raw"
    raw.mkdir()
    for i in range(3):
        _png(raw / f"0{i}.png", 200, 300)

    assert fs.finish(tmp_path) == 0
    assert (tmp_path / "all-five.png").exists()


def test_composing_with_nothing_captured_is_an_honest_failure(tmp_path: Path) -> None:
    """⚠ A run that produced no image is not a success, and `--compose-only`
    against an empty directory is the easiest way to get one by accident."""
    (tmp_path / "raw").mkdir()
    assert fs.finish(tmp_path) == 1
    with pytest.raises(ValueError):
        fs.uniform_canvas([])


def test_finish_reads_raw_and_writes_beside_it(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    for i in range(3):
        _png(raw / f"0{i}.png", 120, 100)

    assert fs.finish(tmp_path) == 0
    # The three tiles, plus the contact sheet the run also writes.
    assert sorted(p.name for p in tmp_path.glob("*.png")) == [
        "00.png",
        "01.png",
        "02.png",
        "all-five.png",
    ]
    # The originals survive: re-laying must never mean re-asking.
    assert len(list(raw.glob("*.png"))) == 3


def test_the_run_shoots_into_raw_and_composes_afterwards() -> None:
    """⚠ Both halves are source-level on purpose. A behavioural test of
    `compose_uniform` cannot see that the live run stopped calling it, or that
    it went back to writing shots straight into the output directory — the
    'correct helper nothing calls' shape (gotchas 62/66/79)."""
    tree = ast.parse(_SRC)
    run = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "run")
    dump = ast.dump(run)
    assert "'raw'" in dump or '"raw"' in dump, "run() no longer separates the raw shots"
    called = {
        n.func.id for n in ast.walk(run) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "finish" in called, "run() captures shots and never lays them out"
