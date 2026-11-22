"""One company's Rent Manager credentials: decrypted, guarded, never shown.

Three properties, and the last two would rot quietly:

1. the secrets round-trip through Fernet and the row is read for ONE company;
2. a secret never reaches a repr, a string, an error or a log (terms rule 8);
3. there is no env fallback anywhere in the package (gotcha 70): checked on
   the AST, because a behavioural test cannot see a fallback being added.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest

from helixis.rentmanager import credentials as rmc
from helixis.rentmanager.credentials import CredentialsUnusableError, RentManagerCredentials
from tests.unit._rentmanager_fakes import (
    COMPANY,
    CORP,
    LOCATION,
    PARTNER,
    PASSWORD,
    USERNAME,
    FakePool,
    app_settings,
    credentials_row,
)

PACKAGE = Path(rmc.__file__).resolve().parent


async def _load(row: dict | None) -> RentManagerCredentials | None:
    return await rmc.load_credentials(
        FakePool(credentials=row), COMPANY, app_settings=app_settings()
    )


async def test_the_secrets_round_trip_and_the_plain_values_are_guarded() -> None:
    row = credentials_row(app_settings(), corp_id="SynthCorp", partner=PARTNER)
    creds = await _load(row)
    assert creds is not None
    assert (creds.username, creds.password, creds.partner_token) == (USERNAME, PASSWORD, PARTNER)
    assert creds.corp_id == "synthcorp", "a host label, lowercased"
    assert (creds.company_id, creds.location_id) == (COMPANY, LOCATION)
    assert creds.secrets() == (USERNAME, PASSWORD, PARTNER)


async def test_the_partner_token_is_optional() -> None:
    creds = await _load(credentials_row(app_settings()))
    assert creds is not None and creds.partner_token is None
    assert creds.secrets() == (USERNAME, PASSWORD)


async def test_the_row_is_read_for_this_company_only() -> None:
    pool = FakePool(credentials=credentials_row(app_settings()))
    await rmc.load_credentials(pool, COMPANY, app_settings=app_settings())
    ((sql, args),) = pool.log
    assert "FROM public.company_rentmanager_credentials WHERE company_id = $1::uuid" in sql
    assert args == (COMPANY,)
    assert "*" not in sql, "a later column must not ride into every caller's hands"


async def test_no_row_is_not_a_customer_and_no_company_reads_nothing() -> None:
    assert await _load(None) is None
    pool = FakePool(credentials=credentials_row(app_settings()))
    assert await rmc.load_credentials(pool, "", app_settings=app_settings()) is None
    assert pool.log == []


def test_no_secret_reaches_a_repr_or_a_string() -> None:
    creds = RentManagerCredentials(
        company_id=COMPANY,
        corp_id=CORP,
        location_id=LOCATION,
        username=USERNAME,
        password=PASSWORD,
        partner_token=PARTNER,
    )
    shown = repr(creds) + str(creds) + f"{creds}" + f"{creds!r}"
    for secret in (USERNAME, PASSWORD, PARTNER):
        assert secret not in shown
    assert CORP in shown, "the plain values still show, so the repr is useful"


@pytest.mark.parametrize(
    ("over", "why"),
    [
        ({"corp_id": "evil.example/x"}, "corp id"),
        ({"corp_id": ""}, "corp id"),
        ({"location_id": 0}, "location"),
        ({"location_id": True}, "location"),
        ({"location_id": None}, "location"),
        ({"encrypted_password": "not-a-fernet-token"}, "decrypt"),
    ],
)
async def test_an_unusable_row_is_refused_by_name_never_by_value(
    over: dict[str, object], why: str, caplog: pytest.LogCaptureFixture
) -> None:
    row = credentials_row(app_settings())
    row.update(over)
    with caplog.at_level(logging.DEBUG), pytest.raises(CredentialsUnusableError) as caught:
        await _load(row)
    assert why in str(caught.value)
    for secret in (USERNAME, PASSWORD, "evil.example"):
        assert secret not in str(caught.value) and secret not in caplog.text


async def test_a_partner_token_that_cannot_be_a_header_is_refused() -> None:
    row = credentials_row(app_settings(), partner="token\r\nX-Injected: 1")
    with pytest.raises(CredentialsUnusableError) as caught:
        await _load(row)
    assert "X-Injected" not in str(caught.value)


async def test_the_data_key_comes_from_settings_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No settings handed in: the deployment's, read now (one patch point)."""
    monkeypatch.setattr("helixis.settings.get_settings", lambda: app_settings())
    pool = FakePool(credentials=credentials_row(app_settings()))
    creds = await rmc.load_credentials(pool, COMPANY)
    assert creds is not None and creds.password == PASSWORD


def test_nothing_in_the_package_reads_credentials_from_the_environment() -> None:
    """Gotcha 70's shape is an env fallback. Walk every module: no ``os``
    environment read, and no attribute of ``settings.rentmanager`` beyond its
    switches."""
    files = sorted(PACKAGE.glob("*.py"))
    assert len(files) >= 6, "the walk is not reading the package"
    offenders: list[str] = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in {"environ", "getenv"}:
                offenders.append(f"{path.name}:{node.lineno} os.{node.attr}")
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "rentmanager"
                and node.attr not in {"enabled", "writes_enabled", "sync_interval_hours"}
            ):
                offenders.append(f"{path.name}:{node.lineno} settings.rentmanager.{node.attr}")
            if isinstance(node, ast.ImportFrom) and node.module == "os":
                offenders.append(f"{path.name}:{node.lineno} from os import ...")
    assert offenders == []


# ── which saved credential set (the session token's key) ──────────────


def test_every_save_is_a_new_version_and_the_version_names_nothing_secret() -> None:
    """A re-save is a reconnect, even with the same values: each save encrypts
    afresh, so the version changes and every process logs in again."""
    settings = app_settings()
    first, again = credentials_row(settings), credentials_row(settings)
    assert rmc.version_of(first) != rmc.version_of(again)
    assert rmc.version_of(first) == rmc.version_of(dict(first)), "stable for one save"
    assert rmc.version_of({**first, "location_id": 2}) != rmc.version_of(first)
    assert rmc.version_of({**first, "corp_id": "othercorp"}) != rmc.version_of(first)
    with_token = credentials_row(settings, partner=PARTNER)
    assert rmc.version_of({**with_token, "encrypted_partner_token": None}) != rmc.version_of(
        with_token
    )
    version = rmc.version_of(with_token)
    assert len(version) == 16
    for secret in (USERNAME, PASSWORD, PARTNER):
        assert secret not in version


def test_the_loaded_credentials_carry_the_version_of_their_row() -> None:
    row = credentials_row(app_settings())
    creds = rmc.decrypt_row(row, app_settings(), company_id=COMPANY)
    assert creds.version == rmc.version_of(row)


def test_the_version_reads_every_column_the_login_is_made_of() -> None:
    """A login column left out of the fingerprint is a change the token cache
    would not see. The loader's own column list is the reference."""
    loaded = {c.strip() for c in rmc._COLUMNS.split(",")} - {"company_id"}
    assert set(rmc._LOGIN_COLUMNS) == loaded
