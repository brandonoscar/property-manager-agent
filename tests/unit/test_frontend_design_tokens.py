"""globals.css stays on occupella.com's type and colour (2026-09-29).

The TSX half of this guard is ``frontend/src/designTokens.test.ts``. This half
exists because vitest returns a CSS import as an empty string, ``?raw``
included, so a vitest check of the stylesheet passes on nothing.

Comments are removed first: the stylesheet explains its history, and a
comment quoting the old value must not trip the rule (gotcha 40).
"""

import re
from pathlib import Path

CSS = Path(__file__).resolve().parents[2] / "frontend" / "src" / "styles" / "globals.css"
RETIRED = re.compile(r"geist|playfair|#1e73bc", re.IGNORECASE)


def _code() -> str:
    return re.sub(r"/\*.*?\*/", "", CSS.read_text(encoding="utf-8"), flags=re.DOTALL)


def test_nothing_is_set_below_12px() -> None:
    small = [
        m.group(0)
        for m in re.finditer(r"font(?:-size)?:[^;]*?(\d+(?:\.\d+)?)px", _code())
        if float(m.group(1)) < 12
    ]
    assert small == []


def test_no_retired_face_or_colour() -> None:
    assert RETIRED.findall(_code()) == []


def test_the_blue_and_the_faces_are_the_sites() -> None:
    code = _code()
    assert re.search(r"--iris:\s*#1957A0", code, re.IGNORECASE)
    assert code.count('"IBM Plex Sans Variable"') == 2  # @theme and :root mirror each other
    assert code.count('"Fraunces Variable"') >= 2
    assert '"IBM Plex Mono"' in code


def test_the_checker_reads_real_css() -> None:
    assert len(_code()) > 5000
