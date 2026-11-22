"""The Propertyware connect surface (``api/routes/propertyware.py``).

Every request goes through the real ``create_app``, so a router that is not
mounted fails every test here (a 404), and the dark-deployment tests' 503
proves it is mounted whatever the switch says. The pool is a fake that speaks
the routes' statements over ``company_propertyware_credentials`` with the
columns, NOT NULLs and ``base_url`` default read from migration 0107 itself,
and answers a SELECT with only the columns it names. Propertyware is the
synthetic fake from slice 1 (``_propertyware_fake``). What is pinned:

  - an admin floor on every route, failing closed;
  - the switch gate on the save, the test and Sync now, and its absence on
    the read, the disconnect and the sync status (gotcha 120);
  - the one-PMS trigger's refusal as a 409 naming the other system;
  - no secret in any response, 422 or log line, and no last4 of the secret;
  - ``base_url`` never written; ``last_tested_at`` set only by a passed test
    and cleared by every save;
  - the test reads ONE building, reports Propertyware's own count, and starts
    the first sync only when it passes; a 401 and a 403 say different things;
  - the disconnect deletes the row, then purges by company alone;
  - the sync status in Rentvine's field names, stale tables named with why;
  - the field names, against the TypeScript service (rule 15).
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import inspect
import logging
import re
import threading
import time
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import httpx
import jwt
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from helixis.api.routes import propertyware as routes
from helixis.main import create_app
from helixis.pms import sync_cycle
from helixis.pms.errors import Failure, PmsApiError
from helixis.pms.mirror import SyncResult
from helixis.propertyware import _paths
from helixis.propertyware import client as pw_client
from helixis.propertyware import sync as pw_sync
from helixis.security import CredentialCipher
from helixis.settings import Settings
from tests.unit._pg_bind_contract import reject_unbindable
from tests.unit._propertyware_fake import (
    CLIENT_ID,
    CLIENT_SECRET,
    COMPANY,
    SYSTEM_ID,
    TEST_KEY,
    TODAY,
    FakePool,
    FakePropertyware,
    creds_row,
    fixture_rows,
    inserted_rows,
)
from tests.unit._propertyware_fake import settings as fake_settings
from tests.unit._supabase_tokens import TEST_ISSUER, TEST_SUPABASE_URL

REPO = Path(__file__).resolve().parents[2]
PW = "propertyware"
TEST_JWT_SECRET = "test-secret-key-for-unit-tests-only-not-production"
SERVICE = REPO / "frontend/src/services/propertyware.ts"
MIGRATION = REPO / "supabase/migrations/0107_rentmanager_propertyware_credentials.sql"

_EVERY_ROUTE = [
    ("GET", "/api/v1/propertyware/credentials"),
    ("PUT", "/api/v1/propertyware/credentials"),
    ("DELETE", "/api/v1/propertyware/credentials"),
    ("POST", "/api/v1/propertyware/test"),
    ("POST", "/api/v1/propertyware/sync"),
    ("GET", "/api/v1/propertyware/sync"),
]

_BODY = {"system_id": SYSTEM_ID, "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}


# ── the table, as migration 0107 creates it ───────────────────────────────


def _table() -> tuple[list[str], set[str], str]:
    """The table's columns, its NOT NULL columns, and ``base_url``'s default."""
    sql = MIGRATION.read_text(encoding="utf-8")
    block = sql.split("create table if not exists public.company_propertyware_credentials (", 1)[
        1
    ].split("\n);", 1)[0]
    columns = re.findall(r"^ {4}(\w+)\s+(?:uuid|text|jsonb|timestamptz)\b", block, re.M)
    not_null = {
        name
        for name, rest in re.findall(r"^ {4}(\w+)\s+([^\n]*)", block, re.M)
        if "not null" in rest or "primary key" in rest
    }
    default = re.search(r"base_url\s+text not null\s+default '([^']+)'", block)
    assert columns and default, "migration 0107's table moved: re-point this reader"
    return columns, not_null, default.group(1)


COLUMNS, NOT_NULL, BASE_URL_DEFAULT = _table()


def _norm(sql: str) -> str:
    return " ".join(sql.split())


class RoutePool:
    """``company_propertyware_credentials`` and the role read, as the routes
    send them, refusing what asyncpg and the table would refuse."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.role: str | None = "admin"
        self.insert_raises: BaseException | None = None
        self.purge_deletes: dict[str, int] = {}
        #: ("row", "deleted") and ("purged", table), in the order they ran.
        self.events: list[tuple[str, str]] = []
        self.statements: list[str] = []
        #: ``pms.watermarks`` rows Sync now stored: (company, resource, when).
        self.asked: list[tuple[Any, ...]] = []

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        reject_unbindable(sql, args)
        s = _norm(sql)
        self.statements.append(s)
        if s.startswith("SELECT role FROM public.company_users"):
            return {"role": self.role} if self.role is not None else None
        if "FROM public.company_propertyware_credentials" in s:
            row = self.rows.get(args[0])
            if row is None:
                return None
            selected = [c.strip() for c in s.split("SELECT", 1)[1].split("FROM", 1)[0].split(",")]
            # Only what was selected, like asyncpg: a column the status reads
            # and the SELECT left out is a KeyError here as in production.
            return {c: row[c] for c in selected}
        raise AssertionError(f"a statement this fake does not know: {s}")

    async def execute(self, sql: str, *args: Any) -> str:
        reject_unbindable(sql, args)
        s = _norm(sql)
        self.statements.append(s)
        if s.startswith("INSERT INTO public.company_propertyware_credentials"):
            if self.insert_raises is not None:
                raise self.insert_raises
            self._upsert(s, args)
            return "INSERT 0 1"
        if s.startswith("DELETE FROM public.company_propertyware_credentials"):
            self.events.append(("row", "deleted"))
            return f"DELETE {1 if self.rows.pop(args[0], None) else 0}"
        if s.startswith("INSERT INTO helixis_pms_sync_watermarks"):
            self.asked.append(args)
            return "INSERT 0 1"
        if s.startswith(
            "UPDATE public.company_propertyware_credentials SET last_tested_at = now()"
        ):
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
                return f"DELETE {pool.purge_deletes.get(table, 0)}"

        yield _Conn()

    def store(self, **overrides: Any) -> dict[str, Any]:
        """A saved connection, encrypted with the test key."""
        cipher = CredentialCipher(_settings())
        now = datetime.now(UTC)
        row = {
            "company_id": COMPANY,
            "system_id": SYSTEM_ID,
            "encrypted_client_id": cipher.encrypt(CLIENT_ID),
            "client_id_last4": CLIENT_ID[-4:],
            "encrypted_client_secret": cipher.encrypt(CLIENT_SECRET),
            "base_url": BASE_URL_DEFAULT,
            "capabilities": {},
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
        propertyware={"enabled": enabled},
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
        patch("helixis.api.routes.propertyware.get_settings", return_value=settings),
        # The switch (`pms.registry.enabled`) and the credentials loader read
        # the settings at call time, through the module.
        patch("helixis.settings.get_settings", return_value=settings),
        patch("helixis.api.routes.propertyware.get_pool", return_value=pool),
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
    """A deployment with the Propertyware switch off: the shipped default."""
    yield from _client(_settings(enabled=False), pool)


def _put(c: TestClient, body: Any = None) -> httpx.Response:
    return c.put(
        "/api/v1/propertyware/credentials", headers=_auth(), json=_BODY if body is None else body
    )


def _get(c: TestClient) -> dict[str, Any]:
    resp = c.get("/api/v1/propertyware/credentials", headers=_auth())
    assert resp.status_code == 200, resp.text
    return resp.json()


@contextlib.contextmanager
def _vendor(server: Any) -> Iterator[Any]:
    """Every client the route builds talks to ``server`` (a fake Propertyware,
    or an ``httpx.AsyncClient`` over a mock transport), never the network."""
    real = pw_client.build_client

    def build(creds: Any, *, http: Any = None) -> Any:
        return real(creds, http=server if isinstance(server, httpx.AsyncClient) else server.http())

    with patch.object(pw_client, "build_client", build):
        yield server


def _test(c: TestClient) -> dict[str, Any]:
    resp = c.post("/api/v1/propertyware/test", headers=_auth())
    assert resp.status_code == 200, resp.text
    return resp.json()


# ── the field names: one contract, three readers ──────────────────────────


def test_the_field_names_are_the_contract_the_wizard_is_built_on() -> None:
    """The onboarding wizard (helixis-onboarding) is built against these names
    (ADR 0003): a rename here breaks it with nothing in this repo failing."""
    from helixis.api.routes.rentvine import RentvineSyncResult

    fields = routes.PropertywareCredentialsInput.model_fields
    assert list(fields) == ["system_id", "client_id", "client_secret"]
    assert fields["system_id"].annotation is str
    assert fields["client_id"].annotation is SecretStr
    assert fields["client_secret"].annotation is SecretStr
    assert set(routes.PropertywareCredentialsStatus.model_fields) == {
        "configured",
        "system_id",
        "client_id_last4",
        "last_tested_at",
        "purged_rows",
        "connector_enabled",
    }
    assert set(routes.PropertywareTestResult.model_fields) == {"ok", "properties_count", "error"}
    assert set(routes.PropertywareSyncResult.model_fields) == set(RentvineSyncResult.model_fields)


def _ts_fields(interface: str) -> set[str]:
    text = SERVICE.read_text(encoding="utf-8")
    block = re.search(rf"export interface {interface} \{{(.*?)\n\}}", text, re.S)
    assert block, f"propertyware.ts no longer declares {interface}"
    return set(re.findall(r"^  (\w+)\??:", block.group(1), re.M))


@pytest.mark.parametrize(
    ("interface", "model"),
    [
        ("PropertywareCredentialsInput", routes.PropertywareCredentialsInput),
        ("PropertywareStatus", routes.PropertywareCredentialsStatus),
        ("PropertywareTestResult", routes.PropertywareTestResult),
        ("PropertywareSyncResult", routes.PropertywareSyncResult),
    ],
)
def test_the_page_and_the_route_agree_on_every_field(interface: str, model: Any) -> None:
    """Both sides (rule 15). How the service sends and reads them is pinned
    in ``frontend/src/services/propertyware.test.ts``."""
    assert _ts_fields(interface) == set(model.model_fields)


def _ts_const(name: str) -> str:
    found = re.search(rf"export const {name} = (.*?);", SERVICE.read_text(encoding="utf-8"), re.S)
    assert found, f"propertyware.ts no longer declares {name}"
    return found.group(1)


def test_the_page_polls_inside_the_connectors_rate_limit() -> None:
    """The poll shares ``connectors_limiter`` with every connector call the
    person makes; faster than half its budget, a long sync turns into 429s
    the page counts as failed checks and gives up on."""
    from helixis.api.rate_limit import connectors_limiter

    poll_ms = int(_ts_const("SYNC_POLL_MS").replace("_", ""))
    budget = connectors_limiter.counter.requests * 60 / connectors_limiter.counter.window
    assert 60_000 / poll_ms <= budget / 2


def test_the_form_waits_for_exactly_what_the_server_accepts() -> None:
    """A form floor above the server's would refuse keys the server takes."""
    assert int(_ts_const("MIN_CLIENT_ID_LENGTH")) == routes._CLIENT_ID_MIN
    assert int(_ts_const("MIN_CLIENT_SECRET_LENGTH")) == routes._CLIENT_SECRET_MIN


def test_the_panel_asks_for_the_permission_of_every_read_and_no_other() -> None:
    """The panel tells the customer which permissions to give the key. One
    missing is a table that goes stale on every sync; one extra is access
    the product never uses (the design's least-privilege key)."""
    listed = re.findall(r"'([^']+)'", _ts_const("PROPERTYWARE_READ_PERMISSIONS"))
    needed = list(dict.fromkeys(_paths.PERMISSIONS[f"GET {p}"] for p in pw_client.READS))
    assert listed == needed


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
    """Reads included, and an unverifiable role refused. A member replacing or
    deleting the one shared row breaks the connector for everyone, and the
    disconnect also deletes the mirror."""
    saved = dict(pool.store())
    pool.role = role
    server = FakePropertyware()
    with _vendor(server), patch.object(sync_cycle, "start_now") as start:
        resp = client.request(
            method, path, headers=_auth(), json=_BODY if method == "PUT" else None
        )
    assert resp.status_code == 403
    assert "Only an admin can" in resp.json()["detail"]
    assert pool.rows == {COMPANY: saved}
    assert pool.events == [] and server.requests == []
    start.assert_not_called()


def test_an_account_with_no_company_is_refused_with_somewhere_to_go(pool: RoutePool) -> None:
    for c in _client(_settings(), pool, companies=[]):
        resp = c.get("/api/v1/propertyware/credentials", headers=_auth())
    assert resp.status_code == 403
    assert "team@occupella.com" in resp.json()["detail"]


# ── the switch: the write, the test and Sync now refuse; the rest do not ──


def test_the_save_refuses_while_switched_off_and_stores_nothing(
    dark_client: TestClient, pool: RoutePool
) -> None:
    resp = _put(dark_client)
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert "switched off" in detail and "team@occupella.com" in detail
    # ⚠ The half that matters: a stored row would block every other system's
    # connection through the one-PMS trigger and sync nothing.
    assert pool.rows == {}


def test_the_test_refuses_while_switched_off_and_calls_nothing(
    dark_client: TestClient, pool: RoutePool
) -> None:
    pool.store()
    with patch.object(pw_client, "build_client") as build:
        resp = dark_client.post("/api/v1/propertyware/test", headers=_auth())
    assert resp.status_code == 503
    build.assert_not_called()
    assert pool.rows[COMPANY]["last_tested_at"] is None


def test_sync_now_refuses_while_switched_off_and_starts_nothing(
    dark_client: TestClient, pool: RoutePool
) -> None:
    pool.store()
    with patch.object(sync_cycle, "start_now") as start:
        resp = dark_client.post("/api/v1/propertyware/sync", headers=_auth())
    assert resp.status_code == 503
    start.assert_not_called()


def test_the_read_the_status_and_the_disconnect_answer_while_switched_off(
    dark_client: TestClient, pool: RoutePool
) -> None:
    """A connection saved before the switch went off must still be seen and
    removed: while the row exists, no other system can be connected."""
    pool.store()
    body = _get(dark_client)
    assert body["configured"] is True and body["connector_enabled"] is False
    assert dark_client.get("/api/v1/propertyware/sync", headers=_auth()).status_code == 200
    resp = dark_client.request("DELETE", "/api/v1/propertyware/credentials", headers=_auth())
    assert resp.status_code == 200 and resp.json()["configured"] is False
    assert pool.rows == {}
    assert ("purged", "helixis_properties") in pool.events


def test_the_read_says_the_connector_is_on(client: TestClient) -> None:
    body = _get(client)
    assert body == {
        "configured": False,
        "system_id": None,
        "client_id_last4": None,
        "last_tested_at": None,
        "purged_rows": None,
        "connector_enabled": True,
    }


# ── the save ──────────────────────────────────────────────────────────────


def test_the_save_stores_ciphertext_and_the_read_returns_no_secret(
    client: TestClient, pool: RoutePool
) -> None:
    resp = _put(client)
    assert resp.status_code == 200, resp.text

    stored = pool.rows[COMPANY]
    cipher = CredentialCipher(_settings())
    assert stored["encrypted_client_id"] != CLIENT_ID
    assert stored["encrypted_client_secret"] != CLIENT_SECRET
    assert cipher.decrypt(stored["encrypted_client_id"]) == CLIENT_ID
    assert cipher.decrypt(stored["encrypted_client_secret"]) == CLIENT_SECRET
    # Plaintext on purpose: it identifies the account, like Rentvine's account code.
    assert stored["system_id"] == SYSTEM_ID
    assert stored["client_id_last4"] == CLIENT_ID[-4:]

    for body in (resp.json(), _get(client)):
        assert body["configured"] is True
        assert body["system_id"] == SYSTEM_ID
        assert body["client_id_last4"] == CLIENT_ID[-4:]
        assert body["last_tested_at"] is None
        blob = str(body)
        assert CLIENT_SECRET not in blob and CLIENT_ID not in blob
        # No last4 of the secret exists anywhere (migration 0107 stores none).
        assert CLIENT_SECRET[-4:] not in blob


def test_the_save_never_writes_the_host(client: TestClient, pool: RoutePool) -> None:
    """The column default holds the one host; a stored value must never be
    able to send a request that carries the secret somewhere else."""
    _put(client)
    (insert,) = [s for s in pool.statements if s.startswith("INSERT INTO")]
    named = insert.split("(", 1)[1].split(")", 1)[0]
    assert "base_url" not in named and "base_url" not in insert.split("DO UPDATE SET", 1)[1]
    assert pool.rows[COMPANY]["base_url"] == BASE_URL_DEFAULT == pw_client.BASE_URL


def test_saving_again_clears_the_last_passed_test(client: TestClient, pool: RoutePool) -> None:
    """Keys that changed have not passed a test. The re-save is the upsert's
    DO UPDATE path, which the one-PMS trigger does not fire on."""
    _put(client)
    pool.rows[COMPANY]["last_tested_at"] = datetime.now(UTC)
    assert _get(client)["last_tested_at"] is not None

    other_id = "-".join(("pw", "client", "id", "rotated", "9999"))
    resp = _put(client, {**_BODY, "client_id": other_id})
    assert resp.status_code == 200
    assert resp.json()["last_tested_at"] is None and resp.json()["client_id_last4"] == "9999"
    assert pool.rows[COMPANY]["last_tested_at"] is None


def test_saving_again_clears_a_remembered_write_refusal(
    client: TestClient, pool: RoutePool
) -> None:
    """A write Propertyware refused under the old keys may work under new ones,
    and the refusal tells the person to save the keys again: the save must be
    what clears it (``credentials.refused_at``, read by the write tools)."""
    _put(client)
    pool.rows[COMPANY]["capabilities"] = {"write_refused": {"POST /workorders": "2026-10-05"}}
    assert _put(client).status_code == 200
    assert pool.rows[COMPANY]["capabilities"] == {}


def test_spaces_around_pasted_values_are_not_stored(client: TestClient, pool: RoutePool) -> None:
    body = {
        "system_id": f"  {SYSTEM_ID}\n",
        "client_id": f" {CLIENT_ID} ",
        "client_secret": f"\t{CLIENT_SECRET}\n",
    }
    assert _put(client, body).status_code == 200
    stored = pool.rows[COMPANY]
    assert stored["system_id"] == SYSTEM_ID
    assert CredentialCipher(_settings()).decrypt(stored["encrypted_client_secret"]) == CLIENT_SECRET


@pytest.mark.parametrize(
    ("field", "value", "refusal"),
    [
        ("system_id", "", "_BAD_SYSTEM_ID"),
        ("system_id", "99\r\n0", "_BAD_SYSTEM_ID"),
        ("system_id", "9" * 101, "_BAD_SYSTEM_ID"),
        ("client_id", "abc", "_BAD_CLIENT_ID"),
        ("client_id", "pw-client\x00id", "_BAD_CLIENT_ID"),
        ("client_id", "c" * 513, "_BAD_CLIENT_ID"),
        ("client_secret", "short", "_BAD_CLIENT_SECRET"),
        ("client_secret", "first-half\nsecond-half", "_BAD_CLIENT_SECRET"),
        ("client_secret", "s" * 513, "_BAD_CLIENT_SECRET"),
    ],
)
def test_a_value_that_cannot_be_used_is_refused_in_words_and_never_repeated(
    client: TestClient, pool: RoutePool, field: str, value: str, refusal: str
) -> None:
    """Each value becomes a request header; the loader and the client would
    refuse a control character later (``header_value``), so the save refuses
    it first, in a sentence that names the action and repeats nothing."""
    resp = _put(client, {**_BODY, field: value})
    assert resp.status_code == 422
    assert resp.json() == {"detail": getattr(routes, refusal)}
    assert pool.rows == {}
    if len(value) > 3:
        assert value not in resp.text


@pytest.mark.parametrize(
    ("field", "shortest", "longest"),
    [("system_id", 1, 100), ("client_id", 4, 512), ("client_secret", 8, 512)],
)
def test_the_length_bounds_include_both_ends(
    client: TestClient, pool: RoutePool, field: str, shortest: int, longest: int
) -> None:
    for value in ("k" * shortest, "k" * longest):
        assert _put(client, {**_BODY, field: value}).status_code == 200, (field, len(value))


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({**_BODY, "client_secret": 12345678901234}, id="secret-not-a-string"),
        pytest.param({**_BODY, "client_id": ["a", CLIENT_SECRET]}, id="id-a-list"),
        pytest.param({"system_id": SYSTEM_ID, "client_id": CLIENT_ID}, id="secret-missing"),
        pytest.param(CLIENT_SECRET, id="body-a-string"),
        pytest.param([CLIENT_SECRET], id="body-a-list"),
    ],
)
def test_a_malformed_body_is_refused_without_echoing_anything(
    client: TestClient, pool: RoutePool, body: Any
) -> None:
    """FastAPI's own 422 puts the submitted value in its body, ``SecretStr`` or
    not (measured). So the model takes anything, and the route refuses with a
    sentence of its own."""
    resp = _put(client, body)
    assert resp.status_code == 422
    assert isinstance(resp.json()["detail"], str)
    assert CLIENT_SECRET not in resp.text and "12345678901234" not in resp.text
    assert pool.rows == {}


