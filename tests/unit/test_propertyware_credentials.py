"""One company's Propertyware credentials: loaded, decrypted, never shown.

Terms rule 8: the client id and secret are encrypted at rest, never logged,
never in a prompt. Gotcha 70: no env fallback, ever. The system id is a header
value, so it is guarded before anything is decrypted.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest
from cryptography.fernet import Fernet

from helixis.propertyware import credentials
from helixis.propertyware.credentials import (
    PropertywareCredentials,
    PropertywareCredentialsError,
    load_credentials,
)
from tests.unit._propertyware_fake import (
    CLIENT_ID,
    CLIENT_SECRET,
    COMPANY,
    SYSTEM_ID,
    FakePool,
    creds_row,
    settings,
)


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> None:
    current = settings()
    monkeypatch.setattr("helixis.settings.get_settings", lambda: current)


async def test_this_companys_row_is_loaded_and_decrypted() -> None:
    pool = FakePool(creds=creds_row())
    got = await load_credentials(pool, COMPANY)
    assert got == PropertywareCredentials(
        company_id=COMPANY, system_id=SYSTEM_ID, client_id=CLIENT_ID, client_secret=CLIENT_SECRET
    )
    # Rule 11: the read names the company, and only this company.
    ((sql, args),) = pool.log
    assert "WHERE company_id = $1::uuid" in sql and "company_propertyware_credentials" in sql
    assert args == (COMPANY,)


async def test_no_row_is_not_connected_and_nothing_else_is_tried() -> None:
    pool = FakePool(creds=None)
    assert await load_credentials(pool, COMPANY) is None
    assert len(pool.log) == 1, "one read of this company's row, and no fallback"


async def test_no_company_reads_nothing() -> None:
    pool = FakePool(creds=creds_row())
    assert await load_credentials(pool, "") is None
    assert pool.log == []


@pytest.mark.parametrize("bad", ["99\r\nX-Evil: 1", "99\n0", "", "   ", "\x00"])
async def test_a_system_id_that_is_not_header_safe_is_refused_before_decrypting(bad: str) -> None:
    row = creds_row(system_id=bad)
    # Ciphertext that cannot decrypt: a refusal for the right reason must not
    # depend on decrypting at all.
    row["encrypted_client_id"] = row["encrypted_client_secret"] = "not-a-token"
    with pytest.raises(PropertywareCredentialsError, match="system id"):
        await load_credentials(FakePool(creds=row), COMPANY)


async def test_a_trailing_newline_is_stripped_not_sent() -> None:
    got = await load_credentials(FakePool(creds=creds_row(system_id=f" {SYSTEM_ID}\n")), COMPANY)
    assert got is not None and got.system_id == SYSTEM_ID


async def test_a_row_that_will_not_decrypt_is_refused_loudly_without_its_contents(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Gotcha 11's shape: the data key changed without a re-encrypt."""
    row = creds_row()
    other = settings().model_copy(update={"data_key": Fernet.generate_key().decode()})
    monkeypatch.setattr("helixis.settings.get_settings", lambda: other)
    with caplog.at_level(logging.DEBUG), pytest.raises(PropertywareCredentialsError) as raised:
        await load_credentials(FakePool(creds=row), COMPANY)
    assert "could not be decrypted" in str(raised.value)
    assert raised.value.__cause__ is None and raised.value.__suppress_context__
    assert COMPANY in caplog.text, "an unusable row is logged, naming the company"
    for secret in (CLIENT_ID, CLIENT_SECRET, row["encrypted_client_secret"]):
        assert secret not in caplog.text
        assert secret not in str(raised.value)


def test_the_secrets_never_print(caplog: pytest.LogCaptureFixture) -> None:
    creds = PropertywareCredentials(
        company_id=COMPANY, system_id=SYSTEM_ID, client_id=CLIENT_ID, client_secret=CLIENT_SECRET
    )
    shown = [repr(creds), str(creds), f"{creds}", f"{creds!r}"]
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("test").info("creds %s %r", creds, creds)
    shown.append(caplog.text)
    for text in shown:
        assert CLIENT_ID not in text and CLIENT_SECRET not in text
    assert SYSTEM_ID in repr(creds), "the system id is not a secret and helps an operator"


def test_the_loader_reads_only_the_columns_it_needs() -> None:
    """The stored base_url is never read: the host is a constant, so a stored
    value can never send the secret somewhere else.

    Re-pointed to add ``updated_at``: since the incremental sync, a pass
    reads everything whole when the connection changed after its last full
    read (``sync.choose_pass``), and the change time comes from the same read
    of the row as the keys."""
    columns = {c.strip() for c in credentials._COLUMNS.split(",")}
    assert columns == {
        "company_id",
        "system_id",
        "encrypted_client_id",
        "encrypted_client_secret",
        "updated_at",
    }
    assert "base_url" not in columns


async def test_the_connection_carries_when_its_row_last_changed() -> None:
    changed = datetime(2026, 10, 5, 9, 30, tzinfo=UTC)
    pool = FakePool(creds={**creds_row(), "updated_at": changed})
    got = await credentials.load_connection(pool, COMPANY)
    assert got is not None and got.changed_at == changed
    assert got.credentials == await load_credentials(pool, COMPANY)
    assert len(pool.log) == 2, "one read of the row each: the keys and the time together"


@pytest.mark.parametrize("value", [None, "2026-10-05T09:30:00Z", datetime(2026, 10, 5, 9, 30)])
async def test_a_change_time_that_is_not_a_zoned_time_is_unknown(value: object) -> None:
    """Unknown, never a guess: the pass then cannot tell whether the
    connection changed, so it reads everything whole (``sync.choose_pass``)."""
    got = await credentials.load_connection(
        FakePool(creds={**creds_row(), "updated_at": value}), COMPANY
    )
    assert got is not None and got.changed_at is None
