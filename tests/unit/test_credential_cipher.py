"""Tests for the project-wide Fernet credential cipher.

The cipher is the only place where plaintext at-rest secrets exist —
everything that calls it must round-trip through encrypt/decrypt without
mutation. Tampered ciphertext must surface as a RuntimeError.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from helixis.security.fernet import CredentialCipher
from helixis.settings import Settings


def _settings(data_key: str | None = None) -> Settings:
    return Settings(
        env="test",
        data_key=data_key if data_key is not None else Fernet.generate_key().decode(),
        memory={"enabled": False},
    )


def test_encrypt_decrypt_round_trips() -> None:
    cipher = CredentialCipher(_settings())
    plaintext = "abc-buildium-client-id-12345"
    ciphertext = cipher.encrypt(plaintext)
    assert ciphertext != plaintext
    assert cipher.decrypt(ciphertext) == plaintext


def test_decrypt_tampered_token_raises() -> None:
    cipher = CredentialCipher(_settings())
    ciphertext = cipher.encrypt("hello")
    # Flip one character — Fernet's HMAC will reject it.
    tampered = ciphertext[:-1] + ("A" if ciphertext[-1] != "A" else "B")
    with pytest.raises(RuntimeError):
        cipher.decrypt(tampered)


def test_missing_data_key_raises_at_construction() -> None:
    with pytest.raises(RuntimeError, match="HELIXIS_DATA_KEY"):
        CredentialCipher(_settings(""))


def test_last4_returns_last_four_characters() -> None:
    assert CredentialCipher.last4("abcdef-1234") == "1234"
    assert CredentialCipher.last4("abc") == "abc"  # short input passes through


def test_two_encryptions_of_same_plaintext_differ() -> None:
    """Fernet uses a per-token IV, so two encryptions of the same input
    produce different ciphertexts. Without this, an observer of the
    encrypted column could correlate equal plaintexts across rows."""
    cipher = CredentialCipher(_settings())
    a = cipher.encrypt("identical")
    b = cipher.encrypt("identical")
    assert a != b
    assert cipher.decrypt(a) == cipher.decrypt(b) == "identical"