def test_the_request_model_masks_both_keys_in_its_repr() -> None:
    model = routes.PropertywareCredentialsInput(**_BODY)
    for text in (repr(model), str(model)):
        assert CLIENT_SECRET not in text and CLIENT_ID not in text


def test_a_workspace_on_another_system_is_refused_with_a_409_naming_it(
    client: TestClient, pool: RoutePool
) -> None:
    """The one-PMS trigger (migration 0107) raises a unique violation; a 500
    would tell the customer Occupella is broken, when the state is a rule
    with a remedy."""
    pool.insert_raises = asyncpg.exceptions.UniqueViolationError(
        f"company {COMPANY} is already connected to Rentvine; one PMS per company "
        "(ADR 0010 a1). Disconnect the existing PMS first."
    )
    resp = _put(client)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "Rentvine" in detail and "disconnect" in detail.lower() and "Propertyware" in detail
    assert pool.rows == {}


# ── the disconnect: delete the row, then purge ────────────────────────────


def test_the_disconnect_deletes_the_row_then_purges_every_table(
    client: TestClient, pool: RoutePool
) -> None:
    from helixis.buildium.purge import PURGE_TABLES

    pool.store()
    pool.purge_deletes = {"helixis_properties": 3, "helixis_leases": 2}
    resp = client.request("DELETE", "/api/v1/propertyware/credentials", headers=_auth())
    assert resp.status_code == 200
    assert resp.json() == {
        "configured": False,
        "system_id": None,
        "client_id_last4": None,
        "last_tested_at": None,
        "purged_rows": 5,
        "connector_enabled": None,
    }
    assert pool.rows == {}
    # The row FIRST: a sync that starts after finds no credentials, and one
    # already reading finds them gone before it writes.
    assert pool.events[0] == ("row", "deleted")
    assert [t for kind, t in pool.events[1:] if kind == "purged"] == list(PURGE_TABLES)


