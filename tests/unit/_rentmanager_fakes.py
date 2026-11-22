"""Shared fakes for the Rent Manager tests.

* :func:`portfolio`: a fresh copy of the synthetic fixture
  (``tests/fixtures/rentmanager/portfolio.json``).
* :class:`FakeRentManager`: answers like Rent Manager behind an
  ``httpx.MockTransport``: the login (a quoted JSON string behind a BOM), the
  current location, each collection paged with ``X-Total-Results`` (an empty
  one as a 404, as the Overview documents), and one record of a collection
  read by its id (a 404 when there is none). It ignores ``filters`` and
  ``fields``, as Rent Manager ignores a misspelt parameter (the probe's tests
  rely on that); ``honours_query`` makes a collection read apply its
  ``filters`` and keep only its ``fields``, the way the design expects Rent
  Manager to (RM-6, RM-7). Any path can be overridden.
  :func:`serve` routes the connector's own client to it.
* :class:`FakePool`: records every statement and checks each against what
  asyncpg accepts (``_pg_bind_contract``), so a wrong Python type fails here
  as it would in production. It keeps the watermark table
  (``pms.watermarks``) the way its upsert does: never backwards.

Credentials are built at runtime: a key-shaped literal trips the repo's
secret scan (Rentvine's tests hit that).
"""

from __future__ import annotations

import contextlib
import copy
import json
import re
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
from cryptography.fernet import Fernet

from helixis.pms.watermarks import TABLE as WATERMARKS
from helixis.rentmanager.resources import RESOURCES
from helixis.security import CredentialCipher
from helixis.settings import Settings
from tests.unit._pg_bind_contract import reject_unbindable, reject_untyped_mirror_bind

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "rentmanager" / "portfolio.json"

COMPANY = "00000000-0000-4000-8000-0000000000c1"
#: The day the tests read the fixture on (its header says what that means).
TODAY = date(2026, 10, 5)
CORP = "synthcorp"
LOCATION = 1

DATA_KEY = Fernet.generate_key().decode()
USERNAME = "-".join(("rm", "synthetic", "user"))
PASSWORD = "-".join(("rm", "synthetic", "password", "value"))
PARTNER = "-".join(("rm", "synthetic", "partner", "token"))
TOKEN = "-".join(("rm", "synthetic", "session", "token"))
SECRETS = (USERNAME, PASSWORD, PARTNER, TOKEN)

#: Collection path -> the fixture's key for it.
PATHS = {resource.path: name for name, resource in RESOURCES.items()}
#: ``/<Collection>/<id>``: one record read by its id.
_BY_ID = re.compile(r"^(/[A-Za-z]+)/(\d+)$")


def portfolio() -> dict[str, Any]:
    """A fresh, mutable copy of the synthetic portfolio."""
    return copy.deepcopy(json.loads(FIXTURE.read_text(encoding="utf-8")))


def app_settings(*, enabled: bool = True, data_key: str | None = DATA_KEY) -> Settings:
    base = Settings()
    return base.model_copy(
        update={
            "data_key": data_key,
            "rentmanager": base.rentmanager.model_copy(update={"enabled": enabled}),
        }
    )


def credentials_row(
    settings: Settings,
    *,
    corp_id: Any = CORP,
    location_id: Any = LOCATION,
    partner: str | None = None,
) -> dict[str, Any]:
    """A row as ``company_rentmanager_credentials`` holds it."""
    cipher = CredentialCipher(settings)
    return {
        "company_id": COMPANY,
        "corp_id": corp_id,
        "location_id": location_id,
        "encrypted_username": cipher.encrypt(USERNAME),
        "encrypted_password": cipher.encrypt(PASSWORD),
        "encrypted_partner_token": cipher.encrypt(partner) if partner else None,
    }


def json_response(status: int, body: Any, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, content=json.dumps(body).encode(), headers=headers or {})


Handler = Callable[[httpx.Request], httpx.Response]


