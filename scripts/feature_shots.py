#!/usr/bin/env python3
"""One clean PNG per question: the question AND the finished answer, together —
and every tile the SAME SIZE, so the five read as one set.

    python scripts/chat_probe.py --login --url https://app.occupella.com   # once
    python scripts/feature_shots.py --url https://app.occupella.com
    python scripts/feature_shots.py --compose-only    # re-lay, no live turns

Natural-size captures land in `<out>/raw`; the finished, uniform tiles land in
`<out>`. The split matters: the shots cost five live agent turns and the layout
is a taste decision, so re-laying must never mean re-asking.

⚠ **THIS EXISTS BECAUSE `chat_probe --features` PRODUCED UNUSABLE IMAGES**, and
both reasons are worth writing down, because each is the kind of defect that
looks like a working screenshot until somebody tries to use one.

**1. The turn boundary was not the turn boundary.** `chat_probe._wait_idle`
returns as soon as the composer is enabled and no thinking pill is on screen.
Both of those are ALSO true in the second after Enter is pressed — the composer
has not gone disabled yet and the pill has not rendered — so the wait could
return immediately and the "final" screenshot caught a thread with no answer in
it at all. It is a correct turn-boundary check for a probe that only needs to
know when it may ask the next question; it is not a check that an answer has
finished ARRIVING.

The settle here is the ANSWER'S OWN LENGTH: poll the reply text and require it
to stop growing for `_STABLE_POLLS` consecutive reads, with the composer
enabled and no pill. A streaming answer grows; a finished one does not. That
cannot return early on a state the page passes through on its way in.

⚠ Deliberately NOT "the text stopped changing" over the whole document — a
mostly-static page satisfies that within seconds whether or not an answer ever
came, which is the exact defect `_wait_idle`'s own docstring records replacing.
The reply element, and its length, is the narrow signal.

**2. `full_page=True` does not capture a scrolling thread.** It expands the
BODY; the conversation is its own overflow container, so the image held one
viewport of it — the bottom, because the thread auto-scrolls. The question was
off the top of the frame in every shot.

So the container is EXPANDED first (`height: auto`, `max-height: none`,
`overflow: visible` on the thread and every scrollable ancestor) and the
ELEMENT is screenshotted, which lays the whole exchange out in one image with
the question above the answer. Styles are restored afterwards, though the page
is discarded either way.

**3. A set of different-sized tiles does not read as a product.** Element
captures come out at the answer's natural height, so a one-line reply and a
table are wildly different shapes. `compose_uniform` lays every shot on ONE
canvas — widest width, median-anchored height — centred on its own background
colour, so the padding is invisible.

⚠ **The long answer is SCALED, never CROPPED.** A fixed canvas plus a long
answer forces a choice between three bad-to-good options: cut it off (an answer
truncated mid-sentence reads as a bug, and is the one thing a capability
screenshot must not do), pad everything else to match the longest (four tiles
of empty space), or shrink the outlier. Only the third keeps every word AND the
uniform size. `DEVICE_SCALE` is what pays for it: a 2x capture can lose half its
pixels and still be one-for-one on screen, so a uniform set scales nothing at
all and an outlier stays legible. When one tile does come out visibly smaller
than its peers the run names the file and says the honest fix is a shorter
question (`_MIN_RELATIVE_SCALE`).

**4. The first batch read as "zoomed in", and that was a WIDTH problem.** The
app caps the chat column at 820 CSS px, so a `DEVICE_SCALE`x element capture is
a ~1560px image of a ~780px column — every answer at twice its real size, in a
tall narrow strip. Two changes fix it at the source rather than by shrinking
the result: `_EXPAND_JS` clears the width cap so text wraps at the viewport
(which also makes long answers much SHORTER, cutting the height variance the
canvas rule has to absorb), and the finished tiles are delivered at
`OUTPUT_WIDTH` rather than at the raw 2x capture size.

Selectors and the sign-in flow are imported from `chat_probe`, never copied —
three copies of `COMPOSER` drift, and that module's constants carry the
live-DOM verification comments (gotcha 25).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chat_probe import (  # noqa: E402  (path shim above must run first)
    COMPOSER,
    CONVERSATION_THREAD,
    FEATURE_SHOTS,
    REPLY,
    SESSION_FILE,
    THINKING,
    _start_fresh_thread,
    interactive_login,
)

try:
    from _console import force_utf8_console
except ImportError:  # pragma: no cover - the helper is optional here

    def force_utf8_console() -> None:  # type: ignore[misc]
        return


# The WEB APP, which took `app.` in the 2026-09-15 domain swap. The apex
# serves the marketing site and has no chat surface to drive.
DEFAULT_URL = "https://app.occupella.com"

#: The capture's pixel density. Every shot is taken at this multiple of CSS
#: pixels, and that surplus is what the composer spends when it shrinks a shot
#: to the delivered width or squeezes a long answer onto the shared canvas —
#: down to 1 output pixel per CSS pixel the text is still crisp.
DEVICE_SCALE = 2

#: The browser window. Wider than the old 1440 so the (uncapped, see
#: `_EXPAND_JS`) chat column has room — a wider column wraps the same answer
#: into fewer lines, which is the single biggest lever on both apparent zoom
#: and height variance across the set.
VIEWPORT = {"width": 1680, "height": 1050}

#: How wide a finished tile is, in real pixels.
#:
#: ⚠ This is what stops the images reading as "zoomed in". The capture is
#: `DEVICE_SCALE`x, so an uncapped column at this viewport lands near 2900px
#: wide; shown at 1600 that is ~1.1 output pixels per CSS pixel — roughly what
#: the app looks like on screen. Leaving the raw 2x capture as the deliverable
#: is what made every answer look blown up.
OUTPUT_WIDTH = 1600

#: How much taller than the MEDIAN shot the shared canvas may be.
#:
#: ⚠ This is the whole trade and it is a trade, not a fix. A fixed canvas plus
#: one very long answer forces a choice: crop it (an answer cut mid-sentence
#: reads as a bug), pad every other tile to match (four fifths empty space), or
#: scale the long one down. Scaling is the only one that keeps every word
#: readable AND the tiles identical, so the canvas tracks the median and the
#: outlier shrinks to fit. At 1.0 every tile would match the median exactly and
#: long answers would shrink hard; above ~2.0 one long answer stretches the set.
_HEIGHT_CAP_RATIO = 1.6

#: How much smaller than its PEERS a tile may render before the run says so.
#:
#: ⚠ Measured against the set's own baseline, NOT against an absolute scale.
#: Every shot is the same width, so they all share one width-driven scale
#: (~0.55 at today's numbers) — an absolute threshold would either fire on all
#: five or on none, and would move the day the viewport changes. What actually
#: matters is whether ONE tile came out visibly smaller than the rest, which
#: only happens when its height forced extra shrink.
#:
#: A WARNING, never a refusal: a smaller complete tile beats a cropped one, and
#: the run names the file so the question can be shortened deliberately.
_MIN_RELATIVE_SCALE = 0.85

#: Consecutive polls the answer's length must hold before it counts as done.
#: Three at `_POLL_S` is ~3s of silence — comfortably longer than a gap
#: between streamed chunks and far shorter than any real turn.
#:
#: ⚠ Two is not enough: chunk arrival is bursty and a two-poll window lands
#: inside an ordinary pause often enough to photograph a half-written answer,
#: which is the failure this whole file exists to stop.
_STABLE_POLLS = 3
_POLL_S = 1.0

#: How long one question may take before it is reported as unfinished. The
#: live median is ~20-40s and the tail runs past 240s (the agent's own
#: wall-clock deadline), so this sits above the deadline: a turn the PRODUCT
#: gave up on still produces its salvage answer, and that is a real screenshot.
_SETTLE_S = 300.0

#: Filenames, in montage order. Keyed by a fragment of the question so a
#: reworded question fails loudly here rather than silently writing `q3.png`.
#:
#: ⚠ The KEY is a substring of the live question and the guard checks both
#: directions — an unmatched question and an unused label are both defects
#: (gotcha 63's stale-entry rule, pointed at a filename map).
_LABELS: dict[str, str] = {
    "already decide": "01-contextual-memory",
    "return their deposit": "02-jurisdiction-lookup",
    "family situation": "03-fair-housing-gate",
    "flood zone": "04-geocoding-hazards",
    "miles of": "05-radius-search",
}

#: Undo the app's own scrolling so the whole exchange lays out at once.
#: Applied to the thread AND its ancestors: the overflow is often on a parent
#: wrapper rather than on the element carrying the testid, and styling only
#: the inner one leaves the clipping exactly where it was.
#: ⚠ `maxWidth` is here for a SEPARATE reason and it is the one that made the
#: first batch look "zoomed in". The chat column is capped at 820 CSS px
#: (`ChatThread.tsx`, `max-w-[820px]`), so an element capture at
#: `DEVICE_SCALE` 2 is a ~1560px-wide image of a ~780px column — every answer
#: rendered at twice its real size, in a tall narrow strip. Clearing the cap
#: lets the text wrap at the viewport width instead, which makes each answer
#: much SHORTER as well as wider: closer to the shape of a screenshot, and far
#: less height variance between a one-liner and a table.
_EXPAND_JS = """
(el) => {
  const touched = [];
  let node = el;
  while (node && node !== document.documentElement) {
    touched.push([node, node.getAttribute('style') || '']);
    node.style.height = 'auto';
    node.style.maxHeight = 'none';
    node.style.maxWidth = 'none';
    node.style.overflow = 'visible';
    node = node.parentElement;
  }
  window.__shotRestore = touched;
  return touched.length;
}
"""

_RESTORE_JS = """
() => {
  for (const [node, prior] of (window.__shotRestore || [])) {
    if (prior) node.setAttribute('style', prior);
    else node.removeAttribute('style');
  }
  window.__shotRestore = [];
}
"""


def label_for(question: str) -> str:
    """The output filename stem for a question, or a raise naming the gap."""
    for needle, stem in _LABELS.items():
        if needle in question:
            return stem
    raise KeyError(
        f"no filename label matches {question!r} — add one to _LABELS, or the "
        "shot lands under a name nobody can identify in an editor"
    )


async def _answer_text(page: Any) -> str:
    """The last assistant bubble's visible text, or '' before one exists."""
    with contextlib.suppress(Exception):
        if await page.locator(REPLY).count():
            return (await page.locator(REPLY).last.inner_text()).strip()
    return ""


