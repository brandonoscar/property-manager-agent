"""``scripts/rentvine_owner_links_check.py``, the founder's owner-links check.

Run against a mock Rentvine with synthetic rows (ids 990 000 xxx): it must
pass on the shape the mapper reads, fail on a shape the mapper cannot read,
and send nothing without ``--execute``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from helixis.rentvine import client as rvclient
from helixis.rentvine._row_envelopes import ROW_ENVELOPE

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "rentvine_owner_links_check.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("rentvine_owner_links_check", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["rentvine_owner_links_check"] = mod
    spec.loader.exec_module(mod)
    return mod


check = _load()

PROPS = [
    {"propertyID": 990000101, "name": "Synthetic Maple Court", "portfolioID": 990000951},
    {"propertyID": 990000102, "name": "Synthetic Birch House", "portfolioID": 990000952},
]
PORTS = [
    {"portfolioID": 990000951, "contacts": [{"contactID": 990000401, "percentOwned": "100"}]},
    {"portfolioID": 990000952, "contacts": [{"contactID": 990000402, "percentOwned": "100"}]},
]
OWNERS = [
    {"contactID": 990000401, "name": "Synthetic Holdings LLC"},
    {"contactID": 990000402, "name": "Synthetic Two LLC"},
]


def _wire(op: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    key = ROW_ENVELOPE[op]
    return [r if key is None else {key: r} for r in rows]


@pytest.fixture
def served(monkeypatch):
    """Patch the client the script builds onto a mock Rentvine; returns the
    mutable route table and the list of requests sent."""
    routes: dict[str, list[dict[str, Any]]] = {
        "/properties": _wire("GET /properties", PROPS),
        "/portfolios": _wire("GET /portfolios", PORTS),
        "/owners": _wire("GET /owners", OWNERS),
    }
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


def test_the_spec_shape_passes_and_prints_owner_names(served, capsys):
    assert asyncio.run(check.run(None)) == 0
    out = capsys.readouterr().out
    assert "PASS: shape" in out
    assert "Synthetic Maple Court: Synthetic Holdings LLC" in out
    assert "VERDICT: PASS" in out
    _, sent = served
    assert all(line.startswith("GET ") for line in sent)


def test_a_missing_envelope_fails_the_shape(served, capsys):
    routes, _ = served
    routes["/portfolios"] = PORTS  # flat, never wrapped
    assert asyncio.run(check.run(None)) == 1
    out = capsys.readouterr().out
    assert "FAIL" in out and "envelope" in out


def test_a_renamed_contacts_key_fails(served, capsys):
    routes, _ = served
    renamed = [{"portfolioID": p["portfolioID"], "owners": p["contacts"]} for p in PORTS]
    routes["/portfolios"] = _wire("GET /portfolios", renamed)
    assert asyncio.run(check.run(None)) == 1
    assert "FAIL" in capsys.readouterr().out


def test_without_execute_nothing_is_sent(served, capsys):
    _, sent = served
    assert check.main([]) == 0
    assert sent == []
    assert "nothing sent" in capsys.readouterr().out


def test_without_credentials_it_does_not_run(monkeypatch):
    for var in check.CRED_VARS:
        monkeypatch.delenv(var, raising=False)
    assert check.main(["--execute"]) == 2


def test_the_script_never_prints_a_credential_value(served, capsys):
    check.main([])
    out = capsys.readouterr().out
    assert "acme" not in out


@pytest.mark.parametrize(
    ("stored", "code", "word"),
    [
        ({990000401: [990000101], 990000402: [990000102]}, 0, "PASS: all 2"),
        ({990000401: [], 990000402: [990000102]}, 1, "1 of 2 owner(s) differ"),
        ({}, 1, "no Rentvine owners stored"),
    ],
)
def test_the_mirror_is_held_against_the_join(served, monkeypatch, capsys, stored, code, word):
    async def fake_links(company_id: str) -> dict[int, list[int]]:
        assert company_id == "6a456191-0000-4000-8000-000000000001"
        return stored

    monkeypatch.setattr(check, "_mirror_links", fake_links)
    monkeypatch.setenv(check.DB_VAR, "postgres://synthetic")
    assert asyncio.run(check.run("6a456191-0000-4000-8000-000000000001")) == code
    assert word in capsys.readouterr().out