def test_every_table_the_propertyware_pass_writes_is_purged() -> None:
    """The purge is Buildium's list (one mirror). A table this pass writes
    and the list does not hold would survive every disconnect."""
    from helixis.buildium.purge import PURGE_TABLES

    written = set(pw_sync.SYNC_STATE_STAGE) | set(pw_sync.DERIVED_TABLES)
    assert written <= set(PURGE_TABLES)
    assert "helixis_mirror_sync_state" in PURGE_TABLES  # the freshness stamps


def test_the_purge_delegates_and_sends_no_sql_of_its_own() -> None:
    """A table list typed here would be the copy that goes stale (gotcha 25),
    and a ``pms`` filter would skip the tables without the column in silence
    (gotcha 112). So the module calls the Buildium purge and nothing else."""
    from helixis.propertyware import purge as module

    tree = ast.parse(Path(inspect.getfile(module)).read_text(encoding="utf-8"))
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "purge_buildium_mirror" in called
    strings = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    assert not [s for s in strings if re.search(r"\b(DELETE|helixis_\w+)\b", s)]


# ── the test ──────────────────────────────────────────────────────────────


def test_the_test_reads_one_building_reports_the_accounts_count_and_starts_the_sync(
    client: TestClient, pool: RoutePool
) -> None:
    """⚠ The count is Propertyware's ``X-Total-Count``, not the one row asked
    for: reporting that would tell every customer they have one building."""
    pool.store()
    server = FakePropertyware(totals={"/buildings": 417})
    with _vendor(server), patch.object(sync_cycle, "start_now") as start:
        body = _test(client)
    assert body == {"ok": True, "properties_count": 417, "error": None}
    (request,) = server.requests
    assert server.paths() == ["/buildings"]
    assert dict(request.url.params) == {"limit": "1"}
    assert pool.rows[COMPANY]["last_tested_at"] is not None
    start.assert_called_once()
    assert start.call_args.args[1:] == (PW, COMPANY)


