"""Rate limits: holds, Retry-After, backoff, and the per-account token bucket.

Copies the behaviour of Rentvine's client (``rentvine/client.py``), with the
same constants, so every connector honours a vendor's limits the same way:

* A 429 and any ``Retry-After`` (seconds or an HTTP date) are binding: the
  whole account is held for that long, and nothing in this process sends to it
  until the time is up (Rentvine terms rule 1; both new connectors' terms docs).
* An unreadable wait counts as :data:`UNREADABLE_RETRY_AFTER_S`, never as zero.
* A wait longer than :data:`MAX_RETRY_AFTER_S` stops the request instead of
  sleeping through it, so a long hold never turns into a long request.
* Jitter only ever lengthens a wait.

⚠ PER PROCESS, like Rentvine's: the web and the worker each keep their own
holds. Both honour every 429; sharing state across processes is a later
decision (design/connector-shared-layer.md, Risks).
"""

from __future__ import annotations

import email.utils
import math
import random
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

#: Waits before each retry of a retryable READ, in seconds. The attempt count
#: is ``len(RETRY_BACKOFF_S) + 1``, derived so the two cannot drift.
RETRY_BACKOFF_S: tuple[float, ...] = (0.5, 2.0, 5.0)

#: Waits after a 429 that names no wait. Slower on purpose: a 429 asks for
#: LESS load, so a shorter schedule would turn a throttle into a burst.
RATE_LIMIT_BACKOFF_S: tuple[float, ...] = (10.0, 30.0)

#: The longest server-asked wait one request waits out in place. A longer ask
#: stops the request and holds the account instead (never shortened).
MAX_RETRY_AFTER_S = 30.0

#: What an unreadable wait (junk, negative, not finite) is taken to ask for.
#: Above :data:`MAX_RETRY_AFTER_S`, so the request stops and the account holds.
UNREADABLE_RETRY_AFTER_S = 60.0

#: Random extra wait on every backoff, as a fraction of it. Added, never
#: subtracted, so jitter cannot shorten a wait the server asked for.
JITTER_FRACTION = 0.25

#: ``{account key: time.monotonic() deadline}``. The key is ``"<pms>:<account>"``.
_holds: dict[str, float] = {}


def _now() -> float:
    return time.monotonic()


def jitter(delay: float) -> float:
    """``delay`` plus up to :data:`JITTER_FRACTION` more. Never less."""
    return delay * (1.0 + random.uniform(0.0, JITTER_FRACTION))  # noqa: S311 — not crypto


def hold(account: str, seconds: float) -> None:
    """Refuse to send to ``account`` for ``seconds``. Extends, never shortens."""
    _holds[account] = max(_holds.get(account, 0.0), _now() + seconds)


def held_for(account: str) -> float | None:
    """Seconds left on ``account``'s hold, or ``None`` when it may be called."""
    deadline = _holds.get(account)
    if deadline is None:
        return None
    remaining = deadline - _now()
    if remaining <= 0:
        _holds.pop(account, None)
        return None
    return remaining


def _seconds_from(raw: str) -> float | None:
    """One header value as seconds to wait, ``None`` for no wait, or
    :data:`UNREADABLE_RETRY_AFTER_S` for anything that cannot be read."""
    raw = raw.strip()
    try:
        seconds = float(raw)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(raw)
        except (TypeError, ValueError, IndexError):
            return UNREADABLE_RETRY_AFTER_S
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - datetime.now(UTC)).total_seconds()
        return seconds if seconds > 0 else None
    if not math.isfinite(seconds) or seconds < 0:
        return UNREADABLE_RETRY_AFTER_S
    return seconds if seconds > 0 else None


def retry_after_seconds(
    headers: Mapping[str, str], names: Sequence[str] = ("retry-after",)
) -> float | None:
    """The first of ``names`` that the response carries, as seconds to wait.

    ``Retry-After`` takes both legal forms (seconds and an HTTP date). A
    connector may name a vendor header after it (Rent Manager's
    ``X-RateTimeLeft``, ``@unverified``). ``None`` means no wait was asked; a
    header that is present and unreadable is never ``None``.
    """
    lowered = {k.lower(): v for k, v in headers.items()}
    for name in names:
        raw = lowered.get(name.lower())
        if raw is not None:
            return _seconds_from(raw)
    return None


@dataclass(frozen=True)
class RateHeaders:
    """A vendor's quota headers, when a connector knows their names.

    Every name is ``@unverified`` until a live response shows it. When the
    remaining count falls to :attr:`low_fraction` of the limit or below, the
    account holds until the reset, so the vendor is never pushed to a 429.
    """

    limit: str
    remaining: str
    reset_seconds: str
    low_fraction: float = 0.10


def low_quota_hold(headers: Mapping[str, str], spec: RateHeaders | None) -> float | None:
    """Seconds to hold because the quota is nearly spent, or ``None``.

    A missing or unreadable limit or remaining count holds nothing (nothing is
    known). A reset that cannot be read holds :data:`UNREADABLE_RETRY_AFTER_S`.
    """
    if spec is None:
        return None
    lowered = {k.lower(): v for k, v in headers.items()}
    try:
        limit = float(lowered[spec.limit.lower()])
        remaining = float(lowered[spec.remaining.lower()])
    except (KeyError, ValueError):
        return None
    if not (math.isfinite(limit) and math.isfinite(remaining)) or limit <= 0:
        return None
    if remaining > limit * spec.low_fraction:
        return None
    raw = lowered.get(spec.reset_seconds.lower())
    if raw is None:
        return UNREADABLE_RETRY_AFTER_S
    # A reset already due needs no hold; an unreadable one reads as 60s.
    return _seconds_from(raw)


def schedule_for(status: int, retry_after: float | None) -> tuple[float, ...]:
    """The backoff a failure earns: the slower one for a 429 that named no
    wait. Whether to retry at all is the classifier's call, never this one."""
    if status == 429 and retry_after is None:
        return RATE_LIMIT_BACKOFF_S
    return RETRY_BACKOFF_S


def retry_delay(retry_after: float | None, fallback: float) -> float | None:
    """How long to wait before the next attempt, or ``None`` to stop.

    The server's number wins and never gets LESS than our fallback. An ask
    above :data:`MAX_RETRY_AFTER_S` is ``None``: stop rather than come back
    before the server said we could.
    """
    if retry_after is None:
        return fallback
    if retry_after > MAX_RETRY_AFTER_S:
        return None
    return max(retry_after, fallback)


def bucket(account: str, requests_per_sec: float) -> Any:
    """The per-account request-rate ceiling: the same limiter Buildium and
    Rentvine use, process-wide and keyed by account, so every client for one
    account shares one budget. Every attempt takes a token, retries included."""
    from helixis.buildium.client import _get_rate_bucket

    return _get_rate_bucket(account, requests_per_sec)


__all__ = [
    "JITTER_FRACTION",
    "MAX_RETRY_AFTER_S",
    "RATE_LIMIT_BACKOFF_S",
    "RETRY_BACKOFF_S",
    "UNREADABLE_RETRY_AFTER_S",
    "RateHeaders",
    "bucket",
    "hold",
    "held_for",
    "jitter",
    "low_quota_hold",
    "retry_after_seconds",
    "retry_delay",
    "schedule_for",
]