async def wait_for_settled_answer(page: Any, *, timeout_s: float = _SETTLE_S) -> str:
    """Block until the answer has STOPPED GROWING, then return it.

    ⚠ Length, not the composer. `chat_probe._wait_idle` reads composer-enabled
    plus no-thinking-pill, and that combination is also true in the moment
    BEFORE the turn starts — so it can return instantly and hand a screenshot
    of an empty thread. Growth cannot: an answer that has not begun has length
    0 and an answer still streaming keeps changing.

    The composer and pill are still required, as a second condition rather
    than the only one — a paused stream with the pill still up is not done.
    """
    deadline = time.monotonic() + timeout_s
    last = -1
    stable = 0
    while time.monotonic() < deadline:
        text = await _answer_text(page)
        thinking = await page.locator(THINKING).count()
        enabled = await page.locator(COMPOSER).first.is_enabled()
        if text and len(text) == last and enabled and not thinking:
            stable += 1
            if stable >= _STABLE_POLLS:
                return text
        else:
            stable = 0
        last = len(text)
        await asyncio.sleep(_POLL_S)
    # Name the stuck signal — an un-diagnosable timeout cost a whole 42-minute
    # run once (gotcha 43), and this one costs a live turn per question.
    raise RuntimeError(
        f"answer never settled in {timeout_s:.0f}s "
        f"(chars={last}, composer enabled={enabled}, thinking pills={thinking})"
    )


