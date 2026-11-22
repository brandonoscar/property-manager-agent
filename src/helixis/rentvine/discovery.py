"""Find a company's Rentvine subdomain instead of asking them to type it.

WHY THIS EXISTS
    Buildium is one host for every customer (``api.buildium.com``), so a
    customer hands over an API key pair and nothing else; the account id is
    backfilled afterwards by ``POST /buildium/test``. Rentvine is not: its own
    spec declares ``servers[0].url`` as ``https://{account}.rentvine.com/api/
    manager`` with ``account`` a per-customer variable, and HTTP Basic as the
    only scheme. The subdomain is therefore part of the HOSTNAME, and none of
    the 143 documented paths is an identity endpoint — so there is no server to
    ask "which account is this key for?" until the answer is already known.

    That makes "paste your two keys" a discovery problem rather than a lookup:
    generate the handful of subdomains this customer plausibly owns, and let
    the credential itself say which one is right.

WHAT MAKES A HIT EVIDENCE
    A 200 from an authenticated read proves the pair authenticates AGAINST THAT
    HOST. A different tenant's host does not accept another tenant's key, so a
    single hit identifies the account rather than merely suggesting it.

    ⚠ **TWO HITS ARE A REFUSAL, NOT A TIE-BREAK.** Under the model above two
    candidates cannot both authenticate, so if two do, the model is wrong —
    most likely Rentvine answers a wildcard host and routes on the credential
    alone. Picking one would store a guess and hide the fact that the thing
    this module is built on does not hold. Probing every candidate rather than
    stopping at the first hit is what makes that observable at all; the cost is
    a handful of one-row reads, once, on connect.

WHAT IS @unverified
    Everything about what a WRONG subdomain does. ``*.rentvine.com`` is
    egress-blocked from the build container (ADR 0010 blocker B1), so nobody
    has watched a miss arrive: it may be DNS failure, a TLS mismatch, a 404, or
    a 401. :func:`_probe` therefore treats *every* non-200 and *every*
    transport exception identically — the code is correct under all four
    readings rather than betting on one. The first live connect settles it.

SAFETY
    Every candidate goes through ``transport.normalize_account_code`` before it
    can reach a URL, so a candidate derived from a customer-controlled company
    name cannot re-point the request off ``*.rentvine.com``. The credential is
    only ever sent to a host inside that zone.

    ⚠ **CANDIDATES ARE NEVER DERIVED FROM THE KEY.** A subdomain is resolved
    through DNS, which is plaintext — seeding a candidate with a prefix of the
    API key would put key material into a query that travels in the clear. The
    two sources here (the company's own name, the admin's email domain) are
    already non-secret.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

import httpx

from helixis.rentvine import transport
from helixis.rentvine.sync import SOURCES

logger = logging.getLogger(__name__)

#: Ceiling on candidates probed for one connect attempt.
#:
#: Small on purpose. These are unauthenticated-to-us guesses at somebody's
#: hostname; a long list is a port scan wearing a helpful costume, and the
#: honest fallback (ask for the subdomain) costs the customer one field.
MAX_CANDIDATES = 6

#: Seconds to wait on one candidate.
#:
#: ⚠ **NO RETRIES HERE, deliberately, and that is a departure from
#: ``client.RETRY_BACKOFF_S``.** The client retries because a transient failure
#: costs a whole mirror table (gotcha 147). A miss here is the EXPECTED outcome
#: for most candidates, so retrying one would spend the schedule proving a
#: negative — ``MAX_CANDIDATES`` × four attempts × the backoff is a minute of
#: wall clock inside a request a person is waiting on.
PROBE_TIMEOUT_S = 8.0

#: Words a property-management company's name carries that its subdomain
#: usually does not.
_NOISE_WORDS = frozenset(
    {
        "property",
        "properties",
        "management",
        "managements",
        "mgmt",
        "realty",
        "real",
        "estate",
        "rentals",
        "rental",
        "group",
        "holdings",
        "partners",
        "company",
        "co",
        "llc",
        "inc",
        "ltd",
        "lp",
        "the",
    }
)

#: Mail hosts that say nothing about the company's subdomain.
_PUBLIC_MAIL = frozenset(
    {
        "gmail",
        "googlemail",
        "yahoo",
        "hotmail",
        "outlook",
        "live",
        "msn",
        "aol",
        "icloud",
        "me",
        "mac",
        "proton",
        "protonmail",
        "comcast",
        "verizon",
        "att",
        "sbcglobal",
        "cox",
        "charter",
    }
)

_WORD_RE = re.compile(r"[a-z0-9]+")


def _words(text: str) -> list[str]:
    return _WORD_RE.findall((text or "").lower())


def candidate_codes(*, company_name: str = "", email: str = "") -> tuple[str, ...]:
    """Subdomains this customer plausibly owns, best guess first.

    Pure, ordered, deduplicated and capped at :data:`MAX_CANDIDATES`. Every
    entry has already survived ``transport.normalize_account_code``, so a
    caller cannot turn one into a request that leaves ``*.rentvine.com``.

    The ordering is the claim: the full name comes first because Rentvine
    subdomains are usually the company name run together, and the
    noise-stripped form comes next because "Blue Ridge Properties" is very
    often ``blueridge``. Nothing here is clever — it is the same guess a human
    makes, made without asking them.
    """
    words = _words(company_name)
    trimmed = [w for w in words if w not in _NOISE_WORDS] or words

    raw: list[str] = []
    if words:
        raw.append("".join(words))
        raw.append("-".join(words))
    if trimmed and trimmed != words:
        raw.append("".join(trimmed))
        raw.append("-".join(trimmed))
    # ⚠ NOT the bare first word. "Blue Ridge Properties" would add `blue`,
    # which is far likelier to be a STRANGER'S tenant than this customer's —
    # every candidate is an authentication attempt against somebody's host, so
    # a guess that is mostly wrong is a cost paid by people who are not our
    # customer. The joined forms already cover the real shapes.

    domain = (email or "").rpartition("@")[2]
    labels = _words(domain.rpartition(".")[0] or domain)
    # ⚠ The label BEFORE the public suffix, and only when it is not a public
    # mail host — probing `gmail.rentvine.com` for every customer with a Gmail
    # address is noise that can only ever miss.
    if labels and labels[-1] not in _PUBLIC_MAIL:
        raw.append(labels[-1])

    out: list[str] = []
    for value in raw:
        try:
            code = transport.normalize_account_code(value)
        except transport.RentvineTransportError:
            continue
        if code not in out:
            out.append(code)
        # ⚠ UNREACHABLE TODAY AND KEPT ANYWAY. The loop above emits at most
        # five forms against a ceiling of six, so no company name can reach
        # this break — it bounds a FUTURE generator that adds a sixth shape,
        # which is when an unauthenticated guess at somebody's hostname starts
        # costing strangers something. `test_the_cap_binds_when_it_can` says
        # the same thing and supplies the one input that makes it observable.
        if len(out) == MAX_CANDIDATES:
            break
    return tuple(out)


@dataclass(frozen=True)
class Discovery:
    """What the probe learned. ``found`` is set only when exactly one hit."""

    found: str | None
    tried: tuple[str, ...]
    #: Every candidate that authenticated, when more than one did. Empty in
    #: the ordinary case — including the ordinary FAILURE case, where nothing
    #: authenticated and the caller must ask.
    ambiguous: tuple[str, ...] = ()


async def _probe(client: httpx.AsyncClient, code: str) -> bool:
    """Does this credential authenticate against this subdomain?

    ⚠ Reads the path the SYNC reads (``sync.SOURCES[0]``) rather than whichever
    endpoint is cheapest, for gotcha 153's reason: a check that exercises a
    path the product never uses proves the credential parses and nothing about
    whether the product works.
    """
    url = transport.base_url(code) + transport.build_path(SOURCES[0][1])
    try:
        resp = await client.get(url, params=transport.page_params(1, 1))
    except httpx.HTTPError as exc:
        # DNS failure, TLS mismatch, timeout — all mean "not this one", and
        # which one it is has never been observed (see the module docstring).
        logger.debug("rentvine discovery: %s did not answer — %s", code, exc)
        return False
    return resp.status_code == 200


async def discover_account_code(
    *,
    api_key: str,
    api_secret: str,
    candidates: tuple[str, ...],
    client: httpx.AsyncClient | None = None,
) -> Discovery:
    """Probe every candidate and report which one owns this credential."""
    if not candidates:
        return Discovery(found=None, tried=())
    if not api_key or not api_secret:
        raise ValueError("discovery requires both api_key and api_secret")

    owns = client is None
    probe_client = client or httpx.AsyncClient(
        auth=(api_key, api_secret),
        timeout=PROBE_TIMEOUT_S,
        headers={"Accept": "application/json"},
    )
    try:
        results = await asyncio.gather(*(_probe(probe_client, c) for c in candidates))
    finally:
        if owns:
            await probe_client.aclose()

    hits = tuple(code for code, ok in zip(candidates, results, strict=True) if ok)
    if len(hits) == 1:
        return Discovery(found=hits[0], tried=candidates)
    # Zero hits and several hits are both "we do not know", and the caller
    # must say which one it was — they need opposite sentences.
    return Discovery(found=None, tried=candidates, ambiguous=hits if len(hits) > 1 else ())


__all__ = [
    "MAX_CANDIDATES",
    "PROBE_TIMEOUT_S",
    "Discovery",
    "candidate_codes",
    "discover_account_code",
]
