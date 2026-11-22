"""Every legal page says who you are actually dealing with.

⚠ The Terms of Service did not name the contracting entity ANYWHERE. Section 12
was "Questions about these Terms: team@occupella.com" — an agreement whose text
never identifies the party on the other side of it. The Privacy Policy had the
same hole in its controller section, and the billing page never said whose
subscription it is. Three of six pages, on the day of a paid public launch.

⚠ **This guard reads VISIBLE TEXT, and that is not fussiness.** A plain
`grep -c "Oscar Ventures LLC"` reports ZERO on the fixed privacy page, because
the name wraps across two source lines. The same class as gotcha 77's carrier
phrase split by `<strong>` tags, and gotchas 40/49's line-based scans: a check
that reads bytes answers a different question from the one a reader asks.

⚠ **STILL OPEN, recorded rather than guessed: there is no postal address on any
page.** GDPR Article 13 asks for the controller's contact details, CCPA expects
a designated contact method, and CAN-SPAM requires a physical address in
commercial email. Oscar Ventures LLC's address is written nowhere in this repo
— the A2P flow collects the CUSTOMER's address, not ours — so inventing one
would put a false statement on a legal page, which is worse than the gap. The
last test below does not demand an address; it makes sure that once one appears
it appears on every page, so the fix cannot land half-applied.
"""

from __future__ import annotations

import re
from pathlib import Path

_LEGAL = Path(__file__).resolve().parents[2] / "frontend" / "public" / "legal"

#: The registered entity, exactly as the app shell and the login screen say it.
_OPERATOR = "Oscar Ventures LLC"
_SUPPORT_EMAIL = "team@occupella.com"

#: A US street address, loosely.
#:
#: ⚠ Loose enough that it CANNOT be used on a whole page, and this guard's own
#: first run proved it: the SMS page carries sample messages reading
#: "rent of $1,450 for Unit 4B, 400 Oak St", which is a fictitious PROPERTY in
#: an example — not the operator's contact address. Matched twice, failed the
#: test, and the verdict would have been "an address already exists" for a page
#: that has none. So it is only ever applied in the window after the operator's
#: own name, which is where a contact block puts one.
_ADDRESS_RE = re.compile(
    r"\b\d{1,6}\s+[A-Z][\w.'-]*(?:\s+[\w.'-]+){0,4}\s+"
    r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Lane|Ln|Way|Suite|Ste|Court|Ct|Place|Pl)\b",
    re.IGNORECASE,
)


def _pages() -> dict[str, str]:
    pages = {p.name: p.read_text(encoding="utf-8") for p in sorted(_LEGAL.glob("*.html"))}
    assert len(pages) >= 5, f"only found {len(pages)} legal pages — the glob is wrong"
    return pages


def _visible(html: str) -> str:
    """What a reader sees: comments gone, tags gone, whitespace collapsed.

    Collapsing whitespace is the load-bearing step — without it the operator
    name is invisible on any page where the source happens to wrap between
    "Ventures" and "LLC", which is most of them.
    """
    body = html.split("</head>", 1)[-1]
    body = re.sub(r"<!--.*?-->", " ", body, flags=re.S)
    body = re.sub(r"<[^>]+>", " ", body)
    return re.sub(r"\s+", " ", body)


def _has_operator_address(html: str) -> bool:
    """Does an address appear as OURS, rather than as an example?

    Only the text following the operator's own name counts — a contact block
    reads "Oscar Ventures LLC, <address>", while a sample rent reminder never
    names the company at all.
    """
    text = _visible(html)
    return any(
        _ADDRESS_RE.search(text[m.end() : m.end() + 300])
        for m in re.finditer(re.escape(_OPERATOR), text)
    )


def test_every_legal_page_names_the_operator() -> None:
    missing = sorted(name for name, html in _pages().items() if _OPERATOR not in _visible(html))
    assert not missing, (
        f"these legal pages never say who operates the service: {missing}. "
        "A policy that does not identify the party is not a policy anybody can "
        "act on."
    )


def test_every_legal_page_offers_a_way_to_reach_a_person() -> None:
    missing = sorted(
        name for name, html in _pages().items() if _SUPPORT_EMAIL not in _visible(html)
    )
    assert not missing, f"no contact route on: {missing}"


def test_the_operator_name_is_spelled_one_way() -> None:
    """Six pages each write it independently, so a rename drifts silently and
    the pages start describing two companies."""
    for name, html in _pages().items():
        text = _visible(html)
        for wrong in ("Oscar Ventures, LLC", "Oscar Ventures Llc", "Oscar Ventures LLC."):
            assert wrong not in text, f"{name} spells the operator '{wrong}'"


def test_a_postal_address_lands_on_every_page_or_none() -> None:
    """The open item, in the only form that can pass honestly today.

    No page carries an address, so this passes. The moment one does, every page
    has to — a controller address on the privacy policy and not on the terms is
    the half-applied state that reads as an oversight to a regulator and as a
    complete disclosure to us.
    """
    carrying = sorted(name for name, html in _pages().items() if _has_operator_address(html))
    if not carrying:
        return  # today's state: the gap is named in this module's docstring.
    missing = sorted(set(_pages()) - set(carrying))
    assert not missing, (
        f"a postal address was added to {carrying} but not to {missing}. "
        "It belongs on all of them or none — see this file's header for why "
        "the address is missing in the first place."
    )
