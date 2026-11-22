"""The DoorLoop connect surface (``api/routes/doorloop.py``).

Every request goes through the real ``create_app``, so a router that is not
mounted fails here with a 404. The pool is a fake that speaks the routes'
statements over ``company_doorloop_credentials``, with the columns, NOT NULLs
and ``base_url`` default read from migration 0109 itself, and answers a SELECT
with only the columns it names. DoorLoop is an ``httpx.MockTransport`` that
records every request. What is pinned:

  - an admin floor on every route, failing closed;
  - the switch gate on the save, the test and Sync now, and its absence on the
    read, the sync status and the disconnect (gotcha 120);
  - the one-PMS trigger's refusal as a 409 naming the other system;
  - the key in no response, 422 or log line; only its last four are stored in
    the clear;
  - ``base_url`` never written; ``last_tested_at`` set only by a passed test
    and cleared by every save;
  - the test sends ONE GET of properties with the bearer key and no query,
    starts the first sync only when it passes, and a wrong key, a key without
    access and a broken answer each say something different;
  - Sync now answers at once and its status names each stale table and why,
    in Propertyware's field names;
  - the disconnect deletes the row, then purges by company alone;
  - the field names and the form's floor, against the TypeScript service.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import httpx
import jwt
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr

from helixis.api.routes import doorloop as routes
from helixis.doorloop import client as dl_client
from helixis.doorloop import sync as dl_sync
from helixis.main import create_app
from helixis.pms import rate, sync_cycle
from helixis.pms.errors import Failure, PmsApiError
from helixis.pms.mirror import SyncResult
from helixis.security import CredentialCipher
from helixis.settings import Settings
from tests.unit._pg_bind_contract import reject_unbindable
from tests.unit._supabase_tokens import TEST_ISSUER, TEST_SUPABASE_URL

REPO = Path(__file__).resolve().parents[2]
TEST_JWT_SECRET = "test-secret-key-for-unit-tests-only-not-production"
TEST_KEY = Fernet.generate_key().decode()
COMPANY = "00000000-0000-4000-8000-0000000000d1"
#: A synthetic key, never a real one. Long enough for the server's floor.
API_KEY = "dl-synthetic-api-key-never-log-me-0001"
SERVICE = REPO / "frontend/src/services/doorloop.ts"
MIGRATION = REPO / "supabase/migrations/0109_doorloop_credentials.sql"

_EVERY_ROUTE = [
    ("GET", "/api/v1/doorloop/credentials"),
    ("PUT", "/api/v1/doorloop/credentials"),
    ("DELETE", "/api/v1/doorloop/credentials"),
    ("POST", "/api/v1/doorloop/test"),
    ("POST", "/api/v1/doorloop/sync"),
    ("GET", "/api/v1/doorloop/sync"),
]

_BODY = {"api_key": API_KEY}


# ── the table, as migration 0109 creates it ───────────────────────────────


def _table() -> tuple[list[str], set[str], str]:
    """The table's columns, its NOT NULL columns, and ``base_url``'s default."""
    sql = MIGRATION.read_text(encoding="utf-8")
    block = sql.split("create table if not exists public.company_doorloop_credentials (", 1)[
        1
    ].split("\n);", 1)[0]
    columns = re.findall(r"^ {4}(\w+)\s+(?:uuid|text|jsonb|timestamptz)\b", block, re.M)
    not_null = {
        name
        for name, rest in re.findall(r"^ {4}(\w+)\s+([^\n]*)", block, re.M)
        if "not null" in rest or "primary key" in rest
    }
    default = re.search(r"base_url\s+text not null\s+default '([^']+)'", block)
    assert columns and default, "migration 0109's table moved: re-point this reader"
    return columns, not_null, default.group(1)


COLUMNS, NOT_NULL, BASE_URL_DEFAULT = _table()


def _norm(sql: str) -> str:
    return " ".join(sql.split())


class RoutePool:
    """``company_doorloop_credentials`` and the role read, as the routes send
    them, refusing what asyncpg and the table would refuse."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.role: str | None = "admin"
        self.insert_raises: BaseException | None = None
        #: ("row", "deleted") and ("purged", table), in the order they ran.
        self.events: list[tuple[str, str]] = []
        self.statements: list[str] = []

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        reject_unbindable(sql, args)
        s = _norm(sql)
        self.statements.append(s)
        if s.startswith("SELECT role FROM public.company_users"):
            return {"role": self.role} if self.role is not None else None
        if "FROM public.company_doorloop_credentials" in s:
            row = self.rows.get(args[0])
            if row is None:
                return None
            selected = [c.strip() for c in s.split("SELECT", 1)[1].split("FROM", 1)[0].split(",")]
            return {c: row[c] for c in selected}
        raise AssertionError(f"a statement this fake does not know: {s}")

    async def execute(self, sql: str, *args: Any) -> str:
        reject_unbindable(sql, args)
        s = _norm(sql)
        self.statements.append(s)
        if s.startswith("INSERT INTO public.company_doorloop_credentials"):
            if self.insert_raises is not None:
                raise self.insert_raises
            self._upsert(s, args)
            return "INSERT 0 1"
        if s.startswith("DELETE FROM public.company_doorloop_credentials"):
            self.events.append(("row", "deleted"))
            return f"DELETE {1 if self.rows.pop(args[0], None) else 0}"
        if s.startswith("UPDATE public.company_doorloop_credentials SET last_tested_at = now()"):
            row = self.rows.get(args[0])
            if row is not None:
                row["last_tested_at"] = datetime.now(UTC)
            return f"UPDATE {1 if row else 0}"
        raise AssertionError(f"a statement this fake does not know: {s}")

    def _upsert(self, s: str, args: tuple[Any, ...]) -> None:
        named = [c.strip() for c in s.split("(", 1)[1].split(")", 1)[0].split(",")]
        tokens = [t.strip() for t in s.split("VALUES (", 1)[1].split(")", 1)[0].split(",")]

        def value(token: str) -> Any:
            if token.upper() == "NULL":
                return None
            return args[int(token.split("::", 1)[0].lstrip("$")) - 1]

        sent = dict(zip(named, (value(t) for t in tokens), strict=True))
        unknown = set(sent) - set(COLUMNS)
        if unknown:
            raise asyncpg.exceptions.UndefinedColumnError(f"no such column: {sorted(unknown)}")
        company = sent["company_id"]
        existing = self.rows.get(company)
        if existing is None:
            now = datetime.now(UTC)
            row = {
                "base_url": BASE_URL_DEFAULT,
                "capabilities": {},
                "last_tested_at": None,
                "created_at": now,
                "updated_at": now,
                **sent,
            }
            missing = sorted(c for c in NOT_NULL if row.get(c) is None)
            if missing:
                raise asyncpg.exceptions.NotNullViolationError(f"null value in {missing}")
            self.rows[company] = row
            return
        for assignment in s.split("DO UPDATE SET", 1)[1].split(","):
            column, expr = (part.strip() for part in assignment.split("=", 1))
            if expr.upper() == "NULL":
                existing[column] = None
            elif expr == "'{}'::jsonb":
                existing[column] = {}
            else:
                existing[column] = sent[expr.removeprefix("EXCLUDED.")]

    @contextlib.asynccontextmanager
    async def acquire(self) -> AsyncIterator[Any]:
        pool = self

        class _Conn:
            async def execute(self, sql: str, *args: Any) -> str:
                reject_unbindable(sql, args)
                assert args == (COMPANY,), "the purge is by company alone"
                table = sql.split("public.", 1)[1].split()[0]
                pool.events.append(("purged", table))
                return "DELETE 0"

        yield _Conn()

    def store(self, **overrides: Any) -> dict[str, Any]:
        """A saved connection, encrypted with the test key."""
        cipher = CredentialCipher(_settings())
        now = datetime.now(UTC)
        row = {
            "company_id": COMPANY,
            "encrypted_api_key": cipher.encrypt(API_KEY),
            "api_key_last4": API_KEY[-4:],
            "base_url": BASE_URL_DEFAULT,
            "capabilities": {"refused": {"x": "y"}},
            "last_tested_at": None,
            "created_at": now,
            "updated_at": now,
            **overrides,
        }
        self.rows[COMPANY] = row
        return row