def uniform_canvas(sizes: list[tuple[int, int]], out_width: int = OUTPUT_WIDTH) -> tuple[int, int]:
    """One canvas every tile shares: widest width, median-anchored height.

    ⚠ **Anchored on the MEDIAN, capped, not on the maximum.** Taking the tallest
    shot as the canvas is the obvious choice and it is wrong the moment one
    answer runs long: four tiles then carry a screen of empty space so the fifth
    can be lossless. Taking the median exactly is wrong the other way — every
    long answer shrinks even when the set is uniform. So the canvas is the
    tallest shot UNLESS that exceeds `_HEIGHT_CAP_RATIO` times the median, in
    which case the outlier is the thing that gives.

    A uniform set therefore scales NOTHING (max ≤ median x 1.6 → canvas = max),
    which is the common case and costs no sharpness at all.
    """
    if not sizes:
        raise ValueError("no shots to compose — nothing was captured")
    widths = [w for w, _ in sizes]
    heights = [h for _, h in sizes]
    cap = int(round(statistics.median(heights) * _HEIGHT_CAP_RATIO))
    raw_w, raw_h = max(widths), min(max(heights), cap)
    # Down to the delivered width, never up: a capture narrower than
    # `out_width` is already as sharp as it will ever be, and enlarging it is
    # the same upscale `fit_scale` refuses one level down.
    k = min(1.0, out_width / raw_w)
    return max(1, round(raw_w * k)), max(1, round(raw_h * k))


