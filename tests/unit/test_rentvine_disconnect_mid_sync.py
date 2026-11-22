"""A disconnect while a sync runs leaves no Rentvine data behind.

``DELETE /api/v1/rentvine/credentials`` deletes the saved login and then purges
the mirror. A pass that had already loaded the login went on calling Rentvine
with the withdrawn keys, and went on writing after the purge. A pass that loads
its own login now reads it again before every stage and after its last write
(``sync._check_login``, ``sync._purge_if_disconnected``).

Every test here drives the real ``sync.sync_company`` on the path that loads
its own credentials (no ``client=``): the loader and the client constructor are
patched so the pass reads an HTTP mock, and the pool keeps ONE ordered log of
Rentvine requests, SQL statements and what the customer did, so "after the
disconnect" is a position in that log. All rows are synthetic.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from helixis.buildium.purge import PURGE_TABLES
from helixis.rentvine import credentials, sync
from helixis.settings import Settings

from ._pg_bind_contract import reject_unbindable, reject_untyped_mirror_bind
from .test_rentvine_credentials import _encrypted_row
from .test_rentvine_credentials import _settings as _cipher_settings
from .test_rentvine_sync import (
    CONTACT,
    PROPERTY,
    UNIT,
    WORK_ORDER,
    _client,
    _settings,
    export_leases,
    lease_route,
    wire,
)

CO = "11111111-1111-1111-1111-111111111111"

#: The saved login, as the credentials table holds it (ciphertext stand-ins).
SAVED: dict[str, Any] = {
    "company_id": CO,
    "account_code": "acme",
    "encrypted_api_key": "gAAAAB-key-1",
    "api_key_last4": "3456",
    "encrypted_api_secret": "gAAAAB-secret-1",
    "encrypted_webhook_secret": None,
    "base_url": "https://acme.rentvine.com/api/manager",
    "last_tested_at": None,
}

APPLICATION = {"applicationID": 990000901, "unitID": 990000203, "applicants": "Ana Ruiz"}

#: The one property's units: an unpaged route, read per property.
UNITS_REQUEST = sync.UNITS_PATH.format(propertyID=PROPERTY["propertyID"])

#: The one request each stage makes with these fixtures, in the order the pass
#: makes them: the ``SOURCES`` tables, then owner links, leases (no leases, so
#: no rent reads), units (one property), bills and applicants.
STAGE_REQUESTS: tuple[str, ...] = (
    *(path for _, path in sync.SOURCES),
    sync.PORTFOLIOS_PATH,
    sync.LEASES_EXPORT_PATH,
    UNITS_REQUEST,
    sync.PAYABLES_PATH,
    sync.APPLICATIONS_PATH,
)

#: Where a change can land BEFORE a stage check: while the login is loaded,
#: or while each stage but the last is reading. One per stage check.
BETWEEN_STAGES: tuple[str, ...] = ("load", *STAGE_REQUESTS[:-1])

#: The purge's own statement (``buildium.purge.purge_buildium_mirror``).
_PURGE = re.compile(r"DELETE FROM public\.(\w+) WHERE company_id = \$1")
_WRITE = re.compile(r"(INSERT INTO|UPDATE|DELETE FROM) (\w+)")

_COMPLETE = {"pagination-total-pages": "1"}


def _app() -> Settings:
    app = Settings()
    return app.model_copy(update={"rentvine": app.rentvine.model_copy(update={"enabled": True})})


class World:
    """The saved login, a mirror and one ordered log.

    ``log`` holds ``("rentvine", path)``, ``("login", version or "none")`` for
    each read of the saved login, ``("write"|"prune"|"purge", table)``, and
    ``("customer", "disconnect"|"resave")`` / ``("db", "down")`` at the moment
    ``action`` lands. ``mirror`` is the set of tables holding rows written
    since that table was last purged: empty at the end means nothing the pass
    wrote outlived a purge.
    """

    def __init__(
        self,
        *,
        at: tuple[str, str] | None = None,
        action: str | None = None,
        leases: list[dict[str, Any]] | None = None,
    ) -> None:
        self.row: dict[str, Any] | None = dict(SAVED)
        self.at = at
        self.action = action
        self.leases = leases or []
        self.log: list[tuple[str, str]] = []
        self.mirror: set[str] = set()
        self.db_down = False
        self.pool = self._pool()

    def after(self, marker: tuple[str, str]) -> list[tuple[str, str]]:
        return self.log[self.log.index(marker) + 1 :]

    def calls(self, kind: str) -> list[tuple[str, str]]:
        return [entry for entry in self.log if entry[0] == kind]

    def moment(self, kind: str, name: str = "") -> None:
        """Land ``action`` the first time the pass reaches ``at``."""
        if self.at is None or self.at[0] != kind:
            return
        if (kind == "sql" and not name.startswith(self.at[1])) or (
            kind == "request" and name != self.at[1]
        ):
            return
        self.at = None
        if self.action == "disconnect":
            # The route: the login is deleted, then the mirror is purged.
            self.row = None
            self.mirror.clear()
            self.log.append(("customer", "disconnect"))
        elif self.action == "resave":
            assert self.row is not None
            # Saved again: every save encrypts afresh.
            self.row = {
                **self.row,
                "encrypted_api_key": "gAAAAB-key-2",
                "encrypted_api_secret": "gAAAAB-secret-2",
            }
            self.log.append(("customer", "resave"))
        elif self.action == "db_down":
            self.db_down = True
            self.log.append(("db", "down"))

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/manager")
        self.log.append(("rentvine", path))
        self.moment("request", path)
        rows = {
            "/properties": wire("GET /properties", PROPERTY),
            "/tenants": wire("GET /tenants", CONTACT),
            "/owners": wire("GET /owners", {**CONTACT, "contactID": 229940}),
            "/vendors": wire("GET /vendors", {**CONTACT, "contactID": 229941}),
            "/maintenance/work-orders": wire("GET /maintenance/work-orders", WORK_ORDER),
            "/accounting/accounts": [],
            "/portfolios": [],
            "/accounting/payables/search": [],
            "/screening/applications": wire("GET /screening/applications", APPLICATION),
        }
        if path in rows:
            return httpx.Response(200, json=rows[path], headers=_COMPLETE)
        if path == UNITS_REQUEST:
            return httpx.Response(200, json=wire("GET /properties/{propertyID}/units", UNIT))
        answered = lease_route(request, self.leases)
        if answered is not None:
            return answered
        raise AssertionError(f"the pass asked for {path}, which no stage here reads")

    def _pool(self) -> Any:
        conn = MagicMock()

        async def fetchrow(sql: str, *args: Any) -> dict[str, Any] | None:
            if "company_rentvine_credentials" not in sql:
                return None  # nothing stamped yet (get_stage_sync)
            # Rule 11: one company's row, never a scan.
            assert args == (CO,) and "WHERE company_id = $1" in sql, (sql, args)
            if self.db_down:
                raise ConnectionResetError("the database connection was lost")
            named = sql.split("SELECT", 1)[1].split("FROM", 1)[0]
            row = (
                None
                if self.row is None
                else {c.strip(): self.row[c.strip()] for c in named.split(",")}
            )
            self.log.append(("login", "none" if row is None else credentials.version_of(row)))
            return row

        async def execute(sql: str, *args: Any) -> str:
            reject_unbindable(sql, args)
            reject_untyped_mirror_bind(sql, args)
            statement = " ".join(sql.split())
            self.moment("sql", statement)
            purged = _PURGE.fullmatch(statement)
            if purged:
                self.mirror.discard(purged.group(1))
                self.log.append(("purge", purged.group(1)))
                return "DELETE 0"
            written = _WRITE.match(statement)
            assert written, f"a statement this fake does not model: {statement[:80]}"
            verb, table = written.groups()
            if verb == "DELETE FROM":
                self.log.append(("prune", table))
                return "DELETE 0"
            self.mirror.add(table)
            self.log.append(("write", table))
            return "INSERT 0 1"

        conn.fetchrow = AsyncMock(side_effect=fetchrow)
        conn.execute = AsyncMock(side_effect=execute)
        conn.fetch = AsyncMock(return_value=[])
        tx = MagicMock()
        tx.__aenter__ = AsyncMock(return_value=tx)
        tx.__aexit__ = AsyncMock(return_value=False)
        conn.transaction = MagicMock(return_value=tx)
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        pool = MagicMock()
        pool.acquire = MagicMock(return_value=ctx)
        # The company's timezone (company_clock): none set, so the default.
        pool.fetchrow = AsyncMock(return_value=None)
        return pool


async def _run_owned(world: World, monkeypatch: pytest.MonkeyPatch) -> sync.SyncResult:
    """``sync_company`` with no ``client=``: it loads the login itself."""

    async def load(company_id: str) -> Any:
        assert company_id == CO
        if world.row is None:
            return None
        world.moment("load")
        return _settings()

    monkeypatch.setattr(sync, "load_rentvine_settings_for_company", load)
    monkeypatch.setattr(
        sync, "RentvineClient", lambda _creds, *, company_id: _client(world.handler)
    )
    return await sync.sync_company(world.pool, CO, settings=_app())


def _at(lands_at: str) -> tuple[str, str]:
    return ("load", "") if lands_at == "load" else ("request", lands_at)


# ── removed while it runs ────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("lands_at", BETWEEN_STAGES)
async def test_a_disconnect_between_stages_stops_the_pass_and_purges_again(
    lands_at: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The disconnect lands while one stage reads. The next stage never starts,
    and the pass purges again, which takes what the stage in flight wrote after
    the route's purge."""
    world = World(at=_at(lands_at), action="disconnect")
    with pytest.raises(sync.RentvineSyncDisabled, match="removed"):
        await _run_owned(world, monkeypatch)

    after = world.after(("customer", "disconnect"))
    # Not one more call with the withdrawn keys.
    assert [e for e in after if e[0] == "rentvine"] == []
    # The check that saw it was followed by one whole purge and nothing else.
    seen = world.log.index(("login", "none"))
    assert world.log[seen + 1 :] == [("purge", t) for t in PURGE_TABLES]
    assert world.mirror == set()


