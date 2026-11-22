"""Tests for the Buildium credential management routes.

These cover the contract that matters: plaintext never round-trips
through the read API, ciphertext is what's persisted, and the
``/test`` flow exercises BuildiumClient with the decrypted creds.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from helixis.main import create_app
from helixis.settings import Settings
from tests.unit._supabase_tokens import TEST_ISSUER, TEST_SUPABASE_URL

TEST_JWT_SECRET = "test-secret-key-for-unit-tests-only-not-production"
TEST_DATA_KEY = Fernet.generate_key().decode()


def _make_token(sub: str = "user-1") -> str:
    return jwt.encode(
        {
            "sub": sub,
            "email": "test@example.com",
            "role": "authenticated",
            "aud": "authenticated",
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
            "iss": TEST_ISSUER,
        },
        TEST_JWT_SECRET,
        algorithm="HS256",
    )


@pytest.fixture
def test_settings() -> Settings:
    return Settings(
        env="test",
        auth={"jwt_secret": TEST_JWT_SECRET},
        supabase={"url": TEST_SUPABASE_URL},
        data_key=TEST_DATA_KEY,
        memory={"enabled": False},
    )


@pytest.fixture
def fake_pool():
    """An asyncpg pool stand-in.

    Every method (``execute``, ``fetchrow``, ``fetch``) returns a
    pre-configured value the test sets up. The shared ``rows`` dict
    is keyed on company_id so tests can simulate a one-row table
    without spinning up a real database.
    """
    # ``role`` is the caller's company_users role that ``require_role`` reads on
    # the shared-connection write routes. Default ``admin`` so the existing
    # happy-path tests (which don't care about roles) keep passing; the role-floor
    # gap-tests below flip it to ``member`` / ``None`` to assert the 403.
    state: dict[str, dict] = {"rows": {}, "role": "admin"}

    pool = MagicMock()

    async def execute(sql: str, *args):
        sql_normalized = " ".join(sql.split())
        if sql_normalized.startswith("INSERT INTO public.company_buildium_credentials"):
            # account_id was dropped from the write path (migration 0005).
            # The row simulates an existing column with a NULL default.
            state["rows"][args[0]] = {
                "company_id": args[0],
                "encrypted_client_id": args[1],
                "encrypted_client_secret": args[2],
                "client_id_last4": args[3],
                "encrypted_webhook_secret": None,
                "webhook_secret_last4": None,
                "account_id": None,
                "base_url": args[4],
                "last_tested_at": None,
            }
        elif sql_normalized.startswith("DELETE FROM public.company_buildium_credentials"):
            state["rows"].pop(args[0], None)
        elif sql_normalized.startswith(
            "UPDATE public.company_buildium_credentials SET last_tested_at"
        ):
            # Args: (company_id, account_id_or_None). COALESCE preserves
            # the existing account_id when the new value is None.
            row = state["rows"].get(args[0])
            if row:
                row["last_tested_at"] = datetime.now(UTC)
                if len(args) >= 2 and args[1] is not None:
                    row["account_id"] = args[1]
        elif sql_normalized.startswith(
            "UPDATE public.company_buildium_credentials SET encrypted_webhook_secret = $2"
        ):
            row = state["rows"].get(args[0])
            if row:
                row["encrypted_webhook_secret"] = args[1]
                row["webhook_secret_last4"] = args[2]
        elif sql_normalized.startswith(
            "UPDATE public.company_buildium_credentials SET encrypted_webhook_secret = NULL"
        ):
            row = state["rows"].get(args[0])
            if row:
                row["encrypted_webhook_secret"] = None
                row["webhook_secret_last4"] = None

    async def fetchrow(sql: str, *args):
        s = " ".join(sql.split())
        if s.startswith("SELECT role FROM public.company_users"):
            # require_role's gate query. args = (user_id, company_id).
            role = state.get("role")
            return {"role": role} if role is not None else None
        # Credentials-row query. args = (company_id,).
        return state["rows"].get(args[0]) if args else None

    async def fetch(_sql: str, _company_id: str):
        return []

    pool.execute = AsyncMock(side_effect=execute)
    pool.fetchrow = AsyncMock(side_effect=fetchrow)
    pool.fetch = AsyncMock(side_effect=fetch)
    pool._state = state
    return pool


@pytest.fixture
def client(test_settings, fake_pool):
    # The connectors limiter is a module-level singleton (rate_limit.py).
    # Clearing its bucket map ensures one test's traffic doesn't push a
    # later test over the per-user cap and turn it into a 429.
    from helixis.api.rate_limit import connectors_limiter

    connectors_limiter.counter._log.clear()

    with (
        patch("helixis.api.auth.get_settings", return_value=test_settings),
        patch("helixis.api.routes.buildium.get_settings", return_value=test_settings),
        patch("helixis.api.routes.buildium.get_pool", return_value=fake_pool),
        patch(
            "helixis.api.auth._resolve_company_ids",
            new=AsyncMock(return_value=["company-A"]),
        ),
    ):
        app = create_app(test_settings)
        app.state.memory_manager = MagicMock(available=False)
        app.state.composio_client = MagicMock(available=False)

        @asynccontextmanager
        async def noop_lifespan(app):
            yield

        app.router.lifespan_context = noop_lifespan

        with TestClient(app) as c:
            yield c


# ── /credentials ───────────────────────────────────────────────────


def test_credentials_get_unconfigured(client):
    resp = client.get(
        "/api/v1/buildium/credentials",
        headers={"Authorization": f"Bearer {_make_token()}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is False
    assert body["client_id_last4"] is None


def test_credentials_put_then_get_returns_masked_only(client, fake_pool):
    token = _make_token()
    resp = client.put(
        "/api/v1/buildium/credentials",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "client_id": "abcdef-1234",
            "client_secret": "supersecretvalue",
            "environment": "sandbox",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is True
    assert body["client_id_last4"] == "1234"
    assert body["environment"] == "sandbox"
    # Plaintext must never make it to the response.
    assert "supersecretvalue" not in resp.text
    assert "abcdef-1234" not in resp.text

    # And the row stored ciphertext, not plaintext.
    stored = fake_pool._state["rows"]["company-A"]
    assert stored["encrypted_client_id"] != "abcdef-1234"
    assert stored["encrypted_client_secret"] != "supersecretvalue"
    cipher = Fernet(TEST_DATA_KEY.encode())
    assert cipher.decrypt(stored["encrypted_client_id"].encode()) == b"abcdef-1234"
    assert cipher.decrypt(stored["encrypted_client_secret"].encode()) == b"supersecretvalue"


def test_credentials_delete_clears_row(client, fake_pool):
    token = _make_token()
    client.put(
        "/api/v1/buildium/credentials",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "client_id": "abc-1234",
            "client_secret": "secret-12345678",
            "environment": "sandbox",
        },
    )
    resp = client.delete(
        "/api/v1/buildium/credentials",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["configured"] is False
    assert "company-A" not in fake_pool._state["rows"]


# ── /test ──────────────────────────────────────────────────────────


def test_test_returns_error_when_no_creds_saved(client):
    resp = client.post(
        "/api/v1/buildium/test",
        headers={"Authorization": f"Bearer {_make_token()}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "no saved" in body["error"].lower()


def _make_buildium_client_double(*, rentals=None, account=None, account_raises=None):
    """Build a fake BuildiumClient async-context-manager.

    Routes by path so ``/rentals`` and ``/administration/account`` can
    have independent return values (or raise independently).
    """
    rentals = rentals if rentals is not None else [{"Name": "Helixis Demo Account"}]

    async def _request(method, path, **kwargs):
        if path == "/rentals":
            return rentals
        if path == "/administration/account":
            if account_raises is not None:
                raise account_raises
            return account if account is not None else {"Id": 652932, "CompanyName": "Demo"}
        raise AssertionError(f"unexpected path {path}")

    fake = MagicMock()
    fake.request = AsyncMock(side_effect=_request)
    fake.aclose = AsyncMock()
    fake.__aenter__ = AsyncMock(return_value=fake)
    fake.__aexit__ = AsyncMock(return_value=False)
    return fake


def test_test_calls_buildium_with_decrypted_creds(client, fake_pool):
    token = _make_token()
    client.put(
        "/api/v1/buildium/credentials",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "client_id": "abc-1234",
            "client_secret": "secret-12345678",
            "environment": "sandbox",
        },
    )

    fake_client = _make_buildium_client_double()

    with patch("helixis.api.routes.buildium.BuildiumClient", return_value=fake_client):
        resp = client.post(
            "/api/v1/buildium/test",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["account_name"] == "Helixis Demo Account"
    assert body["properties_count"] == 1
    # Both endpoints get hit on a successful test.
    paths = [c.args[1] for c in fake_client.request.call_args_list]
    assert "/rentals" in paths
    assert "/administration/account" in paths


def test_test_backfills_account_id_from_administration_account(client, fake_pool):
    """The /test flow is the single source of truth for populating
    ``account_id``. The form no longer collects it (migration 0005), so
    this is how webhook routing gets a row keyed by AccountId."""
    token = _make_token()
    client.put(
        "/api/v1/buildium/credentials",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "client_id": "abc-1234",
            "client_secret": "secret-12345678",
            "environment": "sandbox",
        },
    )
    # Pre-condition: row exists with account_id NULL (matches PUT shape).
    assert fake_pool._state["rows"]["company-A"]["account_id"] is None

    fake_client = _make_buildium_client_double(account={"Id": 652932, "CompanyName": "Demo"})

    with patch("helixis.api.routes.buildium.BuildiumClient", return_value=fake_client):
        resp = client.post(
            "/api/v1/buildium/test",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    # The internal account_id must not surface to the user-facing response.
    assert "652932" not in resp.text
    assert fake_pool._state["rows"]["company-A"]["account_id"] == 652932


def test_test_account_already_linked_returns_clean_error_not_crash(client, fake_pool):
    """Regression (prod 2026-07-26, Sentry PYTHON-FASTAPI-38): connecting a
    Buildium account already linked to another workspace hit the unique
    constraint on account_id and escaped as an UNHANDLED 500 — which skips
    CORSMiddleware, so the onboarding wizard saw an opaque 'failed to fetch'.
    The account-claim UPDATE is now caught: the endpoint returns a clear
    ok=False error (CORS-safe), still records last_tested_at (the creds were
    valid), and does NOT claim the account."""
    import asyncpg

    token = _make_token()
    client.put(
        "/api/v1/buildium/credentials",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "client_id": "abc-1234",
            "client_secret": "secret-12345678",
            "environment": "sandbox",
        },
    )

    # The account-claiming UPDATE collides (another company owns 652932); the
    # follow-up last_tested_at-only UPDATE must still succeed.
    orig_execute = fake_pool.execute.side_effect

    async def execute_with_conflict(sql: str, *args):
        if "account_id = COALESCE" in " ".join(sql.split()):
            raise asyncpg.UniqueViolationError(
                "duplicate key value violates unique constraint "
                '"company_buildium_credentials_account_id_uidx"'
            )
        return await orig_execute(sql, *args)

    fake_pool.execute = AsyncMock(side_effect=execute_with_conflict)

    fake_client = _make_buildium_client_double(account={"Id": 652932})
    with patch("helixis.api.routes.buildium.BuildiumClient", return_value=fake_client):
        resp = client.post(
            "/api/v1/buildium/test",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200  # handled — NOT a 500 that would skip CORS
    body = resp.json()
    assert body["ok"] is False
    assert "already connected" in body["error"].lower()
    assert "652932" in body["error"]  # tells the user which account
    row = fake_pool._state["rows"]["company-A"]
    assert row["last_tested_at"] is not None  # creds validated
    assert row["account_id"] is None  # but the account was NOT claimed


def test_test_is_idempotent_when_account_id_already_set(client, fake_pool, caplog):
    """Running /test twice with a stable Buildium account does not log
    a rotation warning and leaves the stored id unchanged."""
    token = _make_token()
    client.put(
        "/api/v1/buildium/credentials",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "client_id": "abc-1234",
            "client_secret": "secret-12345678",
            "environment": "sandbox",
        },
    )

    fake_client = _make_buildium_client_double(account={"Id": 652932})
    with patch("helixis.api.routes.buildium.BuildiumClient", return_value=fake_client):
        client.post(
            "/api/v1/buildium/test",
            headers={"Authorization": f"Bearer {token}"},
        )
        caplog.clear()
        with caplog.at_level("WARNING"):
            resp = client.post(
                "/api/v1/buildium/test",
                headers={"Authorization": f"Bearer {token}"},
            )

    assert resp.status_code == 200
    assert fake_pool._state["rows"]["company-A"]["account_id"] == 652932
    rotation_logs = [r for r in caplog.records if "account_id changed" in r.getMessage()]
    assert rotation_logs == []


def test_test_logs_rotation_warning_when_account_id_changes(client, fake_pool, caplog):
    """If the user rotates to a different Buildium account, /test
    overwrites the stored id and logs a warning so an operator can
    diagnose any downstream confusion."""
    token = _make_token()
    client.put(
        "/api/v1/buildium/credentials",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "client_id": "abc-1234",
            "client_secret": "secret-12345678",
            "environment": "sandbox",
        },
    )
    # Seed a stale account_id directly on the row.
    fake_pool._state["rows"]["company-A"]["account_id"] = 111111

    fake_client = _make_buildium_client_double(account={"Id": 652932})
    with caplog.at_level("WARNING"):
        with patch("helixis.api.routes.buildium.BuildiumClient", return_value=fake_client):
            resp = client.post(
                "/api/v1/buildium/test",
                headers={"Authorization": f"Bearer {token}"},
            )

    assert resp.status_code == 200
    assert fake_pool._state["rows"]["company-A"]["account_id"] == 652932
    rotation_logs = [r for r in caplog.records if "account_id changed" in r.getMessage()]
    assert len(rotation_logs) == 1
    assert "111111" in rotation_logs[0].getMessage()
    assert "652932" in rotation_logs[0].getMessage()


def test_test_succeeds_even_if_administration_account_fails(client, fake_pool):
    """If /administration/account 5xx's or otherwise fails, the test
    must still report ok=True (the user's creds are good — that's what
    /rentals proved). The stored account_id stays NULL until next /test."""
    from helixis.buildium.errors import BuildiumApiError

    token = _make_token()
    client.put(
        "/api/v1/buildium/credentials",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "client_id": "abc-1234",
            "client_secret": "secret-12345678",
            "environment": "sandbox",
        },
    )

    fake_client = _make_buildium_client_double(
        account_raises=BuildiumApiError(
            status=502,
            message="bad gateway",
            path="/administration/account",
        ),
    )
    with patch("helixis.api.routes.buildium.BuildiumClient", return_value=fake_client):
        resp = client.post(
            "/api/v1/buildium/test",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["properties_count"] == 1
    assert fake_pool._state["rows"]["company-A"]["account_id"] is None


def test_test_never_shows_credentials_that_buildium_echoes_back(client, fake_pool, caplog):
    """``/test`` returns ``f"Buildium rejected the request: {exc}"`` to the page
    and logs it, and the error's message is Buildium's body. A server or proxy
    that echoes the auth headers put the credentials in both until the client
    masked them. A REAL client here, not the double, so the mask runs against
    the credentials the route decrypted."""
    import logging

    import httpx

    from helixis.buildium.client import BuildiumClient

    caplog.set_level(logging.DEBUG)
    # Built at runtime: a key-shaped literal here trips gitleaks (it did, on
    # 0732772; pinned in .gitleaksignore).
    client_id = "-".join(("bdid", "fake", "id", "for", "tests"))
    secret = "-".join(("bd", "fake", "secret", "for", "tests"))
    token = _make_token()
    client.put(
        "/api/v1/buildium/credentials",
        headers={"Authorization": f"Bearer {token}"},
        json={"client_id": client_id, "client_secret": secret, "environment": "sandbox"},
    )

    def echoing_client(settings):
        assert settings.client_secret == secret  # the route's own decrypted creds
        echo = f"denied: x-buildium-client-id: {client_id}; x-buildium-client-secret: {secret}"
        http = httpx.AsyncClient(
            base_url="https://x.test/v1",
            transport=httpx.MockTransport(lambda _req: httpx.Response(401, text=echo)),
        )
        return BuildiumClient(settings, http_client=http)

    with patch("helixis.api.routes.buildium.BuildiumClient", side_effect=echoing_client):
        resp = client.post("/api/v1/buildium/test", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "denied:" in body["error"]  # the real failure path ran
    logged = "\n".join(r.getMessage() for r in caplog.records)
    for credential in (client_id, secret):
        assert credential not in resp.text
        assert credential not in logged


# ── /backfill ──────────────────────────────────────────────────────


def _seed_connected_company(client, fake_pool, *, account_id: int = 652932) -> str:
    """PUT creds + set a stored account_id so /backfill's preconditions
    (saved creds AND account_id) are met. Returns the auth token."""
    token = _make_token()
    client.put(
        "/api/v1/buildium/credentials",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "client_id": "abc-1234",
            "client_secret": "secret-12345678",
            "environment": "sandbox",
        },
    )
    fake_pool._state["rows"]["company-A"]["account_id"] = account_id
    return token


def test_backfill_geocodes_newly_synced_properties(client, fake_pool):
    """A successful /backfill must kick the geocode pass immediately, so
    "properties near X" / "vendors near property" work the moment a user
    hits Sync data instead of waiting for the worker's next mirror tick."""
    from helixis.buildium.backfill import BackfillResult

    token = _seed_connected_company(client, fake_pool)

    geocode = AsyncMock(return_value=3)
    with (
        patch(
            # ⚠ RE-POINTED 2026-09-16, never relaxed. These three assert the geocode
            # pass around the mirror pull, which is unchanged — only the
            # function the route calls moved, because the Sync data button now
            # goes through the wrapper that records `buildium_sync_state` (see
            # test_stranded_sync_recovers.py). Patching the old name would leave
            # these exercising a call the route no longer makes.
            "helixis.api.routes.buildium.run_initial_sync",
            new=AsyncMock(return_value=BackfillResult(properties=3)),
        ),
        patch("helixis.api.routes.buildium.geocode_company_properties", new=geocode),
    ):
        resp = client.post(
            "/api/v1/buildium/backfill",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    # The geocode pass ran, scoped to the caller's company.
    geocode.assert_awaited_once()
    assert geocode.await_args.kwargs["company_id"] == "company-A"


def test_backfill_succeeds_even_if_geocode_pass_raises(client, fake_pool):
    """Geocoding is an enhancement, never load-bearing: a raising geocode
    pass must not turn a good backfill into a failed sync."""
    from helixis.buildium.backfill import BackfillResult

    token = _seed_connected_company(client, fake_pool)

    with (
        patch(
            # ⚠ RE-POINTED 2026-09-16, never relaxed. These three assert the geocode
            # pass around the mirror pull, which is unchanged — only the
            # function the route calls moved, because the Sync data button now
            # goes through the wrapper that records `buildium_sync_state` (see
            # test_stranded_sync_recovers.py). Patching the old name would leave
            # these exercising a call the route no longer makes.
            "helixis.api.routes.buildium.run_initial_sync",
            new=AsyncMock(return_value=BackfillResult(properties=2)),
        ),
        patch(
            "helixis.api.routes.buildium.geocode_company_properties",
            new=AsyncMock(side_effect=RuntimeError("maps down")),
        ),
    ):
        resp = client.post(
            "/api/v1/buildium/backfill",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["properties"] == 2


def test_backfill_does_not_geocode_when_mirror_fails(client, fake_pool):
    """If the backfill itself crashes, the route returns an error and the
    geocode pass never runs (nothing new to geocode)."""
    token = _seed_connected_company(client, fake_pool)

    geocode = AsyncMock(return_value=0)
    with (
        patch(
            # ⚠ RE-POINTED 2026-09-16, never relaxed. These three assert the geocode
            # pass around the mirror pull, which is unchanged — only the
            # function the route calls moved, because the Sync data button now
            # goes through the wrapper that records `buildium_sync_state` (see
            # test_stranded_sync_recovers.py). Patching the old name would leave
            # these exercising a call the route no longer makes.
            "helixis.api.routes.buildium.run_initial_sync",
            new=AsyncMock(side_effect=RuntimeError("buildium 500")),
        ),
        patch("helixis.api.routes.buildium.geocode_company_properties", new=geocode),
    ):
        resp = client.post(
            "/api/v1/buildium/backfill",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    geocode.assert_not_awaited()


# ── /webhook-secret ────────────────────────────────────────────────


def test_get_webhook_secret_returns_unconfigured_when_no_row(client):
    """No credentials row at all — the dedicated status endpoint reports
    unconfigured rather than echoing back stale last4 bytes."""
    resp = client.get(
        "/api/v1/buildium/webhook-secret",
        headers={"Authorization": f"Bearer {_make_token()}"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"configured": False, "last4": None}


def test_get_webhook_secret_returns_unconfigured_when_secret_null(client, fake_pool):
    """Row exists (API creds saved) but encrypted_webhook_secret IS NULL —
    matches the post-PUT-credentials, pre-PUT-webhook-secret state."""
    token = _make_token()
    client.put(
        "/api/v1/buildium/credentials",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "client_id": "abc-1234",
            "client_secret": "secret-12345678",
            "environment": "sandbox",
        },
    )
    # Sanity-check the fixture state before the GET.
    assert fake_pool._state["rows"]["company-A"]["encrypted_webhook_secret"] is None

    resp = client.get(
        "/api/v1/buildium/webhook-secret",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"configured": False, "last4": None}


def test_get_webhook_secret_returns_configured_with_last4(client, fake_pool):
    """Full row with secret saved — endpoint surfaces last4 only, never
    the encrypted payload."""
    token = _make_token()
    client.put(
        "/api/v1/buildium/credentials",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "client_id": "abc-1234",
            "client_secret": "secret-12345678",
            "environment": "sandbox",
        },
    )
    plaintext = "secret-12345678"
    client.put(
        "/api/v1/buildium/webhook-secret",
        headers={"Authorization": f"Bearer {token}"},
        json={"secret": plaintext},
    )

    resp = client.get(
        "/api/v1/buildium/webhook-secret",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"configured": True, "last4": plaintext[-4:]}
    # The encrypted blob must never round-trip through the response.
    encrypted = fake_pool._state["rows"]["company-A"]["encrypted_webhook_secret"]
    assert encrypted not in resp.text
    assert plaintext not in resp.text


def test_get_webhook_secret_rejects_unauthenticated(client):
    """No Authorization header → 401, same as every other buildium route."""
    resp = client.get("/api/v1/buildium/webhook-secret")
    assert resp.status_code == 401


def test_get_webhook_secret_isolates_by_company(client, fake_pool):
    """A user authenticated to company-A must never see company-B's
    saved secret. Company-B is seeded directly on the fixture's row map
    to simulate another tenant's data sitting in the same table."""
    fake_pool._state["rows"]["company-B"] = {
        "company_id": "company-B",
        "encrypted_client_id": "x",
        "encrypted_client_secret": "x",
        "client_id_last4": "0000",
        "encrypted_webhook_secret": "encrypted-other-company-secret",
        "webhook_secret_last4": "9999",
        "account_id": None,
        "base_url": "https://apisandbox.buildium.com",
        "last_tested_at": None,
    }
    # The `client` fixture maps the authenticated user to company-A only.
    resp = client.get(
        "/api/v1/buildium/webhook-secret",
        headers={"Authorization": f"Bearer {_make_token()}"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"configured": False, "last4": None}
    # Company-B's last4 must not appear anywhere in the response body.
    assert "9999" not in resp.text


def test_webhook_secret_requires_credentials_saved_first(client):
    """PUT /webhook-secret with no API creds yet is a 400 — we can't
    store the secret because there's no row to attach it to."""
    resp = client.put(
        "/api/v1/buildium/webhook-secret",
        headers={"Authorization": f"Bearer {_make_token()}"},
        json={"secret": "secret-12345678"},
    )
    assert resp.status_code == 400
    assert "Save your Buildium API credentials" in resp.json()["detail"]


def test_webhook_secret_paste_stores_user_supplied_value_encrypted(client, fake_pool):
    """The user pastes the secret Buildium showed them; we encrypt &
    persist it and never echo it back."""
    token = _make_token()
    client.put(
        "/api/v1/buildium/credentials",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "client_id": "abc-1234",
            "client_secret": "secret-12345678",
            "environment": "sandbox",
        },
    )
    plaintext = "secret-12345678"
    resp = client.put(
        "/api/v1/buildium/webhook-secret",
        headers={"Authorization": f"Bearer {token}"},
        json={"secret": plaintext},
    )
    assert resp.status_code == 204
    # Plaintext must never round-trip through the response.
    assert plaintext not in resp.text

    stored = fake_pool._state["rows"]["company-A"]
    assert stored["encrypted_webhook_secret"] is not None
    assert stored["encrypted_webhook_secret"] != plaintext
    cipher = Fernet(TEST_DATA_KEY.encode())
    assert cipher.decrypt(stored["encrypted_webhook_secret"].encode()).decode() == plaintext
    # Last4 reflects the user-pasted value, not a server-generated one.
    assert stored["webhook_secret_last4"] == plaintext[-4:]


def test_webhook_secret_replace_overwrites_old_value(client, fake_pool):
    """Calling PUT a second time with a new value rotates the secret."""
    token = _make_token()
    client.put(
        "/api/v1/buildium/credentials",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "client_id": "abc-1234",
            "client_secret": "secret-12345678",
            "environment": "sandbox",
        },
    )
    first = "first-secret-AAAAAAAA"
    second = "second-secret-BBBBBBB"
    client.put(
        "/api/v1/buildium/webhook-secret",
        headers={"Authorization": f"Bearer {token}"},
        json={"secret": first},
    )
    client.put(
        "/api/v1/buildium/webhook-secret",
        headers={"Authorization": f"Bearer {token}"},
        json={"secret": second},
    )
    cipher = Fernet(TEST_DATA_KEY.encode())
    stored = fake_pool._state["rows"]["company-A"]
    assert cipher.decrypt(stored["encrypted_webhook_secret"].encode()).decode() == second


def test_webhook_secret_too_short_is_rejected(client, fake_pool):
    """The validation guard catches obvious pastes that didn't include
    the whole value."""
    token = _make_token()
    client.put(
        "/api/v1/buildium/credentials",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "client_id": "abc-1234",
            "client_secret": "secret-12345678",
            "environment": "sandbox",
        },
    )
    resp = client.put(
        "/api/v1/buildium/webhook-secret",
        headers={"Authorization": f"Bearer {token}"},
        json={"secret": "abc"},
    )
    assert resp.status_code == 422  # pydantic validation error


