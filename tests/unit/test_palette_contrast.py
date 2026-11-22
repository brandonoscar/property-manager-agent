"""Colour contrast, measured — and a ratchet so it cannot quietly get worse.

The accessibility page said the palette "was designed for" WCAG AA and had
"never been measured". This is the measurement, computed from `globals.css`
rather than asserted from memory, so a palette edit is scored the moment it
lands.

⚠ **This lives in pytest and not in vitest, and that is not a preference.**
Vitest stubs CSS imports to the EMPTY STRING in the jsdom environment — both
`import css from './globals.css?raw'` and the `import.meta.glob` form return
`''`, measured. Every "nothing is below the floor" assertion would then be
vacuously true, which is the exact failure mode this file exists to catch. The
frontend has no `@types/node`, so reading the file from disk there does not
typecheck either. Python reads it in three lines, and this is the same reason
`test_frontend_security_headers.py` and `test_legal_pages_name_the_operator.py`
read frontend files from here.

## What the numbers actually said (2026-09-08, against --canvas #FFFFFF)

    --ink            18.19    --ink-secondary  9.29    --iris-press  8.74
    --chrome-ink     15.82    --ink-muted      6.39    --danger      6.54
    --ink-subtle      4.73    --iris           4.96    --caution     4.70
    --iris-hover      4.15  <- AA large text only
    --ink-faint       2.61  <- FAILS even the 3:1 large-text bar
    --iris-light      2.50  <- not used as text (one chip fill)

⚠ **`--ink-faint` is a real AA failure and is deliberately NOT fixed here.** It
is used as text in 74 places, and every value reaching 4.5:1 is darker than
`--ink-subtle` — so raising it alone inverts the ramp and the "faintest" token
becomes more prominent than the one above it. Fixing it properly means
re-spacing three tokens across the whole app: a DESIGN.md change and a founder's
call, not something to smuggle into a launch-day branch. What WAS fixed is the
one place it carried instructions somebody has to read — the unmet password
requirements, now `--ink-subtle`.

⚠ **A ratchet, not a conformance gate.** Failing on today's palette would
red-gate every unrelated PR until that design decision lands, and a gate nobody
can satisfy is one people switch off. It fails when a token gets WORSE than it
is today, and when a NEW token arrives below the 3:1 floor — both directions
nobody would otherwise notice.
"""

from __future__ import annotations

import re
from pathlib import Path

_CSS = Path(__file__).resolve().parents[2] / "frontend" / "src" / "styles" / "globals.css"

#: Foreground tokens measured against --canvas, as they stand today.
_BASELINE = {
    "--ink": 18.19,
    "--titlebar-ink": 18.19,
    "--chrome-ink": 15.82,
    "--ink-secondary": 9.29,
    "--iris-press": 8.74,
    "--chrome-ink-muted": 6.82,
    "--danger": 6.54,
    "--ink-muted": 6.39,
    "--titlebar-ink-muted": 6.39,
    "--iris": 4.96,
    "--ink-subtle": 4.73,
    "--caution": 4.70,
    "--iris-hover": 4.15,
    "--ink-faint": 2.61,
    "--iris-light": 2.50,
}


def _tokens() -> dict[str, str]:
    css = _CSS.read_text(encoding="utf-8")
    return dict(re.findall(r"(--[\w-]+):\s*(#[0-9A-Fa-f]{3,8})\s*;", css))


def _luminance(hex_colour: str) -> float:
    h = hex_colour.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    channels = []
    for i in (0, 2, 4):
        c = int(h[i : i + 2], 16) / 255
        channels.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    r, g, b = channels
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    la, lb = _luminance(a), _luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def test_the_palette_is_being_read_at_all() -> None:
    """An empty parse makes every assertion below vacuously true — which is
    precisely how the vitest version of this file passed while measuring
    nothing."""
    tokens = _tokens()
    assert len(tokens) > 20, f"only parsed {len(tokens)} colour tokens"
    assert "--canvas" in tokens


def test_the_baseline_still_names_tokens_that_exist() -> None:
    """A renamed token silently drops out of the ratchet, which is how a
    regression walks past a guard that only checks what it can find."""
    present = _tokens()
    gone = sorted(t for t in _BASELINE if t not in present)
    assert not gone, f"the baseline names tokens that no longer exist: {gone}"


def test_no_measured_token_got_worse() -> None:
    tokens = _tokens()
    canvas = tokens["--canvas"]
    worse = []
    for name, was in _BASELINE.items():
        now = contrast(tokens[name], canvas)
        # 0.05 of slack absorbs rounding in the recorded figures; beyond that
        # is a real lightening of the text or darkening of the ground.
        if now < was - 0.05:
            worse.append(f"{name}: {was} -> {now:.2f}")
    assert not worse, "contrast regressed:\n  " + "\n  ".join(worse)


def test_no_new_text_token_lands_below_the_large_text_floor() -> None:
    """The two already below 3:1 are recorded above with the reason they stay.
    A third arriving unremarked is what this catches."""
    tokens = _tokens()
    canvas = tokens["--canvas"]
    offenders = []
    for name, hexv in tokens.items():
        if not re.match(r"^--(ink|iris|danger|caution|success)", name):
            continue
        if name in _BASELINE or re.search(r"(bg|soft|ring|line|edge)", name):
            continue
        ratio = contrast(hexv, canvas)
        if ratio < 3:
            offenders.append(f"{name} ({hexv}, {ratio:.2f}:1)")
    assert not offenders, (
        f"new colour token(s) below 3:1 on --canvas: {offenders}. If it is "
        "decorative and never carries text, exclude it explicitly; if it is "
        "text, it needs 4.5:1."
    )


def test_the_password_checklist_does_not_use_the_failing_token() -> None:
    """The one instance that was fixed rather than recorded.

    An unmet requirement is the line somebody has to READ in order to pick a
    password that will be accepted, so it must not be the hardest text on the
    screen to see — which is exactly where the palette had it.
    """
    settings = (_CSS.parents[1] / "components" / "SettingsPage.tsx").read_text(encoding="utf-8")
    checklist = re.search(
        r"color:\s*r\.met\s*\?\s*\"var\(--ink-muted\)\"\s*:\s*\"var\((--[\w-]+)\)\"",
        settings,
    )
    assert checklist, "the password-requirement colours moved — re-point this guard"
    unmet = checklist.group(1)
    tokens = _tokens()
    ratio = contrast(tokens[unmet], tokens["--canvas"])
    assert ratio >= 4.5, (
        f"unmet password requirements render in {unmet} at {ratio:.2f}:1, under "
        "the 4.5:1 AA bar for text this size"
    )
