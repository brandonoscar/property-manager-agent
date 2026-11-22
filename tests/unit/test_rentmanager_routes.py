"""The Rent Manager connect surface: save, read, test, sync, disconnect.

What is pinned, each a rule the brief or the Rentvine precedent states:

  - the wire names are the onboarding wizard's contract (ADR 0003), and the
    web app's service declares the same ones (rule 15);
  - every route needs an admin and fails closed; saving, testing and syncing
    refuse while the connector is off, while the read, the status poll and the
    disconnect do not (gotcha 120);
  - no secret comes back in any answer or refusal, or reaches a log line, and
    a refusal names the field, never the value (FastAPI's own 422 echoes the
    whole body, password included: measured);
  - a company on another system gets a readable 409 and nothing is stored;
  - the test signs in, confirms the location and counts from one page's total
    header; a 401 and a 403 give opposite instructions (gotcha 102);
    ``last_tested_at`` is set only by a passed test of the details still
    saved, and a pass starts the first sync;
  - the disconnect deletes the row, then purges through ``purge.py``;
  - the sync answers name stale stages and never give them a count.

The database is a stand-in that holds the columns migration 0107 declares and
answers each SELECT with exactly the columns it names; Rent Manager is the
synthetic one in ``_rentmanager_fakes``.
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
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import httpx
import jwt
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from helixis.api.routes import rentmanager as route
from helixis.api.routes.rentvine import RentvineSyncResult
from helixis.main import create_app
from helixis.pms import auth as pms_auth
from helixis.pms import http, sync_cycle
from helixis.pms.errors import Failure
from helixis.pms.mirror import SyncResult
from helixis.pms.watermarks import TABLE as WATERMARKS
from helixis.rentmanager import client as rm_client
from helixis.rentmanager import sync as rm_sync
from helixis.rentmanager.credentials import TABLE, version_of
from helixis.rentmanager.resources import RESOURCES
from helixis.settings import Settings
from tests.unit._pg_bind_contract import reject_unbindable
from tests.unit._rentmanager_fakes import (
    COMPANY,
    CORP,
    DATA_KEY,
    LOCATION,
    PARTNER,
    PASSWORD,
    TOKEN,
    USERNAME,
    FakeRentManager,
    json_response,
    portfolio,
    serve,
)
from tests.unit._supabase_tokens import TEST_ISSUER, TEST_SUPABASE_URL

REPO = Path(__file__).resolve().parents[2]
SERVICE = REPO / "frontend/src/services/rentmanager.ts"
MIGRATION = REPO / "supabase/migrations/0107_rentmanager_propertyware_credentials.sql"
JWT_SECRET = "test-secret-key-for-unit-tests-only-not-production"
BASE = "/api/v1/rentmanager"
SECRETS = (USERNAME, PASSWORD, PARTNER, TOKEN)
PMS = "rentmanager"
_DROP = object()


# ── a database that holds what migration 0107 declares ─────────────────


def _declared_columns() -> dict[str, bool]:
    """``{column: required}`` for the credentials table, read from migration
    0107: required means NOT NULL with no default, or the primary key."""
    sql = MIGRATION.read_text(encoding="utf-8")
    found = re.search(rf"create table if not exists public\.{TABLE} \((.*?)\n\);", sql, re.S)
    assert found, "migration 0107 no longer creates the table; re-point this reader"
    out: dict[str, bool] = {}
    for line in found.group(1).splitlines():
        line = line.split("--")[0].strip().lower()
        col = re.match(r"^([a-z][a-z0-9_]*)\s+(uuid|text|integer|jsonb|timestamptz)\b(.*)$", line)
        if col:
            name, _type, rest = col.groups()
            out[name] = "primary key" in rest or ("not null" in rest and "default" not in rest)
    assert {"company_id", "corp_id", "username_last4", "base_url", "last_tested_at"} <= set(out), (
        out
    )
    return out


COLUMNS = _declared_columns()


def _split_top(text: str) -> list[str]:
    """Split on commas outside parentheses."""
    parts, depth, current = [], 0, ""
    for ch in text:
        depth += ch == "("
        depth -= ch == ")"
        if ch == "," and depth == 0:
            parts.append(current.strip())
            current = ""
        else:
            current += ch
    return [*parts, current.strip()] if current.strip() else parts


def _bound(token: str, args: tuple[Any, ...]) -> Any:
    """``$3`` or ``$1::uuid`` as its argument, ``NULL`` as None."""
    token = token.strip()
    if token.upper() == "NULL":
        return None
    found = re.fullmatch(r"\$(\d+)(?:::\w+)?", token)
    assert found, f"a value this stand-in does not model: {token}"
    return args[int(found.group(1)) - 1]


class RoutePool:
    """asyncpg over ``company_rentmanager_credentials``, the role read, the
    purge and the sync's watermark table (``ask_for_full_pass``). A column the
    table lacks, a NOT NULL one left empty, an ON CONFLICT that forgets a
    column, or a SELECT of a column that does not exist all fail here as they
    would in Postgres."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.role: str | None = "admin"
        self.insert_raises: BaseException | None = None
        self.purge_counts: dict[str, int] = {}
        self.purged: list[str] = []
        self.log: list[str] = []
        self.watermarks: dict[str, datetime] = {}
        self.watermark_raises: BaseException | None = None

    @staticmethod
    def _scoped(flat: str, args: tuple[Any, ...]) -> None:
        """Rule 11: a statement on the table names the company, as ``$1``."""
        if f"public.{TABLE}" in flat:
            assert "company_id = $1" in flat or flat.startswith(f"INSERT INTO public.{TABLE} ")
            assert args and args[0] == COMPANY, flat

    async def execute(self, sql: str, *args: Any) -> str:
        reject_unbindable(sql, args)
        flat = " ".join(sql.split())
        self._scoped(flat, args)
        self.log.append(flat)
        if flat.startswith(f"INSERT INTO public.{TABLE} "):
            return self._upsert(flat, args)
        if flat.startswith(f"DELETE FROM public.{TABLE} WHERE company_id = $1"):
            self.rows.pop(args[0], None)
            return "DELETE 1"
        if flat.startswith(f"INSERT INTO {WATERMARKS} "):
            if self.watermark_raises is not None:
                raise self.watermark_raises
            assert args[0] == COMPANY, flat
            held = self.watermarks.get(args[1])
            self.watermarks[args[1]] = args[2] if held is None else max(held, args[2])
            return "INSERT 0 1"
        raise AssertionError(f"a statement this stand-in does not model: {flat}")

    def _upsert(self, flat: str, args: tuple[Any, ...]) -> str:
        if self.insert_raises is not None:
            raise self.insert_raises
        found = re.search(
            r"\((.*?)\) VALUES \((.*?)\) ON CONFLICT \(company_id\) DO UPDATE SET (.*)$", flat
        )
        assert found, flat
        names = [c.strip() for c in found.group(1).split(",")]
        values = dict(
            zip(names, [_bound(v, args) for v in _split_top(found.group(2))], strict=True)
        )
        assert set(names) <= set(COLUMNS), set(names) - set(COLUMNS)
        empty = [c for c, required in COLUMNS.items() if required and values.get(c) is None]
        if empty:
            raise asyncpg.exceptions.NotNullViolationError(f"null value in {empty}")
        company = values["company_id"]
        if company not in self.rows:
            self.rows[company] = {c: None for c in COLUMNS} | {"capabilities": {}} | values
            return "INSERT 0 1"
        row = self.rows[company]
        for assignment in _split_top(found.group(3)):
            column, value = (s.strip() for s in assignment.split("=", 1))
            if value.upper() == "NULL":
                row[column] = None
            elif value == "'{}'::jsonb":  # the cleared ``capabilities``
                row[column] = {}
            else:
                row[column] = values[value.removeprefix("EXCLUDED.")]
        return "INSERT 0 1"

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        reject_unbindable(sql, args)
        flat = " ".join(sql.split())
        self._scoped(flat, args)
        self.log.append(flat)
        if flat.startswith("SELECT role FROM public.company_users"):
            return {"role": self.role} if self.role is not None else None
        if f"FROM public.{TABLE} WHERE company_id = $1::uuid" in flat:
            row = self.rows.get(args[0])
            return None if row is None else self._select(flat, row)
        if "FROM public.companies" in flat:
            return {"timezone": "UTC"}
        raise AssertionError(f"a read this stand-in does not model: {flat}")

    @staticmethod
    def _select(flat: str, row: dict[str, Any]) -> dict[str, Any]:
        items = _split_top(re.search(r"^SELECT (.*?) FROM ", flat).group(1))  # type: ignore[union-attr]
        out: dict[str, Any] = {}
        for item in items:
            aliased = re.fullmatch(r"(.*) AS (\w+)", item, re.I)
            if aliased is None:
                assert item in COLUMNS, f"the table has no column {item!r}"
                out[item] = row[item]
                continue
            expr, alias = aliased.groups()
            assert expr == "coalesce(encrypted_partner_token, '') <> ''", expr
            out[alias] = bool(row["encrypted_partner_token"] or "")
        return out

    async def fetchval(self, sql: str, *args: Any) -> Any:
        reject_unbindable(sql, args)
        flat = " ".join(sql.split())
        self._scoped(flat, args)
        self.log.append(flat)
        found = re.fullmatch(
            rf"UPDATE public\.{TABLE} SET last_tested_at = now\(\) WHERE (.*) RETURNING company_id",
            flat,
        )
        assert found, f"a statement this stand-in does not model: {flat}"
        row = self.rows.get(args[0])
        if row is None:
            return None
        for condition in found.group(1).split(" AND "):
            same = re.fullmatch(r"(\w+) = (\$\d+(?:::\w+)?)", condition)
            alike = re.fullmatch(r"(\w+) IS NOT DISTINCT FROM (\$\d+)", condition)
            assert same or alike, condition
            column, token = (same or alike).groups()  # type: ignore[union-attr]
            want = _bound(token, args)
            # SQL: NULL = anything is not true; IS NOT DISTINCT FROM matches NULLs.
            if same and (row[column] is None or row[column] != want):
                return None
            if alike and row[column] != want:
                return None
        row["last_tested_at"] = datetime.now(UTC)
        return args[0]

    @contextlib.asynccontextmanager
    async def acquire(self) -> Any:
        yield _PurgeConnection(self)