# ── /recent-events resilience ──────────────────────────────────────


def test_recent_events_returns_empty_when_table_missing(client, fake_pool):
    """The buildium_events table isn't part of the supabase migrations
    bundle, so on a fresh prod env it can legitimately not exist. The
    route must not 500 — the panel should render its empty state."""
    import asyncpg

    fake_pool.fetch = AsyncMock(
        side_effect=asyncpg.exceptions.UndefinedTableError(
            'relation "buildium_events" does not exist'
        )
    )

    resp = client.get(
        "/api/v1/buildium/recent-events",
        headers={"Authorization": f"Bearer {_make_token()}"},
    )
    assert resp.status_code == 200
    assert resp.json() == []


def test_recent_events_500s_loud_on_unexpected_db_error(client, fake_pool):
    """PR #96 — pre-fix this handler silently returned ``[]`` on any
    DB exception other than UndefinedTable / UndefinedColumn, which
    hid the PR #92 summary-truth bug for 30 minutes (the inbox just
    rendered "no events" instead of surfacing the actual error).
    The new contract: log at ``error`` with the traceback and
    re-raise so FastAPI returns 500 and Render monitoring picks
    it up.

    Pre-PR-#96 this test asserted 200 + ``[]``. The flipped
    assertion below pins the new "loud failure" contract."""
    fake_pool.fetch = AsyncMock(side_effect=RuntimeError("connection lost"))

    # TestClient's session client doesn't carry the
    # ``raise_server_exceptions=False`` flag by default; rebuild a
    # one-shot client off the same FastAPI app the fixture mounted
    # so the re-raised exception surfaces as a 500 response object
    # instead of propagating to the caller.
    bare_client = TestClient(client.app, raise_server_exceptions=False)
    resp = bare_client.get(
        "/api/v1/buildium/recent-events",
        headers={"Authorization": f"Bearer {_make_token()}"},
    )
    assert resp.status_code == 500


