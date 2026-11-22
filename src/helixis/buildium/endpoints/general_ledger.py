"""General Ledger — GL accounts, balances, and transactions (read-only).

Maps to Buildium's "General Ledger" API group. We expose only the read
paths; journal-entry / GL-account writes (POST/PUT) are intentionally
omitted (money-write surface).
"""

from __future__ import annotations

# See applicants.py: ``list`` shadows the builtin in PEP 563 annotation
# resolution, so list-returning annotations use ``builtins.list``.
import builtins
from typing import Any, cast

from helixis.buildium.client import BuildiumClient
from helixis.buildium.schemas import GLAccount, GLAccountBalance, GLTransaction


class GeneralLedger:
    def __init__(self, client: BuildiumClient) -> None:
        self._client = client

    async def list_accounts(self, **filters: Any) -> builtins.list[GLAccount]:
        """List GL accounts (the chart of accounts).

        Buildium spec: ``GET /v1/glaccounts``. Accepts an ``accounttypes``
        array filter (``Asset``, ``Liability``, ``Equity``, ``Income``,
        ``Expense``) — pass it as a list; the client flattens lists into
        repeated query params.
        """
        return await self._client.list_all("/glaccounts", filters or None, GLAccount)

    async def get_account(self, gl_account_id: int) -> GLAccount:
        """``GET /v1/glaccounts/{glAccountId}``."""
        return cast(
            GLAccount,
            await self._client.request("GET", f"/glaccounts/{gl_account_id}", schema=GLAccount),
        )

    async def list_account_balances(self, **filters: Any) -> builtins.list[GLAccountBalance]:
        """Per-account balances.

        Buildium spec: ``GET /v1/glaccounts/balances``. ``asofdate``
        (``YYYY-MM-DD``) is required; ``accountingbasis`` (``Cash`` /
        ``Accrual``) defaults to the account's configuration when omitted.
        """
        return await self._client.list_all(
            "/glaccounts/balances", filters or None, GLAccountBalance
        )

    async def list_transactions(self, **filters: Any) -> builtins.list[GLTransaction]:
        """General-ledger transactions in a date window.

        Buildium spec: ``GET /v1/generalledger/transactions``. ``startdate``
        and ``enddate`` (``YYYY-MM-DD``) are REQUIRED by Buildium for this
        endpoint — calling without them 400s.
        """
        return await self._client.list_all(
            "/generalledger/transactions", filters or None, GLTransaction
        )

    async def get_transaction(self, transaction_id: int) -> GLTransaction:
        """``GET /v1/generalledger/transactions/{transactionId}``."""
        return cast(
            GLTransaction,
            await self._client.request(
                "GET",
                f"/generalledger/transactions/{transaction_id}",
                schema=GLTransaction,
            ),
        )