class _PurgeConnection:
    def __init__(self, pool: RoutePool) -> None:
        self.pool = pool

    async def execute(self, sql: str, *args: Any) -> str:
        found = re.fullmatch(r"DELETE FROM public\.(\w+) WHERE company_id = \$1", sql.strip())
        assert found and args == (COMPANY,), sql
        self.pool.purged.append(found.group(1))
        self.pool.log.append(f"PURGE {found.group(1)}")
        return f"DELETE {self.pool.purge_counts.get(found.group(1), 0)}"


# ── the app ────────────────────────────────────────────────────────────


def _token() -> str:
    now = int(time.time())
    claims = {
        "sub": "00000000-0000-4000-8000-0000000000a1",
        "email": "admin@example.com",
        "role": "authenticated",
        "aud": "authenticated",
        "exp": now + 3600,
        "iat": now,
        "iss": TEST_ISSUER,
    }
    return jwt.encode(claims, JWT_SECRET, algorithm="HS256")


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {_token()}"}


def _settings(*, enabled: bool = True) -> Settings:
    return Settings(
        env="test",
        auth={"jwt_secret": JWT_SECRET},
        supabase={"url": TEST_SUPABASE_URL},
        data_key=DATA_KEY,
        memory={"enabled": False},
        rentmanager={"enabled": enabled},
    )


@asynccontextmanager
async def _noop_lifespan(_app: Any) -> Any:
    yield


