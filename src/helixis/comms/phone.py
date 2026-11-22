"""Phone-number normalization for the contact-comms spine.

**Why this exists.** Twilio speaks E.164 (``+15551234567``). The Buildium
mirror stores whatever Buildium returns — human format, un-normalized
(``(555) 123-4567``, ``555.123.4567``, ``555-123-4567 x12``). An exact string
compare between the two never matches, so a known tenant texting in would look
like an unknown number and the AI would lose all context. The CRM's
``find_lead_by_phone`` already has this latent bug (it matches ``phone = $2``
verbatim) — it only works today because the CRM stores the same E.164 it later
looks up. Buildium contacts don't have that luxury, so every phone comparison
in the comms spine goes through here first.

**Scope.** Occupella is US property management (Buildium US), so normalization
is US-default: a bare 10-digit number is assumed ``+1``. International numbers
already carrying a ``+`` country code are preserved best-effort. We deliberately
avoid a full ``libphonenumber`` dependency — the match key below (national
significant digits) is what US PM software uses in practice and needs no extra
package, no model download, and no per-region metadata.

Two functions, two jobs:
- ``normalize_e164`` — a canonical ``+1XXXXXXXXXX`` for display / storage / the
  Twilio ``to`` field. Returns ``None`` when the input can't be a real number.
- ``phone_match_key`` — the *comparison* value: national significant digits
  (US country code dropped), used to match an inbound number against the
  mirror. ``(555) 123-4567``, ``555.123.4567`` and ``+1 555 123 4567`` all
  collapse to ``5551234567``. Match on this, never on the raw string.
"""

from __future__ import annotations

import re

# US NANP national significant number is exactly 10 digits.
_US_NSN_LEN = 10
_US_COUNTRY_CODE = "1"


def _digits(raw: str) -> str:
    """Every digit in ``raw``, in order — drops spaces, dashes, parens, dots,
    a leading ``+``, and any ``x123`` extension noise (which is also digits, so
    callers that care about extensions must strip them before this; for match
    purposes an extension on a cell number is vanishingly rare)."""
    return re.sub(r"\D", "", raw or "")


def phone_match_key(raw: str | None) -> str | None:
    """The value two phone numbers are compared on. ``None`` when ``raw`` has
    too few digits to be a phone number.

    Returns the **national significant digits**: for a US number that's the last
    10 digits (the leading ``1`` country code, if present, is dropped). This is
    what makes an E.164 inbound number match a human-format mirror row.

    - ``"+15551234567"`` → ``"5551234567"``
    - ``"(555) 123-4567"`` → ``"5551234567"``
    - ``"1-555-123-4567"`` → ``"5551234567"``
    - ``"555-1234"`` (7 digits, no area code) → ``None`` (not resolvable)

    For a longer international string we keep the last 10 digits as the key too;
    that's lenient (it can't tell +44 from +1 on the last 10) but the mirror is
    a single US company's contacts, so a cross-country-code collision on the
    same 10 trailing digits is not a real risk here. Documented so a future
    international expansion revisits it rather than trusting it blindly.
    """
    digits = _digits(raw or "")
    # An 11-digit US number is country-code + NSN; drop the leading 1.
    if len(digits) == _US_NSN_LEN + 1 and digits.startswith(_US_COUNTRY_CODE):
        digits = digits[1:]
    if len(digits) < _US_NSN_LEN:
        return None
    return digits[-_US_NSN_LEN:]


def normalize_e164(raw: str | None) -> str | None:
    """Best-effort canonical E.164 (``+1XXXXXXXXXX`` for US). ``None`` when the
    input can't be a real number.

    Use this for the Twilio ``to`` field, for display, and when storing a
    number we originated. For *matching* against the mirror use
    ``phone_match_key`` — normalization alone won't rescue a mirror row that was
    stored as ``(555) 123-4567``.

    - ``"(555) 123-4567"`` → ``"+15551234567"``
    - ``"5551234567"`` → ``"+15551234567"``
    - ``"+44 20 7946 0000"`` → ``"+442079460000"`` (preserved, not forced to +1)
    - ``"12345"`` → ``None``
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    had_plus = raw.lstrip().startswith("+")
    digits = _digits(raw)
    if not digits:
        return None
    # Already-international (had a leading +): preserve the country code as-is.
    if had_plus:
        # A lone "+" with <7 digits isn't a real number.
        return f"+{digits}" if len(digits) >= 7 else None
    # No plus: assume US/NANP.
    if len(digits) == _US_NSN_LEN:
        return f"+{_US_COUNTRY_CODE}{digits}"
    if len(digits) == _US_NSN_LEN + 1 and digits.startswith(_US_COUNTRY_CODE):
        return f"+{digits}"
    return None


def phones_match(a: str | None, b: str | None) -> bool:
    """True when two raw phone strings refer to the same number. Both are
    reduced to their match key first; two ``None`` keys never match (an
    unparseable number matches nothing, including another unparseable one)."""
    key_a = phone_match_key(a)
    key_b = phone_match_key(b)
    return key_a is not None and key_a == key_b
