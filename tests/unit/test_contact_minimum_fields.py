"""The chat's contact tools send the model the minimum a task needs.

Rentvine terms rule 20 (``design/rentvine-terms-rules.md``): data sent to model
providers is egress the customer answers for, so send the minimum fields a task
needs. The mirror is shared by every PMS, so this holds for Buildium too.

* A property snapshot says whether an owner has an email or phone on file,
  never the value (``test_owner_path.py`` pins that half).
* ``contact_lookup`` hands over emails and phones only for ONE exact match.
  Eight people's contact details are not needed to ask "which one?".
* A fuzzy match is a guess about who the user meant: never any contacts.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helixis.agents import mirror_tools

from .test_contact_ambiguity_note import _lookup, _tenant

NOTE = mirror_tools._CONTACTS_WITHHELD_NOTE


@pytest.mark.asyncio
async def test_one_exact_match_gets_its_contact_details() -> None:
    out = await _lookup([_tenant("Marcus", "Whitfield", "1200 Dexter Ave")])
    assert "marcus@t.example" in out
    assert NOTE not in out


@pytest.mark.asyncio
async def test_several_matches_get_names_and_roles_but_no_contacts() -> None:
    out = await _lookup(
        [
            _tenant("Marcus", "Whitfield", "1200 Dexter Ave"),
            _tenant("Elena", "Whitfield", "1700 W Washington St"),
        ]
    )
    assert "Marcus Whitfield" in out and "Elena Whitfield" in out
    assert "tenant at 1200 Dexter Ave" in out  # enough to ask which one
    assert "marcus@t.example" not in out and "elena@t.example" not in out
    assert NOTE in out  # and the model is told how to get them


@pytest.mark.asyncio
async def test_a_fuzzy_match_never_sends_contacts_even_for_one_candidate() -> None:
    conn = MagicMock()
    conn.fetch = AsyncMock(
        return_value=[
            {
                "full_name": "John Smith",
                "kind": "tenant",
                "email": "j@x.example",
                "phone": "555-0101",
                "sim": 0.62,
            }
        ]
    )
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_CM(conn))
    out = await mirror_tools._fuzzy_contact_fallback(pool, "co-1", "Jhon Smtih")
    assert "John Smith (tenant)" in out
    assert "j@x.example" not in out and "555-0101" not in out
    assert "Buildium" not in out  # the mirror may be Rentvine's


class _CM:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    async def __aenter__(self) -> Any:
        return self.conn

    async def __aexit__(self, *_a: Any) -> None:
        return None


def _live(tenants: list[Any]):
    """Patch the live Buildium fallback's seams with ``tenants``."""
    from helixis.agents import buildium_tools as bt

    api = SimpleNamespace(
        rentals=SimpleNamespace(
            list_tenants=AsyncMock(return_value=tenants),
            list_owners=AsyncMock(return_value=[]),
        ),
        vendors=SimpleNamespace(list=AsyncMock(return_value=[])),
    )
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return [
        patch.object(bt, "_open_api", AsyncMock(return_value=(client, api, None))),
        patch.object(bt, "_tenant_full_name", lambda t: t.name),
        patch.object(bt, "_first_phone", lambda t: t.phone),
    ]


async def _run_live(tenants: list[Any]) -> str:
    patches = _live(tenants)
    for p in patches:
        p.start()
    try:
        out = await mirror_tools._live_contact_fallback("smith")
    finally:
        for p in patches:
            p.stop()
    assert out is not None
    return out


@pytest.mark.asyncio
async def test_the_live_fallback_follows_the_same_rule() -> None:
    one = await _run_live([SimpleNamespace(name="Ann Smith", email="ann@x.example", phone=None)])
    assert "ann@x.example" in one

    two = await _run_live(
        [
            SimpleNamespace(name="Ann Smith", email="ann@x.example", phone="555-0102"),
            SimpleNamespace(name="Bob Smith", email="bob@x.example", phone=None),
        ]
    )
    assert "Ann Smith (tenant)" in two and "Bob Smith (tenant)" in two
    assert "ann@x.example" not in two and "555-0102" not in two and "bob@x.example" not in two
    assert NOTE in two


def test_the_rule_is_one_match() -> None:
    assert mirror_tools._contacts_allowed(1)
    assert not mirror_tools._contacts_allowed(0)
    assert not mirror_tools._contacts_allowed(2)
