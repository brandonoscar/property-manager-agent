"""Stripe sends a paying customer back to the PRODUCT, not to the front door.

`POST /billing/checkout` and `POST /billing/portal` both hand Stripe a URL to
return to. Everybody who reaches either is already signed in and mid-payment,
so the destination is the web app — landing them on the public landing page is
a dead end at the exact moment they are handing us money.

⚠ THE LITERAL WAS `https://occupella.com` AND IT STOPPED BEING RIGHT WITHOUT
CHANGING. The apex was this app until the 2026-09-15 domain swap; afterwards it
is the marketing site and the app is `app.occupella.com`. Nothing failed, no
test moved, and a comment cannot fail (gotcha 48) — the whole defect is a
constant whose meaning was edited by a DNS change.

⚠ AND IT WAS TYPED TWICE, once in each route, with nothing comparing them.
That is gotcha 25's fan-out: two copies of one fact, so a fix that reaches one
reads as done. There is one constant now, and this file asserts both call sites
read it rather than trusting that they do (gotchas 62/66/69/94/103 — five
recorded instances of a well-tested helper a route quietly ignored).

⚠ NOT VERIFIED AGAINST STRIPE. stripe.com is egress-blocked from this container
(gotcha 75), so what is pinned is the value we send, not what Stripe does with
it.
"""

from __future__ import annotations

import ast
import pathlib
from urllib.parse import urlparse

from helixis.api.routes.billing import _RETURN_URL

SOURCE = pathlib.Path(__file__).resolve().parents[2] / "src/helixis/api/routes/billing.py"


def test_the_return_url_is_the_web_app() -> None:
    host = urlparse(_RETURN_URL).hostname
    assert host == "app.occupella.com", (
        f"Stripe returns customers to {host}. The apex is the marketing site — "
        "a paying customer must land back in the product."
    )
    # Stated separately so a future host change still refuses a platform URL:
    # a raw deploy hostname is what a customer reads in the status bar coming
    # back from a payment page.
    assert not any(p in str(host) for p in ("vercel.app", "onrender.com", "netlify.app")), (
        f"{host} is a platform hostname, not the product"
    )
    assert _RETURN_URL.startswith("https://")


def test_both_stripe_call_sites_read_the_constant() -> None:
    """A behavioural test cannot see a route that hardcodes its own URL.

    ⚠ The check is on the FALLBACK EXPRESSION, not on the absence of the old
    string. `settings.billing.portal_return_url or "https://…"` is exactly how
    this was written before, and it reads as perfectly ordinary — so what has
    to be asserted is that every `or` default here resolves to the constant.
    """
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))

    fallbacks: list[ast.expr] = []
    for node in ast.walk(tree):
        # `<something>.portal_return_url or <fallback>`
        if not isinstance(node, ast.BoolOp) or not isinstance(node.op, ast.Or):
            continue
        left = node.values[0]
        if isinstance(left, ast.Attribute) and left.attr == "portal_return_url":
            fallbacks.append(node.values[-1])

    assert len(fallbacks) == 2, (
        f"found {len(fallbacks)} Stripe return-url fallbacks, expected 2 "
        "(checkout and the portal) — a third call site needs a deliberate look"
    )
    for expr in fallbacks:
        assert isinstance(expr, ast.Name) and expr.id == "_RETURN_URL", (
            "a Stripe return-url fallback is a literal again, not the shared "
            "constant — the two can now disagree and nothing compares them"
        )