def test_the_test_reads_the_list_the_sync_reads_first() -> None:
    """Both sides of the claim: a test of a path the product never reads
    proves the keys parse and nothing about whether the product works."""
    assert routes._TEST_PATH == pw_client.READS[0] == "/buildings"


class _NoTotal(FakePropertyware):
    def __call__(self, request: httpx.Request) -> httpx.Response:
        answer = super().__call__(request)
        return httpx.Response(
            answer.status_code, content=answer.content, headers={"content-type": "application/json"}
        )


def test_a_count_propertyware_did_not_state_is_unknown_never_zero(
    client: TestClient, pool: RoutePool
) -> None:
    pool.store()
    with _vendor(_NoTotal()), patch.object(sync_cycle, "start_now"):
        body = _test(client)
    assert body == {"ok": True, "properties_count": None, "error": None}


def _failing_test(client: TestClient, pool: RoutePool, status: int) -> dict[str, Any]:
    pool.store()
    with (
        _vendor(FakePropertyware(fail={"/buildings": status})),
        patch.object(sync_cycle, "start_now") as start,
        patch("helixis.pms.http._sleep", new=AsyncMock()),
    ):
        body = _test(client)
    assert body["ok"] is False and body["properties_count"] is None
    # Nothing that failed is stamped, and no sync starts on it.
    assert pool.rows[COMPANY]["last_tested_at"] is None
    start.assert_not_called()
    return body


