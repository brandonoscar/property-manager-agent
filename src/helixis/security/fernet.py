"""Fernet-based envelope encryption for at-rest credentials.

The single project-wide ``HELIXIS_DATA_KEY`` (Fernet key) wraps every
secret we persist in Postgres — Buildium client_id, client_secret, the
per-company webhook signing secret, etc. Tokens carry their own AEAD
(Fernet = AES-128-CBC + HMAC-SHA256), so corruption / tampering is
detected at decrypt-time.

Long-term we'll move these into Supabase Vault (see ADR-0002 /
docs/security-posture.md). The Vault migration replaces this module
with a thin client that still exposes the same encrypt/decrypt
surface, so call sites don't need to change.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from helixis.settings import Settings


class CredentialCipher:
    """Encrypt / decrypt small strings with the project's data key."""

    def __init__(self, settings: Settings) -> None:
        if not settings.data_key:
            raise RuntimeError(
                "HELIXIS_DATA_KEY is not set — at-rest credential encryption "
                "cannot run. Generate one with "
                "`python -c 'from cryptography.fernet import Fernet; "
                "print(Fernet.generate_key().decode())'` and provision it "
                "to every environment."
            )
        # Fernet keys are 32 bytes URL-safe base64-encoded. Pass through
        # the raw string — Fernet itself raises ValueError on bad keys.
        self._fernet = Fernet(settings.data_key.encode())

    def encrypt(self, plaintext: str) -> str:
        """Encrypt ``plaintext`` and return the URL-safe ciphertext token."""
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        """Decrypt ``ciphertext``; raises RuntimeError if tampering detected."""
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken as exc:  # pragma: no cover — paranoia path
            raise RuntimeError(
                "Failed to decrypt credential: token is invalid or expired. "
                "This usually means HELIXIS_DATA_KEY rotated without "
                "re-encrypting existing rows."
            ) from exc

    @staticmethod
    def last4(plaintext: str) -> str:
        """Last four characters for masked-display in the UI."""
        return plaintext[-4:] if len(plaintext) >= 4 else plaintext
