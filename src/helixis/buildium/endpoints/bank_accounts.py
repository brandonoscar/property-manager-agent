"""Bank accounts."""

from __future__ import annotations

from typing import Any, cast

from helixis.buildium.client import BuildiumClient
from helixis.buildium.schemas import BankAccount


class BankAccounts:
    def __init__(self, client: BuildiumClient) -> None:
        self._client = client

    async def list(self, **filters: Any) -> list[BankAccount]:
        return await self._client.list_all("/bankaccounts", filters or None, BankAccount)

    async def get(self, bank_account_id: int) -> BankAccount:
        return cast(
            BankAccount,
            await self._client.request(
                "GET", f"/bankaccounts/{bank_account_id}", schema=BankAccount
            ),
        )