@pytest.mark.asyncio
async def test_a_disconnect_during_the_last_stages_write_is_purged_after_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No stage check follows the last stage, so the check after the last write
    is what catches a disconnect there: the applicant row and the freshness
    stamps land after the route's purge, and the pass's own purge follows
    them."""
    world = World(at=("sql", "INSERT INTO helixis_applicants"), action="disconnect")
    with pytest.raises(sync.RentvineSyncDisabled, match="removed"):
        await _run_owned(world, monkeypatch)

    after = world.after(("customer", "disconnect"))
    assert ("write", "helixis_applicants") in after
    assert ("write", "helixis_mirror_sync_state") in after
    last_write = max(i for i, e in enumerate(world.log) if e[0] == "write")
    assert world.log[last_write + 1 :] == [
        ("login", "none"),
        *(("purge", t) for t in PURGE_TABLES),
    ]
    assert world.mirror == set()


# ── replaced while it runs ───────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("lands_at", BETWEEN_STAGES)
async def test_a_replaced_login_stops_the_pass_and_purges_nothing(
    lands_at: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A re-save (perhaps for another account) stops the pass at the next
    stage. Nothing is purged: the next pass reads with the new login and
    rewrites the mirror. Landing during the load is the case that needs the
    version read BEFORE the login it describes."""
    world = World(at=_at(lands_at), action="resave")
    with pytest.raises(sync.RentvineSyncDisabled, match="replaced"):
        await _run_owned(world, monkeypatch)

    after = world.after(("customer", "resave"))
    assert [e for e in after if e[0] == "rentvine"] == []
    assert world.calls("purge") == []


