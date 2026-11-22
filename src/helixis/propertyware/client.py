"""The Propertyware client: a thin configuration over ``helixis.pms.http``.

Everything a request needs is decided here and nowhere else:

* **The operations index is the generated one, cut down to the reads this
  slice makes** (:data:`READS`). ``_paths.py`` is generated from Propertyware's
  official spec, case and all, because a wrong-case path answers 404
  (api-notes §4). A path outside the index is refused before sending.
* **No payment-type endpoint, reads included** (design W3, terms rule 11).
  :func:`operations` refuses to build an index holding one, and a test walks
  the index the client is actually given.
* **No beta read** (terms rule 2). The generated index records each
  operation's beta marker; a beta-marked read is refused the same way.
* **The sync's client cannot write.** :func:`config` allows no write verb at
  all, so its ``send_once`` refuses everything before sending; the probe and
  Phase 8 build on it. The two work-order writes (design W4) have a client of
  their own (:func:`write_config`): exactly their two operations and the
  three single-record reads they need, the verbs ``POST`` and ``PATCH`` and
  no other, and they are sent only by ``pms.gated_write``. Never a DELETE,
  a bulk endpoint, a beta write or a payment-type path.
* **Every list is paged with ``limit=500`` exactly** (design: over 500 is
  ambiguous) and is complete only when the rows read equal ``X-Total-Count``.
  A 404 on a list call is an ERROR: no status is read as "no rows"
  (:data:`PAGING` sets no ``empty_statuses``), so a wrong path leaves the
  table stale instead of emptying it.
* **One request in flight and at most 2 a second per company** (design W6),
  until real limits are seen (PW-12).
* **A sync pass spends from a call budget** (:class:`CallBudget`, sized in
  ``sync.CALL_BUDGET``): its client refuses, before sending, every page asked
  for once the budget is spent, so a pass's length is bounded in code (rule
  13). The probe, Phase 8 and the connect test build a client without one.

The three credential headers are named exactly as the spec names them
(``_paths.AUTH_HEADERS``). The spec declares no ``securitySchemes``, so the
generator reads them from its Authentication section.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from helixis.pms.auth import Auth, HeaderAuth
from helixis.pms.http import ClientConfig, PmsHttpClient, PmsResponse, operation_key
from helixis.pms.paging import OffsetPaging
from helixis.propertyware import _paths, writes
from helixis.propertyware.credentials import PropertywareCredentials

#: The ``pms.registry`` key.
PMS = "propertyware"

#: The API root, a constant. The credentials row has a ``base_url`` column
#: and it is deliberately not read: a stored value must never be able to send
#: requests carrying the client secret to another host.
BASE_URL = "https://api.propertyware.com/pw/api/rest/v1"

#: Design W6: one request in flight, at most two a second per company, until
#: Propertyware's real limits are seen. The only published figure is a
#: commented-out "FUTURE" draft (api-notes §5).
# @unverified PW-12 — Propertyware's real rate limit and any rate header;
# recorded from the first throttle met in a live session.
REQUESTS_PER_SEC = 2.0
CONCURRENCY = 1

#: Always sent, exactly. Over 500 is "overridden to the default to 500",
#: which is ambiguous (api-notes §4).
# @unverified PW-3 — that limit=500 returns 500 rows when that many exist;
# settled by one leases read on a live account.
PAGE_LIMIT = 500

#: Offset paging with the total in ``X-Total-Count``.
#:
#: ⚠ No ``empty_statuses``: a 404 on a list is an error, never "no rows".
#: Propertyware answers a wrong-case path or paging name with 404, and an
#: emptied table would read as "this company has none".
# @unverified PW-4 — that X-Total-Count is present on every list read here
# (the spec declares it on each); settled by the first page of each list.
PAGING = OffsetPaging(limit=PAGE_LIMIT, total_header="X-Total-Count")

#: Sent on every full read. Each list's description says "Sortable by: ...
#: id", and a stable order keeps offset paging from skipping or repeating a
#: row that changes while the read is under way.
# @unverified PW-13 — that orderby=id asc is accepted on each list (the spec
# lists id as sortable); settled by one call per list on a live account.
ORDER_BY_ID = "id asc"

#: The change-time sort key, as each list's "Sortable by" note writes it
#: (``_paths.SORTABLE``).
CHANGE_SORT_KEY = "lastmodifieddatetime"

#: Sent on an incremental (windowed) read of a list that declares
#: :data:`CHANGE_SORT_KEY` sortable (:func:`sorts_by_change`): the window's
#: rows oldest change first, the design's order. Every other windowed read
#: sends :data:`ORDER_BY_ID`.
# @unverified PW-13 — that orderby=lastmodifieddatetime asc is accepted on
# each list whose description lists the field as sortable; settled by one
# call per list on a live account.
ORDER_BY_CHANGE = f"{CHANGE_SORT_KEY} asc"

#: Every read this slice makes, as the spec writes the path. Nothing else is
#: in the client's index.
READS: tuple[str, ...] = (
    "/buildings",
    "/units",
    "/portfolios",
    "/contacts",
    "/vendors",
    "/leases",
    "/leases/charges",
    "/workorders",
    "/bills",
    "/accounting/glaccounts",
)

#: Path words that name a payment-type endpoint (design W3): bill payments,
#: vendor checks, owner draws, owner contributions, bank deposits, lease
#: payments, refunds and auto e-payments. Matched inside each lower-cased
#: path segment, so ``/bills/payments``, ``/leases/{leaseId}/autoepayments``
#: and ``/accounting/bankdeposits`` all match. Charges are not here: W3 reads
#: delinquency from them, and they are not a payment endpoint.
PAYMENT_WORDS: tuple[str, ...] = (
    "payment",
    "vendorcheck",
    "ownerdraw",
    "ownercontribution",
    "bankdeposit",
    "refund",
)


def sorts_by_change(path: str) -> bool:
    """Whether the list at ``path`` declares :data:`CHANGE_SORT_KEY` sortable."""
    return CHANGE_SORT_KEY in _paths.SORTABLE.get(operation_key("GET", path), frozenset())


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


class UnusableReadError(RuntimeError):
    """A read in :data:`READS` that the client must not make: undocumented,
    beta-marked, payment-type, or a list without ``X-Total-Count``."""


class UnusableWriteError(RuntimeError):
    """An operation the write client must not hold: undocumented, beta-marked,
    payment-type, a DELETE or a bulk endpoint."""


def is_payment_path(path: str) -> bool:
    """Whether ``path`` names a payment-type endpoint (:data:`PAYMENT_WORDS`).
    ``{params}`` are not segments."""
    segments = [s for s in path.lower().split("/") if s and not s.startswith("{")]
    return any(word in segment for segment in segments for word in PAYMENT_WORDS)


def operations() -> dict[str, frozenset[str]]:
    """The client's operations index: :data:`READS`, each as a GET from the
    generated index with the query parameters the spec declares for it.

    Refuses (:class:`UnusableReadError`) a read that is not in the spec, is
    beta-marked, names a payment endpoint, or declares no ``X-Total-Count``
    (such a list could never be read as complete). Checked when a client is
    built, not at import, so a bad index stops the sync and never the readers
    that only need this package's stage map.
    """
    index: dict[str, frozenset[str]] = {}
    for path in READS:
        key = operation_key("GET", path)
        if key not in _paths.QUERY_PARAMS:
            raise UnusableReadError(f"{key} is not in Propertyware's spec")
        if key in _paths.BETA:
            raise UnusableReadError(f"{key} is beta-marked; terms rule 2 keeps it out")
        if is_payment_path(path):
            raise UnusableReadError(f"{key} is a payment-type endpoint (design W3)")
        if key not in _paths.TOTAL_COUNT:
            raise UnusableReadError(f"{key} declares no X-Total-Count; it can never be complete")
        index[key] = _paths.QUERY_PARAMS[key]
    return index


def config(system_id: str) -> ClientConfig:
    """The client configuration for one Propertyware account."""
    return ClientConfig(
        pms=PMS,
        account=system_id,
        base_url=BASE_URL,
        operations=operations(),
        requests_per_sec=REQUESTS_PER_SEC,
        concurrency=CONCURRENCY,
        # No write verb at all: the sync, the probe and Phase 8 send nothing.
        # The two writes have their own client (write_config).
        write_methods=frozenset(),
    )


def write_operations() -> dict[str, frozenset[str]]:
    """The write client's operations index: the two writes
    (``writes.WRITES``) and the single-record reads they need
    (``writes.WRITE_READS``), each from the generated index.

    Refuses (:class:`UnusableWriteError`) an operation that is not in the
    spec, is beta-marked (terms rule 12: a beta write is a per-customer
    capability Brandon turns on, not in this build), names a payment-type
    endpoint, deletes, or is a bulk endpoint (terms rule 14).
    """
    wanted = [*writes.WRITES, *(("GET", path) for path in writes.WRITE_READS)]
    index: dict[str, frozenset[str]] = {}
    for method, path in wanted:
        key = operation_key(method, path)
        if key not in _paths.QUERY_PARAMS:
            raise UnusableWriteError(f"{key} is not in Propertyware's spec")
        if key in _paths.BETA:
            raise UnusableWriteError(f"{key} is beta-marked; terms rule 12 keeps it out")
        if is_payment_path(path):
            raise UnusableWriteError(f"{key} is a payment-type endpoint (design W3)")
        if method == "DELETE" or "bulk" in path.lower().split("/"):
            raise UnusableWriteError(f"{key}: no deletes and no bulk endpoints (terms rule 14)")
        index[key] = _paths.QUERY_PARAMS[key]
    return index


def write_config(system_id: str) -> ClientConfig:
    """The client configuration the work-order writes use: their operations
    only, and exactly their verbs. Same host, pacing and account key as the
    sync's client, so the two share one rate budget and one breaker."""
    return ClientConfig(
        pms=PMS,
        account=system_id,
        base_url=BASE_URL,
        operations=write_operations(),
        requests_per_sec=REQUESTS_PER_SEC,
        concurrency=CONCURRENCY,
        write_methods=writes.WRITE_METHODS,
    )


