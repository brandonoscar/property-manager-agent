"""Unit tests for Buildium webhook signature verification."""

from __future__ import annotations

import base64
import hmac
import time
from hashlib import sha256

import pytest

from helixis.buildium.errors import SignatureVerificationError
from helixis.webhooks.signature import compute_signature, verify_signature

SECRET = "whsec_test_abc123"
BODY = b'{"EventName":"Lease.Created","AccountId":42,"LeaseId":99}'


def _sign(secret: str, timestamp: str, body: bytes) -> str:
    mac = hmac.new(secret.encode(), timestamp.encode() + b"." + body, sha256).digest()
    return base64.b64encode(mac).decode()


def test_compute_signature_matches_reference() -> None:
    timestamp = "1713635000"
    expected = _sign(SECRET, timestamp, BODY)
    assert compute_signature(SECRET, timestamp, BODY) == expected


def test_verify_signature_accepts_fresh_valid_signature() -> None:
    now = time.time()
    ts = str(int(now))
    sig = _sign(SECRET, ts, BODY)
    # Should not raise.
    verify_signature(
        secret=SECRET,
        timestamp=ts,
        signature=sig,
        raw_body=BODY,
        freshness_seconds=300,
        now=now,
    )


def test_verify_signature_rejects_tampered_body() -> None:
    now = time.time()
    ts = str(int(now))
    sig = _sign(SECRET, ts, BODY)
    tampered = BODY.replace(b"99", b"100")
    with pytest.raises(SignatureVerificationError):
        verify_signature(
            secret=SECRET,
            timestamp=ts,
            signature=sig,
            raw_body=tampered,
            freshness_seconds=300,
            now=now,
        )


def test_verify_signature_rejects_wrong_secret() -> None:
    now = time.time()
    ts = str(int(now))
    sig = _sign("whsec_wrong", ts, BODY)
    with pytest.raises(SignatureVerificationError):
        verify_signature(
            secret=SECRET,
            timestamp=ts,
            signature=sig,
            raw_body=BODY,
            freshness_seconds=300,
            now=now,
        )


def test_verify_signature_rejects_stale_timestamp() -> None:
    now = time.time()
    ts = str(int(now - 600))  # 10 minutes old
    sig = _sign(SECRET, ts, BODY)
    with pytest.raises(SignatureVerificationError, match="drift"):
        verify_signature(
            secret=SECRET,
            timestamp=ts,
            signature=sig,
            raw_body=BODY,
            freshness_seconds=300,
            now=now,
        )


def test_verify_signature_rejects_missing_headers() -> None:
    with pytest.raises(SignatureVerificationError, match="timestamp"):
        verify_signature(
            secret=SECRET,
            timestamp=None,
            signature="sig",
            raw_body=BODY,
        )
    with pytest.raises(SignatureVerificationError, match="signature"):
        verify_signature(
            secret=SECRET,
            timestamp="1",
            signature=None,
            raw_body=BODY,
        )


def test_verify_signature_rejects_non_numeric_timestamp() -> None:
    with pytest.raises(SignatureVerificationError, match="not a number"):
        verify_signature(
            secret=SECRET,
            timestamp="not-a-number",
            signature="sig",
            raw_body=BODY,
        )


def test_verify_signature_rejects_length_mismatch() -> None:
    now = time.time()
    ts = str(int(now))
    # Truncated signature — length mismatch should trip before timing-safe compare.
    with pytest.raises(SignatureVerificationError, match="length"):
        verify_signature(
            secret=SECRET,
            timestamp=ts,
            signature="abc",
            raw_body=BODY,
            freshness_seconds=300,
            now=now,
        )


def test_empty_secret_refused() -> None:
    with pytest.raises(SignatureVerificationError, match="Empty"):
        compute_signature("", "1", b"body")


def test_signature_matches_buildium_csharp_reference() -> None:
    """Ground truth: replicate the C# sample from the spec.

        var signedPayload = $"{timestamp}.{eventNotificationMessage}";
        var hasher = new HMACSHA256(Encoding.UTF8.GetBytes(m_secretKey));
        var computedHash = hasher.ComputeHash(Encoding.UTF8.GetBytes(signedPayload));
        var computedHashString = Convert.ToBase64String(computedHash);
        return computedHashString.Equals(signature);

    Hand-computed value below must match what verify_signature accepts.
    """
    secret = "test_secret"
    timestamp = "1735128000"
    body = (
        b'{"EventName":"Rental.Created",'
        b'"EventDateTime":"2024-12-25T12:00:00.000000Z",'
        b'"AccountId":42,"PropertyId":7}'
    )
    digest = hmac.new(
        secret.encode("utf-8"),
        f"{timestamp}.".encode("ascii") + body,
        sha256,
    ).digest()
    signature = base64.b64encode(digest).decode("ascii")
    # Should not raise.
    verify_signature(
        secret=secret,
        timestamp=timestamp,
        signature=signature,
        raw_body=body,
        now=float(timestamp) + 60,
    )


def test_signature_accepts_55_minute_old_timestamp_within_default_window() -> None:
    """Buildium's third retry is 1 hour after the original send. The
    default freshness window (3900s) covers up to 65 minutes of drift."""
    now = time.time()
    ts = str(int(now - 55 * 60))
    sig = _sign(SECRET, ts, BODY)
    # Should not raise — uses the default DEFAULT_FRESHNESS_SECONDS.
    verify_signature(
        secret=SECRET,
        timestamp=ts,
        signature=sig,
        raw_body=BODY,
        now=now,
    )


def test_signature_rejects_timestamp_older_than_default_window() -> None:
    """4000 seconds ago is past the default 3900s window."""
    now = time.time()
    ts = str(int(now - 4000))
    sig = _sign(SECRET, ts, BODY)
    with pytest.raises(SignatureVerificationError, match="drift"):
        verify_signature(
            secret=SECRET,
            timestamp=ts,
            signature=sig,
            raw_body=BODY,
            now=now,
        )


def test_default_freshness_constant_covers_buildium_retry_schedule() -> None:
    """DEFAULT_FRESHNESS_SECONDS must be >= 3600 (Buildium's third
    retry interval) plus a small buffer for clock skew + queueing."""
    from helixis.webhooks.signature import DEFAULT_FRESHNESS_SECONDS

    assert DEFAULT_FRESHNESS_SECONDS >= 3600
