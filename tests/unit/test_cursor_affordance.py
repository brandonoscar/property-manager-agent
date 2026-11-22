"""Every clickable thing in the app must show a hand.

Tailwind v4's preflight DELIBERATELY dropped ``cursor: pointer`` on buttons --
v3 set it, v4 matches the browser default of ``cursor: default``. Nothing in
this repo put it back, so measured 2026-08-30: **132 ``<button`` elements
across ``frontend/src/components``, 29 carrying an explicit ``cursor-pointer``**
-- meaning 103 clickable things rendered with an arrow. The CRM pipeline cards,
the Operations tab strip and every card whose whole surface is a button. The
handful that felt right were the ones somebody had hand-annotated.

The failure mode is SILENCE: nothing errors, nothing logs, the control works
perfectly for anyone who tries clicking it anyway. That is what makes it a
corpus guard rather than a behavioural one.

⚠ This reads the CORPUS, not the fix (gotcha 45). A test asserting only "the
CSS contains a cursor rule" would pass forever while the ratio it protects
drifted underneath it.

⚠ It lives in the PYTHON suite on purpose. The obvious home is a vitest file
next to ``globals.css``, and that home is a trap: this project has no
``@types/node``, and both ``import css from './globals.css?raw'`` and
``import.meta.glob('./globals.css', {query: '?raw'})`` return the **empty
string** under the vitest config (CSS is stubbed). An empty string still
satisfies ``typeof === 'string'``, so a guard written there reads nothing and
reports green -- gotcha 49's "a scan that collects zero cases passes". The
backend suite already owns this shape of check
(``test_scripts_console_encoding.py``, ``test_powershell_scripts_ascii.py``).

⚠ Comments are stripped before scanning (gotchas 40/59). The rule's own
explanation quotes ``cursor: default`` and ``cursor-pointer`` in order to
explain itself, so a whole-file scan matches the sentence documenting the
guard rather than the guard.
"""

from __future__ import annotations

import re
from pathlib import Path

_FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "src"
_CSS = _FRONTEND / "styles" / "globals.css"
_COMPONENTS = _FRONTEND / "components"


def _statements() -> str:
    """The stylesheet with block comments removed -- rules, not prose."""
    return re.sub(r"/\*.*?\*/", "", _CSS.read_text(encoding="utf-8"), flags=re.DOTALL)


def test_an_enabled_button_gets_a_pointer() -> None:
    assert re.search(
        r"button:not\(:disabled\)[^{]*\{[^}]*cursor:\s*pointer", _statements(), re.DOTALL
    ), (
        "the base pointer rule is gone -- Tailwind v4's preflight does not set "
        "one, so most of the app's buttons are back to showing an arrow"
    )


def test_a_disabled_button_gets_not_allowed() -> None:
    """The other direction, and it is not cosmetic: a disabled control showing
    a hand invites the click it is about to swallow."""
    assert re.search(r"button:disabled[^{]*\{[^}]*cursor:\s*not-allowed", _statements(), re.DOTALL)


def test_the_rule_is_layered_so_a_component_can_still_override_it() -> None:
    """Unlayered CSS beats every ``@layer``, so an unlayered rule here would
    override a deliberate ``cursor-default`` / ``cursor-wait`` anywhere in the
    app.

    That is the exact trap the margin/padding reset directly above it already
    records in its own comment -- an unlayered reset silently no-op'd every
    Tailwind spacing utility app-wide.
    """
    css = _statements()
    layer = css.index("@layer base")
    rule = css.index("button:not(:disabled)")
    assert rule > layer, "the cursor rule sits before @layer base"
    # The layer must not have closed between the two.
    assert "@layer" not in css[layer + len("@layer base") : rule], (
        "the cursor rule is outside the base layer -- it now beats every "
        "component that deliberately sets its own cursor"
    )


def test_most_buttons_still_depend_on_the_base_rule() -> None:
    """The measurement that makes the rule worth keeping.

    If someone ever annotates every button by hand this flips and the base
    rule becomes optional -- but until then, deleting it silently un-clicks
    most of the app.
    """
    buttons = 0
    annotated = 0
    files = [p for p in _COMPONENTS.rglob("*.tsx") if ".test." not in p.name]
    for path in files:
        src = path.read_text(encoding="utf-8")
        buttons += len(re.findall(r"<button", src))
        annotated += len(re.findall(r"cursor-pointer", src))

    # Guard the scan itself: a wrong path collecting zero files would report
    # green against the very defect it targets (gotcha 49).
    assert len(files) > 20, f"only {len(files)} component files found -- the scan is broken"
    assert buttons > 50, f"only {buttons} buttons found -- the scan is broken"
    assert annotated < buttons / 2, (
        f"{annotated} of {buttons} buttons now carry their own cursor -- if that "
        "is deliberate, this guard's premise has changed and it should be "
        "rewritten rather than deleted"
    )
