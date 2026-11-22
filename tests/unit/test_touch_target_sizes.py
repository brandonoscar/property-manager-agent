"""How big the tap targets actually are, and the floor they must not drop below.

The audit carried this as "NOT MEASURED. Several controls are 11px text in
tight rows" with "do this before launch" against it. This is the measurement.

## What it found (2026-09-08)

    interactive <button>/<a>          204
      with an explicit height          81
      under the 44px guidance          74   (43 of them at 32px)
      under WCAG 2.2 AA's 24x24         1   <- fixed

⚠ **Two different bars, and conflating them is what makes this row useless.**
44x44 is WCAG 2.5.5, level **AAA**, and a guideline this product does not claim.
24x24 is WCAG 2.5.8, level **AA**, added in WCAG 2.2 — that one is a real bar,
and exactly one control was under it: a 20x20 dismiss button on the invite
banner, now 24x24. The cost of missing a dismiss button on a phone is tapping
whatever is behind it, which is the worst kind of miss.

The 32px rows are a DENSITY decision, not an oversight: this is a data-dense
desktop tool whose tables put many rows on screen at once, and 44px everywhere
would change what the product is. Recorded as a decision rather than silently
left, per this repo's own rule about saying which gaps are choices.

⚠ **123 controls have NO explicit height and are unmeasurable from here.** Their
size comes from padding and line-height resolved by a browser. Saying "204
controls are fine" would be a claim about 123 elements nobody has looked at —
the honest figure is the 81 that state their own height. Closing that gap needs
a real device, which is what audit row 461 is for and is still open.
"""

from __future__ import annotations

import re
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "frontend" / "src"

#: WCAG 2.2 AA (2.5.8). The AAA bar of 44px is deliberately not enforced —
#: see the module docstring.
_MIN_AA_PX = 24


def _opening_tag(source: str, start: int) -> str:
    """The tag text, scanned with BALANCED braces.

    A regex stopping at the first `}` truncates any tag carrying a handler with
    a block body, and then misses the very attributes being looked for.
    """
    depth = 0
    for i in range(start, len(source)):
        c = source[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        elif c == ">" and depth == 0:
            return source[start:i]
    return source[start:]


def _declared_size(tag: str) -> float | None:
    """Height in px when the element states one, else None.

    Tailwind's `h-N` is N*4px. `None` means "resolved by the browser from
    padding", which cannot be answered from source and must not be counted as
    a pass.

    ⚠ **Comments are stripped first, and a mutation is what found that.** JSX
    allows `//` comments between attributes, and the fix on the invite banner's
    dismiss button carries one reading "h-6 w-6, not h-5 w-5". The scanner read
    the COMMENT's `h-6` and reported 24px — so reverting the button to 20x20
    left this guard green, on the single element it was written for. Same class
    as every other scan in this repo that matched the sentence explaining the
    rule instead of the rule.
    """
    tag = re.sub(r"//[^\n]*", "", tag)
    tw = re.search(r"\bh-(\d+(?:\.\d+)?)\b", tag)
    if tw:
        return float(tw.group(1)) * 4
    px = re.search(r"height:\s*(\d+)", tag)
    return float(px.group(1)) if px else None


def _controls() -> list[tuple[str, int, float]]:
    out: list[tuple[str, int, float]] = []
    for path in sorted(_SRC.rglob("*.tsx")):
        if ".test." in path.name:
            continue
        source = path.read_text(encoding="utf-8")
        for m in re.finditer(r"<(button|a)\b", source):
            tag = _opening_tag(source, m.start())
            if "onClick" not in tag and "href" not in tag:
                continue
            size = _declared_size(tag)
            if size is None:
                continue
            line = source[: m.start()].count("\n") + 1
            out.append((str(path.relative_to(_SRC)), line, size))
    return out


def test_the_scan_finds_controls_at_all() -> None:
    """A selector that stops matching reports a clean sweep over nothing."""
    found = _controls()
    assert len(found) > 40, f"only {len(found)} sized controls found — the scan is broken"


def test_no_control_is_below_the_wcag_aa_minimum() -> None:
    too_small = [f"{f}:{line} ({int(px)}px)" for f, line, px in _controls() if px < _MIN_AA_PX]
    assert not too_small, (
        f"tap targets under WCAG 2.2 AA's {_MIN_AA_PX}x{_MIN_AA_PX}: {too_small}. "
        "On a phone the cost of missing one is activating whatever sits behind it."
    )


def test_the_measurement_still_describes_this_codebase() -> None:
    """The docstring above quotes real numbers, and a comment cannot fail.

    Not an exact pin — controls come and go — but a drift large enough to make
    the recorded figures misleading should force somebody to re-measure and
    re-state them rather than leave prose nobody re-checked.
    """
    sized = _controls()
    under_44 = [c for c in sized if c[2] < 44]
    assert 50 <= len(sized) <= 130, (
        f"{len(sized)} sized controls now, against 81 when this was written — "
        "re-run the measurement and update the docstring."
    )
    assert len(under_44) <= 110, (
        f"{len(under_44)} controls under the 44px guidance, against 74 when this "
        "was written. That is a density drift worth a deliberate look."
    )
