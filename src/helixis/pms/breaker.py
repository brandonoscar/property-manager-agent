"""The circuit breaker: stop calling a vendor that keeps failing.

A copy of Rentvine's (``rentvine/client.py``, founder go-ahead 2026-10-01 for
terms rule 25), with the same constants, keyed by account so one customer's
outage never pauses another's:

* OPENS after :data:`BREAKER_FAILURES` outage failures (a 5xx, a transport
  error or a 429) within :data:`BREAKER_WINDOW_S`.
* While OPEN for :data:`BREAKER_OPEN_S`, nothing is sent: reads say
  "{name} unavailable" and writes are refused.
* Then HALF-OPEN: exactly one READ goes through as a probe. Any answer closes
  it (a 404 is the vendor working); an outage failure re-opens it. Writes stay
  refused until it closes: a write is never the probe.

⚠ Only requests that were SENT count. A refusal into a rate hold or into this
breaker sent nothing and says nothing about the vendor.

⚠ PER PROCESS, like the holds in ``pms.rate``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from helixis.pms.errors import PmsApiError, PmsUnavailableError

logger = logging.getLogger(__name__)

#: Outage failures that open the breaker.
BREAKER_FAILURES = 5
#: The window those failures must fall inside, in seconds.
BREAKER_WINDOW_S = 300.0
#: How long the breaker stays open before a probe is allowed, in seconds.
BREAKER_OPEN_S = 600.0
#: A probe that never reported back (a cancelled task) is abandoned after this
#: long, so half-open cannot wedge shut.
BREAKER_PROBE_STALE_S = 120.0


@dataclass
class _Breaker:
    #: ``time.monotonic()`` of each outage failure since the last answer.
    failures: list[float] = field(default_factory=list)
    #: When OPEN ends. ``None`` is CLOSED.
    open_until: float | None = None
    #: When the half-open probe was let through, while it is in flight.
    probe_started: float | None = None


#: ``{account key: _Breaker}``. The key is ``"<pms>:<account>"``.
_breakers: dict[str, _Breaker] = {}


def _now() -> float:
    return time.monotonic()


def is_outage(status: int) -> bool:
    """A failure that says the vendor is struggling: a transport error
    (status 0), a 429, or a 5xx. Any other 4xx is the vendor WORKING."""
    return status == 0 or status == 429 or status >= 500


def admit(account: str, name: str, *, probe: bool) -> None:
    """Raise :class:`PmsUnavailableError` unless a request may be sent.

    ``probe`` is True for reads, which may be the one half-open probe, and
    False for writes, which are refused until the breaker has closed.
    """
    b = _breakers.get(account)
    if b is None or b.open_until is None:
        return
    now = _now()
    if now < b.open_until:
        raise PmsUnavailableError(name, b.open_until - now)
    if not probe:
        raise PmsUnavailableError(name, None)
    if b.probe_started is not None and now - b.probe_started < BREAKER_PROBE_STALE_S:
        raise PmsUnavailableError(name, None)
    b.probe_started = now


def record(account: str, exc: PmsApiError | None) -> None:
    """Record the outcome of a request that was SENT. ``None`` is success."""
    b = _breakers.setdefault(account, _Breaker())
    now = _now()
    probing = b.probe_started is not None
    if exc is None or not is_outage(exc.status):
        b.failures.clear()
        b.open_until = None
        b.probe_started = None
        return
    if probing:
        b.probe_started = None
        b.failures.clear()
        b.open_until = now + BREAKER_OPEN_S
        logger.warning("%s: probe failed, circuit stays open %.0fs", account, BREAKER_OPEN_S)
        return
    b.failures = [t for t in b.failures if now - t <= BREAKER_WINDOW_S]
    b.failures.append(now)
    if len(b.failures) >= BREAKER_FAILURES:
        b.failures.clear()
        b.open_until = now + BREAKER_OPEN_S
        logger.warning(
            "%s: %d outage failures, circuit open %.0fs", account, BREAKER_FAILURES, BREAKER_OPEN_S
        )


__all__ = [
    "BREAKER_FAILURES",
    "BREAKER_OPEN_S",
    "BREAKER_PROBE_STALE_S",
    "BREAKER_WINDOW_S",
    "admit",
    "is_outage",
    "record",
]
