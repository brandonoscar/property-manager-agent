"""Rent Manager's HTTP surface, thin over ``helixis.pms.http``.

Everything here is a decision about Rent Manager; the behaviour (documented
calls only, read retries, binding waits, the breaker, scrubbed errors) is the
shared client's.

* **Only the calls the connector makes:** the sync's reads, the login POST,
  and the two work-order writes of design R4 (create, change the status) with
  the single-record reads they plan and read back from. ``write_methods`` is
  POST alone, so ``send_once`` refuses every other verb, and the index holds
  no other write: there is no DELETE anywhere (terms rule 13), and no money
  path (``pms.gated_write`` refuses one anyway). The writes go through
  ``pms.gated_write`` only (``writes.py``, ``agents/rentmanager_tools.py``).
* **``saveOptions`` is never sent** (terms rule 12): it is a forbidden key in
  any body or query, refused before anything leaves the process.
* **Status meanings** (api-notes §8): a 403 on a collection means the user
  can see NONE of it, so the table goes stale, never empty (rule 14). An empty
  collection may be a 404 (documented) or a 204 (seen in clients); only a
  collection read's first page reads either as empty. A 404 on one record is
  "not found or not permitted", never "deleted".
* **Session token** (``pms.auth.SessionTokenAuth``): one login per (company,
  location), sent in ``X-RM12Api-ApiToken``; one re-login and one replay
  after a 401, and a second 401 marks the credentials broken.
* **One location** (design R2): reads are scoped to the location the login
  named. :func:`current_location_id` reads it back so the pass can refuse a
  mismatch. ``ChangeLocation`` is never called, and is not in the index.
* **Filters only where a read filters** (design "Sync"): ``filters`` is a
  parameter of the collections an incremental pass narrows to what changed,
  and of nothing else. :func:`filters_honoured` is the canary that checks
  Rent Manager applies one at all.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from helixis.pms import registry
from helixis.pms.auth import SessionTokenAuth, header_value, host_label
from helixis.pms.errors import Failure, PmsApiError
from helixis.pms.http import ClientConfig, PmsHttpClient, operation_key
from helixis.pms.paging import PageNumberPaging
from helixis.pms.rate import RateHeaders
from helixis.rentmanager.credentials import RentManagerCredentials
from helixis.rentmanager.resources import RESOURCES, Resource

PMS = registry.RENTMANAGER
NAME = registry.PMS[PMS].name

# @unverified RM-1 the token header and the login path, from Rent Manager's
# Overview (T1c, p.13). Settled by one AuthorizeUser call, recording the
# request's field names and the response's shape (never the token).
TOKEN_HEADER = "X-RM12Api-ApiToken"
LOGIN_PATH = "/Authentication/AuthorizeUser"

# @unverified RM-2 the partner token's header name, from one open-source
# client (T2), and whether it replaces the user login. Settled by Rent
# Manager's answer to vendor question 2, then one call.
PARTNER_HEADER = "X-RM12Api-PartnerToken"

# @unverified RM-10 that this action returns the location the token is
# scoped to (the help scrape lists only its name). Settled by logging in to
# two locations and comparing what it returns.
CURRENT_LOCATION_PATH = "/Locations/CurrentLocation"

# @unverified RM-6 the paging parameter names (the help scrape's pageSize and
# pageNumber). Settled by the canary read on the probe account.
READ_PARAMS: frozenset[str] = frozenset({"fields", "embeds", "pageSize", "pageNumber"})

#: A collection an incremental pass narrows to what changed also takes
#: ``filters`` (``Resource.changed_query``). A lookup never does.
FILTERED_READ_PARAMS: frozenset[str] = READ_PARAMS | {"filters"}

#: Keys that must never appear in a request. Rent Manager's overrides (hard
#: close, owner overdraft) are the customer's own controls; a 412 is a new
#: decision for a person, never an override (terms rule 12, design R4). The
#: spellings the sources use (api-notes §14.11) and their lower case.
FORBIDDEN_KEYS: frozenset[str] = frozenset({"saveOptions", "SaveOptions", "saveoptions"})

#: Where a work order is created: the collection itself (design R4).
ISSUES_PATH = RESOURCES["work_orders"].path

# @unverified RM-27 the single-record paths the two writes read: one work
# order (with its linked property and unit embedded), one property and one
# unit, by id (the help scrape's method lists, T1c). Settled by the probe's
# RM-27 section: one GET of each by an id the collection read returned.
ISSUE_PATH = "/ServiceManagerIssues/{ServiceManagerIssueID}"
PROPERTY_PATH = "/Properties/{PropertyID}"
UNIT_PATH = "/Units/{UnitID}"

# @unverified RM-14 the categories lookup's path (the help scrape names it
# beside the status and priority lookups). Settled by reading it whole.
CATEGORIES_PATH = "/ServiceManagerCategories"

#: The writes (design R4): create one work order, change one work order. Rent
#: Manager has no PUT or PATCH; an update is a POST to the record (Overview
#: p.1-2). Nothing else is ever written: no note (RM-15 is open), no money,
#: no DELETE.
WRITE_OPERATIONS: Mapping[str, frozenset[str]] = {
    operation_key("POST", ISSUES_PATH): frozenset(),
    operation_key("POST", ISSUE_PATH): frozenset(),
}

#: The reads the writes plan and read back from. The work order is read whole
#: (no ``fields``): whichever concurrency value it carries must come back
#: (RM-9), and naming one it lacks could be refused.
WRITE_READS: Mapping[str, frozenset[str]] = {
    operation_key("GET", ISSUE_PATH): frozenset({"embeds"}),
    operation_key("GET", PROPERTY_PATH): frozenset({"fields"}),
    operation_key("GET", UNIT_PATH): frozenset({"fields"}),
    operation_key("GET", CATEGORIES_PATH): READ_PARAMS,
}

#: The operation index: every read the pass makes, the location read, the
#: login, and the two writes with their reads. Nothing else can be sent.
OPERATIONS: Mapping[str, frozenset[str]] = {
    **{
        operation_key("GET", r.path): FILTERED_READ_PARAMS if r.incremental else READ_PARAMS
        for r in RESOURCES.values()
    },
    operation_key("GET", CURRENT_LOCATION_PATH): frozenset(),
    operation_key("POST", LOGIN_PATH): frozenset(),
    **WRITE_READS,
    **WRITE_OPERATIONS,
}

#: The one verb the writes use. ``send_once`` refuses any other.
WRITE_METHODS: frozenset[str] = frozenset({"POST"})

#: Rent Manager's own status meanings, over ``pms.errors.DEFAULT_MEANINGS``
#: (Overview p.12-13). 410 is a resource removed after a deprecation notice:
#: retrying cannot bring it back.
STATUS_MEANINGS: Mapping[int, tuple[Failure, bool]] = {
    403: (Failure.PERMISSION, False),
    404: (Failure.NOT_FOUND, False),
    410: (Failure.UNDOCUMENTED, False),
}

# @unverified RM-4 an empty collection is a 404 (Overview) or a 204 (two
# clients). Settled by a filter that matches nothing, logging the status.
# @unverified RM-5 the paging headers and the 1,000-row page. Settled by one
# read of a resource with more than 1,000 rows, or pageSize=10 on a small one.
PAGING = PageNumberPaging(
    page_param="pageNumber",
    size_param="pageSize",
    page_size=1000,
    total_header="X-Total-Results",
    empty_statuses=frozenset({404, 204}),
)

# @unverified RM-13 the quota headers' names and units (a third party's
# summary of the PDF). Settled by logging the headers of the first 20 calls.
RATE_HEADERS = RateHeaders(
    limit="X-RateLimit", remaining="X-RateRemaining", reset_seconds="X-RateTimeLeft"
)

#: Design R6: at most one request a second and two at a time per company,
#: until real rate headers have been recorded.
REQUESTS_PER_SEC = 1.0
CONCURRENCY = 2

#: Design "Rate limits": a 429 that names no wait holds the account 60s.
BARE_429_HOLD_S = 60.0

# @unverified RM-19 the maintenance window's wording (two open-source
# clients) and that it is an outage. Settled by Rent Manager's answer or the
# first such reply in the app logs, which carry the vendor's error text.
MAINTENANCE_TEXT = "performing database updates"
MAINTENANCE_HOLD_S = 300.0


def maintenance_hold(status: int, text: str) -> float | None:
    """Seconds to hold the account for Rent Manager's maintenance window
    ("Service unavailable while performing database updates"), or ``None``.
    An outage, not a reason to retry hard (terms rule 3)."""
    if status >= 400 and MAINTENANCE_TEXT in (text or "").lower():
        return MAINTENANCE_HOLD_S
    return None


def base_url(corp_id: str) -> str:
    """``https://{corp}.api.rentmanager.com``. The corp id passes the host
    label guard first, so a stored value can never redirect a call."""
    return f"https://{host_label(corp_id, what='the Rent Manager corp id')}.api.rentmanager.com"


class _Masked(str):
    """A header value whose repr hides it. ``ClientConfig`` is a dataclass whose
    repr prints every field, and a partner token is a secret (terms rule 8)."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "'***'"