def _client(settings: Settings, pool: RoutePool) -> Iterator[TestClient]:
    from helixis.api.rate_limit import connectors_limiter

    connectors_limiter.counter._log.clear()
    with (
        patch("helixis.api.auth.get_settings", return_value=settings),
        # The switch (pms.registry) and the cipher read the settings here.
        patch("helixis.settings.get_settings", return_value=settings),
        patch.object(route, "get_pool", return_value=pool),
        patch("helixis.api.auth._resolve_company_ids", new=AsyncMock(return_value=[COMPANY])),
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
    """The shipped default: ``HELIXIS_RENTMANAGER__ENABLED`` false."""
    yield from _client(_settings(enabled=False), pool)


@pytest.fixture
def vendor(monkeypatch: pytest.MonkeyPatch) -> FakeRentManager:
    """The synthetic Rent Manager behind the connector's own client."""

    async def _no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr(http, "_sleep", _no_wait)
    return serve(monkeypatch, FakeRentManager())


@pytest.fixture
def kicks(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    """``sync_cycle.start_now`` calls, recorded instead of started."""
    seen: list[tuple[Any, ...]] = []

    def _start(pool: Any, pms: str, company_id: str) -> sync_cycle.SyncRun:
        seen.append((pool, pms, company_id))
        return sync_cycle.SyncRun(pms=pms, started_at=datetime.now(UTC))

    monkeypatch.setattr(sync_cycle, "start_now", _start)
    return seen


def _body(**changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "corp_id": CORP,
        "location_id": LOCATION,
        "username": USERNAME,
        "password": PASSWORD,
    }
    body.update(changes)
    return {k: v for k, v in body.items() if v is not _DROP}


def _put(c: TestClient, **changes: Any) -> httpx.Response:
    return c.put(f"{BASE}/credentials", headers=_auth(), json=_body(**changes))


def _get(c: TestClient) -> dict[str, Any]:
    return c.get(f"{BASE}/credentials", headers=_auth()).json()


def _test(c: TestClient) -> dict[str, Any]:
    return c.post(f"{BASE}/test", headers=_auth()).json()


# ── the contract (ADR 0003, rule 15) ───────────────────────────────────

#: The names the onboarding wizard is built against. A rename on either side
#: breaks a customer's connect in the other repo.
WIRE = {
    route.RentManagerCredentialsInput: {
        "corp_id",
        "location_id",
        "username",
        "password",
        "partner_token",
    },
    route.RentManagerCredentialsStatus: {
        "configured",
        "corp_id",
        "location_id",
        "username_last4",
        "partner_token_saved",
        "last_tested_at",
        "purged_rows",
        "connector_enabled",
    },
    route.RentManagerTestResult: {"ok", "properties_count", "error"},
    route.RentManagerSyncResult: {"ok", "rows", "stale", "error", "running", "started_at"},
}

#: Python model -> the TypeScript interface that mirrors it.
TS_NAMES = {
    route.RentManagerCredentialsInput: "RentManagerCredentialsInput",
    route.RentManagerCredentialsStatus: "RentManagerStatus",
    route.RentManagerTestResult: "RentManagerTestResult",
    route.RentManagerSyncResult: "RentManagerSyncResult",
}


def _ts_fields(interface: str) -> set[str]:
    source = SERVICE.read_text(encoding="utf-8")
    block = re.search(rf"export interface {interface} \{{(.*?)\n\}}", source, re.S)
    assert block, f"rentmanager.ts no longer declares {interface}"
    return set(re.findall(r"^  (\w+)\??:", block.group(1), re.M))


def test_the_wire_names_are_the_wizards_contract() -> None:
    for model, names in WIRE.items():
        assert set(model.model_fields) == names, model.__name__
    # The sync answer is Rentvine's, name for name: one poll loop shape.
    assert set(route.RentManagerSyncResult.model_fields) == set(RentvineSyncResult.model_fields)
    fields = route.RentManagerCredentialsInput.model_fields
    assert fields["location_id"].annotation is int
    assert fields["username"].annotation is SecretStr
    assert fields["password"].annotation is SecretStr
    assert fields["partner_token"].annotation == SecretStr | None
    assert fields["partner_token"].default is None
    assert route.RentManagerCredentialsStatus.model_fields["partner_token_saved"].annotation is bool


@pytest.mark.parametrize("model", list(TS_NAMES), ids=lambda m: m.__name__)
def test_the_web_service_declares_the_same_names(model: Any) -> None:
    assert _ts_fields(TS_NAMES[model]) == set(model.model_fields)


def test_the_page_polls_inside_the_connectors_rate_limit() -> None:
    """The poll shares ``connectors_limiter`` with every connector call the
    user makes; faster than half its budget, a long sync turns into 429s."""
    from helixis.api.rate_limit import connectors_limiter

    found = re.search(r"export const SYNC_POLL_MS = ([\d_]+);", SERVICE.read_text(encoding="utf-8"))
    assert found, "rentmanager.ts no longer declares SYNC_POLL_MS"
    poll_ms = int(found.group(1).replace("_", ""))
    budget = connectors_limiter.counter.requests * 60 / connectors_limiter.counter.window
    assert 60_000 / poll_ms <= budget / 2


def test_the_panel_shows_the_routes_own_sentence_for_a_bad_field() -> None:
    """Both sides (rule 15): the panel says why Save is off with the sentence
    this route would answer, so the two cannot drift into different advice."""
    panel = (REPO / "frontend/src/components/connectors/RentManagerApiDetail.tsx").read_text(
        encoding="utf-8"
    )
    block = re.search(r"export const FIELD_ADVICE = \{(.*?)\n\} as const;", panel, re.S)
    assert block, "RentManagerApiDetail.tsx no longer declares FIELD_ADVICE"
    shown = dict(re.findall(r"(\w+):\s*'([^']*)'", block.group(1)))
    assert shown, "the scan found no sentences; re-point it"
    assert shown == {field: route._FIELD_ADVICE[field] for field in shown}
    assert {"corp_id", "location_id"} <= set(shown)


def test_the_documented_body_is_the_model() -> None:
    """The PUT reads its own body (below), so the schema the API documents is
    declared by hand; it must be the model's."""
    app = create_app(_settings())
    put = app.openapi()["paths"][f"{BASE}/credentials"]["put"]
    schema = put["requestBody"]["content"]["application/json"]["schema"]
    assert set(schema["properties"]) == WIRE[route.RentManagerCredentialsInput]
    assert set(schema["required"]) == {"corp_id", "location_id", "username", "password"}


# ── save and read ──────────────────────────────────────────────────────


def test_nothing_saved_reads_as_not_configured_and_says_the_switch(client: TestClient) -> None:
    body = _get(client)
    assert body["configured"] is False and body["connector_enabled"] is True


def test_the_save_stores_ciphertext_and_the_read_returns_no_secret(
    client: TestClient, pool: RoutePool
) -> None:
    from cryptography.fernet import Fernet

    resp = _put(client, partner_token=PARTNER)
    assert resp.status_code == 200, resp.text
    row = pool.rows[COMPANY]
    cipher = Fernet(DATA_KEY.encode())
    assert cipher.decrypt(row["encrypted_username"].encode()).decode() == USERNAME
    assert cipher.decrypt(row["encrypted_password"].encode()).decode() == PASSWORD
    assert cipher.decrypt(row["encrypted_partner_token"].encode()).decode() == PARTNER
    assert row["username_last4"] == USERNAME[-4:]
    for column in ("encrypted_username", "encrypted_password", "encrypted_partner_token"):
        assert all(secret not in row[column] for secret in SECRETS)
    for answer in (resp.json(), _get(client)):
        assert answer["configured"] is True
        assert answer["corp_id"] == CORP and answer["location_id"] == LOCATION
        assert answer["username_last4"] == USERNAME[-4:]
        assert answer["partner_token_saved"] is True
        assert answer["last_tested_at"] is None
        assert not any(secret in str(answer) for secret in SECRETS)
        # No tail of the password or the partner token, under any name.
        shown = [value for value in answer.values() if isinstance(value, str)]
        assert not any(PASSWORD[-4:] in v or PARTNER[-4:] in v for v in shown)


def test_the_base_url_comes_from_the_guarded_corp_id_never_the_request(
    client: TestClient, pool: RoutePool
) -> None:
    resp = _put(client, corp_id="  SynthCorp  ", base_url="https://evil.example")
    assert resp.status_code == 200, resp.text
    row = pool.rows[COMPANY]
    assert row["corp_id"] == "synthcorp"
    assert (
        row["base_url"]
        == rm_client.base_url("synthcorp")
        == "https://synthcorp.api.rentmanager.com"
    )


def test_a_blank_partner_token_is_none_never_an_empty_value(
    client: TestClient, pool: RoutePool
) -> None:
    assert _put(client, partner_token=PARTNER).status_code == 200
    for blank in ("   ", "", None, _DROP):
        resp = _put(client, partner_token=blank)
        assert resp.status_code == 200, (blank, resp.text)
        assert pool.rows[COMPANY]["encrypted_partner_token"] is None
        assert resp.json()["partner_token_saved"] is False
        assert _put(client, partner_token=PARTNER).json()["partner_token_saved"] is True


def test_a_short_username_is_never_shown_whole(client: TestClient, pool: RoutePool) -> None:
    resp = _put(client, username="api")
    assert resp.status_code == 200
    assert resp.json()["username_last4"] is None
    assert pool.rows[COMPANY]["username_last4"] == ""


def test_saving_again_replaces_every_value_and_clears_the_test(
    client: TestClient, pool: RoutePool
) -> None:
    assert _put(client, partner_token=PARTNER).status_code == 200
    first = dict(pool.rows[COMPANY])
    pool.rows[COMPANY]["last_tested_at"] = datetime.now(UTC)
    resp = _put(client, location_id=7, username="other-api-user")
    assert resp.status_code == 200
    row = pool.rows[COMPANY]
    assert row["last_tested_at"] is None, "new details have not passed a test"
    assert row["location_id"] == 7 and resp.json()["username_last4"] == "user"
    assert row["encrypted_password"] != first["encrypted_password"]
    assert row["encrypted_partner_token"] is None, "absent means none, not the old one"


# ── refusals that name the field, never the value ──────────────────────


@pytest.mark.parametrize(
    "corp_id",
    ["evil.example/x", "synth corp", "synthcorp.api.rentmanager.com", "", "-", 12, None],
)
def test_a_corp_id_that_is_not_a_host_label_is_refused(
    client: TestClient, pool: RoutePool, corp_id: Any
) -> None:
    resp = _put(client, corp_id=corp_id)
    assert resp.status_code == 422
    assert resp.json()["detail"] == route._FIELD_ADVICE["corp_id"]
    assert pool.rows == {}


@pytest.mark.parametrize(
    "location_id",
    [True, False, "1", 1.0, 1.5, 0, -3, 2_147_483_648, None, [1], {"id": 1}],
)
def test_a_location_that_is_not_a_positive_whole_number_is_refused(
    client: TestClient, pool: RoutePool, location_id: Any
) -> None:
    resp = _put(client, location_id=location_id)
    assert resp.status_code == 422
    assert resp.json()["detail"] == route._FIELD_ADVICE["location_id"]
    assert pool.rows == {}


@pytest.mark.parametrize("location_id", [1, 2_147_483_647])
def test_the_location_bounds_include_both_ends(
    client: TestClient, pool: RoutePool, location_id: int
) -> None:
    assert _put(client, location_id=location_id).status_code == 200
    assert pool.rows[COMPANY]["location_id"] == location_id


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"username": _DROP}, "username"),
        ({"username": "   "}, "username"),
        ({"username": "u" * 257}, "username"),
        ({"password": _DROP}, "password"),
        ({"password": "   "}, "password"),
        ({"password": "p" * 1025}, "password"),
        ({"partner_token": "line\nbreak"}, "partner_token"),
        ({"partner_token": 42}, "partner_token"),
        ({"corp_id": _DROP}, "corp_id"),
    ],
)
def test_each_refusal_names_its_field_and_never_echoes_a_value(
    client: TestClient, pool: RoutePool, change: dict[str, Any], field: str
) -> None:
    """FastAPI's own 422 answers ``"input": {...the whole body...}``, the
    password with it. This route words its own, from the field's name."""
    resp = _put(client, **{"partner_token": PARTNER, **change})
    assert resp.status_code == 422
    assert resp.json() == {"detail": route._FIELD_ADVICE[field]}
    for secret in (*SECRETS, "line\nbreak", "u" * 257, "p" * 1025):
        assert secret not in resp.text
    assert pool.rows == {}