def fit_scale(size: tuple[int, int], canvas: tuple[int, int]) -> float:
    """How far to shrink one shot so it fits, never above 1.0.

    ⚠ **Never upscales.** A shot shorter than the canvas is PADDED, not
    stretched: enlarging a capture past its own pixels is the soft-text failure
    `_demo_zoom` already learned (its `clamp_to_headroom` is the same rule for
    the video crop). Padding costs whitespace; upscaling costs legibility on
    the tile that did not need anything doing to it.
    """
    (w, h), (cw, ch) = size, canvas
    if w <= 0 or h <= 0:
        raise ValueError(f"a shot has no pixels: {size}")
    return min(1.0, cw / w, ch / h)


#: Pixels of background kept around the content after trimming.
_TRIM_PAD = 24

#: How different from the background a pixel must be to count as content.
#: Low, because the app's surfaces sit close to its page colour — a high
#: threshold would trim away the cards and keep only the text.
_TRIM_TOLERANCE = 12


def trim_margins(img: Any, pad: int = _TRIM_PAD) -> Any:
    """Crop the uniform background border off a shot.

    ⚠ **This is what makes the text big enough to read.** The thread element
    is as wide as the column and as tall as the layout leaves it, so a short
    answer captures with a screenful of empty page around it — and once every
    tile is scaled to a shared canvas, that empty space is what the scaling
    spends its pixels on. Trimming first means the ANSWER fills the frame
    rather than the margin.

    ⚠ **Fails OPEN.** A shot that is uniformly background has no bounding box,
    and an image that trims to nothing is worse than one with wide margins —
    the same decline-to-act rule `_only_matching` follows. Returns the
    original untouched in that case.
    """
    from PIL import Image, ImageChops

    ref = Image.new("RGB", img.size, img.getpixel((0, 0)))
    diff = ImageChops.difference(img.convert("RGB"), ref)
    # Amplify so a near-background surface still clears getbbox's zero test,
    # then threshold: a tolerance read as "close enough to background".
    box = diff.point(lambda v: 255 if v > _TRIM_TOLERANCE else 0).convert("L").getbbox()
    if not box:
        return img
    left, top, right, bottom = box
    return img.crop(
        (
            max(0, left - pad),
            max(0, top - pad),
            min(img.width, right + pad),
            min(img.height, bottom + pad),
        )
    )