def test_a_401_names_all_three_values_and_says_to_save_them_again(
    client: TestClient, pool: RoutePool
) -> None:
    error = _failing_test(client, pool, 401)["error"]
    assert error == routes._TEST_ADVICE[Failure.CREDENTIALS]
    for value in ("organization ID", "client ID", "client secret"):
        assert value in error
    assert "save them again" in error


def test_a_403_names_the_permission_and_all_three_values_and_never_says_reconnect(
    client: TestClient, pool: RoutePool
) -> None:
    """Gotcha 102: a 403 means Propertyware accepted the key and refused this
    read. The status for a wrong organization ID is not documented (PW-2), so
    the design has every 401 and 403 name all three values."""
    error = _failing_test(client, pool, 403)["error"]
    assert error == routes._TEST_ADVICE[Failure.PERMISSION]
    assert "BUILDINGS - Read" in error
    for value in ("organization ID", "client ID", "client secret"):
        assert value in error
    assert "reconnect" not in error.lower()
    assert error != routes._TEST_ADVICE[Failure.CREDENTIALS]


def test_the_403_sentence_follows_the_reads_beta_marker() -> None:
    """The design's rule for a 403: a beta-marked operation needs
    Propertyware's beta program; any other needs the key's permission."""
    assert "GET /buildings" not in _paths.BETA
    plain = routes._refused_advice()
    assert "The API key lacks BUILDINGS - Read" in plain and "beta" not in plain
    with patch.object(_paths, "BETA", frozenset({"GET /buildings"})):
        beta = routes._refused_advice()
    assert "beta program" in beta and "ask Propertyware support" in beta
    for sentence in (plain, beta):
        assert "reconnect" not in sentence.lower() and "organization ID" in sentence


