"""``scripts/rentvine_bills_check.py``, the founder's bills check.

Against a mock Rentvine with synthetic rows (ids 990 000 xxx): passes on the
spec's shape, fails on a shape the sync cannot read, sends nothing without
``--execute``, and holds the mirror against Rentvine's own totals.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from helixis.rentvine import client as rvclient

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "rentvine_bills_check.py"
CO = "6a456191-0000-4000-8000-000000000001"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("rentvine_bills_check", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["rentvine_bills_check"] = mod
    spec.loader.exec_module(mod)
    return mod


check = _load()


def _line(bill: int, txn: int, amount: str, paid: str, payee: int) -> dict[str, Any]:
    return {
        "bill": {
            "billID": bill,
            "parentBillID": None,
            "billDate": "2026-09-20",
            "dateDue": "2026-10-15",
            "isVoided": 0,
            "payeeContactID": payee,
        },
        "transaction": {"transactionID": txn, "amount": amount, "amountPaid": paid, "isVoided": 0},
        "ledger": {"ledgerID": 990000801, "ledgerTypeID": 3},
        "property": {"propertyID": 990000101},
    }


PAYABLES = [
    _line(990000701, 1, "300.00", "0.00", 990000501),
    _line(990000701, 2, "180.00", "0.00", 990000501),
    _line(990000702, 3, "220.00", "220.00", 990000502),
]
# Wrapped the way ROW_ENVELOPE says the server sends a vendor row.
VENDORS = [
    {"contact": {"contactID": 990000501, "name": "Synthetic Plumbing Co"}},
    {"contact": {"contactID": 990000502, "name": "Synthetic Electric"}},
]


@pytest.fixture
def served(monkeypatch):
    routes: dict[str, Any] = {"/accounting/payables/search": PAYABLES, "/vendors": VENDORS}
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(f"{request.method} {request.url.path}")
        for suffix, body in routes.items():
            if request.url.path.endswith(suffix):
                return httpx.Response(200, json=body, headers={"pagination-total-pages": "1"})
        return httpx.Response(404, json={})

    real = rvclient.RentvineClient

    def build(settings: Any, **_: Any) -> Any:
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler), auth=("k", "s"))
        return real(settings, client=http)

    monkeypatch.setattr(rvclient, "RentvineClient", build)
    for var in check.CRED_VARS:
        monkeypatch.setenv(var, "acme" if var.endswith("CODE") else "x")
    return routes, sent


def test_the_spec_shape_passes_and_totals_the_unpaid_bills(served, capsys):
    assert asyncio.run(check.run(None)) == 0
    out = capsys.readouterr().out
    assert "PASS: shape" in out
    assert "bills: 2 · voided, left out: 0 · unpaid: 1" in out
    assert "owed on unpaid bills: $480.00" in out
    assert "Synthetic Plumbing Co: $480.00, due 2026-10-15" in out
    assert "VERDICT: PASS" in out
    assert all(line.startswith("GET ") for line in served[1])


def test_an_amount_that_is_not_a_decimal_fails(served, capsys):
    routes, _ = served
    routes["/accounting/payables/search"] = [_line(990000703, 4, "n/a", "0.00", 990000501)]
    assert asyncio.run(check.run(None)) == 1
    assert "does not read as a decimal" in capsys.readouterr().out


def test_a_row_with_no_transaction_beside_the_bill_fails(served, capsys):
    routes, _ = served
    routes["/accounting/payables/search"] = [{"bill": PAYABLES[0]["bill"]}]
    assert asyncio.run(check.run(None)) == 1
    assert "no 'transaction' object" in capsys.readouterr().out


def test_without_execute_nothing_is_sent(served, capsys):
    assert check.main([]) == 0
    assert served[1] == []
    assert "acme" not in capsys.readouterr().out


def test_without_credentials_it_does_not_run(monkeypatch):
    for var in check.CRED_VARS:
        monkeypatch.delenv(var, raising=False)
    assert check.main(["--execute"]) == 2


@pytest.mark.parametrize(
    ("stored", "code", "word"),
    [
        ((2, Decimal("480.00")), 0, "PASS: 2 bill(s), $480.00 owed"),
        ((3, Decimal("480.00")), 1, "the mirror holds 3 bill(s)"),
        ((2, Decimal("300.00")), 1, "owing $300.00"),
    ],
)
def test_the_mirror_is_held_against_rentvine(served, monkeypatch, capsys, stored, code, word):
    async def fake(company_id: str) -> Any:
        assert company_id == CO
        return stored

    monkeypatch.setattr(check, "_mirror_bills", fake)
    monkeypatch.setenv(check.DB_VAR, "postgres://synthetic")
    assert asyncio.run(check.run(CO)) == code
    assert word in capsys.readouterr().out