def request_headers(creds: RentManagerCredentials) -> dict[str, str]:
    """The headers on every request, the login included: ``Accept``, and the
    partner token when one is stored (design R1). The shared client puts them
    on the HTTP client it opens; the token's repr is masked, and it is masked
    in errors through :func:`session_auth`'s ``static_secrets``."""
    headers = {"Accept": "application/json"}
    if creds.partner_token:
        token = header_value(creds.partner_token, what="the partner token")
        headers[PARTNER_HEADER] = _Masked(token)
    return headers


def client_config(creds: RentManagerCredentials) -> ClientConfig:
    """Everything this connector decides about Rent Manager's HTTP surface.

    ⚠ ``hold_headers`` is Retry-After ONLY, though the design says a 429
    holds for Retry-After, else X-RateTimeLeft. The shared client reads its
    hold headers on EVERY error, and Rent Manager reportedly sends
    X-RateTimeLeft (seconds to the hourly reset) on every response, so
    naming it here would hold the account until the reset after an ordinary
    404 empty list. A 429 that also says the quota is spent holds until
    X-RateTimeLeft through :data:`RATE_HEADERS` instead; one that says
    nothing holds :data:`BARE_429_HOLD_S`.
    """
    return ClientConfig(
        pms=PMS,
        account=f"{creds.corp_id}:{creds.location_id}",
        base_url=base_url(creds.corp_id),
        operations=OPERATIONS,
        requests_per_sec=REQUESTS_PER_SEC,
        concurrency=CONCURRENCY,
        hold_headers=("retry-after",),
        bare_429_hold_s=BARE_429_HOLD_S,
        status_meanings=STATUS_MEANINGS,
        forbidden_keys=FORBIDDEN_KEYS,
        rate_headers=RATE_HEADERS,
        outage_hold=maintenance_hold,
        # The two work-order writes (design R4), and nothing else: the index
        # holds no other write, and pms.gated_write is the only sender.
        write_methods=WRITE_METHODS,
        extra_headers=request_headers(creds),
    )