@pytest.mark.parametrize("status", [429, 502])
def test_an_answer_that_never_came_names_an_action(
    client: TestClient, pool: RoutePool, status: int
) -> None:
    error = _failing_test(client, pool, status)["error"]
    assert error == routes._TEST_ADVICE_UNDOCUMENTED
    assert "test again in a few minutes" in error and "team@occupella.com" in error


def test_a_404_names_the_organization_id(client: TestClient, pool: RoutePool) -> None:
    error = _failing_test(client, pool, 404)["error"]
    assert error == routes._TEST_ADVICE[Failure.NOT_FOUND]
    assert "organization ID" in error


def test_every_classified_failure_has_advice_and_none_is_the_default() -> None:
    """A missing entry would fall back to the undocumented sentence, telling
    someone with a revoked key that nothing is wrong with what they entered."""
    assert set(routes._TEST_ADVICE) == set(Failure) - {Failure.UNDOCUMENTED}
    assert routes._TEST_ADVICE_UNDOCUMENTED not in routes._TEST_ADVICE.values()


@pytest.mark.parametrize(
    "sentence",
    [
        *routes._TEST_ADVICE.values(),
        routes._TEST_ADVICE_UNDOCUMENTED,
        routes._TEST_TIMED_OUT,
        routes._TEST_CRASHED,
        routes._NO_CONNECTION,
        routes._UNUSABLE_ROW,
    ],
)
def test_every_answer_of_the_test_names_an_action(sentence: str) -> None:
    """Rule 16: each says what the person can do next."""
    actions = ("save", "create a key", "test again", "Test once more", "team@occupella.com")
    assert any(action in sentence for action in actions), sentence


def test_a_test_with_nothing_saved_says_so_and_calls_nothing(
    client: TestClient, pool: RoutePool
) -> None:
    with patch.object(pw_client, "build_client") as build:
        body = _test(client)
    assert body == {"ok": False, "properties_count": None, "error": routes._NO_CONNECTION}
    build.assert_not_called()


def test_a_saved_row_that_cannot_be_read_says_to_save_again(
    client: TestClient, pool: RoutePool
) -> None:
    pool.store(encrypted_client_secret="not-a-fernet-token")
    with patch.object(pw_client, "build_client") as build:
        body = _test(client)
    assert body["error"] == routes._UNUSABLE_ROW
    build.assert_not_called()


def test_a_crash_inside_the_test_never_reaches_the_customer_verbatim(
    client: TestClient, pool: RoutePool
) -> None:
    pool.store()
    boom = RuntimeError("asyncpg pool exhausted at 0x7f3a")
    with patch.object(pw_client, "build_client", side_effect=boom):
        body = _test(client)
    assert body["error"] == routes._TEST_CRASHED
    assert "asyncpg" not in body["error"] and "0x7f3a" not in body["error"]
    assert pool.rows[COMPANY]["last_tested_at"] is None


