"""Test doubles shared by the Propertyware tests.

* :class:`FakePropertyware`: answers the client's GETs from the synthetic
  fixtures, paged by ``offset``/``limit`` with ``X-Total-Count``, the way the
  official spec documents. It applies the change window
  (``lastModifiedDateTimeStart``/``End``, inclusive at both ends) and the
  sort, refusing with a 400 a sort key the list does not declare
  (``_paths.SORTABLE``, from the spec) or a window time it cannot read. It
  can fail a list, lie about a total, and records every request, so a test
  asserts what would have left the process.
* :class:`FakePool`: records every statement and answers like asyncpg would,
  including the bind contract (``tests/unit/_pg_bind_contract.py``): a fake
  that accepts what the driver refuses is an inverted test. It keeps the
  ``pms.watermarks`` rows (never moving one backwards, as the real upsert's
  ``GREATEST`` does) and serves the mirror's units it is given.
"""

from __future__ import annotations

import contextlib
import copy
import json
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from cryptography.fernet import Fernet

from helixis.propertyware import _paths
from helixis.security import CredentialCipher
from helixis.settings import Settings
from tests.unit._pg_bind_contract import reject_unbindable, reject_untyped_mirror_bind

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "propertyware"

COMPANY = "00000000-0000-4000-8000-0000000000b7"
SYSTEM_ID = "990000001"
CLIENT_ID = "pw-client-id-synthetic-0001"
CLIENT_SECRET = "pw-client-secret-synthetic-never-log-me"
TEST_KEY = Fernet.generate_key().decode()
TODAY = date(2026, 10, 5)
API_PREFIX = "/pw/api/rest/v1"

#: The fixture each read serves.
FIXTURE_FOR_PATH: dict[str, str] = {
    "/buildings": "buildings.json",
    "/units": "units.json",
    "/portfolios": "portfolios.json",
    "/contacts": "contacts_tenants.json",
    "/vendors": "vendors.json",
    "/leases": "leases.json",
    "/leases/charges": "lease_charges.json",
    "/workorders": "workorders.json",
    "/bills": "bills.json",
    "/accounting/glaccounts": "glaccounts.json",
}


def fixture_rows(path: str) -> list[dict[str, Any]]:
    """A fresh copy of the rows the fixture for ``path`` holds."""
    data = json.loads((FIXTURES / FIXTURE_FOR_PATH[path]).read_text(encoding="utf-8"))
    return copy.deepcopy(data["rows"])


def all_rows() -> dict[str, list[dict[str, Any]]]:
    return {path: fixture_rows(path) for path in FIXTURE_FOR_PATH}


def settings(*, enabled: bool = True) -> Settings:
    """Settings with the Propertyware switch set and a test data key."""
    base = Settings()
    switches = base.propertyware.model_copy(update={"enabled": enabled})
    return base.model_copy(update={"data_key": TEST_KEY, "propertyware": switches})


#: When :func:`creds_row`'s connection last changed: before every pass a
#: test runs, so only a test that moves it sees "the connection changed".
CONNECTED_AT = datetime(2026, 9, 1, tzinfo=UTC)

#: When a test's first pass starts (``sync._now``), on :data:`TODAY`.
FIRST_PASS = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


class Clock:
    """``sync._now`` for a test: fixed at :data:`FIRST_PASS` until moved."""

    def __init__(self, now: datetime = FIRST_PASS):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> datetime:
        self.now += timedelta(**delta)
        return self.now


def creds_row(
    *,
    company_id: str = COMPANY,
    system_id: str = SYSTEM_ID,
    client_id: str = CLIENT_ID,
    client_secret: str = CLIENT_SECRET,
    updated_at: datetime | None = CONNECTED_AT,
) -> dict[str, Any]:
    """A ``company_propertyware_credentials`` row, encrypted with the test key."""
    cipher = CredentialCipher(settings())
    return {
        "company_id": company_id,
        "system_id": system_id,
        "encrypted_client_id": cipher.encrypt(client_id),
        "encrypted_client_secret": cipher.encrypt(client_secret),
        "updated_at": updated_at,
    }


