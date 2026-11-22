"""``scripts/rentvine_balances_check.py``, the founder's rent-balances check.

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
SCRIPT = REPO / "scripts" / "rentvine_balances_check.py"
CO = "6a456191-0000-4000-8000-000000000001"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("rentvine_balances_check", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["rentvine_balances_check"] = mod
    spec.loader.exec_module(mod)
    return mod


check = _load()


def _row(lease_id: int, owed: Any, **extra: Any) -> dict[str, Any]:
    return {
        "lease": {"leaseID": lease_id, "propertyID": 990000101, "unitID": 990000201},
        "balances": {"unpaidTotalAmount": owed, "pastDueTotalAmount": owed},
        **extra,
    }


EXPORT = [_row(990000601, 1450), _row(990000602, 0), _row(990000603, 80)]


@pytest.fixture
def served(monkeypatch):
    routes: dict[str, Any] = {"/leases/export": EXPORT}
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


def test_the_spec_shape_passes_and_totals_who_owes(served, capsys):
    assert asyncio.run(check.run(None)) == 0
    out = capsys.readouterr().out
    assert "PASS: shape" in out
    assert "leases that owe: 2 · total owed: $1,530.00" in out
    assert "lease 990000601: $1,450.00" in out
    assert "VERDICT: PASS" in out
    assert all(line.startswith("GET ") for line in served[1])


def test_an_export_without_balances_fails(served, capsys):
    routes, _ = served
    routes["/leases/export"] = [{"lease": {"leaseID": 990000601}}]
    assert asyncio.run(check.run(None)) == 1
    assert "'balances' beside the lease is missing" in capsys.readouterr().out


def test_one_unreadable_balance_fails_because_the_sync_would_refuse(served, capsys):
    routes, _ = served
    routes["/leases/export"] = [_row(990000601, 1450), _row(990000602, "n/a")]
    assert asyncio.run(check.run(None)) == 1
    assert "1 lease(s) came without a readable balance" in capsys.readouterr().out


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
        ((2, Decimal("1530.00")), 0, "PASS: 2 lease(s), $1,530.00 owed"),
        ((3, Decimal("1530.00")), 1, "the mirror holds 3 lease(s)"),
        ((2, Decimal("80.00")), 1, "owing $80.00"),
    ],
)
def test_the_mirror_is_held_against_rentvine(served, monkeypatch, capsys, stored, code, word):
    async def fake(company_id: str) -> Any:
        assert company_id == CO
        return stored

    monkeypatch.setattr(check, "_mirror_balances", fake)
    monkeypatch.setenv(check.DB_VAR, "postgres://synthetic")
    assert asyncio.run(check.run(CO)) == code
    assert word in capsys.readouterr().out
