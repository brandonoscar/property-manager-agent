"""The DoorLoop connector's registry entry, client and credentials (slice A0).

What is pinned:
  - DoorLoop is a registry system, switched off by default, read from the
    mirror only, and every PMS-fed table its sync does not fill is blind,
    never a clean zero (rule 14);
  - the sync cycle runs its pass only when it is switched on;
  - the client reads only what DoorLoop documents (every GET in the generated
    index, a list with its paging and sorting parameters) and writes nothing;
    anything else is refused before sending;
  - DoorLoop's 405 means the key's access role, never a broken key, and its
    403 is the edge limiting this IP, which holds the account;
  - the bearer header, and the bare key masked wherever an error shows it;
  - the credentials loader: a round trip, a key that never prints, and an
    unusable row that raises without the key and is logged by company only.
All values are synthetic.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
import pytest
from cryptography.fernet import Fernet

from helixis.db.pms_coverage import PMS_FED_TABLES, blind_tables, fed_tables
from helixis.doorloop import _paths
from helixis.doorloop import client as dl_client
from helixis.doorloop import credentials as dl_credentials
from helixis.pms import rate, registry, sync_cycle
from helixis.pms.errors import Failure, PmsApiError, RefusedBeforeSendingError, classify_status
from helixis.security import CredentialCipher
from helixis.settings import Settings

COMPANY = "00000000-0000-4000-8000-0000000000d2"
API_KEY = "dl-synthetic-api-key-never-log-me-0002"
TEST_KEY = Fernet.generate_key().decode()


def _settings(**doorloop: Any) -> Settings:
    return Settings(env="test", data_key=TEST_KEY, memory={"enabled": False}, doorloop=doorloop)


# ── the registry, and what "no sync yet" means to every reader ────────────


def test_doorloop_is_a_mirror_only_system_named_as_doorloop_names_itself() -> None:
    found = registry.PMS[registry.DOORLOOP]
    assert (found.key, found.name) == ("doorloop", "DoorLoop")
    assert found.credentials_table == dl_credentials.TABLE == "company_doorloop_credentials"
    assert registry.is_mirror_pms(registry.DOORLOOP)
    assert registry.DOORLOOP in sync_cycle.systems()


def test_it_ships_switched_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("helixis.settings.get_settings", lambda: Settings(env="test"))
    assert registry.enabled(registry.DOORLOOP) is False
    assert registry.writes_enabled(registry.DOORLOOP) is False


def test_every_pms_table_its_sync_does_not_fill_reads_blind() -> None:
    """A DoorLoop workspace must read "not synced", never "$0" or "no
    tenants", for anything its sync does not fill.

    Re-pointed from "with no sync module it fills nothing": since slice A1 the
    sync exists and fills exactly its stage map (test_doorloop_shared_layer);
    every other PMS-fed table is still blind."""
    for attr in ("SYNC_STATE_STAGE", "DERIVED_TABLES", "RUN_NOTE", "sync_company"):
        assert registry.connector_attr(registry.DOORLOOP, "sync", attr) is not None
    fed = fed_tables(registry.DOORLOOP)
    assert fed and fed < PMS_FED_TABLES
    assert blind_tables(registry.DOORLOOP, PMS_FED_TABLES) == PMS_FED_TABLES - fed


@pytest.mark.parametrize("enabled", [True, False])
def test_the_sync_cycle_runs_its_pass_only_when_switched_on(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, enabled: bool
) -> None:
    """Re-pointed from "the sync cycle skips it even when switched on": since
    slice A1 the cycle finds DoorLoop's pass. Switched on, it runs it;
    switched off, it never does (the pass itself is in test_doorloop_sync)."""
    from helixis.doorloop import sync as dl_sync
    from helixis.pms.mirror import SyncResult

    calls: list[str] = []

    async def recorded(_pool: Any, company_id: str) -> SyncResult:
        calls.append(company_id)
        return SyncResult(company_id=company_id, pms=registry.DOORLOOP)

    monkeypatch.setattr(dl_sync, "sync_company", recorded)
    monkeypatch.setattr("helixis.settings.get_settings", lambda: _settings(enabled=enabled))
    assert registry.enabled(registry.DOORLOOP) is enabled
    with caplog.at_level(logging.WARNING):
        result = asyncio.run(sync_cycle.sync_one(object(), registry.DOORLOOP, COMPANY))
    assert "the connector is not built" not in caplog.text
    if enabled:
        assert isinstance(result, SyncResult) and calls == [COMPANY]
    else:
        assert result is None and calls == []


# ── the client: what DoorLoop documents, no write ─────────────────────────


def test_the_index_is_every_documented_get_and_no_write() -> None:
    """Both sides (rule 15): the client's index against the generated one."""
    ops = dl_client.operations()
    assert set(ops) == {k for k in _paths.QUERY_PARAMS if k.startswith("GET ")}
    for key, params in ops.items():
        declared = _paths.QUERY_PARAMS[key]
        expected = declared | _paths.LIST_PARAMS if key in _paths.LISTS else declared
        assert params == expected, key
    assert "page_size" in ops["GET /properties"]
    assert "page_size" not in ops["GET /properties/{propertyId}"]
    config = dl_client.config()
    assert config.write_methods == frozenset()
    assert config.base_url == dl_client.BASE_URL == _paths.BASE_URL
    assert config.concurrency == 1
    # Under the documented 50 a minute per key (DL-4).
    assert config.requests_per_sec * 60 < 50
    assert config.rate_headers == dl_client.RATE_HEADERS