@pytest.mark.asyncio
async def test_a_resave_during_the_last_stages_write_keeps_what_it_wrote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After the last write only a disconnect counts. The rows were read with
    the login saved when the stage began, and the next pass rewrites them."""
    world = World(at=("sql", "INSERT INTO helixis_applicants"), action="resave")
    result = await _run_owned(world, monkeypatch)
    assert result.ok, result.failed
    assert result.rows_by_table["helixis_applicants"] == 1
    assert world.calls("purge") == []


# ── a check that cannot be made ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_stage_check_that_fails_stops_the_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unconfirmed is a reason to stop: the database error propagates (and
    ``sync_one`` logs it as a fault), with no further Rentvine call."""
    world = World(at=("request", "/owners"), action="db_down")
    with pytest.raises(ConnectionResetError):
        await _run_owned(world, monkeypatch)
    after = world.after(("db", "down"))
    assert [e for e in after if e[0] == "rentvine"] == []
    assert world.calls("purge") == []


@pytest.mark.asyncio
async def test_an_after_write_check_that_fails_is_logged_and_the_result_kept(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    world = World(at=("sql", "INSERT INTO helixis_applicants"), action="db_down")
    with caplog.at_level(logging.ERROR, logger="helixis.rentvine.sync"):
        result = await _run_owned(world, monkeypatch)
    assert result.rows_by_table["helixis_applicants"] == 1
    assert world.calls("purge") == []
    [record] = [r for r in caplog.records if "after-write disconnect check" in r.getMessage()]
    assert record.levelno == logging.ERROR and record.exc_info is not None


# ── nothing changes ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_with_no_change_the_owned_pass_is_the_pass_it_always_was(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same result, same requests, same statements in the same order as the
    same pass given a client; the only additions are the login reads. Leases
    are read here (three rent reads), so a check inside a stage would show."""
    leases = export_leases(3)
    owned = World(leases=leases)
    result = await _run_owned(owned, monkeypatch)

    given = World(leases=leases)
    expected = await sync.sync_company(
        given.pool, CO, settings=_app(), client=_client(given.handler)
    )
    assert result == expected
    assert result.ok, result.failed
    assert [e for e in owned.log if e[0] != "login"] == given.log
    # One read before the load, one before each stage (the SOURCES tables,
    # then owner links, leases, units, bills and applicants), one after the
    # last write. Never one per row (rule 13).
    stages = len(sync.SOURCES) + len(("owner links", "leases", "units", "bills", "applicants"))
    version = credentials.version_of(SAVED)
    assert owned.calls("login") == [("login", version)] * (1 + stages + 1)


@pytest.mark.asyncio
async def test_a_pass_given_a_client_never_reads_the_saved_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tests' path and the operator scripts' (``rentvine_phase8.py``): the
    caller owns the login. With no login saved at all, a pass that checked
    would stop at once; this one runs to the end."""
    monkeypatch.setattr(
        sync, "load_rentvine_settings_for_company", AsyncMock(side_effect=AssertionError)
    )
    world = World()
    world.row = None
    result = await sync.sync_company(world.pool, CO, settings=_app(), client=_client(world.handler))
    assert result.ok, result.failed
    assert world.calls("login") == [] and world.calls("purge") == []
    assert [e[1] for e in world.calls("rentvine")] == list(STAGE_REQUESTS)


# ── the version ──────────────────────────────────────────────────────────────


def test_the_version_follows_every_login_column_and_nothing_else() -> None:
    """Saving a webhook signing key is not a reconnect, and ``/test`` stamps
    ``last_tested_at`` and then starts a sync: neither may stop one."""
    version = credentials.version_of(SAVED)
    assert re.fullmatch(r"[0-9a-f]{16}", version)
    for column in ("account_code", "encrypted_api_key", "encrypted_api_secret"):
        assert credentials.version_of({**SAVED, column: f"{SAVED[column]}-2"}) != version, column
    for column, value in (
        ("encrypted_webhook_secret", "gAAAAB-signing-key"),
        ("last_tested_at", "2026-10-05T12:00:00+00:00"),
        ("updated_at", "2026-10-05T12:00:01+00:00"),
        ("api_key_last4", "9999"),
        ("base_url", "https://elsewhere.example.com"),
    ):
        assert credentials.version_of({**SAVED, column: value}) == version, column


def test_saving_the_same_login_again_is_a_new_version() -> None:
    """Every save encrypts afresh, so a reconnect with the same keys is still
    a reconnect. Ciphertext only: the version names no value."""
    settings = _cipher_settings()
    first, again = _encrypted_row(settings), _encrypted_row(settings)
    assert credentials.version_of(first) != credentials.version_of(again)
    assert "acme" not in credentials.version_of(first)


@pytest.mark.asyncio
async def test_the_saved_login_is_read_for_one_company_and_never_the_webhook_key() -> None:
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={k: SAVED[k] for k in credentials._LOGIN_COLUMNS})
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)

    assert await credentials.saved_version(pool, CO) == credentials.version_of(SAVED)
    sql, *args = conn.fetchrow.await_args.args
    assert args == [CO] and "WHERE company_id = $1" in sql
    assert "encrypted_webhook_secret" not in sql

    conn.fetchrow = AsyncMock(return_value=None)
    assert await credentials.saved_version(pool, CO) is None
