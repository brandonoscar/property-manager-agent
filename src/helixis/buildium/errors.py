"""Buildium API errors."""

from __future__ import annotations

from helixis.common.errors import HelixisError


class BuildiumError(HelixisError):
    """Base class for Buildium integration errors."""


class BuildiumApiError(BuildiumError):
    """Non-retriable error returned by the Buildium API."""

    def __init__(
        self,
        status: int,
        message: str,
        *,
        code: str | None = None,
        request_id: str | None = None,
        path: str | None = None,
    ) -> None:
        self.status = status
        self.code = code
        self.request_id = request_id
        self.path = path
        super().__init__(f"Buildium {status} {path}: {message}")


class BuildiumRetryableError(BuildiumError):
    """Transient error that should be retried with backoff."""

    def __init__(self, status: int, message: str, retry_after: float | None = None) -> None:
        self.status = status
        self.retry_after = retry_after
        super().__init__(f"Buildium {status} retryable: {message}")


class QueryTooLargeError(BuildiumError):
    """Query string exceeded Buildium's 4096-char limit. Caller must chunk."""


class SignatureVerificationError(BuildiumError):
    """Webhook signature did not verify."""
