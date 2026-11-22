"""Phone → Buildium-contact resolver. Mirror is mocked (no DB), so these pin the
behavior that matters: unparseable numbers never hit the DB, matches come back
precedence-ordered (tenant → owner → vendor), ambiguity is detected, and names
fall back cleanly. The SQL's own digit-normalization is exercised live in the
integration suite; here we control what ``conn.fetch`` returns."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from helixis.comms import contact_resolver
from helixis.comms.contact_resolver import (
    resolve_all_contacts_by_phone,
    resolve_contact_by_phone,
    resolve_contact_call_target,
)


class _AcquireCM:
    def __init__(self, conn: object) -> None:
        self._conn = conn

    async def __aenter__(self) -> object:
        return self._conn

    async def __aexit__(self, *_a: object) -> bool:
        return False


def _pool_returning(rows: list[dict[str, Any]]) -> tuple[MagicMock, MagicMock]:
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=rows)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AcquireCM(conn))
    return pool, conn


def _pool_fetchrow(row: dict[str, Any] | None) -> tuple[MagicMock, MagicMock]:
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=row)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AcquireCM(conn))
    return pool, conn


@pytest.mark.asyncio
async def test_unparseable_number_short_circuits_without_a_query() -> None:
    pool, conn = _pool_returning([])
    out = await resolve_contact_by_phone(pool, company_id="co-1", phone="not-a-phone")
    assert out is None
    conn.fetch.assert_not_called()  # never touched the DB


@pytest.mark.asyncio
async def test_no_match_returns_none() -> None:
    pool, conn = _pool_returning([])
    out = await resolve_contact_by_phone(pool, company_id="co-1", phone="+15551234567")
    assert out is None
    conn.fetch.assert_awaited_once()
    # Bound args: (sql, company_id, match_key) — the key is the 10 national digits.
    args = conn.fetch.await_args.args
    assert args[1] == "co-1"
    assert args[2] == "5551234567"


@pytest.mark.asyncio
async def test_single_tenant_match() -> None:
    pool, _ = _pool_returning(
        [{"kind": "tenant", "buildium_id": 42, "name": "Josh Gil", "phone": "(555) 123-4567"}]
    )
    out = await resolve_contact_by_phone(pool, company_id="co-1", phone="+15551234567")
    assert out is not None
    assert out.kind == "tenant"
    assert out.buildium_id == 42
    assert out.name == "Josh Gil"
    assert out.phone == "(555) 123-4567"
    assert out.company_id == "co-1"


@pytest.mark.asyncio
async def test_precedence_tenant_beats_owner_beats_vendor() -> None:
    # DB returns them out of precedence order on purpose.
    pool, _ = _pool_returning(
        [
            {"kind": "vendor", "buildium_id": 3, "name": "Acme Plumbing", "phone": "5551234567"},
            {"kind": "owner", "buildium_id": 2, "name": "Ocean LLC", "phone": "5551234567"},
            {"kind": "tenant", "buildium_id": 1, "name": "Josh Gil", "phone": "5551234567"},
        ]
    )
    best = await resolve_contact_by_phone(pool, company_id="co-1", phone="+15551234567")
    assert best is not None
    assert (best.kind, best.buildium_id) == ("tenant", 1)

    all_matches = await resolve_all_contacts_by_phone(pool, company_id="co-1", phone="+15551234567")
    assert [c.kind for c in all_matches] == ["tenant", "owner", "vendor"]


@pytest.mark.asyncio
async def test_ambiguity_is_logged_but_returns_best(caplog: pytest.LogCaptureFixture) -> None:
    pool, _ = _pool_returning(
        [
            {"kind": "tenant", "buildium_id": 1, "name": "Josh Gil", "phone": "5551234567"},
            {"kind": "owner", "buildium_id": 2, "name": "Ocean LLC", "phone": "5551234567"},
        ]
    )
    with caplog.at_level("INFO"):
        best = await resolve_contact_by_phone(pool, company_id="co-1", phone="+15551234567")
    assert best is not None and best.kind == "tenant"
    assert any("distinct Buildium contacts" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_blank_name_falls_back_to_kind_and_id() -> None:
    pool, _ = _pool_returning(
        [{"kind": "vendor", "buildium_id": 9, "name": "  ", "phone": "5551234567"}]
    )
    out = await resolve_contact_by_phone(pool, company_id="co-1", phone="5551234567")
    assert out is not None
    assert out.name == "Vendor #9"


@pytest.mark.asyncio
async def test_none_phone_returns_none_without_query() -> None:
    pool, conn = _pool_returning([])
    out = await resolve_all_contacts_by_phone(pool, company_id="co-1", phone=None)
    assert out == []
    conn.fetch.assert_not_called()


def test_module_exposes_resolvedcontact() -> None:
    # Guards the public surface the transport layer imports.
    assert hasattr(contact_resolver, "ResolvedContact")


# ── outbound direction: contact → dial-ready number ───────────────────────


@pytest.mark.asyncio
async def test_call_target_returns_dialable_e164() -> None:
    pool, conn = _pool_fetchrow({"buildium_id": 42, "name": "Josh Gil", "phone": "(555) 123-4567"})
    target = await resolve_contact_call_target(
        pool, company_id="co-1", kind="tenant", buildium_id=42
    )
    assert target is not None
    assert target.phone_e164 == "+15551234567"  # dial-ready
    assert target.phone_raw == "(555) 123-4567"  # display value preserved
    assert target.name == "Josh Gil"
    assert target.is_dialable is True
    # Scoped query: whitelisted table + the two bound args.
    args = conn.fetchrow.await_args.args
    assert "helixis_tenants" in args[0]
    assert args[1:] == ("co-1", 42)


@pytest.mark.asyncio
async def test_call_target_contact_with_no_number_is_not_dialable() -> None:
    pool, _ = _pool_fetchrow({"buildium_id": 7, "name": "Ocean LLC", "phone": None})
    target = await resolve_contact_call_target(pool, company_id="co-1", kind="owner", buildium_id=7)
    assert target is not None
    assert target.phone_e164 is None
    assert target.is_dialable is False  # caller shows "no phone on file"


@pytest.mark.asyncio
async def test_call_target_missing_contact_returns_none() -> None:
    pool, _ = _pool_fetchrow(None)
    target = await resolve_contact_call_target(
        pool, company_id="co-1", kind="vendor", buildium_id=999
    )
    assert target is None


@pytest.mark.asyncio
async def test_call_target_unknown_kind_never_queries() -> None:
    pool, conn = _pool_fetchrow(None)
    target = await resolve_contact_call_target(
        pool, company_id="co-1", kind="applicant", buildium_id=1
    )
    assert target is None
    conn.fetchrow.assert_not_called()  # kind whitelist blocks the query