def stamp(value: Any) -> datetime | None:
    """A ``lastModifiedDateTime`` (or a window time) as an aware time, or
    ``None`` when it is not one."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


_EARLIEST = datetime.min.replace(tzinfo=UTC)


def _by_change(row: Mapping[str, Any]) -> tuple[bool, datetime]:
    when = stamp(row.get("lastModifiedDateTime"))
    return (when is None, when or _EARLIEST)


def _refused(status: int, code: str) -> httpx.Response:
    return httpx.Response(status, json={"errorCode": code, "userMessage": f"synthetic {code}"})


class FakePropertyware:
    """Propertyware's documented list behaviour over the fixtures."""

    def __init__(
        self,
        rows: Mapping[str, list[dict[str, Any]]] | None = None,
        *,
        fail: Mapping[str, int] | None = None,
        totals: Mapping[str, int] | None = None,
    ):
        self.rows = {k: list(v) for k, v in (rows if rows is not None else all_rows()).items()}
        self.fail = dict(fail or {})
        self.totals = dict(totals or {})
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix(API_PREFIX)
        if request.method != "GET" or path not in self.rows:
            return httpx.Response(
                404, json={"errorCode": "NOT_FOUND", "userMessage": "No such path"}
            )
        if path in self.fail:
            status = self.fail[path]
            return httpx.Response(
                status, json={"errorCode": "E", "userMessage": f"synthetic {status}"}
            )
        params = request.url.params
        rows = list(self.rows[path])
        bounds = {}
        for name in ("lastModifiedDateTimeStart", "lastModifiedDateTimeEnd"):
            if name in params:
                bounds[name] = stamp(params[name])
                if bounds[name] is None:
                    return _refused(400, "INVALID_DATE")
        start, end = bounds.get("lastModifiedDateTimeStart"), bounds.get("lastModifiedDateTimeEnd")
        if bounds:
            rows = [
                r
                for r in rows
                if (when := stamp(r.get("lastModifiedDateTime"))) is not None
                and (start is None or when >= start)
                and (end is None or when <= end)
            ]
        if "orderby" in params:
            field, _, direction = params["orderby"].partition(" ")
            if field not in _paths.SORTABLE.get(f"GET {path}", frozenset()) or direction not in (
                "",
                "asc",
            ):
                return _refused(400, "INVALID_SORT")
            if field == "lastmodifieddatetime":
                rows.sort(key=_by_change)
            elif field == "id":
                rows.sort(key=lambda r: (r.get("id") is None, r.get("id") or 0))
        offset = int(params.get("offset", "0"))
        limit = int(params.get("limit", "100"))
        page = rows[offset : offset + limit]
        total = self.totals.get(path, len(rows))
        return httpx.Response(200, json=page, headers={"X-Total-Count": str(total)})

    def query_of(self, path: str) -> list[dict[str, str]]:
        """Each request to ``path``'s query, in order."""
        return [
            dict(r.url.params) for r in self.requests if r.url.path.removeprefix(API_PREFIX) == path
        ]

    def http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))

    def paths(self) -> list[str]:
        return [r.url.path.removeprefix(API_PREFIX) for r in self.requests]


def _norm(sql: str) -> str:
    return " ".join(sql.split())


class FakeConn:
    def __init__(self, pool: FakePool):
        self.pool = pool

    async def execute(self, sql: str, *args: Any) -> str:
        reject_unbindable(sql, args)
        reject_untyped_mirror_bind(sql, args)
        if self.pool.fail_on and self.pool.fail_on in sql:
            raise RuntimeError("synthetic database failure")
        self.pool.log.append((_norm(sql), args))
        return "DELETE 0" if sql.lstrip().upper().startswith("DELETE") else "INSERT 0 1"

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        reject_unbindable(sql, args)
        self.pool.log.append((_norm(sql), args))
        if "INSERT INTO helixis_turnovers" in sql:
            # open_turnover_if_new: a row only when new or its date moved.
            lease_id, move_out = args[1], args[4]
            if self.pool.turnovers.get(lease_id) == move_out:
                return None
            self.pool.turnovers[lease_id] = move_out
            return {"id": f"turnover-{lease_id}"}
        return None

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        reject_unbindable(sql, args)
        self.pool.log.append((_norm(sql), args))
        return []

    @contextlib.asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        yield