# ── auth gating ─────────────────────────────────────────────────────


def test_buildium_routes_require_auth(client):
    for method, path in [
        ("get", "/api/v1/buildium/credentials"),
        ("put", "/api/v1/buildium/credentials"),
        ("delete", "/api/v1/buildium/credentials"),
        ("post", "/api/v1/buildium/test"),
        ("get", "/api/v1/buildium/webhook-secret"),
        ("put", "/api/v1/buildium/webhook-secret"),
        ("delete", "/api/v1/buildium/webhook-secret"),
        ("get", "/api/v1/buildium/recent-events"),
    ]:
        resp = getattr(client, method)(path)
        assert resp.status_code == 401, (
            f"{method.upper()} {path} should require auth, got {resp.status_code}"
        )


# ── role floor on the shared-connection write routes (#72 red gates) ──
#
# The Buildium credentials + webhook secret are a SINGLE per-company row the
# whole team shares. A member overwriting/deleting them (or rotating the webhook
# secret) silently breaks routing + API access for everyone — so every mutating
# route requires Manager+, and fails CLOSED when the caller's role can't be
# verified. These are the deterministic red gates: remove a `_require_manager`
# call and the matching test below goes red.

# ⚠ RENAMED + WIDENED 2026-09-04: the floor is ADMIN now (founder direction),
# and the two GETs joined the list. They were the real hole — every mutation
# here was already guarded, while a member could read the connection status and
# the webhook secret's last-4. See
# tests/unit/test_buildium_connection_routes_are_admin_only.py for why the
# companion guard is an AST walk: this list is hand-maintained, so it cannot
# notice a SEVENTH route that never gets added to it.
_ADMIN_GATED = [
    ("get", "/api/v1/buildium/credentials", None),
    ("get", "/api/v1/buildium/webhook-secret", None),
    (
        "put",
        "/api/v1/buildium/credentials",
        {"client_id": "abcdef-1234", "client_secret": "supersecretvalue", "environment": "sandbox"},
    ),
    ("delete", "/api/v1/buildium/credentials", None),
    ("post", "/api/v1/buildium/test", None),
    ("post", "/api/v1/buildium/backfill", None),
    ("put", "/api/v1/buildium/webhook-secret", {"secret": "a-real-webhook-secret"}),
    ("delete", "/api/v1/buildium/webhook-secret", None),
]


