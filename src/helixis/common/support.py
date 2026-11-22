"""The one address a customer is told to write to.

⚠ ONE HOME, because "contact support" with no address is an instruction that
cannot be carried out. Five backend messages said exactly that on 2026-09-16
— a 409 on a multi-workspace account, a 403 with no company, a locked A2P
registration, the daily AI budget — while every other message in the product
spelled out ``team@occupella.com``. A customer reading "contact support" on a
screen with no address, no link and no chat widget concludes there is no
support, which is the founder's "looks like there was no fallback" shape
(gotchas 102/106/118: an instruction that cannot change anything is worse
than none).

The frontend keeps its own copy in ``frontend/src/lib/support.ts``; the two
are compared by ``tests/unit/test_support_address_is_reachable.py`` so they
cannot drift into naming different inboxes.
"""

from __future__ import annotations

SUPPORT_EMAIL = "team@occupella.com"


def contact_support(what: str = "") -> str:
    """A sentence fragment that names the address.

    ``contact_support("if you need more")`` →
    ``"contact team@occupella.com if you need more"``. The address is the whole
    point; a caller that wants the words "support" without it is asking for
    the defect back.
    """
    tail = f" {what}" if what else ""
    return f"contact {SUPPORT_EMAIL}{tail}"


__all__ = ["SUPPORT_EMAIL", "contact_support"]
