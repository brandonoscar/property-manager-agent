"""Webhook data-integrity fixes (2026-07 Buildium release audit, PR 1).

Pins the release-blocking invariants: the webhook rate limit clears
Buildium's real burst rate (so a burst can't 429 us into a subscription
suspension), and a webhook-secret DECRYPT failure fires a visible Sentry
alert instead of silently dropping the company's feed as if the account
were merely unknown.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from helixis.settings import Settings


def test_webhook_rate_limit_clears_buildium_burst() -> None:
    # Buildium bursts ~600/min and suspends after 20 consecutive failures;
    # the cap must sit comfortably above the burst, not at the old 120.
    assert Settings(env="test").api.rate_limits.webhooks >= 600


def test_decrypt_failure_fires_a_visible_alert() -> None:
    """A Fernet-decrypt failure on the webhook secret must call
    capture_swallowed — otherwise it returns None and the receiver drops
    the webhook with a 200, indistinguishable from a benign unknown
    account, and the company's whole feed goes dark unnoticed."""
    from helixis.buildium import credentials

    row = {"encrypted_webhook_secret": "corrupt", "company_id": "co-1"}

    class _BadCipher:
        def __init__(self, *_a: object) -> None: ...

        def decrypt(self, _blob: object) -> str:
            raise ValueError("bad key")

    captured: list[str] = []
    with (
        patch.object(credentials, "fetch_buildium_row_by_account_id", AsyncMock(return_value=row)),
        patch.object(credentials, "CredentialCipher", _BadCipher),
        patch.object(credentials, "get_settings", return_value=MagicMock()),
        patch.object(
            credentials,
            "capture_swallowed",
            lambda exc, *, op: captured.append(op),
        ),
    ):
        result = asyncio.run(credentials.load_webhook_secret_by_account_id(12345))

    assert result is None  # still degrades — never crashes the receiver
    assert captured == ["buildium.webhook_secret.decrypt"]  # but now VISIBLE
