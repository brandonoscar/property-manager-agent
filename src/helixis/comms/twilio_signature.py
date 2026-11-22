"""Twilio webhook signature validation for the customer-care SMS endpoints.

Twilio signs every webhook it sends: it concatenates the FULL request URL
(exactly as configured in the Twilio console) with every POST form
parameter in sorted-key order (``key`` immediately followed by ``value``,
no separators), HMAC-SHA1s that string with the account's auth token, and
base64-encodes the digest into the ``X-Twilio-Signature`` header. We
recompute it and constant-time compare.

Implemented directly (no ``twilio`` SDK — the repo stays dependency-light;
same stance as ``connectors/twilio_client.py``, whose validator serves the
CRM lane against ``request.url``). THIS validator is the fail-closed
variant for the customer-care endpoints (``api/routes/twilio.py``): the
signed URL is built from the configured ``twilio.webhook_base_url`` + the
route path — never from the incoming request, which a proxy can rewrite —
so an empty/unset base URL means validation CANNOT pass.

FAIL CLOSED: an empty auth token, empty URL, or missing header returns
False (→ the route 403s) rather than raising, so the caller can uniformly
reject before any DB work.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Mapping


def compute_signature(auth_token: str, url: str, params: Mapping[str, str]) -> str:
    """The base64 HMAC-SHA1 Twilio would send for ``url`` + ``params``.

    Exposed for tests (known-good fixtures compute the expected value with
    this exact recipe) and for building simulated signed requests.
    """
    payload = url
    for key in sorted(params):
        payload += key + (params[key] or "")
    digest = hmac.new(
        auth_token.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha1,
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


def validate_twilio_signature(
    *,
    auth_token: str,
    url: str,
    params: Mapping[str, str],
    signature: str | None,
) -> bool:
    """True only when ``signature`` matches Twilio's scheme for this request.

    ``url`` must be the EXACT public URL configured in the Twilio console
    (``twilio.webhook_base_url`` + the route path — scheme, host, and path
    all participate in the HMAC). Any missing input — empty ``auth_token``,
    empty ``url``, absent ``signature`` — is False, never an exception:
    unverifiable is invalid.
    """
    if not auth_token or not url or not signature:
        return False
    expected = compute_signature(auth_token, url, params)
    return hmac.compare_digest(expected, signature)
