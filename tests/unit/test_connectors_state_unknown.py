""" "We could not read your connection state" is not "you never connected it".

``GET /connectors`` used to render every Composio-backed row as
``isConnected: false`` in two very different situations: the customer genuinely
has not connected Gmail, and this DEPLOYMENT cannot reach Composio at all (no
API key, no ``[connectors]`` extra, or the lookup raised). The second is a fact
about US. The page told the customer to click Connect, and that route answers
503 — an instruction that cannot work is worse than none (gotchas 102/106).

The load-bearing tests here are the NEGATIVE ones (gotchas 38/51): with Composio
healthy, a never-connected row must NOT claim the integration is unavailable,
and Buildium — whose state lives in its own credentials row — must stay knowable
straight through a Composio outage.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import jwt
import pytest
from fastapi.testclient import TestClient

from helixis.main import create_app
from helixis.settings import Settings
from tests.unit._supabase_tokens import TEST_ISSUER, TEST_SUPABASE_URL

TEST_JWT_SECRET = "test-secret-key-for-unit-tests-only-not-production"
COMPANY_ID = "5ca1ab1e-0000-4000-8000-000000000004"
USER_ID = "00000000-0000-0000-0000-000000000001"

#: Every Composio-backed row in the curated catalog. Buildium is deliberately
#: absent — it is an api_key connector with no toolkit.
_COMPOSIO_ROWS = ("gmail", "google_calendar", "google_drive")


def _token() -> str:
    return jwt.encode(
        {
            "sub": USER_ID,
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


class _ComposioUnconfigured:
    """The deployment has no usable Composio client.

    ``ComposioClient.available`` is ``self._initialized and self._client is not
    None`` — computed once in ``__init__`` — so this is the state of a fresh
    white-label install missing HELIXIS_COMPOSIO__API_KEY or the extra.
    """

    available = False

    def list_connections(self, _identity: str) -> list[dict[str, Any]]:  # pragma: no cover
        raise AssertionError("must not be called when available is False")


class _ComposioLookupRaises:
    """Configured, reachable, and the connection lookup blew up."""

    available = True
    _client = SimpleNamespace(connected_accounts=SimpleNamespace())

    def list_connections(self, _identity: str) -> list[dict[str, Any]]:
        raise RuntimeError("SDK mismatch")


class _ComposioHealthy:
    """The ordinary state: gmail connected, the rest simply not set up yet."""

    available = True
    _client = SimpleNamespace(connected_accounts=SimpleNamespace())

    def list_connections(self, _identity: str) -> list[dict[str, Any]]:
        return [{"toolkit": "gmail", "status": "active"}]


@pytest.fixture
def settings() -> Settings:
    return Settings(
        env="test",
        auth={"jwt_secret": TEST_JWT_SECRET},
        supabase={"url": TEST_SUPABASE_URL},
        memory={"enabled": False},
    )


def _rows(settings: Settings, composio: object) -> dict[str, dict[str, Any]]:
    async def fake_resolve(_user_id: str) -> list[str]:
        return [COMPANY_ID]

    with (
        patch("helixis.api.auth._resolve_company_ids", side_effect=fake_resolve),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            c.app.state.composio_client = composio
            resp = c.get(
                "/api/v1/connectors/",
                headers={"Authorization": f"Bearer {_token()}"},
            )
    assert resp.status_code == 200
    return {row["id"]: row for row in resp.json()}


# ── the state is unknown ────────────────────────────────────────


@pytest.mark.parametrize("row_id", _COMPOSIO_ROWS)
def test_an_unconfigured_deployment_flags_every_composio_row(
    settings: Settings, row_id: str
) -> None:
    row = _rows(settings, _ComposioUnconfigured())[row_id]
    assert row["isConnected"] is False
    assert row["stateUnknown"] is True


@pytest.mark.parametrize("row_id", _COMPOSIO_ROWS)
def test_a_failed_lookup_flags_every_composio_row(settings: Settings, row_id: str) -> None:
    """The call raising tells us NOTHING about what the customer connected.

    Rendering that as "not connected" is a claim we cannot support, so the row
    says the state is unreadable instead.
    """
    row = _rows(settings, _ComposioLookupRaises())[row_id]
    assert row["isConnected"] is False
    assert row["stateUnknown"] is True


@pytest.mark.parametrize(
    "composio", [_ComposioUnconfigured(), _ComposioLookupRaises()], ids=["unconfigured", "raises"]
)
def test_buildium_stays_knowable_through_a_composio_outage(
    settings: Settings, composio: object
) -> None:
    """The NARROWING that keeps a knowable fact knowable.

    Buildium is an api_key connector with no toolkit — its state comes from the
    per-company credentials row and a Composio outage says nothing about it.
    Flagging it would tell a customer their Buildium status was unreadable when
    we had read it.
    """
    assert _rows(settings, composio)["buildium"]["stateUnknown"] is False


# ── the state is known ──────────────────────────────────────────


def test_a_healthy_deployment_flags_nothing(settings: Settings) -> None:
    """THE NEGATIVE. A version that always set the flag passes every test above
    and tells every new customer their setup is unfixable — while the ordinary
    first-run path (click Connect) is exactly what they need."""
    rows = _rows(settings, _ComposioHealthy())
    assert rows["gmail"]["isConnected"] is True
    for row_id in _COMPOSIO_ROWS + ("buildium",):
        assert rows[row_id]["stateUnknown"] is False, row_id


def test_a_never_connected_row_on_a_healthy_deployment_still_says_connect(
    settings: Settings,
) -> None:
    """The distinguishing input: not connected AND knowably so."""
    drive = _rows(settings, _ComposioHealthy())["google_drive"]
    assert drive["isConnected"] is False
    assert drive["needsReauth"] is False
    assert drive["stateUnknown"] is False


def test_the_field_is_present_on_every_row(settings: Settings) -> None:
    """The client reads ``Boolean(c.stateUnknown)``, so an ABSENT key is falsy
    and indistinguishable from a healthy deployment — which is the defect this
    whole change exists to close. Pin that the route always emits it."""
    for row in _rows(settings, _ComposioUnconfigured()).values():
        assert "stateUnknown" in row, row["id"]