def test_an_answer_that_is_not_a_list_of_buildings_proves_nothing(
    client: TestClient, pool: RoutePool
) -> None:
    pool.store()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"buildings": []}, headers={"X-Total-Count": "3"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with _vendor(http), patch.object(sync_cycle, "start_now") as start:
        body = _test(client)
    assert body["ok"] is False and body["error"] == routes._TEST_CRASHED
    assert pool.rows[COMPANY]["last_tested_at"] is None
    start.assert_not_called()


def test_a_test_propertyware_does_not_answer_in_time_says_so(
    client: TestClient, pool: RoutePool
) -> None:
    """The web app stops waiting at 45 s; the bound is in code, under it."""
    assert routes._TEST_TIMEOUT_S < 45
    pool.store()

    async def handler(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json=[])

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with (
        _vendor(http),
        patch.object(routes, "_TEST_TIMEOUT_S", 0.05),
        patch.object(sync_cycle, "start_now") as start,
    ):
        body = _test(client)
    assert body["error"] == routes._TEST_TIMED_OUT
    assert pool.rows[COMPANY]["last_tested_at"] is None
    start.assert_not_called()


def test_no_secret_reaches_a_response_or_a_log_line(
    client: TestClient, pool: RoutePool, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        texts = [
            _put(client).text,
            client.get("/api/v1/propertyware/credentials", headers=_auth()).text,
        ]
        for status in (401, 403, 200):
            server = FakePropertyware(fail={"/buildings": status} if status != 200 else None)
            with _vendor(server), patch.object(sync_cycle, "start_now"):
                texts.append(client.post("/api/v1/propertyware/test", headers=_auth()).text)
    for text in (*texts, caplog.text):
        assert CLIENT_SECRET not in text
    for text in texts:
        assert CLIENT_ID not in text


# ── the sync: Sync now and its status ─────────────────────────────────────


def _ended_run(timeout: float = 5.0) -> sync_cycle.SyncRun:
    """Wait (outside the app) until the company's run has ended."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = sync_cycle.sync_status(PW, COMPANY)
        if run is not None and run.finished_at is not None:
            return run
        time.sleep(0.01)
    raise AssertionError("the sync never ended")


def _status(c: TestClient) -> dict[str, Any]:
    resp = c.get("/api/v1/propertyware/sync", headers=_auth())
    assert resp.status_code == 200, resp.text
    return resp.json()


def _gated(result: SyncResult | None) -> tuple[Any, threading.Event]:
    """A ``sync_one`` that waits for the test's go, then answers ``result``."""
    go = threading.Event()

    async def one(_pool: Any, pms: str, company_id: str) -> SyncResult | None:
        assert (pms, company_id) == (PW, COMPANY)
        await asyncio.to_thread(go.wait, 5)
        return result

    return one, go


def test_sync_answers_at_once_and_the_status_reports_the_rows_by_table(
    client: TestClient, pool: RoutePool
) -> None:
    pool.store()
    result = SyncResult(
        company_id=COMPANY,
        pms=PW,
        rows_by_table={"helixis_units": 4, "helixis_leases": 3, "helixis_lease_tenants": 5},
    )
    one, go = _gated(result)
    with patch.object(sync_cycle, "sync_one", one):
        start = client.post("/api/v1/propertyware/sync", headers=_auth()).json()
        assert start["ok"] is True and start["running"] is True and start["started_at"]
        during = _status(client)
        assert during["running"] is True and during["started_at"] == start["started_at"]
        assert during["rows"] == {}
        go.set()
        _ended_run()
        after = _status(client)
    # Keyed by table: leases and their tenant links share one freshness stage,
    # and a stage key would show one count as the other's.
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
    refused = pw_sync.read_failure(
        "/vendors", PmsApiError(403, Failure.PERMISSION, False, "synthetic")
    )
    result = SyncResult(
        company_id=COMPANY,
        pms=PW,
        rows_by_table={"helixis_properties": 5, "helixis_units": 2},
        failed={
            "helixis_vendors": refused,
            "helixis_units": "incomplete read: written, not stamped, not pruned",
        },
    )
    one, go = _gated(result)
    go.set()
    with patch.object(sync_cycle, "sync_one", one):
        client.post("/api/v1/propertyware/sync", headers=_auth())
        _ended_run()
        after = _status(client)
    assert after["ok"] is False and after["stale"] == ["units", "vendors"]
    # A stale table is never counted: 2 units of a part-read would read as the account's.
    assert after["rows"] == {"properties": 5}
    error = after["error"]
    assert "last sync that worked" in error
    assert "Units: Propertyware stopped sending them part-way through." in error
    assert f"Vendors: {refused}" in error and "VENDORS - Read" in error
    # The operator's wording stays in the log.
    assert "incomplete read" not in error and "403" not in error


def test_one_cause_is_said_once_after_every_table_it_stopped() -> None:
    reason = pw_sync.read_failure(
        "/leases", PmsApiError(401, Failure.CREDENTIALS, False, "synthetic")
    )
    sentence = routes._stale_sentence(
        {t: reason for t in ("helixis_lease_balances", "helixis_leases", "helixis_lease_tenants")}
    )
    assert sentence.count(reason) == 1
    assert f"Leases, which tenants are on which lease and rent balances: {reason}" in sentence


async def test_a_lease_without_a_unit_is_not_an_error_in_the_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fixtures hold leases with no unit (single-family homes often have
    none). The real pass over them is clean, and the status says so."""
    current = fake_settings(enabled=True)
    monkeypatch.setattr("helixis.settings.get_settings", lambda: current)
    monkeypatch.setattr(pw_sync, "today_in", lambda _zone: TODAY)
    mirror_pool = FakePool(creds=creds_row())
    result = await pw_sync.sync_company(mirror_pool, COMPANY, http=FakePropertyware().http())
    leases = inserted_rows(mirror_pool, "helixis_leases")
    assert len(leases) == len(fixture_rows("/leases"))
    assert any(row.get("unit_id") is None for row in leases), (
        "the fixtures lost their no-unit lease"
    )

    run = sync_cycle.SyncRun(pms=PW, started_at=datetime.now(UTC), finished_at=datetime.now(UTC))
    run.result = result
    status = routes._ended(run)
    assert status.ok is True and status.stale == [] and status.error is None
    assert status.rows["leases"] == len(leases)


def test_a_click_during_a_sync_waits_on_that_sync(client: TestClient, pool: RoutePool) -> None:
    """The run a passed test started, or a first click, is joined: same start,
    and no second crawl of the same account."""
    pool.store()
    run, started = sync_cycle.claim_or_join(PW, COMPANY)
    assert started
    with patch.object(sync_cycle, "sync_one") as one:
        body = client.post("/api/v1/propertyware/sync", headers=_auth()).json()
    assert body["running"] is True and body["started_at"] == run.started_at.isoformat()
    one.assert_not_called()
    run.result = SyncResult(company_id=COMPANY, pms=PW, rows_by_table={"helixis_units": 1})
    sync_cycle.release(COMPANY, run)
    assert _status(client)["rows"] == {"units": 1}


def test_a_sync_that_did_not_finish_names_what_to_do(client: TestClient, pool: RoutePool) -> None:
    pool.store()
    one, go = _gated(None)
    go.set()
    with patch.object(sync_cycle, "sync_one", one):
        client.post("/api/v1/propertyware/sync", headers=_auth())
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
        body = client.post("/api/v1/propertyware/sync", headers=_auth()).json()
    assert body["ok"] is False and body["running"] is False
    assert body["error"] == routes._SYNC_DID_NOT_FINISH
    assert _status(client)["running"] is False


def test_sync_without_a_saved_connection_does_not_run(client: TestClient, pool: RoutePool) -> None:
    with patch.object(sync_cycle, "start_now") as start:
        body = client.post("/api/v1/propertyware/sync", headers=_auth()).json()
    assert body["ok"] is False and body["error"] == routes._NO_CONNECTION
    start.assert_not_called()
    assert pool.asked == [], "nothing asked for a company with nothing to sync"


def test_sync_now_asks_for_a_full_read_before_its_pass_starts(
    client: TestClient, pool: RoutePool
) -> None:
    """The page shows the run's counts ("Synced 12 buildings"): a pass that
    read only the changes would show "0 buildings" (rule 14). And Phase 8's
    advice after a difference is Sync now, which must read everything."""
    pool.store()
    asked_when_it_ran: list[int] = []

    async def one(_pool: Any, pms: str, company_id: str) -> SyncResult:
        asked_when_it_ran.append(len(pool.asked))
        return SyncResult(company_id=company_id, pms=pms)

    with patch.object(sync_cycle, "sync_one", one):
        client.post("/api/v1/propertyware/sync", headers=_auth())
        _ended_run()
    assert asked_when_it_ran == [1], "asked before the pass chose its kind"
    ((company, resource, when),) = pool.asked
    assert company == COMPANY
    assert resource == pw_sync.resource(pw_sync.FULL_ASKED_MARKER)
    assert isinstance(when, datetime) and when.tzinfo is not None


def test_sync_now_switched_off_asks_for_nothing(dark_client: TestClient, pool: RoutePool) -> None:
    pool.store()
    assert dark_client.post("/api/v1/propertyware/sync", headers=_auth()).status_code == 503
    assert pool.asked == []


def test_the_status_before_any_sync_says_how_to_run_one(client: TestClient) -> None:
    body = _status(client)
    assert body["ok"] is False and body["running"] is False and body["started_at"] is None
    assert "Click Sync now" in body["error"]


# ── the card ──────────────────────────────────────────────────────────────


async def test_the_card_is_listed_by_the_shared_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    """The catalog row is this branch's; when it shows is the shared rule's
    (``test_connectors_toolkit_rows``): switch on and no PMS held, here."""
    from helixis.api.routes import connectors as route
    from helixis.pms import registry
    from tests.unit.test_connectors_rentvine_row import _rows

    (row,) = [spec for spec in route.SUPPORTED_CONNECTORS if spec["id"] == PW]
    assert row["category"] == "property_management" and row["authMethod"] == "api_key"
    monkeypatch.setattr(registry, "enabled", lambda key: key in (PW, "buildium"))
    monkeypatch.setattr("helixis.db.company_pms.company_pms", AsyncMock(return_value=None))
    monkeypatch.setattr(route, "_last_tested", AsyncMock(return_value=None))
    rows = await _rows(monkeypatch, enabled=False)
    assert rows[PW]["isConnected"] is False and rows[PW]["name"] == "Propertyware"

    monkeypatch.setattr(registry, "enabled", lambda key: key == "buildium")
    assert PW not in await _rows(monkeypatch, enabled=False)
