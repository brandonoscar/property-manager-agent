"""The DoorLoop client: a thin configuration over ``helixis.pms.http``.

* **The operations index is every documented GET** (:mod:`._paths`, generated
  from DoorLoop's reference), each with the query parameters it declares; a
  list also takes the paging and sorting parameters the Pagination and Sorting
  pages document for every list. ``pms.http`` refuses anything outside the
  index before sending. Undocumented endpoints are never used, even where
  DoorLoop's support suggests browser DevTools (design, rule 1).
* **No write verb at all.** ``write_methods`` is empty, so ``send_once``
  refuses everything before sending. Writes come later, through
  ``pms.gated_write``.
* **A 405 is a permission answer, not a broken key.** DoorLoop gives every key
  an access role, and a 405 means "the API token being used or the user on
  which behalf the token was created does not have access" (the Authentication
  page). Read as Rentvine reads a 403: never "reconnect" (gotcha 102).
* **A 403 is DoorLoop's edge limiting this IP**, not a permission answer: "429
  or 403 from the edge: you went over the per-IP limit. Back off exponentially
  with jitter, starting at no less than 30 seconds" (the Rate Limiting page).
  It holds the account for :data:`EDGE_HOLD_S`, as a 429 that names no wait
  does.
* **Paced below the documented limits**: one request in flight, under 50 a
  minute, and a hold once DoorLoop's quota headers say a tenth or less is
  left. The 500-an-hour cap is a sync's to budget: a sync pass spends from a
  :class:`CallBudget`, and its client refuses a page once that is spent, so a
  pass's length is bounded in code (rule 13).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from helixis.doorloop import _paths
from helixis.doorloop.credentials import DoorLoopCredentials
from helixis.pms.auth import Auth, HeaderAuth, header_value
from helixis.pms.errors import Failure
from helixis.pms.http import ClientConfig, PmsHttpClient, PmsResponse
from helixis.pms.paging import PageNumberPaging
from helixis.pms.rate import RateHeaders

#: The ``pms.registry`` key.
PMS = "doorloop"

#: The API root, a constant (the spec's one server, ``_paths.BASE_URL``). The
#: credentials row has a ``base_url`` column and it is deliberately not read: a
#: stored value must never be able to send a request carrying the key to
#: another host.
# @unverified DL-1 — documented (the spec's server and the Authentication
# page's example), never yet answered live; settled by the connection test on
# a live key.
BASE_URL = "https://app.doorloop.com/api"

#: 42 a minute: under the documented 50 a minute per key, AND above the quota
#: hold. At 45 a minute (0.75 a second) a rolling minute would leave exactly a
#: tenth of 50, which is where :data:`RATE_HEADERS` holds the account, so any
#: read longer than a minute could stop on a hold of up to a minute (a pass
#: refuses a hold above ``pms.rate.MAX_RETRY_AFTER_S``). Eight left is above it.
# @unverified DL-4 — documented on the Rate Limiting page: 50 a minute and 500
# an hour per key, rolling, and about 10 a second per IP; settled live by the
# first throttle met, or by a sync that meets none.
REQUESTS_PER_SEC = 0.7
CONCURRENCY = 1

#: DoorLoop's quota headers (the Rate Limiting page). Once a tenth or less of
#: the window is left, the account holds until the reset.
RATE_HEADERS = RateHeaders(
    limit="RateLimit-Limit", remaining="RateLimit-Remaining", reset_seconds="RateLimit-Reset"
)

#: How long a 403, or a 429 that names no wait, holds the account. DoorLoop's
#: edge answers either to an IP over its limit and asks for a backoff "starting
#: at no less than 30 seconds". Above ``pms.rate.MAX_RETRY_AFTER_S``, so the
#: request stops rather than sleeping through it.
EDGE_HOLD_S = 60.0

#: What the connection test reads: the first page of properties, with no
#: query parameter.
# @unverified DL-3 — a 405 for a role without access is documented (the
# Authentication page); a 200 for a key whose role reads properties, and a 401
# for a wrong key, are not; settled by the test on a live key, and the 401 by
# one call with a wrong key.
TEST_READ = "/properties"

#: DoorLoop's meaning for a status the shared defaults read otherwise.
STATUS_MEANINGS: dict[int, tuple[Failure, bool]] = {
    # The key's access role (the Authentication page).
    405: (Failure.PERMISSION, False),
    # DoorLoop's edge limiting this IP (the Rate Limiting page): back off.
    403: (Failure.UNDOCUMENTED, True),
}


def edge_hold(status: int, _body: str) -> float | None:
    """A 403 is DoorLoop's edge limiting this IP: hold the account."""
    return EDGE_HOLD_S if status == 403 else None


