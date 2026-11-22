"""Stripe webhook signature verification.

Stripe signs every webhook: it builds ``{timestamp}.{raw_body}``, HMAC-SHA256s
it with the endpoint's signing secret, and sends the result hex-encoded in a
``Stripe-Signature`` header shaped

    t=1492774577,v1=5257a869e7ec…,v1=<another during a secret rotation>

Implemented directly rather than via the ``stripe`` SDK — the repo stays
dependency-light, the same stance ``comms/twilio_signature.py`` and
``memory/voyage_embedder.py`` take.

FAILS CLOSED, and this one has no testing bypass at all. Gotchas 68 and 74
both record a ``skip_signature_check`` flag reaching a real webhook, and this
endpoint decides whether somebody has PAID — a forged event could grant a
subscription or cancel a customer's. There is nothing to add here later.

⚠ THE RAW BODY IS THE SIGNED BYTES. Parsing the JSON and re-serialising it
changes whitespace and key order, so the recomputed HMAC will never match and
every legitimate event is rejected. The route must read ``await
request.body()`` and hand those bytes here BEFORE any parsing.

⚠ stripe.com is EGRESS-BLOCKED from the dev container (gotcha 75), so this
scheme comes from Stripe's documentation and not from a live delivery.
`verify` is exercised against signatures this module's own `compute_signature`
builds, which proves the algorithm is self-consistent — NOT that it matches
Stripe. Confirm against a real event on the first live delivery; a wrong
recipe rejects every webhook, which is loud and safe rather than quiet.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time

logger = logging.getLogger(__name__)

DEFAULT_TOLERANCE_S = 300
"""How stale a signed timestamp may be.

⚠ This is the REPLAY guard and it is the reason the timestamp is signed at
all. Without it a captured `invoice.paid` could be posted back for ever and
keep renewing an account nobody is paying for. Stripe's own default is 300s.
"""


def compute_signature(secret: str, timestamp: str, payload: bytes) -> str:
    """The hex HMAC-SHA256 Stripe would send for this timestamp + body."""
    signed = timestamp.encode("utf-8") + b"." + payload
    return hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()


def _parse_header(header: str) -> tuple[str | None, list[str]]:
    """Split ``t=…,v1=…,v1=…`` into its timestamp and every v1 signature.

    ⚠ Returns ALL v1 values, not the first. Stripe sends two during a signing
    secret rotation, and taking only one makes a rotation look like an attack:
    half the deliveries fail verification for reasons nobody can see.
    """
    timestamp: str | None = None
    signatures: list[str] = []
    for part in header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            timestamp = value
        elif key == "v1":
            signatures.append(value)
    return timestamp, signatures


def verify(
    secret: str,
    header: str,
    payload: bytes,
    *,
    tolerance_s: int = DEFAULT_TOLERANCE_S,
    now: float | None = None,
) -> bool:
    """True only for a body Stripe signed with ``secret`` inside the window."""
    if not secret or not header or not payload:
        return False

    timestamp, signatures = _parse_header(header)
    if timestamp is None or not signatures:
        return False

    try:
        signed_at = int(timestamp)
    except ValueError:
        return False

    current = time.time() if now is None else now
    # Guards BOTH directions. A far-future timestamp is as suspect as a stale
    # one — an attacker who could set it forward would otherwise mint a
    # signature that stays valid indefinitely.
    if abs(current - signed_at) > tolerance_s:
        logger.warning("stripe webhook: timestamp outside tolerance, rejecting")
        return False

    expected = compute_signature(secret, timestamp, payload)
    # compare_digest against every candidate, and never short-circuit on the
    # first mismatch in a way that leaks position through timing.
    return any(hmac.compare_digest(expected, candidate) for candidate in signatures)
