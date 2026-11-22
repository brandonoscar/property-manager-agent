"""Every PMS credentials table is the backend's alone (migration 0110), and a
Buildium client never sends the keys anywhere but Buildium (gotcha 212).

Measured on helixis-test (2026-10-05, again 2026-10-07): 0003 and 0100 let any
company member read and rewrite their company's Buildium and Rentvine
credentials rows through PostgREST, past the API's admin check. Buildium's
client used the row's ``base_url`` as its host, so a member could have pointed
it at their own server and been sent the company's client id and secret.

Two sides, read together (rule 15):

* the migrations: walked in order, every credentials table ends with no policy
  and with anon and authenticated revoked, and the set of credentials tables is
  the registry's;
* the code: the address the connect route stores is exactly what the loader
  accepts, and any other address is refused before a secret is decrypted.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet

from helixis.api.routes import buildium as buildium_routes
from helixis.buildium import credentials as bc
from helixis.pms import registry
from helixis.rentvine import credentials as rv_credentials
from helixis.rentvine.transport import RentvineTransportError
from helixis.security import CredentialCipher
from helixis.settings import Settings

REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO / "supabase" / "migrations"

#: The two roles PostgREST runs a request as. Supabase grants both every
#: privilege on a new public table by default, which no migration states, so a
#: table counts as granted until a migration revokes it.
API_ROLES = ("anon", "authenticated")

_CREDENTIALS = re.compile(r"^company_[a-z0-9_]+_credentials$")
_TABLE = r"(?:public\.)?\"?([a-z0-9_]+)\"?"
_CREATE_TABLE = re.compile(rf"^create table (?:if not exists )?{_TABLE}", re.I)
_CREATE_POLICY = re.compile(rf"^create policy (\"[^\"]+\"|\S+) on {_TABLE}", re.I)
_DROP_POLICY = re.compile(rf"^drop policy (?:if exists )?(\"[^\"]+\"|\S+) on {_TABLE}", re.I)
_REVOKE = re.compile(rf"^revoke (.+?) on (?:table )?{_TABLE} from (.+)$", re.I | re.S)
_GRANT = re.compile(rf"^grant (.+?) on (?:table )?{_TABLE} to (.+)$", re.I | re.S)


def _statements(sql: str) -> list[str]:
    """One file's statements, comments removed, whitespace collapsed.

    ⚠ Statement-level, never a substring search over the file: every migration
    header quotes the statements it is about (gotcha 40's trap). A statement
    built inside a ``DO`` block or ``format()`` is invisible here, which is why
    the applied state is also checked from the live catalog
    (``docs/migrations/applied.md``, 0110).
    """
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    sql = "\n".join(line.split("--", 1)[0] for line in sql.splitlines())
    return [" ".join(part.split()) for part in sql.split(";") if part.strip()]


def _roles(text: str) -> set[str]:
    return {r.strip().strip('"').lower() for r in text.split(",")}


def _final_state() -> tuple[set[str], dict[str, set[str]], dict[str, set[str]]]:
    """Walk every migration in order: the credentials tables created, the
    policies each ends with, and the API roles each is still granted to."""
    created: set[str] = set()
    policies: dict[str, set[str]] = {}
    granted: dict[str, set[str]] = {}
    for path in sorted(MIGRATIONS.glob("[0-9][0-9][0-9][0-9]_*.sql")):
        for stmt in _statements(path.read_text(encoding="utf-8")):
            if (m := _CREATE_TABLE.match(stmt)) and _CREDENTIALS.match(m[1]):
                created.add(m[1])
                granted.setdefault(m[1], set(API_ROLES))
            elif m := _CREATE_POLICY.match(stmt):
                policies.setdefault(m[2], set()).add(m[1].strip('"'))
            elif m := _DROP_POLICY.match(stmt):
                policies.setdefault(m[2], set()).discard(m[1].strip('"'))
            elif m := _REVOKE.match(stmt):
                granted.setdefault(m[2], set(API_ROLES)).difference_update(_roles(m[3]))
            elif m := _GRANT.match(stmt):
                granted.setdefault(m[2], set()).update(_roles(m[3]) & set(API_ROLES))
    return created, policies, granted


CREDENTIALS_TABLES = sorted(spec.credentials_table for spec in registry.PMS.values())


def test_the_migrations_create_exactly_the_registrys_credentials_tables() -> None:
    created, _policies, _granted = _final_state()
    assert created == set(CREDENTIALS_TABLES)


@pytest.mark.parametrize("table", CREDENTIALS_TABLES)
def test_no_credentials_table_ends_with_a_policy(table: str) -> None:
    """RLS on with no policy: a member's token reads and writes nothing."""
    _created, policies, _granted = _final_state()
    assert policies.get(table, set()) == set(), (table, policies.get(table))


@pytest.mark.parametrize("table", CREDENTIALS_TABLES)
def test_no_credentials_table_ends_granted_to_an_api_role(table: str) -> None:
    """So a policy added by mistake later still opens nothing."""
    _created, _policies, granted = _final_state()
    assert granted.get(table) == set(), (table, granted.get(table))


def test_the_walk_sees_the_policies_0003_and_0100_created() -> None:
    """Without this, a parser that matched nothing would pass every test above."""
    seen: dict[str, set[str]] = {}
    for name in ("0003_company_buildium_credentials.sql", "0100_company_rentvine_credentials.sql"):
        for stmt in _statements((MIGRATIONS / name).read_text(encoding="utf-8")):
            if m := _CREATE_POLICY.match(stmt):
                seen.setdefault(m[2], set()).add(m[1].strip('"'))
    assert seen == {
        "company_buildium_credentials": {
            "members can read buildium creds",
            "members can write buildium creds",
        },
        "company_rentvine_credentials": {
            "members can read rentvine creds",
            "members can write rentvine creds",
        },
    }


# ── Buildium: the stored address is one of Buildium's, or nothing is sent ────


def _settings() -> Settings:
    return Settings().model_copy(update={"data_key": Fernet.generate_key().decode()})


def _row(settings: Settings, base_url: Any) -> dict[str, Any]:
    cipher = CredentialCipher(settings)
    return {
        "company_id": "11111111-1111-1111-1111-111111111111",
        "encrypted_client_id": cipher.encrypt("test-client-id"),
        "encrypted_client_secret": cipher.encrypt("test-client-secret"),
        "client_id_last4": "t-id",
        "account_id": 501,
        "base_url": base_url,
        "last_tested_at": None,
    }


def test_the_route_stores_exactly_the_addresses_the_loader_accepts() -> None:
    stored = {buildium_routes._resolve_base_url(env) for env in ("sandbox", "production")}
    assert stored == bc.BUILDIUM_BASE_URLS
    assert {buildium_routes._environment_from_url(url) for url in bc.BUILDIUM_BASE_URLS} == {
        "sandbox",
        "production",
    }


@pytest.mark.parametrize("url", sorted(bc.BUILDIUM_BASE_URLS))
def test_buildiums_own_addresses_load(url: str) -> None:
    s = _settings()
    out = bc.decrypt_buildium_settings_from(_row(s, url), s)
    assert (out.base_url, out.client_id, out.client_secret) == (
        url,
        "test-client-id",
        "test-client-secret",
    )


FOREIGN = [
    "https://attacker.example",
    "http://api.buildium.com",
    "https://api.buildium.com.attacker.example",
    "https://api.buildium.com@attacker.example",
    "https://attacker.example/https://api.buildium.com",
    "https://api.buildium.com:8443",
    "https://api.buildium.com/",
    " https://api.buildium.com",
    "",
    None,
]


@pytest.mark.parametrize("url", FOREIGN)
def test_any_other_address_is_refused_before_a_secret_is_decrypted(
    url: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = _settings()
    row = _row(s, url)

    def no_decrypt(_self: Any, _token: Any) -> str:
        raise AssertionError("a secret was decrypted for a foreign host")

    monkeypatch.setattr(CredentialCipher, "decrypt", no_decrypt)
    with pytest.raises(bc.BuildiumHostRefusedError) as refused:
        bc.decrypt_buildium_settings_from(row, s)
    assert "Save the Buildium keys again" in str(refused.value)


async def test_a_refused_row_loads_as_nothing_and_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _settings()
    reported: list[str] = []

    async def fetch(_company_id: str) -> dict[str, Any]:
        return _row(s, "https://attacker.example")

    monkeypatch.setattr(bc, "fetch_buildium_row", fetch)
    monkeypatch.setattr(bc, "get_settings", lambda: s)
    monkeypatch.setattr(bc, "capture_swallowed", lambda _exc, *, op: reported.append(op))
    assert (
        await bc.load_buildium_settings_for_company("11111111-1111-1111-1111-111111111111") is None
    )
    assert reported == ["buildium.credentials.host_refused"]


# ── Rentvine: its host comes from the account code, never the stored address ─


def _rentvine_row(settings: Settings, **over: Any) -> dict[str, Any]:
    cipher = CredentialCipher(settings)
    row = {
        "company_id": "11111111-1111-1111-1111-111111111111",
        "account_code": "acme",
        "encrypted_api_key": cipher.encrypt("test-key"),
        "api_key_last4": "-key",
        "encrypted_api_secret": cipher.encrypt("test-secret"),
        "base_url": "https://attacker.example",
        "last_tested_at": None,
    }
    row.update(over)
    return row


def test_rentvines_stored_address_is_never_its_host() -> None:
    """Why 0110 needs no Rentvine code change: the column a member could
    rewrite is not read, and the account code is checked as a DNS label."""
    s = _settings()
    loaded = rv_credentials.decrypt_rentvine_settings_from(_rentvine_row(s), s)
    assert rv_credentials.resolved_base_url(loaded) == "https://acme.rentvine.com/api/manager"


@pytest.mark.parametrize(
    "code", ["attacker.example/x?", "attacker.example#", "a b", "acme.attacker"]
)
def test_a_rewritten_account_code_cannot_move_the_host(code: str) -> None:
    s = _settings()
    loaded = rv_credentials.decrypt_rentvine_settings_from(_rentvine_row(s, account_code=code), s)
    with pytest.raises(RentvineTransportError):
        rv_credentials.resolved_base_url(loaded)