def auth(credentials: PropertywareCredentials) -> HeaderAuth:
    """The three credential headers, named as the spec names them.

    ``HeaderAuth`` refuses an empty value or a control character in any of
    them; the system id was already checked when the row was loaded.
    """
    client_id_header, client_secret_header, system_id_header = _paths.AUTH_HEADERS
    return HeaderAuth(
        {
            client_id_header: credentials.client_id,
            client_secret_header: credentials.client_secret,
            system_id_header: credentials.system_id,
        }
    )


def build_client(
    credentials: PropertywareCredentials, *, http: Any = None, budget: CallBudget | None = None
) -> PmsHttpClient:
    """A client bound to this company's Propertyware account, spending from
    ``budget`` when one is given (the sync's; :class:`BudgetedClient`).

    ``http`` is for tests only: an ``httpx.AsyncClient`` over a mock
    transport, handed to ``PmsHttpClient`` as it is. Production passes
    nothing. This package never imports an HTTP library itself
    (``test_pms_gated_write`` holds it to that): every request goes through
    ``PmsHttpClient``.
    """
    if budget is not None:
        return BudgetedClient(
            config(credentials.system_id),
            auth(credentials),
            budget=budget,
            company_id=credentials.company_id,
            client=http,
        )
    return PmsHttpClient(
        config(credentials.system_id),
        auth(credentials),
        company_id=credentials.company_id,
        client=http,
    )


def build_write_client(credentials: PropertywareCredentials, *, http: Any = None) -> PmsHttpClient:
    """The client the work-order writes hand to ``pms.gated_write``: bound to
    this company's account, holding only the write operations. ``http`` is for
    tests only, as in :func:`build_client`."""
    return PmsHttpClient(
        write_config(credentials.system_id),
        auth(credentials),
        company_id=credentials.company_id,
        client=http,
    )


__all__ = [
    "BASE_URL",
    "CHANGE_SORT_KEY",
    "CONCURRENCY",
    "ORDER_BY_CHANGE",
    "ORDER_BY_ID",
    "PAGE_LIMIT",
    "PAGING",
    "PAYMENT_WORDS",
    "PMS",
    "READS",
    "REQUESTS_PER_SEC",
    "BudgetedClient",
    "CallBudget",
    "CallBudgetSpent",
    "UnusableReadError",
    "UnusableWriteError",
    "auth",
    "build_client",
    "build_write_client",
    "config",
    "is_payment_path",
    "operations",
    "sorts_by_change",
    "write_config",
    "write_operations",
]
