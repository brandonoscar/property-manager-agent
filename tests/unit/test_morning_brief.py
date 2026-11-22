"""Morning brief (roadmap §7 rank 2) — item building + route.

Pins: severity ordering (money → stuck work → windows → cold leads),
zero-count items dropped entirely, the five-item cap, and the
company-scoped route shape.
"""

from __future__ import annotations

import time
from contextlib import ExitStack, asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from fastapi.testclient import TestClient

from helixis.db.repositories.morning_brief import MAX_ITEMS, build_morning_brief
from helixis.main import create_app
from helixis.settings import Settings
from tests.unit._supabase_tokens import TEST_ISSUER, TEST_SUPABASE_URL

TEST_JWT_SECRET = "test-secret-key-for-unit-tests-only-not-production"
COMPANY_ID = "5ca1ab1e-0000-4000-8000-000000000004"
ME_USER_ID = "00000000-0000-0000-0000-000000000001"


def _token() -> str:
    return jwt.encode(
        {
            "sub": ME_USER_ID,
            "email": "brandon@example.com",
            "role": "authenticated",
            "aud": "authenticated",
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
            "iss": TEST_ISSUER,
        },
        TEST_JWT_SECRET,
        algorithm="HS256",
    )


def _settings() -> Settings:
    return Settings(
        env="test",
        auth={"jwt_secret": TEST_JWT_SECRET},
        supabase={"url": TEST_SUPABASE_URL},
        memory={"enabled": False},
    )


def _pool(conn: Any) -> MagicMock:
    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    return pool


def _row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "risk_flags": 0,
        "promises_due": 0,
        "delinquent_leases": 0,
        "delinquent_total": 0,
        "stalled_work_orders": 0,
        "expiring_leases": 0,
    }
    base.update(overrides)
    return base


# ── repository ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_brief_orders_by_severity_and_caps_at_five() -> None:
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        return_value=_row(
            promises_due=2,
            delinquent_leases=9,
            delinquent_total=18340.5,
            stalled_work_orders=12,
            expiring_leases=3,
        )
    )
    with patch("helixis.db.repositories.morning_brief.count_cold_leads", AsyncMock(return_value=4)):
        items = await build_morning_brief(_pool(conn), COMPANY_ID)

    assert [i.key for i in items] == [
        "promises_due",
        "delinquency",
        "stalled_work_orders",
        "expiring_leases",
        "cold_leads",
    ]
    assert len(items) <= MAX_ITEMS
    assert items[1].amount == 18340.5
    assert "$18,340 owed across 9 leases" in items[1].label
    assert items[0].count == 2 and "payment promises" in items[0].label
    # navigation targets are real app views
    assert {i.view for i in items} <= {"chat", "inbox", "properties", "crm"}


@pytest.mark.asyncio
async def test_risk_flags_lead_the_brief_and_enforce_the_cap() -> None:
    """Hidden money leaks outrank everything; with all six sources firing
    the five-item cap drops the least-severe (cold leads)."""
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        return_value=_row(
            risk_flags=2,
            promises_due=1,
            delinquent_leases=3,
            delinquent_total=900,
            stalled_work_orders=4,
            expiring_leases=2,
        )
    )
    with patch("helixis.db.repositories.morning_brief.count_cold_leads", AsyncMock(return_value=5)):
        items = await build_morning_brief(_pool(conn), COMPANY_ID)

    assert len(items) == MAX_ITEMS
    assert items[0].key == "risk_flags"
    assert "2 hidden risks found in your books" in items[0].label
    assert [i.key for i in items] == [
        "risk_flags",
        "promises_due",
        "delinquency",
        "stalled_work_orders",
        "expiring_leases",
    ]


@pytest.mark.asyncio
async def test_brief_drops_zero_items_and_can_be_empty() -> None:
    """A quiet morning is a legitimate result — never invent urgency."""
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=_row(stalled_work_orders=1))
    with patch("helixis.db.repositories.morning_brief.count_cold_leads", AsyncMock(return_value=0)):
        items = await build_morning_brief(_pool(conn), COMPANY_ID)
    assert [i.key for i in items] == ["stalled_work_orders"]
    assert items[0].count == 1 and "1 work order stalled" in items[0].label

    conn.fetchrow = AsyncMock(return_value=_row())
    with patch("helixis.db.repositories.morning_brief.count_cold_leads", AsyncMock(return_value=0)):
        assert await build_morning_brief(_pool(conn), COMPANY_ID) == []


# ── route ────────────────────────────────────────────────────────────


def test_route_returns_items() -> None:
    settings = _settings()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=_row(delinquent_leases=3, delinquent_total=900))
    with ExitStack() as stack:
        stack.enter_context(patch("helixis.api.routes.reports.get_pool", return_value=_pool(conn)))
        stack.enter_context(
            patch(
                "helixis.db.repositories.morning_brief.count_cold_leads",
                AsyncMock(return_value=0),
            )
        )
        stack.enter_context(
            patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID])
        )
        stack.enter_context(patch("helixis.api.auth.get_settings", return_value=settings))
        stack.enter_context(patch("helixis.main.get_settings", return_value=settings))
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.get(
                "/api/v1/reports/morning-brief",
                headers={"Authorization": f"Bearer {_token()}"},
            )

    assert resp.status_code == 200
    body = resp.json()
    assert "generated_at" in body
    assert len(body["items"]) == 1
    assert body["items"][0]["key"] == "delinquency"
    assert body["items"][0]["view"] == "chat"