@pytest.mark.parametrize("raw", [b"", b"not json", b"[1, 2]", b'"text"', b"\xff\xfe"])
def test_a_body_that_is_not_an_object_is_refused_plainly(
    client: TestClient, pool: RoutePool, raw: bytes
) -> None:
    resp = client.put(
        f"{BASE}/credentials",
        headers={**_auth(), "Content-Type": "application/json"},
        content=raw,
    )
    assert resp.status_code == 422
    assert resp.json() == {"detail": route._BODY_UNREADABLE}
    assert pool.rows == {}


def test_the_request_model_masks_every_secret_in_its_repr() -> None:
    model = route.RentManagerCredentialsInput.model_validate(_body(partner_token=PARTNER))
    for text in (repr(model), str(model), str(model.model_dump())):
        assert not any(secret in text for secret in SECRETS)


# ── one PMS per workspace ──────────────────────────────────────────────


def test_a_company_on_another_system_is_refused_with_what_to_do(
    client: TestClient, pool: RoutePool
) -> None:
    pool.insert_raises = asyncpg.exceptions.UniqueViolationError(
        f"company {COMPANY} is already connected to Buildium; one PMS per company "
        "(ADR 0010 a1). Disconnect the existing PMS first."
    )
    resp = _put(client)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "Buildium" in detail and "disconnect Buildium first" in detail
    assert "connect Rent Manager" in detail
    assert pool.rows == {}


def test_resaving_for_a_rent_manager_company_is_an_update(
    client: TestClient, pool: RoutePool
) -> None:
    """The trigger fires on INSERT and on an update OF company_id; the upsert's
    SET list never names company_id, so a re-save is not a second system."""
    assert _put(client).status_code == 200
    statement = next(s for s in pool.log if s.startswith(f"INSERT INTO public.{TABLE}"))
    set_list = statement.split("DO UPDATE SET", 1)[1]
    assert "company_id" not in set_list
    names = re.search(r"\((.*?)\) VALUES", statement).group(1)  # type: ignore[union-attr]
    every = {c.strip() for c in names.split(",")} - {"company_id"}
    assigned = {a.split("=")[0].strip() for a in _split_top(set_list)}
    # Re-pointed 2026-10-05: ``capabilities`` is also reset on every save (the
    # writes Rent Manager refused under the old user, ``pms.refusals``), and
    # every inserted column is still assigned.
    assert assigned == every | {"capabilities"}, (
        "a re-save that keeps an old column keeps an old secret"
    )


def test_saving_again_clears_the_remembered_refusals(client: TestClient, pool: RoutePool) -> None:
    """A write Rent Manager refused (a 403) is remembered for a day, and the
    remembered reply says to save the connection again: the save must clear it."""
    assert _put(client).status_code == 200
    statement = next(s for s in pool.log if s.startswith(f"INSERT INTO public.{TABLE}"))
    set_list = _split_top(statement.split("DO UPDATE SET", 1)[1])
    assert "capabilities = '{}'::jsonb" in [" ".join(a.split()) for a in set_list]


def test_the_save_fills_every_column_the_table_requires(
    client: TestClient, pool: RoutePool
) -> None:
    """Both sides (rule 15): the INSERT the save sends, against migration
    0107's columns and its NOT NULLs."""
    assert _put(client).status_code == 200
    statement = next(s for s in pool.log if s.startswith(f"INSERT INTO public.{TABLE} "))
    names = re.search(r"\((.*?)\) VALUES", statement)
    assert names, statement
    named = {c.strip() for c in names.group(1).split(",")}
    required = {c for c, needed in COLUMNS.items() if needed}
    assert required <= named, required - named
    assert named <= set(COLUMNS), named - set(COLUMNS)


# ── the switch (gotcha 120) ────────────────────────────────────────────


