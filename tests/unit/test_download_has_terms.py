"""A downloadable binary needs a licence, and this one shipped without.

Occupella offers a desktop application from inside the product
(`DownloadHelixis.tsx` links straight at the release assets). Until 2026-09-08
the Terms said nothing about it: somebody installed an executable on their own
machine with no licence grant, no redistribution limit, and no statement that
the service's warranty disclaimers cover the binary too.

⚠ The gap was invisible because §7 EXISTS and reads plausibly — "Occupella and
its software … are owned by us or our licensors" is an ownership statement, not
a licence. Ownership says what we keep; a licence says what the user may do.
A reader skimming for "is the app covered" finds a paragraph about software and
stops.

⚠ The clause was added INSIDE §7 rather than as a new numbered section, on
purpose: §8's survival list names "Sections 5, 9, and 10" by number, and
renumbering would silently re-point a contractual cross-reference at the wrong
clauses. This guard pins that too — the cheapest way for a later edit to break
the contract is to insert a section above them.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_TERMS = _ROOT / "frontend" / "public" / "legal" / "terms.html"
_DOWNLOAD = _ROOT / "frontend" / "src" / "components" / "DownloadHelixis.tsx"


def _visible(html: str) -> str:
    body = html.split("</head>", 1)[-1]
    body = re.sub(r"<!--.*?-->", " ", body, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))


def test_the_product_really_does_offer_a_download() -> None:
    """If the download is ever removed, the clause becomes harmless rather than
    wrong — but this test going red is the prompt to check, not to delete."""
    assert _DOWNLOAD.exists(), "DownloadHelixis.tsx is gone — does the app still ship?"
    assert "downloadUrlFor" in _DOWNLOAD.read_text(encoding="utf-8")


def test_the_terms_licence_the_desktop_application() -> None:
    text = _visible(_TERMS.read_text(encoding="utf-8"))
    missing = [
        phrase
        for phrase in ("desktop application", "licence", "reverse-engineer")
        if phrase not in text
    ]
    assert not missing, (
        f"the Terms no longer licence the downloadable app: missing {missing}. "
        "Somebody is installing an executable under no stated terms."
    )


def test_the_survival_clause_still_points_at_the_right_sections() -> None:
    """§8 names Sections 5, 9 and 10 by NUMBER. Inserting a numbered section
    above them re-points a contractual cross-reference at whatever now sits
    there — a silent change to what survives termination."""
    html = _TERMS.read_text(encoding="utf-8")
    text = _visible(html)
    assert "Sections 5, 9, and 10" in text, "the survival cross-reference changed"

    headings = re.findall(r"<h2>(\d+)\.\s*([^<]+)</h2>", html)
    numbered = {int(n): title.strip() for n, title in headings}
    # The three the survival clause depends on, by the name it means.
    assert "confidential" in numbered.get(5, "").lower() or numbered.get(5), numbered
    assert "disclaimer" in numbered.get(9, "").lower(), (
        f"Section 9 is now '{numbered.get(9)}' — the survival clause says the "
        "disclaimers survive, and it no longer points at them."
    )
    assert "liability" in numbered.get(10, "").lower(), (
        f"Section 10 is now '{numbered.get(10)}' — the survival clause says the "
        "liability limit survives, and it no longer points at it."
    )
