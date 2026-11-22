"""Fetch a URL from this server only when it points at the public internet.

A tool that takes a URL from the model and fetches it from the API host is a
server-side request forgery primitive: the model (or a document, email or web
page steering it) names ``http://localhost:8000/...``, a Render private-network
host, or the ``169.254.169.254`` metadata service, and the server fetches what
only the server can reach.

``agents.browser_denylist`` already refuses those for the browser tool, but
by design it never resolves DNS: Browserbase fetches from ITS network, so a
hostname that resolves to a private address there reaches nothing of ours.
A fetch made with ``httpx`` from this process has no such cushion, so here the
name is resolved and EVERY address it returns must be globally routable.

⚠ The vetted address is also the one connected to. Resolving to check and
then letting the HTTP client resolve again would let a name with a zero TTL
answer a public address to the check and ``127.0.0.1`` to the connect (DNS
rebinding). :func:`vet_public_url` therefore returns the request pinned to the
address it vetted, with the original name kept as the ``Host`` header and as
the TLS server name, so the certificate is still checked against the real
hostname. Behind an HTTP proxy the pinned request fails the certificate check
instead of fetching, which is the safe direction.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass, field
from typing import Any

import httpx

_ALLOWED_SCHEMES = frozenset({"http", "https"})


@dataclass(frozen=True)
class PinnedRequest:
    """A GET to send instead of the caller's URL: same path and query, host
    replaced by the vetted address."""

    url: str
    headers: dict[str, str]
    extensions: dict[str, Any] = field(default_factory=dict)


async def _resolve(host: str, port: int) -> list[str]:
    """Every address ``host`` resolves to, through the event loop's resolver
    (a literal IP comes back as itself, with no DNS query)."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def is_public_address(address: str) -> bool:
    """True only for a globally routable unicast address.

    ``is_global`` already excludes loopback, RFC 1918, carrier-grade NAT,
    link-local (the metadata service), documentation ranges, and IPv6 forms
    that embed an IPv4 address (mapped, 6to4, Teredo). Multicast is the one
    non-unicast range it calls global, so it is refused separately."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])  # drop an IPv6 zone id
    except ValueError:
        return False
    return ip.is_global and not ip.is_multicast


async def vet_public_url(url: str) -> tuple[PinnedRequest | None, str | None]:
    """``(request, None)`` when ``url`` may be fetched, else ``(None, reason)``.

    Refuses a non-HTTP scheme, a missing host, a name that does not resolve,
    and a name with ANY non-public address (one private record among public
    ones is how a rebinding name is usually built)."""
    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, TypeError, ValueError):
        return None, "invalid_url"
    if parsed.scheme not in _ALLOWED_SCHEMES:
        return None, "non_http_scheme"
    host = parsed.raw_host.decode("ascii")  # IDNA-encoded, as DNS sees it
    if not host:
        return None, "no_host"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    try:
        addresses = await _resolve(host, port)
    except (OSError, UnicodeError):
        return None, "unresolvable_host"
    if not addresses:
        return None, "unresolvable_host"
    if not all(is_public_address(a) for a in addresses):
        return None, "non_public_address"

    # IPv4 first: an IPv6 answer is no use to a host without IPv6 egress.
    ipv4 = [a for a in addresses if ":" not in a]
    pinned = (ipv4 or addresses)[0]
    extensions: dict[str, Any] = {}
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if parsed.scheme == "https":
            extensions["sni_hostname"] = host
    return (
        PinnedRequest(
            url=str(parsed.copy_with(host=pinned)),
            headers={"Host": parsed.netloc.decode("ascii")},
            extensions=extensions,
        ),
        None,
    )


__all__ = ["PinnedRequest", "is_public_address", "vet_public_url"]
