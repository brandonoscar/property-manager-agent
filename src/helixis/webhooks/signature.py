"""Buildium webhook HMAC-SHA256 signature verification.

Per Buildium's spec
(https://developer.buildium.com — Verifying webhook events):

    1. Concatenate the value of the ``buildium-webhook-timestamp``
       header with the body of the event with a period character in
       between the two values. The body of the event must have all
       newlines removed and not contain spaces after the colon between
       property names and values. (Buildium's senders already emit
       compact JSON — we verify the EXACT bytes Buildium sent.)
    2. Hash the concatenated string using HMAC-SHA-256 with the
       webhook secret as the key.
    3. Convert the resulting hash bytes to a base64 string.
    4. Compare the base64 string to the ``buildium-webhook-signature``
       header with a case-sensitive compare.

The signature is computed over the EXACT bytes of the request body —
do not re-serialize before verifying.

We also reject stale messages to mitigate replay attacks. The default
freshness window is 1 hour + buffer, because Buildium's last delivery
retry is 1 hour after the original send and we don't want to 401 our
own retries.
"""

from __future__ import annotations

import base64
import hmac
import time
from hashlib import sha256

from helixis.buildium.errors import SignatureVerificationError

# Buildium's retry schedule is 1 minute → 10 minutes → 1 hour. Setting
# this below 3600s would 401 Buildium's third retry. 3900s = 1 hour +
# 5 minutes buffer for clock skew and queueing.
DEFAULT_FRESHNESS_SECONDS = 3900


def compute_signature(secret: str, timestamp: str, raw_body: bytes) -> str:
    """Return the base64 HMAC-SHA256 over ``timestamp + "." + raw_body``."""
    if not secret:
        raise SignatureVerificationError("Empty webhook secret")
    signed_payload = timestamp.encode("ascii") + b"." + raw_body
    mac = hmac.new(secret.encode("utf-8"), signed_payload, sha256).digest()
    return base64.b64encode(mac).decode("ascii")


def verify_signature(
    *,
    secret: str,
    timestamp: str | None,
    signature: str | None,
    raw_body: bytes,
    freshness_seconds: int = DEFAULT_FRESHNESS_SECONDS,
    now: float | None = None,
) -> None:
    """Verify the signature on an inbound Buildium webhook.

    Raises SignatureVerificationError on any failure. Uses a timing-safe
    comparison so attackers can't learn the correct signature byte-by-byte.
    """
    if not timestamp:
        raise SignatureVerificationError("Missing buildium-webhook-timestamp header")
    if not signature:
        raise SignatureVerificationError("Missing buildium-webhook-signature header")

    # Freshness — defeats replay of old (signed) bodies.
    try:
        ts_seconds = float(timestamp)
    except ValueError as exc:
        raise SignatureVerificationError(f"Timestamp is not a number: {timestamp!r}") from exc

    now_seconds = now if now is not None else time.time()
    drift = abs(now_seconds - ts_seconds)
    if drift > freshness_seconds:
        raise SignatureVerificationError(
            f"Timestamp drift {drift:.1f}s exceeds {freshness_seconds}s window",
        )

    expected = compute_signature(secret, timestamp, raw_body)

    expected_bytes = expected.encode("ascii")
    provided_bytes = signature.encode("ascii")

    # Lengths must match for timing-safe compare to mean anything.
    if len(expected_bytes) != len(provided_bytes):
        raise SignatureVerificationError("Signature length mismatch")

    if not hmac.compare_digest(expected_bytes, provided_bytes):
        raise SignatureVerificationError("Signature does not match")


__all__ = ["DEFAULT_FRESHNESS_SECONDS", "compute_signature", "verify_signature"]
