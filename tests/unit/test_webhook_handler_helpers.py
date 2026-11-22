"""Unit tests for the v2 webhook-handler shared helpers (`handlers/_helpers`).

These had no direct coverage: the rest of the suite *stubs*
``open_api_for_company`` and never touches ``envelope_int`` /
``envelope_account_id``. This pins the envelope parsing (the entity-id and
AccountId extraction every handler depends on) and the credential-resolution
context manager's both branches — all deterministic, no live Buildium.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helixis.webhooks.handlers import _helpers

# ── envelope_int ─────────────────────────────────────────────────


def test_envelope_int_reads_int() -> None:
    assert _helpers.envelope_int({"Id": 5}, "Id") == 5


def test_envelope_int_coerces_numeric_string() -> None:
    assert _helpers.envelope_int({"Id": "42"}, "Id") == 42


def test_envelope_int_missing_key_is_none() -> None:
    assert _helpers.envelope_int({}, "Id") is None


def test_envelope_int_explicit_none_is_none() -> None:
    assert _helpers.envelope_int({"Id": None}, "Id") is None


def test_envelope_int_non_numeric_is_none() -> None:
    # Non-numeric → None (and a warning), never a raise.
    assert _helpers.envelope_int({"Id": "not-a-number"}, "Id") is None
    assert _helpers.envelope_int({"Id": ["x"]}, "Id") is None


def test_envelope_int_truncates_float() -> None:
    # int() semantics: a float value truncates toward zero.
    assert _helpers.envelope_int({"Id": 3.9}, "Id") == 3


# ── envelope_account_id ──────────────────────────────────────────


def test_envelope_account_id_reads_value() -> None:
    assert _helpers.envelope_account_id({"AccountId": 123}) == 123


def test_envelope_account_id_defaults_to_zero_when_missing() -> None:
    assert _helpers.envelope_account_id({}) == 0


def test_envelope_account_id_defaults_to_zero_when_non_numeric() -> None:
    # envelope_int returns None → the `or 0` fallback fires.
    assert _helpers.envelope_account_id({"AccountId": "bogus"}) == 0


# ── open_api_for_company ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_open_api_yields_none_when_no_credentials() -> None:
    """Degraded path: a company with no stored Buildium creds yields None so
    the handler can no-op gracefully (never raise)."""
    with patch.object(_helpers, "load_buildium_settings_for_company", AsyncMock(return_value=None)):
        async with _helpers.open_api_for_company("co-1") as api:
            assert api is None


@pytest.mark.asyncio
async def test_open_api_yields_buildium_api_when_configured() -> None:
    """Happy path: with creds present, the CM opens a BuildiumClient and yields
    a BuildiumApi wrapping that client."""
    sentinel_client = object()
    sentinel_api = object()

    class _FakeClientCM:
        def __init__(self, *_a, **_k) -> None: ...
        async def __aenter__(self) -> object:
            return sentinel_client

        async def __aexit__(self, *_a: object) -> bool:
            return False

    with (
        patch.object(
            _helpers,
            "load_buildium_settings_for_company",
            AsyncMock(return_value=MagicMock(name="buildium_settings")),
        ),
        patch.object(_helpers, "BuildiumClient", _FakeClientCM),
        patch.object(_helpers, "BuildiumApi", MagicMock(return_value=sentinel_api)) as api_ctor,
    ):
        async with _helpers.open_api_for_company("co-1") as api:
            assert api is sentinel_api
        # The BuildiumApi was built from the opened client.
        api_ctor.assert_called_once_with(sentinel_client)