def compose_uniform(
    shots: list[Path], dest: Path, out_width: int = OUTPUT_WIDTH
) -> list[dict[str, Any]]:
    """Lay every shot on one shared canvas so the set reads as a set.

    Each tile is centred on its OWN background colour, sampled from its top-left
    pixel — so the padding is invisible and the tile stays theme-correct without
    this script knowing anything about the app's palette.
    """
    from PIL import Image

    # ⚠ Trim BEFORE measuring. The canvas is derived from these sizes, so
    # sizing on the untrimmed shots would budget the shared frame for margin
    # that is about to be thrown away — every tile then scales down to fit
    # whitespace, which is exactly how the first batch came out unreadable.
    opened = [(p, trim_margins(Image.open(p).convert("RGB"))) for p in shots]
    canvas = uniform_canvas([im.size for _, im in opened], out_width)
    dest.mkdir(parents=True, exist_ok=True)

    # The scale every tile gets from the width alone — the set's own baseline.
    baseline = canvas[0] / max(im.width for _, im in opened)

    out: list[dict[str, Any]] = []
    for path, img in opened:
        scale = fit_scale(img.size, canvas)
        if scale < 1.0:
            img = img.resize(
                (max(1, round(img.width * scale)), max(1, round(img.height * scale))),
                Image.LANCZOS,
            )
        # Sampling AFTER the trim is safe and was checked rather than assumed:
        # the crop's origin is `max(0, edge - pad)`, so the new top-left is
        # either the old one or a pixel outside the content box — background
        # either way. Sampling the untrimmed source instead cannot change any
        # output, so it would be a distinction to maintain and never observe.
        tile = Image.new("RGB", canvas, img.getpixel((0, 0)))
        tile.paste(img, ((canvas[0] - img.width) // 2, (canvas[1] - img.height) // 2))
        target = dest / path.name
        tile.save(target)
        out.append(
            {
                "file": target.name,
                "size": list(canvas),
                "scale": round(scale, 3),
                "relative": round(scale / baseline, 3) if baseline else 1.0,
                "crisp": bool(baseline) and scale / baseline >= _MIN_RELATIVE_SCALE,
            }
        )
    return out


#: Gap between tiles on the contact sheet, in output pixels.
_SHEET_GAP = 24

#: How wide the whole contact sheet may be. Five portrait tiles side by side
#: would otherwise be 8,000px — a strip nothing can open usefully.
_SHEET_MAX_WIDTH = 3200


def contact_sheet(tiles: list[Path], dest: Path) -> dict[str, Any] | None:
    """One image with every tile side by side, so the SET can be judged at once.

    ⚠ This exists because "open them all" is not a thing an operating system
    does. `start file.png` opens ONE; Explorer thumbnails are too small to read
    and too small to compare; a Photos slideshow shows them one at a time,
    which is exactly the wrong view for "are these the same size and do they
    make sense together". The question is comparative, so the artifact has to
    be comparative.

    Returns `None` for a single tile — a contact sheet of one is just a copy
    under a confusing name.
    """
    from PIL import Image

    if len(tiles) < 2:
        return None

    opened = [Image.open(p).convert("RGB") for p in tiles]
    w, h = opened[0].size
    n = len(opened)
    total = n * w + (n - 1) * _SHEET_GAP
    # Shrink to fit the cap, never enlarge — the same rule as everywhere else.
    k = min(1.0, _SHEET_MAX_WIDTH / total)
    tw, th, gap = max(1, round(w * k)), max(1, round(h * k)), max(1, round(_SHEET_GAP * k))

    sheet = Image.new("RGB", (n * tw + (n - 1) * gap, th), opened[0].getpixel((0, 0)))
    for i, img in enumerate(opened):
        sheet.paste(img.resize((tw, th), Image.LANCZOS) if k < 1.0 else img, (i * (tw + gap), 0))

    path = dest / "all-five.png"
    sheet.save(path)
    return {"file": path.name, "size": list(sheet.size), "tiles": n}


async def shoot(page: Any, question: str, out: Path) -> dict[str, Any]:
    """Ask one question in a fresh thread and write ONE image of the exchange."""
    stem = label_for(question)
    await _start_fresh_thread(page)
    await asyncio.sleep(0.8)

    composer = page.locator(COMPOSER).first
    await composer.click()
    await composer.fill(question)
    await composer.press("Enter")

    started = time.monotonic()
    answer = await wait_for_settled_answer(page)
    # A beat after settling: card payloads (tables, charts, next steps) render
    # AFTER the prose finishes, and they are the half worth photographing
    # (gotcha 56 — `final_text` is prose only; the cards are separate events).
    await asyncio.sleep(1.5)

    thread = page.locator(CONVERSATION_THREAD).first
    await thread.evaluate(_EXPAND_JS)
    shot = out / f"{stem}.png"
    try:
        await thread.screenshot(path=str(shot))
    finally:
        with contextlib.suppress(Exception):
            await page.evaluate(_RESTORE_JS)

    return {
        "file": shot.name,
        "question": question,
        "chars": len(answer),
        "seconds": round(time.monotonic() - started, 1),
        "answer": answer,
    }


def finish(out: Path, out_width: int = OUTPUT_WIDTH) -> int:
    """Compose whatever is in `out/raw` into uniform tiles in `out`.

    Split from `run` so a re-compose costs NOTHING — the shots are the
    expensive half (five live agent turns), the layout is a taste decision that
    wants iterating, and coupling them would mean re-running the browser to
    change a margin.
    """
    raw = sorted((out / "raw").glob("*.png"))
    if not raw:
        print(f"no shots in {(out / 'raw').resolve()} — run without --compose-only first")
        return 1
    laid = compose_uniform(raw, out, out_width)
    w, h = laid[0]["size"]
    print(f"\n{len(laid)} tiles at {w}x{h} -> {out.resolve()}")
    for row in laid:
        note = "" if row["crisp"] else "   <- long answer, shown smaller than the others"
        print(f"  {row['file']}  {row['relative']:.0%} of the others{note}")
    sheet = contact_sheet([out / r["file"] for r in laid], out)
    if sheet:
        print(f"\n  ALL FIVE IN ONE IMAGE:  {(out / sheet['file']).resolve()}")

    soft = [r["file"] for r in laid if not r["crisp"]]
    if soft:
        # ⚠ A warning, not a failure. The tile is usable; the honest fix is a
        # shorter question, and that is the founder's call, not the script's.
        print(
            f"\n  ⚠ {len(soft)} answer(s) ran long enough to be shown smaller than the "
            "rest.\n    They are complete and readable, just not the same text size. "
            "Shorten those questions to fix it properly."
        )
    return 0


async def run(url: str, out: Path, headed: bool, out_width: int = OUTPUT_WIDTH) -> int:
    from playwright.async_api import async_playwright

    raw = out / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not headed)
        ctx = await browser.new_context(
            viewport=dict(VIEWPORT),
            device_scale_factor=DEVICE_SCALE,
            storage_state=str(SESSION_FILE) if SESSION_FILE.exists() else None,
        )
        page = await ctx.new_page()
        await page.goto(url, wait_until="domcontentloaded")

        # ⚠ Refuse to photograph a sign-in screen. Three demo takes were lost
        # to exactly this: the session expires, the app drops to a login form,
        # and the capture records it happily (see chat_probe's own docstring).
        try:
            await page.locator(COMPOSER).first.wait_for(state="attached", timeout=30_000)
        except Exception:
            await browser.close()
            print(
                f"signed out — refusing to shoot. ({SESSION_FILE}: "
                f"{'present' if SESSION_FILE.exists() else 'absent'})\n"
                f"Run:  python scripts/chat_probe.py --login --url {url}",
                file=sys.stderr,
            )
            return 2

        for i, question in enumerate(FEATURE_SHOTS, 1):
            print(f"[{i}/{len(FEATURE_SHOTS)}] {question}")
            try:
                got = await shoot(page, question, raw)
            except Exception as exc:  # noqa: BLE001 — one bad turn is not the run
                print(f"    FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
                results.append({"question": question, "error": str(exc)})
                continue
            print(f"    -> {got['file']}  ({got['chars']} chars, {got['seconds']}s)")
            results.append(got)

        await browser.close()

    (out / "shots.json").write_text(
        json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    ok = [r for r in results if "error" not in r]
    print(f"\n{len(ok)}/{len(results)} shot -> {raw.resolve()}")
    # ⚠ Compose whatever landed. A partial run still deserves usable tiles —
    # and a run that produced no image is not a success, whatever it printed.
    if ok:
        finish(out, out_width)
    return 0 if len(ok) == len(results) and ok else 1


def main() -> int:
    force_utf8_console()
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--url", default=DEFAULT_URL)
    p.add_argument("--out", default="shots")
    p.add_argument("--headed", action="store_true", help="watch it drive the browser")
    p.add_argument("--login", action="store_true", help="sign in by hand and save the session")
    p.add_argument(
        "--compose-only",
        action="store_true",
        help="re-lay the existing shots in <out>/raw without asking anything again",
    )
    p.add_argument(
        "--out-width",
        type=int,
        default=OUTPUT_WIDTH,
        help=f"finished tile width in pixels (default {OUTPUT_WIDTH}); "
        "lower makes every tile smaller, it does not crop anything",
    )
    args = p.parse_args()
    try:
        if args.login:
            return asyncio.run(interactive_login(args.url))
        if args.compose_only:
            return finish(Path(args.out), args.out_width)
        return asyncio.run(run(args.url, Path(args.out), args.headed, args.out_width))
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "compose_uniform",
    "contact_sheet",
    "finish",
    "fit_scale",
    "label_for",
    "shoot",
    "uniform_canvas",
    "wait_for_settled_answer",
]