def test_a_405_is_the_keys_access_role_never_a_broken_key() -> None:
    meaning = dict(dl_client.config().status_meanings)
    assert classify_status(405, meaning) == (Failure.PERMISSION, False)
    # The defaults DoorLoop shares: a 401 is the key itself.
    assert classify_status(401, meaning) == (Failure.CREDENTIALS, False)


def test_a_403_is_the_edge_and_holds_the_account_past_one_requests_wait() -> None:
    """The Rate Limiting page: "429 or 403 from the edge: you went over the
    per-IP limit. Back off ... starting at no less than 30 seconds"."""
    config = dl_client.config()
    meaning = dict(config.status_meanings)
    assert classify_status(403, meaning) == (Failure.UNDOCUMENTED, True)
    assert config.outage_hold is not None
    held = config.outage_hold(403, "")
    assert held is not None and held >= 30 and held > rate.MAX_RETRY_AFTER_S
    assert config.outage_hold(405, "") is None and config.outage_hold(500, "") is None
    assert config.bare_429_hold_s >= 30


def _recording() -> tuple[httpx.AsyncClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(405, json={"message": f"no access for {API_KEY}"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handle)), seen


def _creds() -> dl_credentials.DoorLoopCredentials:
    return dl_credentials.DoorLoopCredentials(company_id=COMPANY, api_key=API_KEY)


def test_every_other_request_is_refused_before_sending() -> None:
    """An undocumented path, a filter the list does not declare, paging on a
    single record, and any write, documented or not: none leaves the process."""

    async def go() -> list[httpx.Request]:
        http, seen = _recording()
        async with dl_client.build_client(_creds(), http=http) as dl:
            for path, query in (
                ("/properties/{propertyId}/units", None),
                ("/properties", {"filter_updatedAt_from": "2026-10-01"}),
                ("/units/{unitId}", {"page_size": 100}),
            ):
                with pytest.raises(RefusedBeforeSendingError):
                    await dl.get(
                        path, path_params={"propertyId": "p1", "unitId": "u1"}, query=query
                    )
            for verb, path in (("POST", "/tasks"), ("PUT", "/tasks/{taskId}"), ("POST", "/notes")):
                with pytest.raises(RefusedBeforeSendingError):
                    await dl.send_once(verb, path, path_params={"taskId": "t1"}, json_body={})
        return seen

    assert asyncio.run(go()) == []


def test_the_one_read_carries_the_bearer_key_and_masks_it_in_the_error() -> None:
    async def go() -> tuple[PmsApiError, list[httpx.Request]]:
        http, seen = _recording()
        async with dl_client.build_client(_creds(), http=http) as dl:
            with pytest.raises(PmsApiError) as raised:
                await dl.get(dl_client.TEST_READ)
        return raised.value, seen

    error, seen = asyncio.run(go())
    (request,) = seen
    assert request.headers["Authorization"] == f"bearer {API_KEY}"
    assert error.failure is Failure.PERMISSION and error.retryable is False
    # DoorLoop echoed the bare key; the error shows neither form of it.
    assert API_KEY not in str(error) and API_KEY not in error.detail


def test_the_auth_refuses_a_key_that_is_unsafe_on_a_header_line() -> None:
    with pytest.raises(ValueError):
        dl_client.BearerAuth(API_KEY + "\r\nX-Injected: 1")
    with pytest.raises(ValueError):
        dl_client.BearerAuth("   ")
    assert set(dl_client.BearerAuth(API_KEY).secrets()) == {API_KEY, f"bearer {API_KEY}"}


# ── the credentials loader ────────────────────────────────────────────────


class _Pool:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row = row
        self.sql: list[str] = []

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        self.sql.append(" ".join(sql.split()))
        assert args == (COMPANY,)
        return self.row


def _row(ciphertext: str) -> dict[str, Any]:
    return {"company_id": COMPANY, "encrypted_api_key": ciphertext}


def test_a_saved_key_round_trips_and_never_prints() -> None:
    settings = _settings()
    sealed = CredentialCipher(settings).encrypt(API_KEY)
    pool = _Pool(_row(sealed))
    creds = asyncio.run(dl_credentials.load_credentials(pool, COMPANY, settings=settings))
    assert creds is not None and creds.api_key == API_KEY
    assert API_KEY not in repr(creds)
    assert pool.sql == [
        "SELECT company_id, encrypted_api_key FROM public.company_doorloop_credentials "
        "WHERE company_id = $1::uuid"
    ]


def test_no_row_is_not_connected_and_no_company_reads_nothing() -> None:
    assert (
        asyncio.run(dl_credentials.load_credentials(_Pool(None), COMPANY, settings=_settings()))
        is None
    )
    pool = _Pool(None)
    assert asyncio.run(dl_credentials.load_credentials(pool, "", settings=_settings())) is None
    assert pool.sql == []


@pytest.mark.parametrize("kind", ["not a token", "unsafe once decrypted"])
def test_an_unusable_row_raises_without_the_key_and_logs_the_company_only(
    kind: str, caplog: pytest.LogCaptureFixture
) -> None:
    settings = _settings()
    cipher = CredentialCipher(settings)
    ciphertext = (
        "not-a-fernet-token" if kind == "not a token" else cipher.encrypt(API_KEY + "\nX: 1")
    )
    with (
        caplog.at_level(logging.DEBUG),
        pytest.raises(dl_credentials.DoorLoopCredentialsError) as raised,
    ):
        asyncio.run(
            dl_credentials.load_credentials(_Pool(_row(ciphertext)), COMPANY, settings=settings)
        )
    assert raised.value.__cause__ is None and raised.value.__suppress_context__
    assert API_KEY not in str(raised.value) and API_KEY not in caplog.text
    assert COMPANY in caplog.text