def test_while_switched_off_nothing_is_saved_tested_or_synced(
    dark_client: TestClient, pool: RoutePool, vendor: FakeRentManager
) -> None:
    resp = _put(dark_client)
    assert resp.status_code == 503
    assert "not enabled" in resp.json()["detail"] and "team@occupella.com" in resp.json()["detail"]
    assert pool.rows == {}, "a dark save would block every other system for nothing"
    pool.rows[COMPANY] = {c: None for c in COLUMNS} | {"company_id": COMPANY}
    assert dark_client.post(f"{BASE}/test", headers=_auth()).status_code == 503
    assert dark_client.post(f"{BASE}/sync", headers=_auth()).status_code == 503
    assert vendor.requests == []
    assert sync_cycle.sync_status(PMS, COMPANY) is None


def test_while_switched_off_the_read_the_poll_and_the_disconnect_still_work(
    dark_client: TestClient, pool: RoutePool
) -> None:
    """Keys saved before the switch went off must stay visible and removable:
    while the row exists, every other system's connect is refused."""
    pool.rows[COMPANY] = {c: None for c in COLUMNS} | {
        "company_id": COMPANY,
        "corp_id": CORP,
        "location_id": LOCATION,
        "username_last4": "user",
    }
    body = _get(dark_client)
    assert body["configured"] is True and body["connector_enabled"] is False
    assert dark_client.get(f"{BASE}/sync", headers=_auth()).status_code == 200
    resp = dark_client.delete(f"{BASE}/credentials", headers=_auth())
    assert resp.status_code == 200 and resp.json()["configured"] is False
    assert pool.rows == {} and pool.purged


def test_the_save_checks_the_role_and_the_switch_before_reading_the_body(
    pool: RoutePool,
) -> None:
    pool.role = "member"
    for c in _client(_settings(), pool):
        assert c.put(f"{BASE}/credentials", headers=_auth(), content=b"junk").status_code == 403
    pool.role = "admin"
    for c in _client(_settings(enabled=False), pool):
        assert c.put(f"{BASE}/credentials", headers=_auth(), content=b"junk").status_code == 503


# ── the admin floor ────────────────────────────────────────────────────

_EVERY_ROUTE = [
    ("GET", f"{BASE}/credentials"),
    ("PUT", f"{BASE}/credentials"),
    ("DELETE", f"{BASE}/credentials"),
    ("POST", f"{BASE}/test"),
    ("POST", f"{BASE}/sync"),
    ("GET", f"{BASE}/sync"),
]


def test_the_role_floor_covers_every_route() -> None:
    served = {
        (method, "/api/v1" + r.path)
        for r in route.router.routes
        for method in getattr(r, "methods", set())
    }
    assert served == set(_EVERY_ROUTE)


def test_every_route_spends_the_connectors_rate_limit_first() -> None:
    """Every route's first statement awaits ``connectors_limiter.check_by_user``:
    a sync, a test and a save all reach Rent Manager or the database."""
    for r in route.router.routes:
        tree = ast.parse(inspect.getsource(r.endpoint))  # type: ignore[attr-defined]
        (func,) = [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)]
        body = [
            n
            for n in func.body
            if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))
        ]
        first = body[0]
        assert isinstance(first, ast.Expr) and isinstance(first.value, ast.Await), func.name
        call = first.value.value
        assert isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute), func.name
        assert call.func.attr == "check_by_user", func.name
        assert isinstance(call.func.value, ast.Name) and call.func.value.id == "connectors_limiter"


@pytest.mark.parametrize("role", ["member", None])
@pytest.mark.parametrize(("method", "path"), _EVERY_ROUTE)
def test_every_route_needs_an_admin_and_fails_closed(
    client: TestClient,
    pool: RoutePool,
    vendor: FakeRentManager,
    role: str | None,
    method: str,
    path: str,
) -> None:
    """Reads included, and an unverifiable role (no membership row) refused."""
    pool.rows[COMPANY] = {c: None for c in COLUMNS} | {"company_id": COMPANY, "corp_id": CORP}
    pool.role = role
    resp = client.request(method, path, headers=_auth(), json=_body() if method == "PUT" else None)
    assert resp.status_code == 403
    assert "Ask an admin" in resp.json()["detail"]
    assert pool.rows[COMPANY]["corp_id"] == CORP and pool.purged == []
    assert vendor.requests == []


# ── /test ──────────────────────────────────────────────────────────────


def test_a_passed_test_counts_from_the_header_stamps_and_starts_the_first_sync(
    client: TestClient,
    pool: RoutePool,
    vendor: FakeRentManager,
    kicks: list[tuple[Any, ...]],
) -> None:
    vendor.data["properties"] = [
        {**portfolio()["properties"][0], "PropertyID": 880000100 + i} for i in range(5)
    ]
    _put(client)
    body = _test(client)
    assert body == {"ok": True, "properties_count": 5, "error": None}
    assert pool.rows[COMPANY]["last_tested_at"] is not None
    assert kicks == [(pool, PMS, COMPANY)]
    # ONE page of one row: the count is Rent Manager's, never the rows read.
    (read,) = vendor.to("/Properties")
    assert read.url.params["pageSize"] == "1" and read.url.params["pageNumber"] == "1"
    assert read.url.params["fields"] == RESOURCES["properties"].query()["fields"]
    assert [r.url.path for r in vendor.requests] == [
        "/Authentication/AuthorizeUser",
        "/Locations/CurrentLocation",
        "/Properties",
    ]


@pytest.mark.parametrize(
    "answer",
    [
        lambda _r: json_response(200, portfolio()["properties"][:1]),
        lambda _r: json_response(200, [], {"X-Total-Results": "many"}),
        lambda _r: httpx.Response(404),
        lambda _r: httpx.Response(204),
    ],
    ids=["no total header", "a total that is not a number", "an empty 404", "an empty 204"],
)
def test_a_count_rent_manager_did_not_state_is_unknown_never_zero(
    client: TestClient,
    pool: RoutePool,
    vendor: FakeRentManager,
    kicks: list[tuple[Any, ...]],
    answer: Callable[[httpx.Request], httpx.Response],
) -> None:
    vendor.overrides["/Properties"] = answer
    _put(client)
    assert _test(client) == {"ok": True, "properties_count": None, "error": None}
    assert pool.rows[COMPANY]["last_tested_at"] is not None and len(kicks) == 1


def test_another_location_is_refused_naming_both_and_what_to_change(
    client: TestClient,
    pool: RoutePool,
    vendor: FakeRentManager,
    kicks: list[tuple[Any, ...]],
) -> None:
    vendor.data["current_location"] = {"LocationID": 2, "Name": "Synthetic Annex"}
    _put(client)
    body = _test(client)
    assert body["ok"] is False and body["properties_count"] is None
    assert "location 2, not location 1" in body["error"]
    assert "change the location ID to 2" in body["error"]
    assert "access to location 1" in body["error"]
    assert pool.rows[COMPANY]["last_tested_at"] is None and kicks == []
    assert vendor.to("/Properties") == [], "nothing is read from a location this is not"