def token_cache_key(company_id: str, location_id: int, version: str) -> str:
    """One token per (company, location, saved credential set), in this process.

    ``version`` is ``credentials.version_of`` the stored row. Saving new
    credentials changes it, so every process logs in afresh with them, and a
    "broken" mark left by the old ones does not outlive them (``pms.auth``)."""
    return f"{PMS}:{company_id}:{location_id}:{version}"


def _login_body(creds: RentManagerCredentials) -> dict[str, Any]:
    # @unverified RM-1 the login body's field names, sent by seven open-source
    # clients (T2). Settled by one AuthorizeUser call.
    return {
        "Username": creds.username,
        "Password": creds.password,
        "LocationID": creds.location_id,
    }


def session_auth(creds: RentManagerCredentials) -> SessionTokenAuth:
    """The login, its token header, and the values masked in every error."""

    async def _login(client: PmsHttpClient) -> str:
        resp = await client.login(LOGIN_PATH, json_body=_login_body(creds))
        # @unverified RM-1 the token arrives as a quoted JSON string (T2).
        # Settled by the first raw login response: its shape, never the token.
        # @unverified RM-16 a UTF-8 BOM may come first; pms.http strips one.
        # Settled by the same first raw login response.
        token = resp.body
        if not isinstance(token, str) or not token.strip():
            raise PmsApiError(
                resp.status,
                Failure.UNDOCUMENTED,
                False,
                f"{NAME}'s login answered without a token Occupella can read",
            )
        return token

    return SessionTokenAuth(
        cache_key=token_cache_key(creds.company_id, creds.location_id, creds.version),
        header=TOKEN_HEADER,
        login=_login,
        static_secrets=creds.secrets(),
        name=NAME,
    )


def build_client(creds: RentManagerCredentials) -> PmsHttpClient:
    """A client bound to this company's Rent Manager account; use it with
    ``async with`` so its connection pool is closed.

    ⚠ It reaches Rent Manager only through ``PmsHttpClient``, which opens its
    own HTTP client with :func:`request_headers`; this package imports no
    HTTP library (``test_pms_gated_write`` holds every toolkit connector to it).
    """
    return PmsHttpClient(client_config(creds), session_auth(creds), company_id=creds.company_id)