class FakeRentManager:
    """A synthetic Rent Manager. Every request is kept in :attr:`requests`."""

    def __init__(
        self,
        data: dict[str, Any] | None = None,
        *,
        token: str = TOKEN,
        overrides: dict[str, Handler] | None = None,
        honours_query: bool = False,
    ) -> None:
        self.data = data if data is not None else portfolio()
        self.token = token
        self.overrides = dict(overrides or {})
        self.honours_query = honours_query
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path in self.overrides:
            return self.overrides[path](request)
        if path == "/Authentication/AuthorizeUser":
            # As the clients saw it (RM-1, RM-16): a BOM, then a quoted JSON string.
            return httpx.Response(200, content=b"\xef\xbb\xbf" + json.dumps(self.token).encode())
        if path == "/Locations/CurrentLocation":
            return json_response(200, self.data["current_location"])
        one = _BY_ID.match(path)
        if one and request.method == "GET" and one.group(1) in PATHS:
            return self.record(PATHS[one.group(1)], int(one.group(2)))
        rows = self.data[PATHS[path]]
        return self.page(request, self.selected(request, rows) if self.honours_query else rows)

    @staticmethod
    def selected(request: httpx.Request, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The rows a collection read asks for: its ``filters`` applied, and
        only its ``fields`` kept."""
        wanted = request.url.params.get("filters")
        if wanted:
            rows = [r for r in rows if all(_matches(r, f) for f in wanted.split(";"))]
        fields = request.url.params.get("fields")
        if fields:
            keep = {name.split(".")[0] for name in fields.split(",")}
            rows = [{k: v for k, v in r.items() if k in keep} for r in rows]
        return rows

    def record(self, name: str, ident: int) -> httpx.Response:
        """One record by its id, or a 404 ("not found or not permitted")."""
        key = RESOURCES[name].key
        for row in self.data[name]:
            if row.get(key) == ident:
                return json_response(200, row)
        return httpx.Response(404, content=b"")

    @staticmethod
    def page(request: httpx.Request, rows: list[dict[str, Any]]) -> httpx.Response:
        if not rows:
            return httpx.Response(404, content=b"")
        number = int(request.url.params.get("pageNumber", "1"))
        size = int(request.url.params.get("pageSize", "1000"))
        chunk = rows[(number - 1) * size : number * size]
        return json_response(200, chunk, {"X-Total-Results": str(len(rows))})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def to(self, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == path]

    @property
    def collection_reads(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path in PATHS]

    def to_filtered(self, spec: str) -> list[httpx.Request]:
        """The requests that carried ``filters=spec``."""
        return [r for r in self.requests if r.url.params.get("filters") == spec]


def _instant(text: str) -> datetime:
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _comparable(have: Any, value: str) -> tuple[Any, Any]:
    """A stored value and a filter's, as like for like: a time as an instant,
    a number as a number, any other text as text."""
    if isinstance(have, str):
        try:
            return _instant(have), _instant(value)
        except ValueError:
            return have, value
    return have, type(have)(value)


def _matches(row: dict[str, Any], spec: str) -> bool:
    """``Field,op,value`` (eq, ge, gt, le, lt) against one row. A row
    without the field matches nothing, as a database comparison with NULL."""
    name, op, value = spec.split(",", 2)
    have = row.get(name)
    if have is None:
        return False
    left, right = _comparable(have, value)
    return {
        "eq": left == right,
        "ge": left >= right,
        "gt": left > right,
        "le": left <= right,
        "lt": left < right,
    }[op]


def serve(monkeypatch: Any, server: FakeRentManager) -> FakeRentManager:
    """Route the HTTP client ``PmsHttpClient`` opens for itself to ``server``.

    The connector imports no HTTP library (``test_pms_gated_write``), so there
    is no seam in it to hand a transport through, and none is wanted: this
    drives the production path, its headers included, with nothing reaching
    a network.
    """
    real = httpx.AsyncClient

    def _client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs.setdefault("transport", server.transport())
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _client)
    return server


class _Conn:
    def __init__(self, pool: FakePool) -> None:
        self.pool = pool

    async def execute(self, sql: str, *args: Any) -> str:
        reject_unbindable(sql, args)
        reject_untyped_mirror_bind(sql, args)
        flat = " ".join(sql.split())
        self.pool.log.append((flat, args))
        if flat.startswith(f"INSERT INTO {WATERMARKS} "):
            self.pool.advance(args[0], args[1], args[2])
        return "DELETE 0" if sql.lstrip().upper().startswith("DELETE") else "INSERT 0 1"

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        return await self.pool.fetchrow(sql, *args)

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        self.pool.log.append((" ".join(sql.split()), args))
        return []

    @contextlib.asynccontextmanager
    async def transaction(self) -> Any:
        yield


class FakePool:
    """Records every statement. ``credentials`` is the row the loader reads;
    ``watermarks`` is this company's watermark table, by resource."""

    def __init__(self, credentials: dict[str, Any] | None = None, timezone: str = "UTC") -> None:
        self.credentials = credentials
        self.timezone = timezone
        self.log: list[tuple[str, tuple[Any, ...]]] = []
        self.watermarks: dict[str, datetime] = {}

    def advance(self, company_id: Any, resource: str, to: datetime) -> None:
        """The upsert's ``GREATEST``: a watermark never moves backwards."""
        assert company_id == COMPANY, "a watermark for another company"
        held = self.watermarks.get(resource)
        self.watermarks[resource] = to if held is None else max(held, to)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        flat = " ".join(sql.split())
        self.log.append((flat, args))
        if f"FROM {WATERMARKS} " in flat:
            assert args[0] == COMPANY, "a watermark read for another company"
            return self.watermarks.get(args[1])
        raise AssertionError(f"a value read this stand-in does not model: {flat[:80]}")

    @contextlib.asynccontextmanager
    async def acquire(self) -> Any:
        yield _Conn(self)

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        flat = " ".join(sql.split())
        self.log.append((flat, args))
        if "company_rentmanager_credentials" in flat:
            return self.credentials
        if "FROM public.companies" in flat:
            return {"timezone": self.timezone}
        return None

    async def execute(self, sql: str, *args: Any) -> str:
        return await _Conn(self).execute(sql, *args)

    def statements(self, starts: str) -> list[tuple[str, tuple[Any, ...]]]:
        return [(s, a) for s, a in self.log if s.upper().startswith(starts.upper())]

    def inserts_into(self, table: str) -> list[tuple[str, tuple[Any, ...]]]:
        return self.statements(f"INSERT INTO {table} ")

    def deletes_from(self, table: str) -> list[tuple[str, tuple[Any, ...]]]:
        return self.statements(f"DELETE FROM {table} ")

    @property
    def stamped(self) -> set[str]:
        """The freshness stages this pass stamped."""
        return {a[1] for _s, a in self.inserts_into("helixis_mirror_sync_state")}

    @property
    def mirror_writes(self) -> list[str]:
        """Every write to a mirror table, by statement (sync state is not one)."""
        return [
            s
            for s, _a in self.log
            if s.upper().startswith(("INSERT INTO HELIXIS_", "DELETE FROM HELIXIS_"))
            and "helixis_mirror_sync_state" not in s
            and WATERMARKS not in s
        ]