class FakePool:
    """Records every statement; answers the credentials and timezone reads."""

    def __init__(
        self,
        *,
        creds: dict[str, Any] | None = None,
        timezone: str | None = "America/Chicago",
        fail_on: str | None = None,
        mirror_units: list[dict[str, Any]] | None = None,
    ):
        self.creds = creds
        self.timezone = timezone
        self.fail_on = fail_on
        self.log: list[tuple[str, tuple[Any, ...]]] = []
        self.turnovers: dict[int, date] = {}
        #: ``helixis_pms_sync_watermarks``: ``{(company, resource): watermark}``.
        self.watermarks: dict[tuple[str, str], datetime] = {}
        #: ``helixis_units`` rows the mirror holds: ``buildium_id``, ``property_id``.
        self.mirror_units = list(mirror_units or [])

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        reject_unbindable(sql, args)
        self.log.append((_norm(sql), args))
        if "company_propertyware_credentials" in sql:
            return self.creds
        if "FROM public.companies" in sql:
            return {"timezone": self.timezone}
        return None

    async def fetchval(self, sql: str, *args: Any) -> Any:
        reject_unbindable(sql, args)
        self.log.append((_norm(sql), args))
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("synthetic database failure")
        if "FROM helixis_pms_sync_watermarks" in sql:
            return self.watermarks.get((args[0], args[1]))
        return None

    async def execute(self, sql: str, *args: Any) -> str:
        reject_unbindable(sql, args)
        self.log.append((_norm(sql), args))
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("synthetic database failure")
        if "INSERT INTO helixis_pms_sync_watermarks" in sql:
            key, to = (args[0], args[1]), args[2]
            if not isinstance(to, datetime) or to.tzinfo is None:
                raise TypeError("a watermark is a time with a zone")
            current = self.watermarks.get(key)
            self.watermarks[key] = to if current is None else max(current, to)
            return "INSERT 0 1"
        return "UPDATE 1"

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        reject_unbindable(sql, args)
        self.log.append((_norm(sql), args))
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("synthetic database failure")
        if "FROM helixis_units" in sql and args[:2] == (COMPANY, "propertyware"):
            return list(self.mirror_units)
        return []

    def mark(self, resource: str, company: str = COMPANY) -> datetime | None:
        """One stored watermark or marker, by its full resource name."""
        return self.watermarks.get((company, resource))

    @contextlib.asynccontextmanager
    async def acquire(self) -> AsyncIterator[FakeConn]:
        yield FakeConn(self)

    # ── reading the log ───────────────────────────────────────────────

    def statements(self, starts: str) -> list[tuple[str, tuple[Any, ...]]]:
        return [(s, a) for s, a in self.log if s.upper().startswith(starts.upper())]

    def inserts(self, table: str) -> list[tuple[str, tuple[Any, ...]]]:
        return self.statements(f"INSERT INTO {table} ")

    def deletes(self, table: str) -> list[tuple[str, tuple[Any, ...]]]:
        return self.statements(f"DELETE FROM {table} ")

    def stamped(self) -> set[str]:
        """The freshness stages ``record_sync`` stamped."""
        return {args[1] for _sql, args in self.inserts("helixis_mirror_sync_state")}

    def touched_credentials(self) -> bool:
        return any("company_propertyware_credentials" in s for s, _a in self.log)


def inserted_rows(pool: FakePool, table: str) -> list[dict[str, Any]]:
    """Each keyed upsert into ``table`` as ``{column: value}``, read back from
    the statement's own column list."""
    out = []
    for sql, args in pool.inserts(table):
        columns = sql.split("(", 1)[1].split(")", 1)[0]
        names = [c.strip() for c in columns.split(",")]
        out.append(dict(zip(names, args, strict=False)))
    return out
