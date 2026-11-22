"""How a connector proves who it is, and the guards on values that reach a URL
or a header.

* :class:`HeaderAuth`: fixed headers on every request (Propertyware's client
  id, client secret and system id).
* :class:`SessionTokenAuth`: log in, cache one token per (company, location)
  in this process, and on a 401 log in ONCE and replay ONCE. A second 401
  marks the credentials broken: every later call refuses without sending,
  until :func:`forget` is called by a reconnect. No loop (Rent Manager's
  undocumented token lifetime, design/rentmanager-connector.md).

The guards copy Rentvine's (``rentvine/transport.py``): a value stored on a
credentials row never becomes a host or a header line without one, so a
stored value cannot redirect calls carrying a credential (measured on
Rentvine: ``evil.example/x`` once became the request's host).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Protocol

from helixis.pms.errors import Failure, PmsApiError

if TYPE_CHECKING:
    from helixis.pms.http import PmsHttpClient

#: One DNS label: what may be interpolated into a host. A dot is refused too:
#: nothing legitimate needs one, and a guard that admits one separator is one
#: parser bug away from admitting the others.
_HOST_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")

#: Any control character, CR and LF included (header injection).
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def host_label(value: str, *, what: str) -> str:
    """``value`` lowercased, if it is a single DNS label; else ``ValueError``
    naming what it should be. Lowercased because DNS is case-insensitive."""
    label = (value or "").strip().strip("/").lower()
    if not label or not _HOST_LABEL_RE.match(label):
        raise ValueError(f"{what} must be letters, digits and hyphens only")
    return label


def header_value(value: str, *, what: str) -> str:
    """``value`` stripped, if it is safe on a header line; else ``ValueError``.
    Refuses empty values and any control character."""
    cleaned = (value or "").strip()
    if not cleaned or _CONTROL_RE.search(cleaned):
        raise ValueError(f"{what} is empty or contains a control character")
    return cleaned


class Auth(Protocol):
    """What :class:`~helixis.pms.http.PmsHttpClient` asks of an auth scheme."""

    #: True when a 401 can be answered by logging in again.
    refreshable: bool

    async def headers(self, client: PmsHttpClient) -> dict[str, str]:
        """The headers for the next request (may log in first)."""
        ...

    async def refresh(self, client: PmsHttpClient, used: Mapping[str, str]) -> None:
        """Answer a 401 on a request sent with ``used``."""
        ...

    def mark_broken(self) -> None:
        """A second 401: stop sending until the credentials are re-saved."""
        ...

    def secrets(self) -> tuple[str, ...]:
        """Every value to mask in an error or a log."""
        ...


class HeaderAuth:
    """Fixed headers on every request. A 401 is final: nothing to refresh."""

    refreshable = False

    def __init__(self, headers: Mapping[str, str]):
        self._headers = {name: header_value(v, what=name) for name, v in headers.items()}

    async def headers(self, client: PmsHttpClient) -> dict[str, str]:
        return dict(self._headers)

    async def refresh(self, client: PmsHttpClient, used: Mapping[str, str]) -> None:
        raise RuntimeError("HeaderAuth has nothing to refresh")  # pragma: no cover

    def mark_broken(self) -> None:
        """Nothing to remember: every call sends the same headers, so the next
        401 says the same thing without a flag."""

    def secrets(self) -> tuple[str, ...]:
        return tuple(self._headers.values())


#: ``{cache key: token}``. Per process; the key names the system, the company
#: and the location, so one company's token is never another's.
_tokens: dict[str, str] = {}
#: Keys whose credentials a second 401 (or a refused login) proved wrong.
_broken: set[str] = set()
#: One login at a time per key, so concurrent requests share one token.
_locks: dict[str, asyncio.Lock] = {}


def forget(cache_key: str) -> None:
    """Drop a key's token and its broken mark (the customer reconnected)."""
    _tokens.pop(cache_key, None)
    _broken.discard(cache_key)


class SessionTokenAuth:
    """A token from a login call, sent in one header.

    ``login`` performs the login through ``client.login(...)`` and returns the
    token. ``static_secrets`` are the values the login sends (username,
    password, partner token), masked wherever an error is shown.
    """

    refreshable = True

    def __init__(
        self,
        *,
        cache_key: str,
        header: str,
        login: Callable[[PmsHttpClient], Awaitable[str]],
        static_secrets: Sequence[str] = (),
        name: str = "the property-management system",
    ):
        self._key = cache_key
        self._header = header
        self._login = login
        self._static = tuple(s for s in static_secrets if s)
        self._name = name

    def _refused(self) -> PmsApiError:
        return PmsApiError(
            401,
            Failure.CREDENTIALS,
            False,
            f"not sent: {self._name} refused these credentials twice. Reconnect "
            f"{self._name} on the Connectors page.",
            sent=False,
        )

    async def _log_in(self, client: PmsHttpClient) -> str:
        try:
            token = await self._login(client)
        except PmsApiError as exc:
            if exc.failure in (Failure.CREDENTIALS, Failure.PERMISSION):
                self.mark_broken()
            raise
        token = header_value(token, what="the session token")
        _tokens[self._key] = token
        return token

    async def headers(self, client: PmsHttpClient) -> dict[str, str]:
        if self._key in _broken:
            raise self._refused()
        token = _tokens.get(self._key)
        if token is None:
            async with _locks.setdefault(self._key, asyncio.Lock()):
                token = _tokens.get(self._key) or await self._log_in(client)
        return {self._header: token}

    async def refresh(self, client: PmsHttpClient, used: Mapping[str, str]) -> None:
        """Log in again, unless another request already replaced the token
        this one was sent with (then its replay uses the new one)."""
        stale = used.get(self._header)
        async with _locks.setdefault(self._key, asyncio.Lock()):
            current = _tokens.get(self._key)
            if current is not None and current != stale:
                return
            _tokens.pop(self._key, None)
            await self._log_in(client)

    def mark_broken(self) -> None:
        _broken.add(self._key)
        _tokens.pop(self._key, None)

    def secrets(self) -> tuple[str, ...]:
        token = _tokens.get(self._key)
        return (*self._static, token) if token else self._static


__all__ = [
    "Auth",
    "HeaderAuth",
    "SessionTokenAuth",
    "forget",
    "header_value",
    "host_label",
]