def test_a_location_that_cannot_be_confirmed_is_refused(
    client: TestClient,
    pool: RoutePool,
    vendor: FakeRentManager,
    kicks: list[tuple[Any, ...]],
) -> None:
    vendor.data["current_location"] = {"Name": "no id"}
    _put(client)
    body = _test(client)
    assert body["ok"] is False and "can't confirm it reads location 1" in body["error"]
    assert "team@occupella.com" in body["error"]
    assert pool.rows[COMPANY]["last_tested_at"] is None and kicks == []


def test_a_401_and_a_403_give_opposite_instructions(
    client: TestClient, vendor: FakeRentManager, kicks: list[tuple[Any, ...]]
) -> None:
    """Gotcha 102: a refused password is fixed by saving it again; a role is
    not, and telling that customer to reconnect sends them to redo what works."""
    _put(client)
    vendor.overrides["/Authentication/AuthorizeUser"] = lambda _r: json_response(401, {})
    refused = _test(client)["error"]
    _put(client)  # a re-save: a fresh login, not the broken mark
    vendor.overrides.pop("/Authentication/AuthorizeUser")
    vendor.overrides["/Properties"] = lambda _r: json_response(403, {})
    forbidden = _test(client)["error"]

    assert refused != forbidden
    assert "save them here again" in refused and "role" not in refused
    assert "role" in forbidden and "do not need changing" in forbidden
    assert "save" not in forbidden.lower() and "reconnect" not in forbidden.lower()
    assert kicks == []


def test_a_refused_login_is_not_sent_again_until_the_details_are_saved_again(
    client: TestClient, vendor: FakeRentManager, kicks: list[tuple[Any, ...]]
) -> None:
    """A second test of the same refused details asks Rent Manager nothing
    (terms rule 2); saving them again is a new login (``version_of``)."""
    _put(client)
    vendor.overrides["/Authentication/AuthorizeUser"] = lambda _r: json_response(401, {})
    first, second = _test(client), _test(client)
    assert first == second and first["ok"] is False
    assert len(vendor.to("/Authentication/AuthorizeUser")) == 1
    vendor.overrides.pop("/Authentication/AuthorizeUser")
    _put(client)
    assert _test(client)["ok"] is True
    assert len(vendor.to("/Authentication/AuthorizeUser")) == 2


def test_every_classified_failure_has_its_own_advice() -> None:
    """A missing entry would fall back to the undocumented wording, which tells
    somebody with a refused password that nothing is wrong."""
    assert set(route._TEST_ADVICE) == set(Failure) - {Failure.UNDOCUMENTED}
    assert route._TEST_ADVICE_UNDOCUMENTED not in route._TEST_ADVICE.values()
    for advice in [*route._TEST_ADVICE.values(), route._TEST_ADVICE_UNDOCUMENTED]:
        assert "Rentvine" not in advice and "Buildium" not in advice


def test_an_unrecognised_answer_names_what_to_do_not_try_again_now(
    client: TestClient, vendor: FakeRentManager, kicks: list[tuple[Any, ...]]
) -> None:
    _put(client)
    vendor.overrides["/Properties"] = lambda _r: json_response(500, {"Message": "boom"})
    error = _test(client)["error"]
    assert error == route._TEST_ADVICE_UNDOCUMENTED
    assert "test again later" in error and "team@occupella.com" in error