@pytest.mark.parametrize(("method", "path", "body"), _ADMIN_GATED)
def test_member_cannot_touch_shared_connection(client, fake_pool, method, path, body):
    """A plain member is refused (403) on every shared-connection route —
    READS included, which is the half that used to be open."""
    fake_pool._state["role"] = "member"
    kwargs = {"headers": {"Authorization": f"Bearer {_make_token()}"}}
    if body is not None:
        kwargs["json"] = body
    resp = getattr(client, method)(path, **kwargs)
    assert resp.status_code == 403, f"{method.upper()} {path} should 403 for a member"
    assert "admin" in resp.text.lower()


@pytest.mark.parametrize(("method", "path", "body"), _ADMIN_GATED)
def test_shared_connection_fails_closed_without_membership(client, fake_pool, method, path, body):
    """No ``company_users`` row → role unverifiable → refuse (fail closed),
    never silently allow."""
    fake_pool._state["role"] = None
    kwargs = {"headers": {"Authorization": f"Bearer {_make_token()}"}}
    if body is not None:
        kwargs["json"] = body
    resp = getattr(client, method)(path, **kwargs)
    assert resp.status_code == 403, f"{method.upper()} {path} should fail closed with no membership"


def test_manager_can_no_longer_change_credentials(client, fake_pool):
    """A MANAGER is now refused — the floor is Admin, not Manager+.

    ⚠ INVERTED 2026-09-04, not deleted. This case previously asserted the
    opposite and was correct when written; the founder raised the floor because
    the Buildium connection is the one shared row whose loss breaks the whole
    company. Keeping the case pointed the other way is what makes a silent
    revert to Manager+ fail — deleting it would leave that revert green.
    """
    fake_pool._state["role"] = "manager"
    resp = client.put(
        "/api/v1/buildium/credentials",
        headers={"Authorization": f"Bearer {_make_token()}"},
        json={
            "client_id": "abcdef-1234",
            "client_secret": "supersecretvalue",
            "environment": "sandbox",
        },
    )
    assert resp.status_code == 403
    assert "admin" in resp.text.lower()


def test_admin_can_still_change_credentials(client, fake_pool):
    """The positive direction. A gate tested only by what it REFUSES passes
    just as well when it refuses everyone (gotcha 38's both-directions rule)."""
    fake_pool._state["role"] = "admin"
    resp = client.put(
        "/api/v1/buildium/credentials",
        headers={"Authorization": f"Bearer {_make_token()}"},
        json={
            "client_id": "abcdef-1234",
            "client_secret": "supersecretvalue",
            "environment": "sandbox",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["configured"] is True


def test_admin_can_still_read_the_connection(client, fake_pool):
    """The reads are gated, not broken."""
    fake_pool._state["role"] = "admin"
    resp = client.get(
        "/api/v1/buildium/credentials",
        headers={"Authorization": f"Bearer {_make_token()}"},
    )
    assert resp.status_code == 200