class BearerAuth(HeaderAuth):
    """``Authorization: bearer <key>`` on every request.

    ``secrets()`` masks the bare key as well as the header value, so an error
    body that echoes only the key is masked too.
    """

    # @unverified DL-2 — the scheme word as the authentication page writes it
    # ("bearer"); settled by the first call on a live key.
    SCHEME = "bearer"

    def __init__(self, api_key: str):
        key = header_value(api_key, what="the DoorLoop API key")
        super().__init__({"Authorization": f"{self.SCHEME} {key}"})
        self._key = key

    def secrets(self) -> tuple[str, ...]:
        return (*super().secrets(), self._key)


def operations() -> dict[str, frozenset[str]]:
    """The client's operations index: every documented GET with the query
    parameters it declares, and a list's paging and sorting parameters. No
    write: this slice sends none."""
    return {
        key: params | (_paths.LIST_PARAMS if key in _paths.LISTS else frozenset())
        for key, params in _paths.QUERY_PARAMS.items()
        if key.startswith("GET ")
    }


def config() -> ClientConfig:
    """The client configuration. No account label in the URL: DoorLoop tells
    accounts apart by the key, so the label is the system's name."""
    return ClientConfig(
        pms=PMS,
        account=PMS,
        base_url=BASE_URL,
        operations=operations(),
        requests_per_sec=REQUESTS_PER_SEC,
        concurrency=CONCURRENCY,
        bare_429_hold_s=EDGE_HOLD_S,
        status_meanings=STATUS_MEANINGS,
        rate_headers=RATE_HEADERS,
        outage_hold=edge_hold,
        # No write verb at all: this slice sends nothing.
        write_methods=frozenset(),
    )


#: How a list is paged (the Pagination page): ``page_number`` from 1 and
#: ``page_size`` up to the documented maximum, the rows under ``data`` and the
#: total under ``total`` in the body.
PAGING = PageNumberPaging(
    page_param="page_number",
    size_param="page_size",
    page_size=_paths.MAX_PAGE_SIZE,
    rows_key="data",
    total_key="total",
)


class CallBudgetSpent(Exception):  # noqa: N818 — a stop, not a fault
    """The pass's call budget is spent: the page was not asked for."""


class CallBudget:
    """At most ``limit`` pages asked for in one sync pass (rule 13).

    A page is one :meth:`PmsHttpClient.get`. The shared client retries a page
    itself on a retryable failure, by its own bounded schedule (``pms.rate``),
    and those retries are not counted again: the shared client has no hook
    that sees each send.
    """

    def __init__(self, limit: int):
        if limit < 1:
            raise ValueError("a call budget allows at least one call")
        self.limit = limit
        self.used = 0

    @property
    def spent(self) -> bool:
        return self.used >= self.limit

    def spend(self) -> None:
        """Take one call, or raise :class:`CallBudgetSpent` without taking it."""
        if self.spent:
            raise CallBudgetSpent(f"the call budget of {self.limit} is spent")
        self.used += 1


class BudgetedClient(PmsHttpClient):
    """A client whose every GET spends one call from a :class:`CallBudget`,
    and is refused before sending once the budget is spent."""

    def __init__(
        self,
        config: ClientConfig,
        auth: Auth,
        *,
        budget: CallBudget,
        company_id: str | None = None,
        client: Any = None,
    ):
        super().__init__(config, auth, company_id=company_id, client=client)
        self.budget = budget

    async def get(
        self,
        template: str,
        *,
        path_params: Mapping[str, Any] | None = None,
        query: Mapping[str, Any] | None = None,
    ) -> PmsResponse:
        self.budget.spend()
        return await super().get(template, path_params=path_params, query=query)


def build_client(
    credentials: DoorLoopCredentials, *, http: Any = None, budget: CallBudget | None = None
) -> PmsHttpClient:
    """A client bound to this company's DoorLoop key, spending from ``budget``
    when one is given (a sync's; :class:`BudgetedClient`).

    ``http`` is for tests only: an ``httpx.AsyncClient`` over a mock transport,
    handed to ``PmsHttpClient`` as it is. Production passes nothing.
    """
    auth = BearerAuth(credentials.api_key)
    if budget is not None:
        return BudgetedClient(
            config(), auth, budget=budget, company_id=credentials.company_id, client=http
        )
    return PmsHttpClient(config(), auth, company_id=credentials.company_id, client=http)


__all__ = [
    "BASE_URL",
    "CONCURRENCY",
    "EDGE_HOLD_S",
    "PAGING",
    "PMS",
    "RATE_HEADERS",
    "REQUESTS_PER_SEC",
    "STATUS_MEANINGS",
    "TEST_READ",
    "BearerAuth",
    "BudgetedClient",
    "CallBudget",
    "CallBudgetSpent",
    "build_client",
    "config",
    "edge_hold",
    "operations",
]