def test_an_address_that_cannot_be_reached_points_at_the_corp_id(
    client: TestClient, vendor: FakeRentManager, kicks: list[tuple[Any, ...]]
) -> None:
    def _no_such_host(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("name resolution failed", request=request)

    vendor.overrides["/Authentication/AuthorizeUser"] = _no_such_host
    _put(client)
    error = _test(client)["error"]
    assert f"{CORP}.api.rentmanager.com" in error and "Check the corp ID" in error


def test_no_saved_connection_says_so_without_calling_rent_manager(
    client: TestClient, vendor: FakeRentManager
) -> None:
    assert _test(client) == {"ok": False, "properties_count": None, "error": route._NOTHING_SAVED}
    assert vendor.requests == []


def test_details_that_no_longer_decrypt_ask_to_be_saved_again(
    client: TestClient, pool: RoutePool, vendor: FakeRentManager, caplog: pytest.LogCaptureFixture
) -> None:
    _put(client)
    pool.rows[COMPANY]["encrypted_password"] = "not-a-fernet-token"
    with caplog.at_level(logging.DEBUG):
        body = _test(client)
    assert body["ok"] is False and body["error"] == route._UNREADABLE
    assert vendor.requests == []
    assert not any(secret in caplog.text for secret in SECRETS)


def test_a_crash_never_reaches_the_customer_verbatim(
    client: TestClient, vendor: FakeRentManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    _put(client)
    monkeypatch.setattr(
        rm_client, "count_of", AsyncMock(side_effect=RuntimeError("pool exhausted at 0x7f3a"))
    )
    body = _test(client)
    assert body["ok"] is False and body["error"] == route._TEST_CRASHED
    assert "0x7f3a" not in body["error"] and "team@occupella.com" in body["error"]


def test_details_replaced_during_the_test_are_not_stamped(
    client: TestClient,
    pool: RoutePool,
    vendor: FakeRentManager,
    kicks: list[tuple[Any, ...]],
) -> None:
    """The stamp says THESE details passed. A save that landed while the test
    ran has passed nothing, so nothing is stamped and no sync starts."""
    _put(client)

    def _resaved_meanwhile(request: httpx.Request) -> httpx.Response:
        pool.rows[COMPANY]["encrypted_password"] = "saved-while-testing"
        return FakeRentManager.page(request, portfolio()["properties"])

    vendor.overrides["/Properties"] = _resaved_meanwhile
    body = _test(client)
    assert body == {"ok": False, "properties_count": None, "error": route._CHANGED_DURING_TEST}
    assert pool.rows[COMPANY]["last_tested_at"] is None and kicks == []


def test_a_connection_without_a_partner_token_is_stamped(
    client: TestClient, pool: RoutePool, vendor: FakeRentManager, kicks: list[tuple[Any, ...]]
) -> None:
    """``= $6`` against a NULL token never matches in SQL; the stamp must
    compare it with IS NOT DISTINCT FROM, or no such connection is ever
    marked tested."""
    _put(client)
    assert pool.rows[COMPANY]["encrypted_partner_token"] is None
    assert _test(client)["ok"] is True
    assert pool.rows[COMPANY]["last_tested_at"] is not None


def test_the_partner_token_rides_the_test_and_never_comes_back(
    client: TestClient, vendor: FakeRentManager, kicks: list[tuple[Any, ...]]
) -> None:
    _put(client, partner_token=PARTNER)
    body = _test(client)
    assert body["ok"] is True
    assert all(r.headers.get(rm_client.PARTNER_HEADER) == PARTNER for r in vendor.requests)
    assert PARTNER not in str(body)


# ── disconnect ─────────────────────────────────────────────────────────


def test_disconnect_deletes_the_row_then_purges_every_mirror_table(
    client: TestClient, pool: RoutePool
) -> None:
    from helixis.buildium.purge import PURGE_TABLES

    _put(client)
    pool.purge_counts = {"helixis_properties": 3, "helixis_leases": 2}
    resp = client.delete(f"{BASE}/credentials", headers=_auth())
    assert resp.status_code == 200
    assert resp.json() == {
        "configured": False,
        "corp_id": None,
        "location_id": None,
        "username_last4": None,
        "partner_token_saved": False,
        "last_tested_at": None,
        "purged_rows": 5,
        "connector_enabled": None,
    }
    assert pool.rows == {} and pool.purged == list(PURGE_TABLES)
    # The row goes FIRST: a pass that starts after it finds no details.
    row_gone = pool.log.index(f"DELETE FROM public.{TABLE} WHERE company_id = $1::uuid")
    assert row_gone < pool.log.index("PURGE helixis_properties")


def test_the_purge_delegates_and_names_no_table_and_no_pms() -> None:
    """One table list (gotcha 25) and company_id alone (gotcha 112)."""
    from helixis.buildium.purge import PURGE_TABLES
    from helixis.rentmanager import purge as module

    tree = ast.parse(Path(inspect.getfile(module)).read_text(encoding="utf-8"))
    called = {
        n.func.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "purge_buildium_mirror" in called
    # Docstrings explain the rule by quoting the defect (rule 17): only code counts.
    docstrings = {
        id(n.value)
        for n in ast.walk(tree)
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
    }
    literals = {
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings
    }
    assert literals, "the scan found no code strings at all; re-point it"
    assert not literals & set(PURGE_TABLES)
    assert not [lit for lit in literals if "delete" in lit.lower() or "pms" in lit.lower()]


def test_saving_again_and_disconnecting_drop_the_old_session_token(
    client: TestClient, pool: RoutePool, vendor: FakeRentManager, kicks: list[tuple[Any, ...]]
) -> None:
    """The session token is a secret (terms rule 8): it does not outlive the
    details it was issued for, in this process."""
    _put(client)
    assert _test(client)["ok"] is True
    first = rm_client.token_cache_key(COMPANY, LOCATION, version_of(pool.rows[COMPANY]))
    assert pms_auth._tokens.get(first) == TOKEN
    _put(client)
    assert first not in pms_auth._tokens
    assert _test(client)["ok"] is True
    second = rm_client.token_cache_key(COMPANY, LOCATION, version_of(pool.rows[COMPANY]))
    assert second != first and pms_auth._tokens.get(second) == TOKEN
    client.delete(f"{BASE}/credentials", headers=_auth())
    assert second not in pms_auth._tokens


# ── no secret anywhere ─────────────────────────────────────────────────


def test_no_secret_reaches_an_answer_or_a_log_across_the_whole_flow(
    client: TestClient,
    pool: RoutePool,
    vendor: FakeRentManager,
    kicks: list[tuple[Any, ...]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    answers: list[str] = []
    with caplog.at_level(logging.DEBUG):
        answers.append(_put(client, partner_token=PARTNER).text)
        answers.append(client.get(f"{BASE}/credentials", headers=_auth()).text)
        answers.append(client.post(f"{BASE}/test", headers=_auth()).text)
        vendor.overrides["/Authentication/AuthorizeUser"] = lambda _r: json_response(
            401, {"DeveloperMessage": f"bad login for {USERNAME}"}
        )
        answers.append(_put(client, partner_token=PARTNER).text)
        answers.append(client.post(f"{BASE}/test", headers=_auth()).text)
        answers.append(_put(client, password=_DROP).text)
        answers.append(client.delete(f"{BASE}/credentials", headers=_auth()).text)
    for text in [*answers, caplog.text]:
        for secret in SECRETS:
            assert secret not in text


# ── Sync now ───────────────────────────────────────────────────────────


@pytest.fixture
def held(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[dict[str, Any], threading.Event]]:
    """The shared pass (``sync_cycle.sync_one``) held until the test lets it
    end, answering ``state["result"]``. The real ``start_now`` runs it."""
    release = threading.Event()
    state: dict[str, Any] = {"result": None, "calls": 0}

    async def _one(_pool: Any, pms: str, company_id: str) -> SyncResult | None:
        assert (pms, company_id) == (PMS, COMPANY)
        state["calls"] += 1
        await asyncio.to_thread(release.wait, 10)
        return state["result"]

    monkeypatch.setattr(sync_cycle, "sync_one", _one)
    yield state, release
    release.set()


def _ended_here(timeout: float = 10.0) -> sync_cycle.SyncRun:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = sync_cycle.sync_status(PMS, COMPANY)
        if run is not None and run.finished_at is not None:
            return run
        time.sleep(0.01)
    raise AssertionError("the sync never ended")


def _sync(c: TestClient) -> dict[str, Any]:
    return c.post(f"{BASE}/sync", headers=_auth()).json()


def _status(c: TestClient) -> dict[str, Any]:
    return c.get(f"{BASE}/sync", headers=_auth()).json()


def test_sync_answers_at_once_then_reports_the_rows_by_stage(
    client: TestClient, held: tuple[dict[str, Any], threading.Event]
) -> None:
    state, release = held
    _put(client)
    state["result"] = SyncResult(
        company_id=COMPANY,
        pms=PMS,
        rows_by_table={
            "helixis_properties": 2,
            "helixis_units": 3,
            "helixis_leases": 5,
            "helixis_lease_tenants": 4,
            "helixis_lease_balances": 1,
            "helixis_turnovers": 2,
        },
    )
    started = _sync(client)
    assert started["ok"] is True and started["running"] is True and started["started_at"]
    during = _status(client)
    assert during["running"] is True and during["started_at"] == started["started_at"]
    assert during["rows"] == {}

    release.set()
    _ended_here()
    after = _status(client)
    assert after == {
        "ok": True,
        # Leases count leases, not their tenant links; turnovers are opened,
        # not synced, so neither is a count here.
        "rows": {"properties": 2, "units": 3, "leases": 5, "balances": 1},
        "stale": [],
        "error": None,
        "running": False,
        "started_at": started["started_at"],
    }
    assert state["calls"] == 1


def test_a_stale_stage_is_named_and_never_counted(
    client: TestClient, held: tuple[dict[str, Any], threading.Event]
) -> None:
    state, release = held
    _put(client)
    state["result"] = SyncResult(
        company_id=COMPANY,
        pms=PMS,
        rows_by_table={"helixis_properties": 2, "helixis_units": 0, "helixis_leases": 5},
        failed={
            "helixis_units": "403 (permission): /Units",
            "helixis_lease_tenants": "write failed: DataError",
        },
    )
    _sync(client)
    release.set()
    _ended_here()
    after = _status(client)
    assert after["ok"] is False and after["stale"] == ["leases", "units"]
    assert after["rows"] == {"properties": 2}, "a stale stage never reads as 0 rows"
    assert "leases, units" in after["error"] and "last sync that worked" in after["error"]
    assert "403" not in after["error"] and "DataError" not in after["error"]
    assert "Sync now" in after["error"]


def test_why_a_pass_read_everything_is_not_a_stale_table(
    client: TestClient, held: tuple[dict[str, Any], threading.Event]
) -> None:
    """Rule 15: the pass reports why it read in full in ``failed``
    (``sync.INCREMENTAL_OFF``); it read every table, so none is stale."""
    state, release = held
    _put(client)
    state["result"] = SyncResult(
        company_id=COMPANY,
        pms=PMS,
        rows_by_table={"helixis_properties": 2},
        failed={rm_sync.INCREMENTAL_OFF: "Rent Manager returned records for a filter"},
    )
    _sync(client)
    release.set()
    _ended_here()
    after = _status(client)
    assert after["ok"] is True and after["stale"] == [] and after["error"] is None
    assert after["rows"] == {"properties": 2}


def test_a_pass_that_wrote_nothing_says_so_and_what_to_do(
    client: TestClient, held: tuple[dict[str, Any], threading.Event]
) -> None:
    state, release = held
    _put(client)
    state["result"] = rm_sync.refused_pass(COMPANY, "location mismatch")
    _sync(client)
    release.set()
    _ended_here()
    after = _status(client)
    assert after["ok"] is False and after["rows"] == {}
    assert "turnovers" in after["stale"] and "leases" in after["stale"]
    assert after["error"] == route._SYNC_NOTHING_CAME
    assert "Test connection" in after["error"]


def test_a_sync_that_did_not_finish_names_what_to_do(
    client: TestClient, held: tuple[dict[str, Any], threading.Event]
) -> None:
    _state, release = held
    _put(client)
    _sync(client)
    release.set()
    _ended_here()
    after = _status(client)
    assert after["ok"] is False and after["running"] is False
    assert "Test connection" in after["error"] and "team@occupella.com" in after["error"]


def test_a_click_during_a_sync_waits_on_that_sync(
    client: TestClient, held: tuple[dict[str, Any], threading.Event]
) -> None:
    state, release = held
    _put(client)
    first, second = _sync(client), _sync(client)
    assert second["running"] is True and second["started_at"] == first["started_at"]
    release.set()
    _ended_here()
    assert state["calls"] == 1, "a second click started a second crawl"


def test_a_click_during_the_sync_a_passed_test_started_joins_it(
    client: TestClient,
    vendor: FakeRentManager,
    held: tuple[dict[str, Any], threading.Event],
) -> None:
    state, release = held
    _put(client)
    assert _test(client)["ok"] is True
    run = sync_cycle.sync_status(PMS, COMPANY)
    assert run is not None and run.finished_at is None, "a passed test starts the first sync"
    assert _sync(client)["started_at"] == run.started_at.isoformat()
    release.set()
    _ended_here()
    assert state["calls"] == 1


def test_sync_without_a_saved_connection_does_not_start(
    client: TestClient, held: tuple[dict[str, Any], threading.Event]
) -> None:
    state, _release = held
    body = _sync(client)
    assert body["ok"] is False and body["error"] == route._NOTHING_SAVED
    assert body["running"] is False and state["calls"] == 0
    assert sync_cycle.sync_status(PMS, COMPANY) is None


def test_sync_now_asks_for_a_full_pass_before_the_run_plans(
    client: TestClient,
    pool: RoutePool,
    held: tuple[dict[str, Any], threading.Event],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The page shows the run's row counts, and only a full pass's are totals:
    a pass by change counts what changed, and "0 properties" would read as a
    portfolio with none (rule 14). The ask is stored before the run starts,
    so the run that plans next reads it, in whichever process runs it."""
    state, release = held
    _put(client)
    seen: dict[str, Any] = {}

    async def _one(_pool: Any, pms: str, company_id: str) -> SyncResult | None:
        seen["asked"] = pool.watermarks.get(rm_sync.FULL_ASKED)
        return state["result"]

    monkeypatch.setattr(sync_cycle, "sync_one", _one)
    _sync(client)
    release.set()
    _ended_here()
    assert seen["asked"] is not None, "the run began before the full pass was asked for"


def test_sync_now_that_cannot_ask_for_a_full_pass_starts_nothing(
    client: TestClient, pool: RoutePool, held: tuple[dict[str, Any], threading.Event]
) -> None:
    state, _release = held
    _put(client)
    pool.watermark_raises = RuntimeError("pool exhausted")
    body = _sync(client)
    assert body["ok"] is False and body["error"] == route._SYNC_NOT_STARTED
    assert "Click Sync now again" in body["error"]
    assert state["calls"] == 0 and sync_cycle.sync_status(PMS, COMPANY) is None


def test_a_passed_test_asks_for_a_full_pass_before_the_first_sync(
    client: TestClient,
    pool: RoutePool,
    vendor: FakeRentManager,
    held: tuple[dict[str, Any], threading.Event],
) -> None:
    state, release = held
    _put(client)
    assert _test(client)["ok"] is True
    assert rm_sync.FULL_ASKED in pool.watermarks
    release.set()
    _ended_here()
    assert state["calls"] == 1


def test_a_passed_test_that_cannot_ask_for_a_full_pass_starts_no_sync(
    client: TestClient,
    pool: RoutePool,
    vendor: FakeRentManager,
    held: tuple[dict[str, Any], threading.Event],
) -> None:
    state, _release = held
    _put(client)
    pool.watermark_raises = RuntimeError("pool exhausted")
    assert _test(client)["ok"] is True, "the test itself passed"
    assert state["calls"] == 0 and sync_cycle.sync_status(PMS, COMPANY) is None


def test_a_sync_that_cannot_start_reads_as_ended(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``start_now`` frees the slot when no task can be made and hands back a
    run that ended without a result (``test_pms_sync_cycle`` pins that). The
    answer must say it ended, or the page polls a run that never ends."""
    _put(client)

    def _could_not_start(_pool: Any, pms: str, company_id: str) -> sync_cycle.SyncRun:
        run = sync_cycle.SyncRun(pms=pms, started_at=datetime.now(UTC))
        sync_cycle.release(company_id, run)
        return run

    monkeypatch.setattr(sync_cycle, "start_now", _could_not_start)
    body = _sync(client)
    assert body["ok"] is False and body["running"] is False
    assert "team@occupella.com" in body["error"]
    assert _status(client)["running"] is False


def test_the_status_before_any_sync_says_so(client: TestClient) -> None:
    body = _status(client)
    assert body["ok"] is False and body["running"] is False and body["started_at"] is None
    assert "Sync now" in body["error"]


def test_the_stage_names_are_the_ones_the_page_knows() -> None:
    """Every stage a sync answer can name has a noun on the page."""
    panel = (REPO / "frontend/src/components/connectors/RentManagerApiDetail.tsx").read_text(
        encoding="utf-8"
    )
    block = re.search(r"const STAGE_NOUNS[^=]*= \{(.*?)\n\};", panel, re.S)
    assert block, "RentManagerApiDetail.tsx no longer declares STAGE_NOUNS"
    nouns = set(re.findall(r"^  (\w+):", block.group(1), re.M))
    assert set(route._COUNTED.values()) <= nouns
    assert set(route._STAGE.values()) - set(route._COUNTED.values()) == {"turnovers"}
