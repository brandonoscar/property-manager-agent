"""The Rentvine connect/disconnect surface — ADR 0010 Decision (e), Phase 6.9.

``rentvine/credentials.py`` could always READ ``company_rentvine_credentials``;
until these routes existed nothing could WRITE one, so the connector was
reachable only through a hand-typed INSERT (gotcha 153). These pin the contract
that makes it safe to expose:

  - plaintext never round-trips through the read API, and there is no
    ``api_secret_last4`` anywhere — migration 0100 deliberately stores none;
  - the WRITE refuses while the connector is dark and the READ + DISCONNECT
    deliberately do not (gotcha 120's asymmetry), because a dark connection is
    strictly worse than none: it blocks Buildium via the 0100 trigger and syncs
    nothing;
  - a company already on Buildium gets an honest 409 rather than a 500;
  - disconnect purges the mirror (ADR 0010 e1) and does not claim to revoke
    the key (e2);
  - ``/test`` tells a 401 and a 403 APART, which is gotcha 102's whole lesson.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import jwt
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from helixis.main import create_app
from helixis.settings import Settings
from tests.unit._supabase_tokens import TEST_ISSUER, TEST_SUPABASE_URL

TEST_JWT_SECRET = "test-secret-key-for-unit-tests-only-not-production"
TEST_DATA_KEY = Fernet.generate_key().decode()

ACCOUNT = "acme"
API_KEY = "rvkey-abcd1234"
API_SECRET = "rvsecret-supersecret-value"
# Built at runtime, like the no-secrets-in-logs fixtures: a key-shaped literal
# trips the repo's gitleaks scan (.gitleaks.toml).
SIGNING_KEY = "-".join(("rv", "fake", "signing", "key"))


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


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {_make_token()}"}


def _settings(*, rentvine_enabled: bool = True, **rentvine: str) -> Settings:
    return Settings(
        env="test",
        auth={"jwt_secret": TEST_JWT_SECRET},
        supabase={"url": TEST_SUPABASE_URL},
        data_key=TEST_DATA_KEY,
        memory={"enabled": False},
        rentvine={"enabled": rentvine_enabled, **rentvine},
    )


@pytest.fixture
def fake_pool():
    """An asyncpg stand-in over a one-row ``company_rentvine_credentials``.

    ⚠ ``role`` defaults to ``admin`` so the happy paths do not each have to
    say so, and the role-floor tests flip it — including to ``None``, which is
    the unverifiable case ``require_role`` must refuse rather than wave through
    (gotchas 72/89's rule: a fixture states which state it means).

    ⚠ ``insert_raises`` is how the 0100 exclusivity trigger is modelled. It is
    NOT a generic error hook: the trigger raises with ``errcode =
    'unique_violation'``, which asyncpg surfaces as this exact class, and the
    route's whole job is turning it into a sentence a customer can act on.
    """
    state: dict[str, object] = {
        "rows": {},
        "role": "admin",
        "insert_raises": None,
        "purge_deletes": {},
        "purged": [],
    }

    pool = MagicMock()

    async def execute(sql: str, *args):
        s = " ".join(sql.split())
        if s.startswith("INSERT INTO public.company_rentvine_credentials"):
            if state["insert_raises"] is not None:
                raise state["insert_raises"]
            # ON CONFLICT DO UPDATE: a re-save overwrites the columns it names
            # and keeps the rest, the signing key included.
            row = state["rows"].setdefault(  # type: ignore[union-attr]
                args[0], {"company_id": args[0], "encrypted_webhook_secret": None}
            )
            row.update(
                {
                    "account_code": args[1],
                    "encrypted_api_key": args[2],
                    "api_key_last4": args[3],
                    "encrypted_api_secret": args[4],
                    "base_url": args[5],
                    "last_tested_at": None,
                }
            )
        elif s.startswith("DELETE FROM public.company_rentvine_credentials"):
            state["rows"].pop(args[0], None)  # type: ignore[union-attr]
        elif s.startswith("UPDATE public.company_rentvine_credentials SET last_tested_at"):
            row = state["rows"].get(args[0])  # type: ignore[union-attr]
            if row:
                row["last_tested_at"] = datetime.now(UTC)
        elif s.startswith(
            "UPDATE public.company_rentvine_credentials SET encrypted_webhook_secret = NULL"
        ):
            row = state["rows"].get(args[0])  # type: ignore[union-attr]
            if row:
                row["encrypted_webhook_secret"] = None

    async def fetchval(sql: str, *args):
        s = " ".join(sql.split())
        if s.startswith("UPDATE public.company_rentvine_credentials SET encrypted_webhook_secret"):
            row = state["rows"].get(args[0])  # type: ignore[union-attr]
            if row is None:
                return None
            row["encrypted_webhook_secret"] = args[1]
            return args[0]
        return None

    async def fetchrow(sql: str, *args):
        s = " ".join(sql.split())
        if s.startswith("SELECT role FROM public.company_users"):
            role = state["role"]
            return {"role": role} if role is not None else None
        return state["rows"].get(args[0]) if args else None  # type: ignore[union-attr]

    async def fetch(_sql: str, *_args):
        return []

    # The purge runs `async with pool.acquire() as conn` and reads asyncpg's
    # status tag off each DELETE, so the fake has to speak that shape or the
    # route's purged_rows would be 0 for a reason unrelated to the code.
    conn = MagicMock()

    async def conn_execute(sql: str, *args):
        table = sql.split("public.")[1].split()[0]
        state["purged"].append(table)  # type: ignore[union-attr]
        return f"DELETE {state['purge_deletes'].get(table, 0)}"  # type: ignore[union-attr]

    conn.execute = AsyncMock(side_effect=conn_execute)

    acquired = MagicMock()
    acquired.__aenter__ = AsyncMock(return_value=conn)
    acquired.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=acquired)

    pool.execute = AsyncMock(side_effect=execute)
    pool.fetchrow = AsyncMock(side_effect=fetchrow)
    pool.fetchval = AsyncMock(side_effect=fetchval)
    pool.fetch = AsyncMock(side_effect=fetch)
    pool._state = state
    return pool


@asynccontextmanager
async def _noop_lifespan(_app):
    yield


def _client(settings: Settings, pool):
    from helixis.api.rate_limit import connectors_limiter

    connectors_limiter.counter._log.clear()

    with (
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.api.routes.rentvine.get_settings", return_value=settings),
        patch("helixis.api.routes.rentvine.get_pool", return_value=pool),
        patch(
            "helixis.api.auth._resolve_company_ids",
            new=AsyncMock(return_value=["company-A"]),
        ),
    ):
        app = create_app(settings)
        app.state.memory_manager = MagicMock(available=False)
        app.state.composio_client = MagicMock(available=False)
        app.router.lifespan_context = _noop_lifespan
        with TestClient(app) as c:
            yield c


@pytest.fixture
def client(fake_pool):
    yield from _client(_settings(), fake_pool)


@pytest.fixture
def dark_client(fake_pool):
    """A deployment where ``rentvine.enabled`` is false — the SHIPPED default."""
    yield from _client(_settings(rentvine_enabled=False), fake_pool)


def _put(c, **overrides):
    body = {"account_code": ACCOUNT, "api_key": API_KEY, "api_secret": API_SECRET}
    body.update(overrides)
    return c.put("/api/v1/rentvine/credentials", headers=_auth(), json=body)


# ── the read + write contract ──────────────────────────────────────────


def test_unconfigured_reads_as_not_connected(client):
    resp = client.get("/api/v1/rentvine/credentials", headers=_auth())
    assert resp.status_code == 200
    assert resp.json()["configured"] is False


def test_the_write_persists_ciphertext_and_the_read_returns_no_plaintext(client, fake_pool):
    assert _put(client).status_code == 200

    stored = fake_pool._state["rows"]["company-A"]
    # The SECRETS are ciphertext...
    assert stored["encrypted_api_key"] != API_KEY
    assert stored["encrypted_api_secret"] != API_SECRET
    cipher = Fernet(TEST_DATA_KEY.encode())
    assert cipher.decrypt(stored["encrypted_api_key"].encode()).decode() == API_KEY
    assert cipher.decrypt(stored["encrypted_api_secret"].encode()).decode() == API_SECRET
    # ...and the account code is NOT, deliberately: it is the subdomain in every
    # request URL, so encrypting it costs a decrypt per call and hides nothing
    # (migration 0100's note 1).
    assert stored["account_code"] == ACCOUNT

    body = client.get("/api/v1/rentvine/credentials", headers=_auth()).json()
    blob = str(body)
    assert API_KEY not in blob
    assert API_SECRET not in blob
    assert body["configured"] is True
    assert body["account_code"] == ACCOUNT
    assert body["api_key_last4"] == API_KEY[-4:]


def test_no_last4_of_the_secret_exists_anywhere_on_the_read(client):
    """⚠ The DELIBERATE ABSENCE, asserted rather than assumed.

    Rentvine shows a secret once at creation. A last4 exists to help somebody
    recognise a key they can still look up, and there is no such surface for
    this one — so four characters of a secret would be exposure with no use
    (migration 0100's note 2). A later "for parity with Buildium" edit is
    exactly what this catches.
    """
    _put(client)
    body = client.get("/api/v1/rentvine/credentials", headers=_auth()).json()
    assert "api_secret_last4" not in body
    assert API_SECRET[-4:] not in str(body)


def test_resaving_for_a_connected_company_succeeds(client, fake_pool):
    """The ON CONFLICT path, which the 0100 trigger does NOT fire on.

    It is declared ``before insert or update OF company_id`` and the upsert's
    SET list does not name that column, so rotating a key for a company already
    on Rentvine is an ordinary update rather than an exclusivity violation.
    """
    assert _put(client).status_code == 200
    resp = _put(client, api_key="rvkey-rotated9999")
    assert resp.status_code == 200
    assert resp.json()["api_key_last4"] == "9999"


# ── one PMS per company ────────────────────────────────────────────────


def test_a_buildium_company_is_refused_with_an_instruction_it_can_follow(client, fake_pool):
    """The 0100 trigger's ``unique_violation`` must not surface as a 500.

    A 500 tells the customer Occupella is broken and leaves them nowhere to go;
    the real state is a rule with a remedy, so the refusal names both (gotchas
    102/106/118/133 — an instruction the reader cannot carry out is worse than
    none).
    """
    fake_pool._state["insert_raises"] = asyncpg.exceptions.UniqueViolationError(
        "company … is already connected to Buildium; one PMS per company (ADR 0010 a1)."
    )
    resp = _put(client)
    assert resp.status_code == 409
    detail = resp.json()["detail"].lower()
    assert "buildium" in detail
    assert "disconnect" in detail
    # Nothing was stored: the refusal must not leave a half-connected workspace.
    assert fake_pool._state["rows"] == {}


# ── the dark-connector asymmetry ───────────────────────────────────────


def test_the_write_refuses_while_the_connector_is_dark(dark_client, fake_pool):
    resp = _put(dark_client)
    assert resp.status_code == 503
    assert "not enabled" in resp.json()["detail"].lower()
    # ⚠ THE HALF THAT MATTERS. Storing a credential on a dark deployment syncs
    # nothing AND blocks the company's Buildium connection through the 0100
    # trigger, so the refusal is worthless unless the row genuinely is absent.
    assert fake_pool._state["rows"] == {}


def test_the_read_and_the_disconnect_work_while_dark(dark_client, fake_pool):
    """Somebody who connected before the flag went off must still see and undo it.

    Refusing the disconnect would strand them twice over: they cannot use
    Rentvine, and they cannot connect Buildium either while the row exists.
    """
    fake_pool._state["rows"]["company-A"] = {
        "company_id": "company-A",
        "account_code": ACCOUNT,
        "encrypted_api_key": "x",
        "api_key_last4": "1234",
        "encrypted_api_secret": "y",
        "encrypted_webhook_secret": None,
        "base_url": "https://acme.rentvine.com/api/manager",
        "last_tested_at": None,
    }
    assert dark_client.get("/api/v1/rentvine/credentials", headers=_auth()).json()["configured"]
    resp = dark_client.request("DELETE", "/api/v1/rentvine/credentials", headers=_auth())
    assert resp.status_code == 200
    assert resp.json()["configured"] is False
    assert fake_pool._state["rows"] == {}


# ── disconnect purges ──────────────────────────────────────────────────


def test_disconnect_purges_the_mirror_and_reports_the_count(client, fake_pool):
    from helixis.buildium.purge import PURGE_TABLES

    _put(client)
    fake_pool._state["purge_deletes"] = {"helixis_properties": 3, "helixis_leases": 2}
    resp = client.request("DELETE", "/api/v1/rentvine/credentials", headers=_auth())
    assert resp.status_code == 200
    assert resp.json()["purged_rows"] == 5
    # Every classified mirror table was swept, not a subset. ADR 0010 a1 makes
    # the company mono-PMS, so `WHERE company_id` is both correct and complete.
    assert fake_pool._state["purged"] == list(PURGE_TABLES)


def test_the_disconnect_route_awaits_the_purge():
    """A behavioural test cannot see a route that stops calling the purge.

    Same idiom as ``test_buildium_purge``'s call-site scan, and for the same
    reason: removing this is a compliance regression (ADR 0010 e1), not a
    refactor.
    """
    from helixis.api.routes import rentvine as route_module

    src = inspect.getsource(route_module.delete_credentials)
    assert "await purge_rentvine_mirror(" in src
    # The credentials tombstone still comes FIRST, so an interleaved sync
    # cannot repopulate what the purge just removed.
    assert src.index("DELETE FROM public.company_rentvine_credentials") < src.index(
        "await purge_rentvine_mirror("
    )


def test_the_purge_delegates_and_keeps_no_table_list_of_its_own():
    """⚠ Two copies of the table list is the drift this module exists to avoid.

    The mirror is one set of tables (ADR 0010 Decision (a)) and a1 makes a
    company mono-PMS, so a hand-typed Rentvine copy of ``PURGE_TABLES`` would
    be a second home whose staleness shows up as a table that silently stops
    being deleted — on a compliance path (gotcha 25).
    """
    from helixis.rentvine import purge as module

    tree = ast.parse(Path(inspect.getfile(module)).read_text(encoding="utf-8"))
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "purge_buildium_mirror" in called
    # No table names typed here at all — the delegation IS the list.
    literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    from helixis.buildium.purge import PURGE_TABLES

    assert not (literals & set(PURGE_TABLES)), (
        "a table name appears as a literal in rentvine/purge.py — the list is "
        "buildium/purge.PURGE_TABLES and must stay there"
    )


def test_the_purge_is_not_narrowed_by_the_pms_column():
    """⚠ The tightening that reads as obviously correct and is gotcha 112.

    Migration 0101 added ``pms`` to 21 tables; the purge list holds 30, and
    ``purge_buildium_mirror`` tolerates ``UndefinedColumnError`` so a
    deploy-drifted environment can still disconnect. So ``AND pms =
    'rentvine'`` would SKIP the nine columnless tables in silence — the event
    log and the Inbox reminders left behind — while reporting a clean purge.
    """
    import re

    from helixis.buildium.purge import purge_buildium_mirror

    src = inspect.getsource(purge_buildium_mirror)
    statements = re.findall(r"DELETE FROM[^\"']*", src)
    assert statements, "the purge's DELETE statement moved — re-point this scan"
    for stmt in statements:
        assert "pms" not in stmt, f"the purge narrowed on pms: {stmt!r}"


# ── the role floor ─────────────────────────────────────────────────────


#: Every route the router serves. ``test_the_role_floor_covers_every_route``
#: fails when a route is added and not listed here; POST ``/sync`` was missing
#: until 2026-10-03.
_EVERY_ROUTE = [
    ("GET", "/api/v1/rentvine/credentials"),
    ("PUT", "/api/v1/rentvine/credentials"),
    ("DELETE", "/api/v1/rentvine/credentials"),
    ("POST", "/api/v1/rentvine/test"),
    ("POST", "/api/v1/rentvine/sync"),
    ("GET", "/api/v1/rentvine/sync"),
    ("PUT", "/api/v1/rentvine/webhook-secret"),
    ("DELETE", "/api/v1/rentvine/webhook-secret"),
]

#: A well-formed body per PUT, so each one reaches the role check rather than
#: stopping at a 422.
_PUT_BODY = {
    "/api/v1/rentvine/credentials": {
        "account_code": ACCOUNT,
        "api_key": API_KEY,
        "api_secret": API_SECRET,
    },
    "/api/v1/rentvine/webhook-secret": {"secret": SIGNING_KEY},
}


def test_the_role_floor_covers_every_route():
    from helixis.api.routes import rentvine as routes

    served = {
        (method, "/api/v1" + route.path)
        for route in routes.router.routes
        for method in getattr(route, "methods", set())
    }
    assert served == set(_EVERY_ROUTE)


@pytest.mark.parametrize("role", ["member", None])
@pytest.mark.parametrize(("method", "path"), _EVERY_ROUTE)
def test_every_route_needs_an_admin_and_fails_closed(client, fake_pool, role, method, path):
    """Reads included, and an unverifiable role refused.

    The credentials are ONE row the whole team shares, so a member replacing or
    deleting them breaks the connector for everyone — and the disconnect also
    deletes the mirror. ``None`` is somebody with no ``company_users`` row at
    all, which least-privilege refuses rather than treats as a member.
    """
    fake_pool._state["role"] = role
    resp = client.request(
        method, path, headers=_auth(), json=_PUT_BODY[path] if method == "PUT" else None
    )
    assert resp.status_code == 403
    assert fake_pool._state["rows"] == {}


# ── /test ──────────────────────────────────────────────────────────────


class _FakeClient:
    def __init__(self, *, page=None, error=None):
        self._page = page
        self._error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def get_page(self, *_a, **_kw):
        if self._error is not None:
            raise self._error
        return self._page


def test_test_reports_the_accounts_total_not_the_one_row_it_asked_for(client, fake_pool):
    """⚠ ``len(rows)`` is 1 by construction — it is the page size we requested.

    Reporting it as "properties found" would tell every customer they have one
    property, which is gotcha 26's class with a number instead of an absence.
    """
    from helixis.rentvine.client import Page

    _put(client)
    page = Page(rows=[{"propertyID": 1}], may_have_more=True, total_pages=42, total_items=417)
    with patch("helixis.api.routes.rentvine.RentvineClient", return_value=_FakeClient(page=page)):
        resp = client.post("/api/v1/rentvine/test", headers=_auth())
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "properties_count": 417, "error": None}
    assert fake_pool._state["rows"]["company-A"]["last_tested_at"] is not None


def test_test_reads_the_path_the_sync_reads():
    """Both sides of the claim (gotcha 28).

    A connection test against a path the product never uses proves the
    credentials parse and nothing about whether the product works.
    """
    from helixis.api.routes.rentvine import _TEST_PATH
    from helixis.rentvine.sync import SOURCES

    assert _TEST_PATH == SOURCES[0][1]


def test_an_unsaved_connection_says_so_rather_than_calling_rentvine(client):
    with patch("helixis.api.routes.rentvine.RentvineClient") as ctor:
        resp = client.post("/api/v1/rentvine/test", headers=_auth())
    assert resp.json() == {"ok": False, "properties_count": None, "error": "No saved credentials."}
    ctor.assert_not_called()


def test_a_401_and_a_403_give_opposite_instructions(client):
    """Gotcha 102's lesson, which this product has already paid for once.

    A 401 means the key is wrong — reconnecting helps. A 403 means Rentvine
    ACCEPTED the key and refused this operation — the fix is the key's role,
    and telling that customer to reconnect sends them to redo something that
    works.
    """
    from helixis.rentvine import transport
    from helixis.rentvine.client import RentvineAPIError

    _put(client)
    messages = {}
    for status_code, failure in (
        (401, transport.Failure.CREDENTIALS),
        (403, transport.Failure.PERMISSION),
    ):
        err = RentvineAPIError(status_code, failure, retryable=False, detail="nope")
        with patch(
            "helixis.api.routes.rentvine.RentvineClient",
            return_value=_FakeClient(error=err),
        ):
            messages[status_code] = client.post("/api/v1/rentvine/test", headers=_auth()).json()[
                "error"
            ]

    assert messages[401] != messages[403]
    assert "key" in messages[401].lower() or "secret" in messages[401].lower()
    assert "role" in messages[403].lower()
    # And the 403 explicitly says the key is fine, because "reconnect" is the
    # one instruction guaranteed not to help there.
    assert "does not need replacing" in messages[403]
    assert "role" not in messages[401].lower()


def test_every_classified_failure_has_advice_and_none_is_the_default(client):
    """A missing key would silently fall back to the undocumented wording.

    That reads as caution and is a wrong claim: it would tell a customer with a
    revoked key that nothing is wrong with what they entered.
    """
    from helixis.api.routes.rentvine import _TEST_ADVICE, _TEST_ADVICE_UNDOCUMENTED
    from helixis.rentvine import transport

    documented = set(transport.Failure) - {transport.Failure.UNDOCUMENTED}
    assert set(_TEST_ADVICE) == documented
    assert _TEST_ADVICE_UNDOCUMENTED not in _TEST_ADVICE.values()


def test_an_undocumented_failure_does_not_tell_the_customer_to_retry(client):
    """The client has already retried a retryable status to exhaustion.

    ``RETRY_BACKOFF_S`` is spent before the error reaches this route, so "try
    again in a moment" is advice the product has already taken and is the
    useless-instruction shape.
    """
    from helixis.rentvine import transport
    from helixis.rentvine.client import RentvineAPIError

    _put(client)
    err = RentvineAPIError(429, transport.Failure.UNDOCUMENTED, retryable=True, detail="slow down")
    with patch("helixis.api.routes.rentvine.RentvineClient", return_value=_FakeClient(error=err)):
        message = client.post("/api/v1/rentvine/test", headers=_auth()).json()["error"]
    assert "try again" not in message.lower()
    assert "nothing is wrong with what you entered" in message.lower()


# ── the router is actually mounted ─────────────────────────────────────
#
# ⚠ There is deliberately NO separate "is it mounted?" test, and that is
# gotcha 58's check answered rather than skipped. A router nobody includes is
# the defect this whole module closes (gotcha 62), so it plainly needs
# covering — but every request above goes through the real ``create_app`` and
# asserts a status that is not 404, which IS the cover. In particular
# ``test_the_write_refuses_while_the_connector_is_dark`` asserts **503**, so it
# also pins that the router is mounted UNCONDITIONALLY rather than behind
# ``rentvine.enabled``.
#
# ⚠ The test that was here first read ``app.routes`` directly and failed
# against correct code — ``include_router`` appends ``_IncludedRouter`` objects
# carrying ``original_router`` and no ``.path`` at all (gotcha 134, measured
# again here). If a mount check is ever genuinely needed, recurse the way
# ``test_frontend_api_paths_exist._routes`` does; do not re-add the naive walk.


# ── what the onboarding wizard needs (Phase 7) ─────────────────────────


def test_the_read_says_the_connector_is_on(client):
    """The wizard asks BEFORE collecting keys, so a customer on a dark
    deployment is told up front instead of after pasting both keys."""
    body = client.get("/api/v1/rentvine/credentials", headers=_auth()).json()
    assert body["connector_enabled"] is True


def test_the_read_says_the_connector_is_off(dark_client):
    # Separate test on purpose: two client fixtures stack their settings patch.
    body = dark_client.get("/api/v1/rentvine/credentials", headers=_auth()).json()
    assert body["connector_enabled"] is False


def test_a_passing_test_starts_the_first_sync(client, fake_pool):
    """Without it the first sync waited for the worker's hourly tick, and the
    wizard's launch screen scanned an empty mirror."""
    from helixis.rentvine.client import Page

    _put(client)
    page = Page(rows=[{"propertyID": 1}], may_have_more=False, total_pages=1, total_items=1)
    with (
        patch("helixis.api.routes.rentvine.RentvineClient", return_value=_FakeClient(page=page)),
        patch("helixis.api.routes.rentvine._kick_initial_sync") as kick,
    ):
        assert client.post("/api/v1/rentvine/test", headers=_auth()).json()["ok"] is True
    kick.assert_called_once()
    assert kick.call_args.args[1] == "company-A"


def test_a_failing_test_starts_no_sync(client, fake_pool):
    from helixis.rentvine.client import RentvineAPIError
    from helixis.rentvine.transport import Failure

    _put(client)
    err = RentvineAPIError(401, Failure.CREDENTIALS, False, "Access Denied")
    with (
        patch("helixis.api.routes.rentvine.RentvineClient", return_value=_FakeClient(error=err)),
        patch("helixis.api.routes.rentvine._kick_initial_sync") as kick,
    ):
        assert client.post("/api/v1/rentvine/test", headers=_auth()).json()["ok"] is False
    kick.assert_not_called()


async def test_the_kick_keeps_a_reference_until_the_sync_finishes():
    """asyncio holds tasks weakly; an unreferenced crawl can be collected."""
    import asyncio

    from helixis.api.routes import rentvine as routes

    gate = asyncio.Event()

    async def _slow(_pool, _company):
        await gate.wait()
        return True

    with patch.object(routes, "start_initial_sync", _slow):
        routes._kick_initial_sync(object(), "company-A")
        assert len(routes._background) == 1
        gate.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    assert routes._background == set()


async def test_a_second_initial_sync_for_the_same_company_does_not_start():
    import asyncio

    from helixis.rentvine import sync_cycle

    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    async def _one(_pool, company_id, _app):
        calls.append(company_id)
        started.set()
        await release.wait()

    with (
        patch.object(sync_cycle, "sync_one", _one),
        patch.object(sync_cycle, "get_settings", MagicMock()),
    ):
        first = asyncio.create_task(sync_cycle.start_initial_sync(object(), "company-A"))
        await started.wait()
        # Bounded: without the guard this second call would start a second
        # crawl and wait on the same gate forever.
        second = sync_cycle.start_initial_sync(object(), "company-A")
        assert await asyncio.wait_for(second, timeout=2) is False
        release.set()
        assert await first is True
    assert calls == ["company-A"]
    # Re-pointed 2026-10-03: the slot map now holds each run's start time.
    assert sync_cycle._initial_running == {}


def test_a_crash_inside_the_test_never_reaches_the_customer_verbatim(client):
    """An unexpected exception used to be shown as "Could not reach Rentvine:
    <exception text>" — an internal detail the customer can do nothing with."""
    _put(client)
    boom = RuntimeError("asyncpg pool exhausted at 0x7f3a")
    with patch("helixis.api.routes.rentvine.RentvineClient", side_effect=boom):
        body = client.post("/api/v1/rentvine/test", headers=_auth()).json()
    assert body["ok"] is False
    assert "asyncpg" not in body["error"] and "0x7f3a" not in body["error"]
    assert "team@occupella.com" in body["error"]


# ── /rentvine/sync: Sync now (Settings → Connectors) and the Operations page's Re-sync
#
# Measured 2026-10-03 on Render: one sandbox sync took 86,978 ms, and the page
# stops waiting for a request at 45 s. So POST starts the run and answers at
# once, and GET reports it until it ends (gotcha 199).


@pytest.fixture(autouse=True)
def _no_sync_state():
    from helixis.rentvine import sync_cycle

    sync_cycle._initial_running.clear()
    sync_cycle._last_finished.clear()
    yield
    sync_cycle._initial_running.clear()
    sync_cycle._last_finished.clear()


def _start(c, *, row=True):
    """POST /sync with the background start captured rather than run."""
    spawned: list = []

    def _capture(coro, _company_id):
        spawned.append(coro)
        return True

    with (
        patch(
            "helixis.api.routes.rentvine.fetch_rentvine_row",
            new=AsyncMock(return_value={"company_id": "company-A"} if row else None),
        ),
        patch("helixis.api.routes.rentvine._spawn", side_effect=_capture),
    ):
        body = c.post("/api/v1/rentvine/sync", headers=_auth()).json()
    return body, spawned


def _finish(spawned, result):
    """Run the captured sync to its end, ``sync_one`` answering ``result``."""
    from helixis.rentvine import sync_cycle

    with (
        patch.object(sync_cycle, "sync_one", new=AsyncMock(return_value=result)),
        patch.object(sync_cycle, "get_settings", MagicMock()),
    ):
        for coro in spawned:
            asyncio.run(coro)


def _status(c):
    return c.get("/api/v1/rentvine/sync", headers=_auth()).json()


def test_sync_answers_at_once_and_the_status_reports_the_rows(client):
    from helixis.rentvine.sync import SyncResult

    body, spawned = _start(client)
    # The request returns before the sync has done anything: nothing the page
    # waits on is the crawl itself.
    assert body["ok"] is True and body["running"] is True and body["started_at"]
    assert len(spawned) == 1
    during = _status(client)
    assert during["running"] is True and during["started_at"] == body["started_at"]
    assert during["rows"] == {}

    _finish(
        spawned,
        SyncResult(
            company_id="company-A", rows_by_table={"helixis_units": 37, "helixis_leases": 8}
        ),
    )
    after = _status(client)
    assert after["running"] is False and after["ok"] is True
    assert after["rows"] == {"units": 37, "leases": 8}
    assert after["started_at"] == body["started_at"]


def test_sync_names_stale_tables_instead_of_calling_them_empty(client):
    from helixis.rentvine.sync import SyncResult

    _body, spawned = _start(client)
    _finish(
        spawned,
        SyncResult(
            company_id="company-A",
            rows_by_table={"helixis_leases": 8},
            failed={"helixis_units": "HTTP 500 from /properties/7/units"},
        ),
    )
    after = _status(client)
    assert after["ok"] is False and after["stale"] == ["units"]
    assert "units" in after["error"] and "last sync that worked" in after["error"]
    assert "HTTP 500" not in after["error"]  # the reason stays in the log


def test_a_click_during_a_sync_waits_on_that_sync(client):
    """Re-pointed 2026-10-03 from "says when one is already running". The
    founder's first Sync now after a passed test met a red "already running"
    refusal, because the test had started a sync. A second click now joins the
    run already going: same start time, and no second crawl."""
    first, spawned_first = _start(client)
    second, spawned_second = _start(client)
    assert second["ok"] is True and second["running"] is True
    assert second["started_at"] == first["started_at"]
    assert len(spawned_first) == 1 and spawned_second == []
    for coro in spawned_first:
        coro.close()


def test_a_click_during_the_sync_a_test_started_joins_it(client):
    """``start_initial_sync`` (what a passed ``/test`` kicks) holds the same slot."""
    from helixis.rentvine import sync_cycle

    run, started = sync_cycle.claim_sync("company-A")
    assert started
    body, spawned = _start(client)
    assert body["running"] is True and body["started_at"] == run.started_at.isoformat()
    assert spawned == []


def test_sync_that_did_not_finish_names_what_to_do(client):
    _body, spawned = _start(client)
    _finish(spawned, None)
    after = _status(client)
    assert after["ok"] is False and after["running"] is False
    assert "team@occupella.com" in after["error"]


def test_sync_without_a_saved_connection_does_not_run(client):
    body, spawned = _start(client, row=False)
    assert body["ok"] is False and "No Rentvine connection" in body["error"]
    assert body["running"] is False and spawned == []


def test_a_sync_that_cannot_start_frees_the_slot(client):
    """Left held, every later click would wait on a run that never ends."""
    from helixis.rentvine import sync_cycle

    with (
        patch(
            "helixis.api.routes.rentvine.fetch_rentvine_row",
            new=AsyncMock(return_value={"company_id": "company-A"}),
        ),
        patch(
            "helixis.api.routes.rentvine._spawn",
            side_effect=lambda coro, _company_id: coro.close(),
        ),
    ):
        body = client.post("/api/v1/rentvine/sync", headers=_auth()).json()
    assert body["ok"] is False and body["running"] is False
    assert "team@occupella.com" in body["error"]
    assert "company-A" not in sync_cycle._initial_running
    # And the status says the same, rather than "running" forever.
    assert _status(client)["running"] is False


def test_a_spawn_that_fails_closes_the_sync_it_was_handed():
    """No running loop is the reachable failure; the coroutine must not leak."""
    from helixis.api.routes import rentvine as routes

    async def _never_started():
        return None

    coro = _never_started()
    assert routes._spawn(coro, "company-A") is False
    assert inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED
    assert routes._background == set()


async def test_the_spawn_keeps_a_reference_until_the_sync_ends():
    """asyncio holds tasks weakly; an unreferenced sync can be collected mid-crawl."""
    from helixis.api.routes import rentvine as routes

    gate = asyncio.Event()

    async def _slow():
        await gate.wait()

    assert routes._spawn(_slow(), "company-A") is True
    assert len(routes._background) == 1
    gate.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert routes._background == set()


def test_a_second_sync_reports_itself_not_the_last_one(client):
    """While a new run goes, the status is THAT run, not the one before it."""
    from helixis.rentvine.sync import SyncResult

    first, spawned = _start(client)
    _finish(spawned, SyncResult(company_id="company-A", rows_by_table={"helixis_units": 1}))
    time.sleep(0.002)  # a distinct start time
    second, spawned_again = _start(client)
    assert second["started_at"] != first["started_at"] and len(spawned_again) == 1
    during = _status(client)
    assert during["running"] is True and during["started_at"] == second["started_at"]
    for coro in spawned_again:
        coro.close()


def test_the_status_before_any_sync_says_so(client):
    body = _status(client)
    assert body["ok"] is False and body["running"] is False and body["started_at"] is None
    assert "Sync now" in body["error"]


def test_sync_refuses_on_a_dark_deployment(dark_client):
    resp = dark_client.post("/api/v1/rentvine/sync", headers=_auth())
    assert resp.status_code == 503


def test_the_page_and_the_route_agree_on_the_sync_answer():
    """Both sides of the poll (rule 15). The page reads ``running`` and
    ``started_at`` to know whether THIS run has ended; a field renamed on one
    side only would leave it polling until it gives up on every sync."""
    import re

    from helixis.api.routes.rentvine import RentvineSyncResult

    service = (Path(__file__).resolve().parents[2] / "frontend/src/services/rentvine.ts").read_text(
        encoding="utf-8"
    )
    block = re.search(r"export interface RentvineSyncResult \{(.*?)\n\}", service, re.S)
    assert block, "rentvine.ts no longer declares RentvineSyncResult"
    declared = set(re.findall(r"^  (\w+)\??:", block.group(1), re.M))
    assert declared == set(RentvineSyncResult.model_fields)
    # How the poll uses them is pinned in frontend/src/services/rentvine.sync.test.ts.


def test_the_page_polls_inside_the_connectors_rate_limit():
    """The poll shares ``connectors_limiter`` with every connector call the
    user makes. Polling faster than half its budget would turn a long sync
    into 429s, which the page counts as failed checks and gives up on."""
    import re

    from helixis.api.rate_limit import connectors_limiter

    service = (Path(__file__).resolve().parents[2] / "frontend/src/services/rentvine.ts").read_text(
        encoding="utf-8"
    )
    found = re.search(r"export const SYNC_POLL_MS = ([\d_]+);", service)
    assert found, "rentvine.ts no longer declares SYNC_POLL_MS"
    poll_ms = int(found.group(1).replace("_", ""))
    budget = connectors_limiter.counter.requests * 60 / connectors_limiter.counter.window
    assert 60_000 / poll_ms <= budget / 2


async def test_a_cancelled_sync_frees_the_slot():
    """A deploy stopping the process mid-sync must not leave the slot held."""
    from helixis.rentvine import sync_cycle

    gate = asyncio.Event()

    async def slow(*_a, **_k):
        await gate.wait()

    run, _started = sync_cycle.claim_sync("company-A")
    with (
        patch.object(sync_cycle, "sync_one", side_effect=slow),
        patch.object(sync_cycle, "get_settings", MagicMock()),
    ):
        task = asyncio.create_task(sync_cycle.run_claimed_sync(MagicMock(), "company-A", run))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert "company-A" not in sync_cycle._initial_running
    assert sync_cycle.sync_status("company-A") is run
    assert run.finished_at is not None and run.result is None


@pytest.mark.asyncio
async def test_sync_now_never_runs_two_syncs_of_one_company():
    from helixis.rentvine import sync_cycle

    gate = asyncio.Event()

    async def slow_sync(*_a, **_k):
        await gate.wait()

    with patch.object(sync_cycle, "sync_one", side_effect=slow_sync):
        first = asyncio.create_task(sync_cycle.sync_now(MagicMock(), "company-A"))
        await asyncio.sleep(0)
        second = await asyncio.wait_for(sync_cycle.sync_now(MagicMock(), "company-A"), timeout=2)
        gate.set()
        ran_first, _ = await asyncio.wait_for(first, timeout=2)
    assert second == (False, None)
    assert ran_first is True
    assert "company-A" not in sync_cycle._initial_running


# ── the webhook signing key ────────────────────────────────────────────
#
# The receiver (`webhooks/rentvine_router.py`) reads
# `company_rentvine_credentials.encrypted_webhook_secret`; until 2026-10-04
# nothing wrote it, so every delivery would have been refused even with the
# signature scheme known (gotcha 200).

_LIVE_SCHEME = {
    "webhook_signature_header": "x-rentvine-signature",
    "webhook_signature_algorithm": "sha256",
    "webhook_signature_payload": "body",
    "webhook_signature_key_encoding": "utf8",
    "webhook_signature_digest_encoding": "base64",
}


def _put_key(c, secret: str = SIGNING_KEY):
    return c.put("/api/v1/rentvine/webhook-secret", headers=_auth(), json={"secret": secret})


def _stored_key(pool) -> str | None:
    from helixis.rentvine.credentials import decrypt_webhook_secret

    return decrypt_webhook_secret(pool._state["rows"]["company-A"], _settings())


def test_the_signing_key_is_stored_encrypted_and_reads_back_through_the_receivers_reader(
    client, fake_pool
):
    _put(client)
    resp = _put_key(client)

    assert resp.status_code == 200, resp.text
    assert resp.json()["webhook_secret_saved"] is True
    blob = fake_pool._state["rows"]["company-A"]["encrypted_webhook_secret"]
    assert blob and SIGNING_KEY not in blob
    # The receiver's own decrypt, not a copy of it.
    assert _stored_key(fake_pool) == SIGNING_KEY


def test_the_read_says_whether_a_key_is_saved_and_never_shows_it(client):
    _put(client)
    assert (
        client.get("/api/v1/rentvine/credentials", headers=_auth()).json()["webhook_secret_saved"]
        is False
    )

    _put_key(client)
    body = client.get("/api/v1/rentvine/credentials", headers=_auth()).json()
    assert body["webhook_secret_saved"] is True
    assert SIGNING_KEY not in str(body)
    assert SIGNING_KEY[-4:] not in str(body), "no last4 of the signing key (migration 0102)"


def test_the_key_is_never_logged_or_echoed_even_when_refused(client, caplog):
    _put(client)
    with caplog.at_level(logging.DEBUG):
        saved = _put_key(client)
        refused = _put_key(client, secret=SIGNING_KEY + " and-more")
    assert saved.status_code == 200
    assert refused.status_code == 422
    for text in (saved.text, refused.text, caplog.text):
        assert SIGNING_KEY not in text


def test_the_request_model_masks_the_key_in_its_repr():
    from helixis.api.routes.rentvine import RentvineWebhookSecretInput

    model = RentvineWebhookSecretInput(secret=SIGNING_KEY)
    assert SIGNING_KEY not in repr(model)
    assert SIGNING_KEY not in str(model)


def test_whitespace_around_a_pasted_key_is_not_stored(client, fake_pool):
    """The capture tool strips its copy before deriving the scheme, so a
    trailing newline kept here would make every signature check fail."""
    _put(client)
    assert _put_key(client, secret=f"  {SIGNING_KEY}\n").status_code == 200
    assert _stored_key(fake_pool) == SIGNING_KEY


@pytest.mark.parametrize("bad", ["", "   ", "k" * 7, "k" * 513, "rv-fake signing-key"])
def test_a_paste_gone_wrong_is_refused_with_a_sentence_and_stores_nothing(client, fake_pool, bad):
    from helixis.api.routes.rentvine import _WEBHOOK_SECRET_MALFORMED

    _put(client)
    resp = _put_key(client, secret=bad)
    assert resp.status_code == 422
    assert resp.json()["detail"] == _WEBHOOK_SECRET_MALFORMED
    assert fake_pool._state["rows"]["company-A"]["encrypted_webhook_secret"] is None


def test_the_length_bounds_include_both_ends(client, fake_pool):
    _put(client)
    assert _put_key(client, secret="k" * 8).status_code == 200
    assert _stored_key(fake_pool) == "k" * 8
    assert _put_key(client, secret="k" * 512).status_code == 200
    assert _stored_key(fake_pool) == "k" * 512


def test_a_key_with_no_connection_saved_is_refused_with_what_to_do_first(client, fake_pool):
    resp = _put_key(client)
    assert resp.status_code == 409
    assert resp.json()["detail"].startswith("Connect Rentvine first")
    assert fake_pool._state["rows"] == {}


def test_saving_again_replaces_the_key(client, fake_pool):
    _put(client)
    _put_key(client)
    assert _put_key(client, "rotated-signing-key").status_code == 200
    assert _stored_key(fake_pool) == "rotated-signing-key"


def test_removing_the_key_keeps_the_connection(client, fake_pool):
    _put(client)
    _put_key(client)
    resp = client.delete("/api/v1/rentvine/webhook-secret", headers=_auth())

    assert resp.status_code == 200
    assert resp.json()["configured"] is True
    assert resp.json()["webhook_secret_saved"] is False
    row = fake_pool._state["rows"]["company-A"]
    assert row["encrypted_webhook_secret"] is None
    assert row["encrypted_api_key"] and row["encrypted_api_secret"]


def test_the_key_write_refuses_while_dark_and_the_removal_does_not(dark_client, fake_pool):
    """The same asymmetry as the credentials (gotcha 120)."""
    cipher = Fernet(TEST_DATA_KEY.encode())
    fake_pool._state["rows"]["company-A"] = {
        "company_id": "company-A",
        "account_code": ACCOUNT,
        "encrypted_api_key": cipher.encrypt(API_KEY.encode()).decode(),
        "api_key_last4": API_KEY[-4:],
        "encrypted_api_secret": cipher.encrypt(API_SECRET.encode()).decode(),
        "encrypted_webhook_secret": cipher.encrypt(SIGNING_KEY.encode()).decode(),
        "base_url": f"https://{ACCOUNT}.rentvine.com/api/manager",
        "last_tested_at": None,
    }
    assert _put_key(dark_client, "key-saved-while-dark").status_code == 503
    assert _stored_key(fake_pool) == SIGNING_KEY

    resp = dark_client.delete("/api/v1/rentvine/webhook-secret", headers=_auth())
    assert resp.status_code == 200
    assert resp.json()["webhook_secret_saved"] is False


@pytest.mark.parametrize(
    ("scheme", "ready"),
    [
        ({}, False),
        ({"webhook_signature_header": "x-rentvine-signature"}, False),
        (_LIVE_SCHEME, True),
    ],
)
def test_the_read_says_whether_this_deployment_can_check_a_signature(fake_pool, scheme, ready):
    """Until it can, the receiver refuses every delivery, and the card must say
    so rather than show a saved key as webhooks working (rule 14). A half-set
    scheme refuses too (`scheme_from_settings`)."""
    for c in _client(_settings(**scheme), fake_pool):
        body = c.get("/api/v1/rentvine/credentials", headers=_auth()).json()
    assert body["webhook_scheme_ready"] is ready


def test_a_key_saved_here_verifies_a_delivery_at_the_receiver(fake_pool, monkeypatch):
    """Both ends of the seam (rule 15). The key this route stores is the one the
    receiver verifies with, through the real cipher, `decrypt_webhook_secret`
    and `verify_signature`. Only the record refresh after the check is faked."""
    from helixis.rentvine import signature as sig
    from helixis.webhooks import rentvine_router

    settings = _settings(**_LIVE_SCHEME)
    for c in _client(settings, fake_pool):
        assert _put(c).status_code == 200
        assert _put_key(c).status_code == 200

    refreshed = AsyncMock(return_value=True)
    monkeypatch.setattr(rentvine_router, "get_settings", lambda: settings)
    monkeypatch.setattr(
        rentvine_router,
        "fetch_rentvine_row_by_account_code",
        AsyncMock(
            side_effect=lambda code: (
                fake_pool._state["rows"].get("company-A") if code == ACCOUNT else None
            )
        ),
    )
    monkeypatch.setattr(rentvine_router, "refresh_one", refreshed)
    monkeypatch.setattr(rentvine_router, "get_pool", MagicMock)
    monkeypatch.setattr(rentvine_router, "write_activity", AsyncMock())
    monkeypatch.setattr(rentvine_router.webhook_limiter, "check_by_ip", AsyncMock())
    monkeypatch.setattr(rentvine_router.webhook_limiter, "refund_by_ip", MagicMock())
    app = FastAPI()
    app.include_router(rentvine_router.router)
    receiver = TestClient(app)

    scheme = sig.scheme_from_settings(settings.rentvine)
    assert scheme is not None
    body = json.dumps({"objectType": "property", "action": "updated", "propertyID": 4610}).encode()

    def deliver(key: str):
        return receiver.post(
            f"/webhooks/rentvine/{ACCOUNT}",
            content=body,
            headers={
                "x-rentvine-signature": sig.compute_digest(scheme, key, body),
                "content-type": "application/json",
            },
        )

    assert deliver(SIGNING_KEY).status_code == 200
    assert refreshed.await_count == 1
    assert deliver("another-accounts-key").status_code == 401
    assert refreshed.await_count == 1

    # Removing the key here is what stops the receiver accepting it.
    for c in _client(settings, fake_pool):
        assert c.delete("/api/v1/rentvine/webhook-secret", headers=_auth()).status_code == 200
    assert deliver(SIGNING_KEY).status_code == 401
    assert refreshed.await_count == 1


def test_the_page_and_the_route_agree_on_the_connection_status():
    """Both sides (rule 15): the card reads `webhook_secret_saved` and
    `webhook_scheme_ready`, and a field renamed on one side only would show a
    saved key as missing, or webhooks as ready when they are not."""
    import re

    from helixis.api.routes.rentvine import RentvineCredentialsStatus

    service = (Path(__file__).resolve().parents[2] / "frontend/src/services/rentvine.ts").read_text(
        encoding="utf-8"
    )
    block = re.search(r"export interface RentvineStatus \{(.*?)\n\}", service, re.S)
    assert block, "rentvine.ts no longer declares RentvineStatus"
    declared = set(re.findall(r"^  (\w+)\??:", block.group(1), re.M))
    assert declared == set(RentvineCredentialsStatus.model_fields)


def test_the_page_builds_the_url_the_receiver_serves():
    """The card shows the URL to paste into Rentvine. A path that drifted from
    the route would 404 every delivery, and nothing on the card would say so."""
    import re

    from helixis.webhooks import rentvine_router

    service = (Path(__file__).resolve().parents[2] / "frontend/src/services/rentvine.ts").read_text(
        encoding="utf-8"
    )
    built = re.search(r"`\$\{[^}]+\}(/webhooks/rentvine/)\$\{(\w+)\}`", service)
    assert built, "rentvine.ts no longer builds the webhook URL"
    served = {getattr(r, "path", "") for r in rentvine_router.router.routes}
    assert served == {built.group(1) + "{account_code}"}


def test_the_page_lists_every_event_the_receiver_handles_and_no_other():
    """The card tells the customer which webhooks to add in Rentvine, one
    event per webhook. An event the receiver handles but the card leaves out is
    a record type that never arrives early, and nothing would say so."""
    import re

    from helixis.rentvine import events

    service = (Path(__file__).resolve().parents[2] / "frontend/src/services/rentvine.ts").read_text(
        encoding="utf-8"
    )
    block = re.search(r"export const RENTVINE_WEBHOOK_EVENTS = \[(.*?)\] as const;", service, re.S)
    assert block, "rentvine.ts no longer declares RENTVINE_WEBHOOK_EVENTS"
    listed = re.findall(r"'([^']+)'", block.group(1))
    handled = {
        f"{obj.replace('_', ' ').title()} {verb}"
        for obj in events.OBJECTS
        for verb in sorted(set(events._VERBS.values()))
    }
    assert len(listed) == len(set(listed))
    assert set(listed) == handled


def test_the_status_read_selects_every_column_the_status_reads():
    """The fake pool returns whole rows whatever the SELECT names, so a column
    dropped from `_fetch_row` would pass every test here and fail in production
    with a KeyError on every read of the card."""
    import re

    from helixis.api.routes import rentvine as routes

    fetch = ast.parse(inspect.getsource(routes._fetch_row))
    (sql,) = [
        n.value
        for n in ast.walk(fetch)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and "FROM public.company_rentvine_credentials" in n.value
    ]
    columns = re.search(r"SELECT(.*?)FROM", sql, re.S)
    assert columns
    selected = {c.strip() for c in columns.group(1).split(",")}

    status = ast.parse(inspect.getsource(routes._row_to_status))
    read = {
        n.slice.value
        for n in ast.walk(status)
        if isinstance(n, ast.Subscript)
        and isinstance(n.value, ast.Name)
        and n.value.id == "row"
        and isinstance(n.slice, ast.Constant)
    }
    assert "encrypted_webhook_secret" in read
    assert read <= selected, read - selected