def location_id_of(body: Any) -> int | None:
    """The location id in a ``CurrentLocation`` answer, or ``None``.

    The help scrape names the action but not its answer, so the shapes a
    reader could see are all accepted: a location record, a list holding one,
    or a bare id. Anything else is ``None``, which the pass reads as "could
    not confirm the location" and refuses (never as a match).
    """
    if isinstance(body, list) and len(body) == 1:
        body = body[0]
    if isinstance(body, Mapping):
        body = body.get("LocationID")
    if isinstance(body, bool):
        return None
    if isinstance(body, int):
        return body
    if isinstance(body, str) and body.strip().isascii() and body.strip().isdigit():
        return int(body.strip())
    return None


async def current_location_id(client: PmsHttpClient) -> int | None:
    """The location the session token is scoped to (logs in first)."""
    resp = await client.get(CURRENT_LOCATION_PATH)
    return location_id_of(resp.body)


#: The design's canary: a filter no record can match. Not one that matches a
#: single row, which a one-property account would pass whether or not the
#: filter was applied.
# @unverified RM-6 that no property has the id 0. Settled with the canary
# itself: any row it brings back says either this or that filters are ignored.
CANARY_FILTER = "PropertyID,eq,0"


async def filters_honoured(client: PmsHttpClient) -> bool:
    """Whether Rent Manager applies a filter: one that can match nothing must
    come back empty. Rows back mean it was ignored, as a misspelt parameter
    is (api-notes §6), and a read narrowed by one could not be trusted.

    One page of one row of ``/Properties``, the key only. A failure
    propagates: an answer that is not a list is no evidence either way.
    """
    path = RESOURCES["properties"].path
    query = {
        "filters": CANARY_FILTER,
        "fields": RESOURCES["properties"].key,
        PAGING.page_param: 1,
        PAGING.size_param: 1,
    }
    try:
        resp = await client.get(path, query=query)
    except PmsApiError as exc:
        if exc.status in PAGING.empty_statuses:
            return True
        raise
    if resp.status in PAGING.empty_statuses:
        return True
    if not isinstance(resp.body, list):
        raise PmsApiError(
            resp.status,
            Failure.UNDOCUMENTED,
            False,
            f"{NAME} answered the filter check with something that is not a list",
        )
    return not resp.body


def total_results(headers: Mapping[str, str]) -> int | None:
    """The collection's row count from :data:`PAGING`'s total header, or
    ``None`` when it is absent or not a whole number (never 0 for unknown)."""
    wanted = PAGING.total_header.lower()
    for name, value in headers.items():
        if name.lower() == wanted:
            text = str(value).strip()
            return int(text) if text.isascii() and text.isdigit() else None
    return None


async def count_of(client: PmsHttpClient, resource: Resource) -> int | None:
    """How many rows Rent Manager says a collection holds, from ONE page of
    one row: the read the sync makes (same path, fields and embeds), with
    ``pageSize=1``. Never reads the whole list.

    ``None`` when the count is not stated, including an empty-collection
    answer (404 or 204): RM-4 is open, so that answer could also be a path
    Rent Manager does not have, and a count it did not state is never 0.
    """
    # @unverified RM-5 that a page of one row states the whole collection's
    # total in X-Total-Results. Settled by the same read, with pageSize=1.
    query = {**resource.query(), PAGING.page_param: 1, PAGING.size_param: 1}
    try:
        resp = await client.get(resource.path, query=query)
    except PmsApiError as exc:
        if exc.status in PAGING.empty_statuses:
            return None
        raise
    if resp.status in PAGING.empty_statuses:
        return None
    return total_results(resp.headers)


__all__ = [
    "BARE_429_HOLD_S",
    "CANARY_FILTER",
    "CATEGORIES_PATH",
    "CONCURRENCY",
    "CURRENT_LOCATION_PATH",
    "FILTERED_READ_PARAMS",
    "FORBIDDEN_KEYS",
    "ISSUES_PATH",
    "ISSUE_PATH",
    "LOGIN_PATH",
    "MAINTENANCE_HOLD_S",
    "NAME",
    "OPERATIONS",
    "PAGING",
    "PARTNER_HEADER",
    "PMS",
    "PROPERTY_PATH",
    "RATE_HEADERS",
    "READ_PARAMS",
    "REQUESTS_PER_SEC",
    "STATUS_MEANINGS",
    "TOKEN_HEADER",
    "UNIT_PATH",
    "WRITE_METHODS",
    "WRITE_OPERATIONS",
    "WRITE_READS",
    "base_url",
    "build_client",
    "client_config",
    "count_of",
    "current_location_id",
    "filters_honoured",
    "location_id_of",
    "maintenance_hold",
    "request_headers",
    "session_auth",
    "token_cache_key",
    "total_results",
]
