"""Path/shape coverage for the BankAccounts endpoint helper.

``buildium/endpoints/bank_accounts.py`` had no test. Pins the exact
``(method, path)`` + schema each call sends to the client (same spirit as
test_buildium_endpoint_paths.py), so a URL or schema regression fails loudly.
Deterministic: the client is mocked, no live Buildium.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from helixis.buildium.endpoints.bank_accounts import BankAccounts
from helixis.buildium.schemas import BankAccount


@pytest.mark.asyncio
async def test_list_hits_bankaccounts_path_with_schema() -> None:
    client = MagicMock()
    client.list_all = AsyncMock(return_value=[])
    accounts = BankAccounts(client)

    await accounts.list()

    client.list_all.assert_awaited_once()
    args = client.list_all.await_args.args
    assert args[0] == "/bankaccounts", f"list URL regressed: {args[0]!r}"
    assert args[2] is BankAccount  # parsed into the BankAccount schema


@pytest.mark.asyncio
async def test_list_passes_filters_through() -> None:
    client = MagicMock()
    client.list_all = AsyncMock(return_value=[])
    accounts = BankAccounts(client)

    await accounts.list(accountnumber="123")

    filters = client.list_all.await_args.args[1]
    assert filters == {"accountnumber": "123"}


@pytest.mark.asyncio
async def test_list_no_filters_passes_none() -> None:
    # ``filters or None`` — an empty filter dict becomes None, not {}.
    client = MagicMock()
    client.list_all = AsyncMock(return_value=[])
    accounts = BankAccounts(client)

    await accounts.list()

    assert client.list_all.await_args.args[1] is None


@pytest.mark.asyncio
async def test_get_hits_single_bankaccount_path_with_schema() -> None:
    client = MagicMock()
    client.request = AsyncMock(return_value=MagicMock())
    accounts = BankAccounts(client)

    await accounts.get(4471)

    client.request.assert_awaited_once()
    method, path = client.request.await_args.args[:2]
    assert method == "GET"
    assert path == "/bankaccounts/4471", f"get URL regressed: {path!r}"
    assert client.request.await_args.kwargs.get("schema") is BankAccount
