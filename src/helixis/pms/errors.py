"""What a failed call to a property-management system means.

Shared by every connector built on ``helixis.pms`` (Rent Manager,
Propertyware). The meanings are Rentvine's (``rentvine/transport.py``
``classify_status``), so a 403 is never told "reconnect" (gotcha 102), with
two additions both new connectors document: 409 (the record changed under
us) and 412 (the vendor wants an override Occupella never sends).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from enum import Enum


class Failure(Enum):
    """What a non-2xx means, in terms a caller can act on."""

    #: 401: the credential itself is wrong or revoked. Reconnecting helps.
    CREDENTIALS = "credentials"
    #: 403: the credential was accepted and THIS operation is not permitted.
    #: The fix is the vendor-side role or permission, never a reconnect.
    PERMISSION = "permission"
    #: 404: no such record (or, where a connector says so, an empty list).
    NOT_FOUND = "not_found"
    #: 400: we sent something malformed. Ours to fix, never the customer's.
    BAD_REQUEST = "bad_request"
    #: 409: the record changed since we read it. Re-read; never resend blind.
    CONFLICT = "conflict"
    #: 412: the vendor asks for an override. Occupella never sends one; the
    #: person decides in the vendor's own screen.
    NEEDS_OVERRIDE = "needs_override"
    #: Anything else, 429 and 5xx included.
    UNDOCUMENTED = "undocumented"
    #: Never a status the vendor sent: Occupella refused before sending (a
    #: rate hold or the circuit breaker). Nothing changed at the vendor.
    UNAVAILABLE = "unavailable"


#: ``status -> (meaning, retryable)``. Everything not listed is
#: ``UNDOCUMENTED`` and retryable, 429 included: a status we cannot explain
#: backs off rather than failing a whole table (Rentvine's rule).
DEFAULT_MEANINGS: Mapping[int, tuple[Failure, bool]] = {
    400: (Failure.BAD_REQUEST, False),
    401: (Failure.CREDENTIALS, False),
    403: (Failure.PERMISSION, False),
    404: (Failure.NOT_FOUND, False),
    409: (Failure.CONFLICT, False),
    412: (Failure.NEEDS_OVERRIDE, False),
}


def classify_status(
    status: int, overrides: Mapping[int, tuple[Failure, bool]] | None = None
) -> tuple[Failure, bool]:
    """``(meaning, retryable)`` for a response status. A connector's own
    ``overrides`` win over the defaults."""
    if overrides and status in overrides:
        return overrides[status]
    return DEFAULT_MEANINGS.get(status, (Failure.UNDOCUMENTED, True))


class PmsApiError(Exception):
    """A request to the vendor came back unusable, or was refused before sending.

    Carries the classified meaning, so callers act on what it means rather than
    re-deciding from a bare status.
    """

    def __init__(
        self,
        status: int,
        failure: Failure,
        retryable: bool,
        detail: str,
        *,
        retry_after: float | None = None,
        sent: bool = True,
    ):
        self.status = status
        self.failure = failure
        self.retryable = retryable
        self.detail = detail
        #: Seconds the server asked us to wait. ``None`` means it did not say,
        #: never "wait zero".
        self.retry_after = retry_after
        #: ``False`` ONLY when Occupella refused before anything left the
        #: process. A write guard reads it to tell "provably not sent, safe to
        #: retry" from "may have landed". Default ``True``: claiming not-sent
        #: wrongly would invite a double write.
        self.sent = sent
        super().__init__(f"{status} ({failure.value}): {detail[:200]}")


class PmsUnavailableError(PmsApiError):
    """The circuit breaker refused to send. Nothing changed at the vendor.

    ``remaining`` is seconds until a probe is allowed, or ``None`` while the
    probe is deciding. Not retryable: a retry loop stops instead of waiting.
    """

    def __init__(self, name: str, remaining: float | None):
        self.remaining = remaining
        when = (
            f"for about {max(1, math.ceil(remaining / 60))} more minute(s)"
            if remaining is not None
            else f"while it checks whether {name} is back"
        )
        super().__init__(
            503,
            Failure.UNAVAILABLE,
            False,
            f"{name} unavailable: not sent. {name} kept failing, so Occupella paused "
            f"calls to it {when}.",
            sent=False,
        )


class RefusedBeforeSendingError(Exception):
    """A request Occupella will not send at all: an operation or query
    parameter the vendor does not document, a forbidden body key, or a verb a
    connector may not use. Raised before anything leaves the process.

    ⚠ Fail-closed on purpose. A vendor that ignores an unknown filter returns
    the UNFILTERED collection with a 200 (gotcha 24), which answers a different
    question convincingly.
    """


__all__ = [
    "DEFAULT_MEANINGS",
    "Failure",
    "PmsApiError",
    "PmsUnavailableError",
    "RefusedBeforeSendingError",
    "classify_status",
]