# ── the app ───────────────────────────────────────────────────────────────


def _settings(*, enabled: bool = True) -> Settings:
    return Settings(
        env="test",
        auth={"jwt_secret": TEST_JWT_SECRET},
        supabase={"url": TEST_SUPABASE_URL},
        data_key=TEST_KEY,
        memory={"enabled": False},
        doorloop={"enabled": enabled},
    )


def _auth() -> dict[str, str]:
    token = jwt.encode(
        {
            "sub": "00000000-0000-4000-8000-0000000000c1",
            "email": "admin@example.com",
            "role": "authenticated",
            "aud": "authenticated",
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
            "iss": TEST_ISSUER,
        },
        TEST_JWT_SECRET,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


@contextlib.asynccontextmanager
async def _noop_lifespan(_app: Any) -> AsyncIterator[None]:
    yield


def _client(
    settings: Settings, pool: RoutePool, companies: list[str] | None = None
) -> Iterator[TestClient]:
    from helixis.api.rate_limit import connectors_limiter

    connectors_limiter.counter._log.clear()
    with (
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.api.routes.doorloop.get_settings", return_value=settings),
        # The switch (`pms.registry.enabled`) and the loader read the settings
        # at call time, through the module.
        patch("helixis.settings.get_settings", return_value=settings),
        patch("helixis.api.routes.doorloop.get_pool", return_value=pool),
        patch(
            "helixis.api.auth._resolve_company_ids",
            new=AsyncMock(return_value=[COMPANY] if companies is None else companies),
        ),
    ):
        app = create_app(settings)
        app.state.memory_manager = MagicMock(available=False)
        app.state.composio_client = MagicMock(available=False)
        app.router.lifespan_context = _noop_lifespan
        with TestClient(app) as c:
            yield c


@pytest.fixture
def pool() -> RoutePool:
    return RoutePool()


@pytest.fixture
def client(pool: RoutePool) -> Iterator[TestClient]:
    yield from _client(_settings(), pool)


@pytest.fixture
def dark_client(pool: RoutePool) -> Iterator[TestClient]:
    """A deployment with the DoorLoop switch off: the shipped default."""
    yield from _client(_settings(enabled=False), pool)


class FakeDoorLoop:
    """Answers every request with ``reply`` and records it."""

    def __init__(self, reply: Callable[[httpx.Request], httpx.Response]) -> None:
        self.reply = reply
        self.requests: list[httpx.Request] = []

    def http(self) -> httpx.AsyncClient:
        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return self.reply(request)

        return httpx.AsyncClient(transport=httpx.MockTransport(handle))


def _answer(status: int, body: Any = None, *, text: str | None = None) -> FakeDoorLoop:
    def reply(_request: httpx.Request) -> httpx.Response:
        if text is not None:
            return httpx.Response(status, text=text)
        return httpx.Response(status, json=body)

    return FakeDoorLoop(reply)


@contextlib.contextmanager
def _vendor(server: FakeDoorLoop) -> Iterator[FakeDoorLoop]:
    """Every client the route builds talks to ``server``, never the network."""
    real = dl_client.build_client

    def build(creds: Any, *, http: Any = None) -> Any:
        return real(creds, http=server.http())

    with patch.object(dl_client, "build_client", build):
        yield server


def _put(c: TestClient, body: Any = None) -> httpx.Response:
    return c.put(
        "/api/v1/doorloop/credentials", headers=_auth(), json=_BODY if body is None else body
    )


def _get(c: TestClient) -> dict[str, Any]:
    resp = c.get("/api/v1/doorloop/credentials", headers=_auth())
    assert resp.status_code == 200, resp.text
    return resp.json()


def _test(c: TestClient) -> dict[str, Any]:
    resp = c.post("/api/v1/doorloop/test", headers=_auth())
    assert resp.status_code == 200, resp.text
    return resp.json()


# ── the field names: one contract, two readers ────────────────────────────


def test_the_field_names_are_the_contract() -> None:
    """The onboarding wizard will be built against these names (ADR 0003)."""
    fields = routes.DoorLoopCredentialsInput.model_fields
    assert list(fields) == ["api_key"]
    assert fields["api_key"].annotation is SecretStr
    assert set(routes.DoorLoopCredentialsStatus.model_fields) == {
        "configured",
        "api_key_last4",
        "last_tested_at",
        "purged_rows",
        "connector_enabled",
    }
    assert set(routes.DoorLoopTestResult.model_fields) == {"ok", "properties_count", "error"}
    assert set(routes.DoorLoopSyncResult.model_fields) == {
        "ok",
        "rows",
        "stale",
        "error",
        "running",
        "started_at",
    }


def _ts_fields(interface: str) -> set[str]:
    text = SERVICE.read_text(encoding="utf-8")
    block = re.search(rf"export interface {interface} \{{(.*?)\n\}}", text, re.S)
    assert block, f"doorloop.ts no longer declares {interface}"
    return set(re.findall(r"^  (\w+)\??:", block.group(1), re.M))


@pytest.mark.parametrize(
    ("interface", "model"),
    [
        ("DoorLoopCredentialsInput", routes.DoorLoopCredentialsInput),
        ("DoorLoopStatus", routes.DoorLoopCredentialsStatus),
        ("DoorLoopTestResult", routes.DoorLoopTestResult),
        ("DoorLoopSyncResult", routes.DoorLoopSyncResult),
    ],
)
def test_the_page_and_the_route_agree_on_every_field(interface: str, model: Any) -> None:
    """Both sides (rule 15). How the service sends them is pinned in
    ``frontend/src/services/doorloop.test.ts``."""
    assert _ts_fields(interface) == set(model.model_fields)


def _ts_const(name: str) -> str:
    found = re.search(rf"export const {name} =\s*(.*?);", SERVICE.read_text(encoding="utf-8"), re.S)
    assert found, f"doorloop.ts no longer declares {name}"
    return found.group(1)


def test_the_form_waits_for_exactly_what_the_server_accepts() -> None:
    """A form floor above the server's would refuse keys the server takes."""
    assert int(_ts_const("MIN_API_KEY_LENGTH")) == routes._API_KEY_MIN


def test_the_panel_and_the_server_say_the_same_about_where_the_key_is() -> None:
    """The panel shows where to make a key and that it needs Premium; the
    server's refusals say it too. Two copies that drift tell a customer two
    different places."""
    assert _ts_const("DOORLOOP_KEY_WHERE").strip().strip('"') == routes._WHERE_THE_KEY_IS


# ── the role floor ────────────────────────────────────────────────────────


def test_the_role_floor_covers_every_route() -> None:
    served = {
        (method, "/api/v1" + route.path)
        for route in routes.router.routes
        for method in getattr(route, "methods", set())
    }
    assert served == set(_EVERY_ROUTE)


@pytest.mark.parametrize("role", ["member", None])
@pytest.mark.parametrize(("method", "path"), _EVERY_ROUTE)
def test_every_route_needs_an_admin_and_fails_closed(
    client: TestClient, pool: RoutePool, role: str | None, method: str, path: str
) -> None:
    saved = dict(pool.store())
    pool.role = role
    server = _answer(200, {"data": []})
    with _vendor(server):
        resp = client.request(
            method, path, headers=_auth(), json=_BODY if method == "PUT" else None
        )
    assert resp.status_code == 403
    assert "Only an admin can" in resp.json()["detail"]
    assert pool.rows == {COMPANY: saved}
    assert pool.events == [] and server.requests == []


def test_an_account_with_no_company_is_refused_with_somewhere_to_go(pool: RoutePool) -> None:
    for c in _client(_settings(), pool, companies=[]):
        resp = c.get("/api/v1/doorloop/credentials", headers=_auth())
    assert resp.status_code == 403
    assert "team@occupella.com" in resp.json()["detail"]


# ── the switch: the save and the test refuse; the rest do not ─────────────


def test_the_save_refuses_while_switched_off_and_stores_nothing(
    dark_client: TestClient, pool: RoutePool
) -> None:
    resp = _put(dark_client)
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert "switched off" in detail and "team@occupella.com" in detail
    # A stored row would block every other system's connection through the
    # one-PMS trigger and sync nothing.
    assert pool.rows == {}


def test_the_test_refuses_while_switched_off_and_calls_nothing(
    dark_client: TestClient, pool: RoutePool
) -> None:
    pool.store()
    with patch.object(dl_client, "build_client") as build:
        resp = dark_client.post("/api/v1/doorloop/test", headers=_auth())
    assert resp.status_code == 503
    build.assert_not_called()
    assert pool.rows[COMPANY]["last_tested_at"] is None


def test_sync_now_refuses_while_switched_off_and_starts_nothing(
    dark_client: TestClient, pool: RoutePool
) -> None:
    pool.store()
    with patch.object(sync_cycle, "start_now") as start:
        resp = dark_client.post("/api/v1/doorloop/sync", headers=_auth())
    assert resp.status_code == 503
    assert "switched off" in resp.json()["detail"]
    start.assert_not_called()


def test_the_read_the_status_and_the_disconnect_answer_while_switched_off(
    dark_client: TestClient, pool: RoutePool
) -> None:
    """A connection saved before the switch went off must still be seen and
    removed: while the row exists, no other system can be connected.

    Re-pointed from "the read and the disconnect": the sync status, added
    with the sync (slice A1), reads and starts nothing, so it answers too."""
    pool.store()
    body = _get(dark_client)
    assert body["configured"] is True and body["connector_enabled"] is False
    assert dark_client.get("/api/v1/doorloop/sync", headers=_auth()).status_code == 200
    resp = dark_client.delete("/api/v1/doorloop/credentials", headers=_auth())
    assert resp.status_code == 200
    assert pool.rows == {}


# ── the save ──────────────────────────────────────────────────────────────


def test_the_save_stores_ciphertext_and_the_last_four_only(
    client: TestClient, pool: RoutePool
) -> None:
    resp = _put(client, {"api_key": f"  {API_KEY}  "})
    assert resp.status_code == 200, resp.text
    row = pool.rows[COMPANY]
    assert row["encrypted_api_key"] != API_KEY
    assert CredentialCipher(_settings()).decrypt(row["encrypted_api_key"]) == API_KEY
    assert row["api_key_last4"] == API_KEY[-4:]
    assert row["last_tested_at"] is None
    # ⚠ Never written: the client's host is a constant.
    assert row["base_url"] == BASE_URL_DEFAULT
    assert not any("base_url" in s for s in pool.statements)
    assert API_KEY not in resp.text
    assert resp.json() == {
        "configured": True,
        "api_key_last4": API_KEY[-4:],
        "last_tested_at": None,
        "purged_rows": None,
        "connector_enabled": None,
    }


def test_a_new_key_clears_the_last_test_and_the_remembered_refusals(
    client: TestClient, pool: RoutePool
) -> None:
    pool.store(last_tested_at=datetime.now(UTC))
    resp = _put(client, {"api_key": API_KEY.replace("0001", "0002")})
    assert resp.status_code == 200, resp.text
    row = pool.rows[COMPANY]
    assert row["last_tested_at"] is None
    assert row["capabilities"] == {}
    assert row["api_key_last4"] == "0002"


@pytest.mark.parametrize(
    "sent",
    [
        {"api_key": ""},
        {"api_key": "short-key"},
        {"api_key": API_KEY + "\r\nX-Injected: 1"},
        {"api_key": "x" * 5000},
        {"api_key": 12345678901234567890},
        {},
        ["not", "an", "object"],
    ],
)
def test_an_unusable_key_is_a_422_that_repeats_nothing(
    client: TestClient, pool: RoutePool, sent: Any
) -> None:
    resp = client.put("/api/v1/doorloop/credentials", headers=_auth(), json=sent)
    assert resp.status_code == 422
    assert resp.json()["detail"] == routes._BAD_API_KEY
    assert "Zapier & API Keys" in resp.json()["detail"]
    assert API_KEY not in resp.text and "X-Injected" not in resp.text
    assert pool.rows == {}


def test_a_workspace_on_another_system_gets_a_409_naming_it(
    client: TestClient, pool: RoutePool
) -> None:
    pool.insert_raises = asyncpg.exceptions.UniqueViolationError(
        f"company {COMPANY} is already connected to Propertyware; one PMS per company "
        "(ADR 0010 a1). Disconnect the existing PMS first."
    )
    resp = _put(client)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "already connected to Propertyware" in detail
    assert detail.endswith("then connect DoorLoop.")
    assert pool.rows == {}


# ── the test ──────────────────────────────────────────────────────────────


def test_a_passed_test_reads_one_page_with_the_bearer_key_and_starts_the_first_sync(
    client: TestClient, pool: RoutePool
) -> None:
    """Re-pointed from "...and starts no sync": since slice A1 a passed test
    starts the first sync, as Propertyware's does, so the copy is not empty
    until the worker's next tick."""
    pool.store()
    server = _answer(200, {"data": [{"id": "p1"}], "total": 7})
    with _vendor(server), patch.object(sync_cycle, "start_now") as start:
        body = _test(client)
    assert body == {"ok": True, "properties_count": 7, "error": None}
    (request,) = server.requests
    assert request.method == "GET"
    assert str(request.url) == "https://app.doorloop.com/api/properties"
    assert request.headers["Authorization"] == f"bearer {API_KEY}"
    start.assert_called_once_with(pool, "doorloop", COMPANY)
    assert pool.rows[COMPANY]["last_tested_at"] is not None


@pytest.mark.parametrize("status", [401, 405, 500])
def test_a_failed_test_starts_no_sync(client: TestClient, pool: RoutePool, status: int) -> None:
    pool.store()
    with (
        _vendor(_answer(status, {"message": "no"})),
        patch.object(sync_cycle, "start_now") as start,
        patch("helixis.pms.http._sleep", new=AsyncMock()),
    ):
        assert _test(client)["ok"] is False
    start.assert_not_called()
    assert pool.rows[COMPANY]["last_tested_at"] is None


def test_a_passed_test_stays_passed_when_the_first_sync_cannot_start(
    client: TestClient, pool: RoutePool
) -> None:
    pool.store()
    with (
        _vendor(_answer(200, {"data": [], "total": 0})),
        patch.object(sync_cycle, "start_now", side_effect=RuntimeError("synthetic")),
    ):
        body = _test(client)
    assert body == {"ok": True, "properties_count": 0, "error": None}
    assert pool.rows[COMPANY]["last_tested_at"] is not None


@pytest.mark.parametrize("body", [{"data": []}, [], {"total": "7"}, {"total": -1}, {"total": True}])
def test_a_count_doorloop_did_not_state_is_none_never_zero(
    client: TestClient, pool: RoutePool, body: Any
) -> None:
    pool.store()
    with _vendor(_answer(200, body)):
        result = _test(client)
    assert result["ok"] is True
    assert result["properties_count"] is None


def test_a_200_that_is_not_json_rows_proves_nothing(client: TestClient, pool: RoutePool) -> None:
    pool.store()
    with _vendor(_answer(200, text="")):
        result = _test(client)
    assert result == {"ok": False, "properties_count": None, "error": routes._TEST_CRASHED}
    assert pool.rows[COMPANY]["last_tested_at"] is None


def test_a_wrong_key_and_a_key_without_access_say_different_things(
    client: TestClient, pool: RoutePool
) -> None:
    """A 401 is the key; DoorLoop's 405 is the key's access role; its 403 is
    the edge limiting this IP (the Rate Limiting page). Telling a customer with
    a role problem to paste the key again sends them in a circle (gotcha 102),
    and so does telling one who was throttled to fix the role."""
    pool.store()
    said: dict[int, str] = {}
    for status in (401, 403, 405):
        with _vendor(_answer(status, {"message": "no"})):
            result = _test(client)
        assert result["ok"] is False
        said[status] = result["error"]
        rate._holds.clear()  # the 403's hold must not answer the next status
    assert said[401] == routes._TEST_ADVICE[routes.Failure.CREDENTIALS]
    assert said[405] == routes._TEST_ADVICE[routes.Failure.PERMISSION]
    assert said[403] == routes._TEST_HELD
    assert "role" not in said[403] and "reconnect" not in said[403].lower()
    assert said[401] != said[405]
    assert "access role" in said[405] and "reconnect" not in said[405].lower()
    assert "Premium" in said[401]
    assert pool.rows[COMPANY]["last_tested_at"] is None


def test_the_key_never_reaches_the_answer_or_the_log(
    client: TestClient, pool: RoutePool, caplog: pytest.LogCaptureFixture
) -> None:
    """DoorLoop echoing the key in an error body is masked before it is logged."""
    pool.store()
    with caplog.at_level(logging.DEBUG), _vendor(_answer(400, {"message": f"bad token {API_KEY}"})):
        result = _test(client)
    assert result["error"] == routes._TEST_ADVICE[routes.Failure.BAD_REQUEST]
    assert API_KEY not in caplog.text
    assert API_KEY not in str(result)


def test_no_connection_and_an_unusable_row_are_told_apart(
    client: TestClient, pool: RoutePool
) -> None:
    assert _test(client) == {
        "ok": False,
        "properties_count": None,
        "error": routes._NO_CONNECTION,
    }
    pool.store(encrypted_api_key="not-a-fernet-token")
    assert _test(client)["error"] == routes._UNUSABLE_ROW


# ── the disconnect ────────────────────────────────────────────────────────


def test_the_disconnect_deletes_the_row_then_purges_by_company(
    client: TestClient, pool: RoutePool
) -> None:
    pool.store()
    resp = client.delete("/api/v1/doorloop/credentials", headers=_auth())
    assert resp.status_code == 200
    assert resp.json()["configured"] is False
    assert resp.json()["purged_rows"] == 0
    assert pool.rows == {}
    # The row goes first, then every purged table, each by company alone.
    assert pool.events[0] == ("row", "deleted")
    assert len(pool.events) > 1 and all(kind == "purged" for kind, _ in pool.events[1:])


def test_every_table_the_doorloop_pass_writes_is_purged() -> None:
    """The purge is Buildium's list (one mirror). A table the sync writes and
    the list does not hold would survive every disconnect (rule 15)."""
    from helixis.buildium.purge import PURGE_TABLES

    written = set(dl_sync.SYNC_STATE_STAGE) | set(dl_sync.DERIVED_TABLES)
    assert written and written <= set(PURGE_TABLES)
    assert "helixis_mirror_sync_state" in PURGE_TABLES  # the freshness stamps


def test_no_surface_calls_the_disconnect_a_revocation() -> None:
    import inspect

    source = inspect.getsource(routes)
    assert "does NOT revoke" in source
    assert not re.search(r"(?<!not )revoke[sd]? the key", source, re.I)


# ── the sync: Sync now and its status ─────────────────────────────────────


def _ended_run(timeout: float = 5.0) -> sync_cycle.SyncRun:
    """Wait (outside the app) until the company's run has ended."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = sync_cycle.sync_status("doorloop", COMPANY)
        if run is not None and run.finished_at is not None:
            return run
        time.sleep(0.01)
    raise AssertionError("the sync never ended")


def _status(c: TestClient) -> dict[str, Any]:
    resp = c.get("/api/v1/doorloop/sync", headers=_auth())
    assert resp.status_code == 200, resp.text
    return resp.json()


def _gated(result: SyncResult | None) -> tuple[Any, threading.Event]:
    """A ``sync_one`` that waits for the test's go, then answers ``result``."""
    go = threading.Event()

    async def one(_pool: Any, pms: str, company_id: str) -> SyncResult | None:
        assert (pms, company_id) == ("doorloop", COMPANY)
        await asyncio.to_thread(go.wait, 5)
        return result

    return one, go


def test_sync_answers_at_once_and_the_status_reports_the_rows_by_table(
    client: TestClient, pool: RoutePool
) -> None:
    pool.store()
    result = SyncResult(
        company_id=COMPANY,
        pms="doorloop",
        rows_by_table={"helixis_units": 4, "helixis_leases": 3, "helixis_lease_tenants": 5},
    )
    one, go = _gated(result)
    with patch.object(sync_cycle, "sync_one", one):
        start = client.post("/api/v1/doorloop/sync", headers=_auth()).json()
        assert start["ok"] is True and start["running"] is True and start["started_at"]
        during = _status(client)
        assert during["running"] is True and during["started_at"] == start["started_at"]
        assert during["rows"] == {}
        go.set()
        _ended_run()
        after = _status(client)
    # Keyed by table: leases and their tenant links share one freshness stage.
    assert after == {
        "ok": True,
        "rows": {"units": 4, "leases": 3, "lease_tenants": 5},
        "stale": [],
        "error": None,
        "running": False,
        "started_at": start["started_at"],
    }


def test_the_status_names_each_stale_table_and_why_and_counts_none_of_them(
    client: TestClient, pool: RoutePool
) -> None:
    pool.store()
    refused = dl_sync.read_failure(PmsApiError(405, Failure.PERMISSION, False, "synthetic"))
    result = SyncResult(
        company_id=COMPANY,
        pms="doorloop",
        rows_by_table={"helixis_properties": 5, "helixis_units": 2},
        failed={
            "helixis_vendors": refused,
            "helixis_units": "incomplete read: written, not stamped, not pruned",
        },
    )
    one, go = _gated(result)
    go.set()
    with patch.object(sync_cycle, "sync_one", one):
        client.post("/api/v1/doorloop/sync", headers=_auth())
        _ended_run()
        after = _status(client)
    assert after["ok"] is False and after["stale"] == ["units", "vendors"]
    # A stale table is never counted: 2 units of a part-read would read as the account's.
    assert after["rows"] == {"properties": 5}
    error = after["error"]
    assert "last sync that worked" in error
    assert "Units: DoorLoop stopped sending them part-way through." in error
    assert f"Vendors: {refused}" in error and "access role" in error
    # The operator's wording stays in the log.
    assert "incomplete read" not in error and "405" not in error


def test_one_cause_is_said_once_after_every_table_it_stopped() -> None:
    reason = dl_sync.CALL_BUDGET_REACHED
    sentence = routes._stale_sentence(
        {t: reason for t in ("helixis_lease_transactions", "helixis_owners", "helixis_work_orders")}
    )
    assert sentence.count(reason) == 1
    assert f"Charges, payments and credits, owners and work orders: {reason}" in sentence


def test_a_click_during_a_sync_waits_on_that_sync(client: TestClient, pool: RoutePool) -> None:
    """The run a passed test started, or a first click, is joined: same start,
    and no second pass over the same key's hourly quota."""
    pool.store()
    run, started = sync_cycle.claim_or_join("doorloop", COMPANY)
    assert started
    with patch.object(sync_cycle, "sync_one") as one:
        body = client.post("/api/v1/doorloop/sync", headers=_auth()).json()
    assert body["running"] is True and body["started_at"] == run.started_at.isoformat()
    one.assert_not_called()
    run.result = SyncResult(company_id=COMPANY, pms="doorloop", rows_by_table={"helixis_units": 1})
    sync_cycle.release(COMPANY, run)
    assert _status(client)["rows"] == {"units": 1}


def test_a_sync_that_did_not_finish_names_what_to_do(client: TestClient, pool: RoutePool) -> None:
    pool.store()
    one, go = _gated(None)
    go.set()
    with patch.object(sync_cycle, "sync_one", one):
        client.post("/api/v1/doorloop/sync", headers=_auth())
        _ended_run()
        after = _status(client)
    assert after["ok"] is False and after["running"] is False
    assert after["error"] == routes._SYNC_DID_NOT_FINISH and "team@occupella.com" in after["error"]


def test_a_sync_that_cannot_start_reports_an_end_not_a_run(
    client: TestClient, pool: RoutePool
) -> None:
    """Left 'running', the page would wait on a run that never ends."""
    pool.store()

    def refuse(coro: Any) -> Any:
        raise RuntimeError("no running event loop")

    with patch.object(sync_cycle, "asyncio", SimpleNamespace(create_task=refuse)):
        body = client.post("/api/v1/doorloop/sync", headers=_auth()).json()
    assert body["ok"] is False and body["running"] is False
    assert body["error"] == routes._SYNC_DID_NOT_FINISH
    assert _status(client)["running"] is False


def test_sync_without_a_saved_connection_does_not_run(client: TestClient, pool: RoutePool) -> None:
    with patch.object(sync_cycle, "start_now") as start:
        body = client.post("/api/v1/doorloop/sync", headers=_auth()).json()
    assert body["ok"] is False and body["error"] == routes._NO_CONNECTION
    start.assert_not_called()


def test_the_status_before_any_sync_says_how_to_run_one(client: TestClient) -> None:
    body = _status(client)
    assert body["ok"] is False and body["running"] is False and body["started_at"] is None
    assert "Click Sync now" in body["error"]


def test_every_stale_reason_a_pass_can_give_reads_whole_in_the_status() -> None:
    """Both sides (rule 15): the pass's own reasons and the writer's, through
    the sentence the page shows. None is lost, and none is operator text."""
    reasons = [
        dl_sync.CALL_BUDGET_REACHED,
        dl_sync._LEDGER_PART_WAY,
        "incomplete read: written, not stamped, not pruned",
        "write failed: UniqueViolationError",
    ]
    tables = list(dl_sync.TABLE_WORDS)[: len(reasons)]
    sentence = routes._stale_sentence(dict(zip(tables, reasons, strict=True)))
    for reason in reasons:
        assert dl_sync.stale_reason(reason) in sentence
    assert "incomplete read" not in sentence and "UniqueViolationError" not in sentence
