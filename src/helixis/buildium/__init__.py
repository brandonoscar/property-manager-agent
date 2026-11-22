"""Buildium integration — HTTP client + endpoint wrappers.

All requests are server-to-server; Buildium does not support CORS.
See docs/buildium-webhooks.md for credential + webhook setup.
"""

from helixis.buildium.client import BuildiumClient
from helixis.buildium.errors import (
    BuildiumApiError,
    BuildiumError,
    BuildiumRetryableError,
    QueryTooLargeError,
    SignatureVerificationError,
)

__all__ = [
    "BuildiumClient",
    "BuildiumError",
    "BuildiumApiError",
    "BuildiumRetryableError",
    "QueryTooLargeError",
    "SignatureVerificationError",
]
