"""Rentvine wire rules: URLs, query validation, pagination, error meaning.

Nothing here decides how Rentvine data maps into our mirror — that is ADR
0010's job and it is still at review. These are the facts about *Rentvine's*
HTTP surface, which are identical under every mapping decision, so they are
safe to build before the gate clears.

Every rule below is sourced to the captured T1 spec
(``docs/rentvine/vendor/docs.rentvine.com/openapi.json``, OpenAPI 3.1.0,
143 paths / 205 operations). **Documented is not observed** — Phase 8 against
a live sandbox is what turns any of it into an observation. Where the spec is
silent, this module declines to guess and says so.

There are no write helpers here on purpose. Rentvine's money surface has
exactly one documented reversal affordance in the entire API, so a write path
needs the confirm gate and the fail-closed role floor that CLAUDE.md gotcha 21
records — that belongs with the write tools, not in transport.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

from ._query_index import QUERY_PARAMS, SPEC_SHA256

__all__ = [
    "SPEC_SHA256",
    "QUERY_PARAMS",
    "BASE_URL_TEMPLATE",
    "PAGE_PARAM",
    "PAGE_SIZE_PARAM",
    "RentvineTransportError",
    "UnknownOperationError",
    "UnknownQueryParameterError",
    "Failure",
    "base_url",
    "normalize_account_code",
    "build_path",
    "operation_key",
    "QueryMap",
    "QueryValue",
    "validate_query",
    "page_params",
    "page_looked_complete",
    "total_pages_from",
    "PAGINATION_HEADERS",
    "MEASURED_PAGINATION_HEADERS",
    "OPERATIONS_WITHOUT_PAGINATION_HEADERS",
    "classify_status",
]

#: T1, ``servers[0].url``. ``account`` is the customer's own subdomain, so the
#: base URL is per-company and cannot be a deployment-global setting.
BASE_URL_TEMPLATE = "https://{account}.rentvine.com/api/manager"

#: T1, ``#/components/parameters/page`` — integer, default 1, referenced by 44
#: operations.
PAGE_PARAM = "page"
#: T1. Declared three ways (two shared components at 25 and 15, plus 13 inline
#: definitions whose defaults span 15/20/25/250/5000). Because the defaults
#: differ by more than two orders of magnitude across operations, a caller must
#: send this explicitly rather than trust whatever a given endpoint defaults to.
PAGE_SIZE_PARAM = "pageSize"


class RentvineTransportError(Exception):
    """Base for refusals raised before a request leaves the process."""


class UnknownOperationError(RentvineTransportError):
    """The spec does not document this verb+path.

    Deliberately NOT the same error as an unknown parameter. The spec is a
    ceiling on what is *documented*, not on what exists: five call sites in the
    reference client have no counterpart in the spec, and four of them look
    like documentation gaps rather than invented paths. So a caller that has a
    real reason to reach one of those can catch this specifically and proceed
    with an ``@unverified`` marker, while a typo still fails loudly.
    """


class UnknownQueryParameterError(RentvineTransportError):
    """A query parameter this operation does not declare.

    ⚠ This is fail-closed BECAUSE the failure it prevents is silent. A filter
    Rentvine does not recognise is ignored, and the request returns the
    UNFILTERED collection with a 200 — CLAUDE.md gotcha 24, which this product
    has already been bitten by on Buildium's ``?propertyids``. Sending an
    unrecognised filter does not raise anywhere; it answers a different
    question convincingly.

    The names are not guessable. Five filters are declared BOTH with and
    without a ``[]`` suffix on different operations (``propertyIDs``,
    ``portfolioIDs``, ``bankAccountIDs``, ``tagIDs``, ``assignedToUserIDs``),
    so neither "always append ``[]``" nor "never append it" is correct.
    """


#: A single DNS label: what may be interpolated into :data:`BASE_URL_TEMPLATE`.
#:
#: ⚠ **THIS IS A HOST GUARD, NOT TIDINESS.** The account code is interpolated
#: into the *authority* of the URL, so any character that can end a hostname
#: re-points the request at a server of the caller's choosing. Measured against
#: the unguarded version: ``account_code="evil.example/x"`` produced
#: ``https://evil.example/x.rentvine.com/api/manager``, whose host is
#: ``evil.example`` — and that string reaches this function straight from the
#: JSON body of ``PUT /api/v1/rentvine/credentials``, where it was bounded only
#: by length. The stored ``base_url`` then drives the UNATTENDED worker sync,
#: so the result is a repeating outbound request from our infrastructure to an
#: attacker-named host carrying an ``Authorization: Basic`` header.
#:
#: A dot is refused along with the rest. ``a.b.rentvine.com`` stays inside the
#: zone and is therefore not an escape, but nothing legitimate needs it, and a
#: guard that admits one separator is one parser bug away from admitting the
#: others (gotchas 38/51 — test what it must spare as well as what it catches).
_ACCOUNT_CODE_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def normalize_account_code(account_code: str) -> str:
    """Return the customer's subdomain, lowercased, or raise.

    Lowercases rather than refusing mixed case: DNS is case-insensitive, the
    customer reads the code off their own address bar, and "Acme" and "acme"
    name the same host. Everything else is a refusal — see
    :data:`_ACCOUNT_CODE_RE` for why this cannot be lenient.
    """
    code = account_code.strip().strip("/").lower()
    if not code:
        raise RentvineTransportError("account_code is required — it is the customer's subdomain")
    if not _ACCOUNT_CODE_RE.match(code):
        raise RentvineTransportError(
            f"{account_code!r} is not a Rentvine subdomain. It is the word before "
            "'.rentvine.com' in your address bar: letters, digits and hyphens only."
        )
    return code


def base_url(account_code: str) -> str:
    """Return the API root for one customer's subdomain."""
    return BASE_URL_TEMPLATE.format(account=normalize_account_code(account_code))


def operation_key(verb: str, template: str) -> str:
    """Normalise to the index's key form, e.g. ``GET /leases/{leaseID}``."""
    return f"{verb.strip().upper()} /{template.strip().lstrip('/')}"


def build_path(template: str, **path_params: object) -> str:
    """Fill a path template, with exactly one leading slash.

    ⚠ The leading slash is handled here rather than left to callers because
    **15 of the reference client's 60 code paths emit one anyway**, producing
    ``/api/manager//leases``. Whether Rentvine normalises a doubled slash is
    not documented and has never been probed, so the safe move is to make the
    doubled form unreachable instead of relying on the server to forgive it.
    """
    filled = template.format(**path_params) if path_params else template
    return "/" + filled.lstrip("/")


#: A query value httpx can actually serialise.
#:
#: ⚠ **NOT ``object``.** ``validate_query``'s return goes straight into
#: ``httpx.AsyncClient.get/post(params=...)``, so a ``dict[str, object]``
#: signature is wider than anything this connector ever puts in one — it bought
#: nothing and cost an identical ``arg-type`` error at every call site. Lists are
#: in the union deliberately: the spec declares repeated params (``propertyIDs[]``)
#: and the contract tests pass them.
QueryValue = str | int | float | bool | None | Sequence[str | int | float | bool | None]
QueryMap = dict[str, QueryValue]


def validate_query(verb: str, template: str, params: QueryMap) -> QueryMap:
    """Return ``params`` unchanged, or refuse before anything is sent.

    Raises :class:`UnknownOperationError` if the spec documents no such operation,
    and :class:`UnknownQueryParameterError` for any key it does not declare.
    """
    key = operation_key(verb, template)
    declared = QUERY_PARAMS.get(key)
    if declared is None:
        raise UnknownOperationError(
            f"{key} is not in the captured Rentvine spec. If this path is real but "
            f"undocumented, catch UnknownOperationError deliberately and mark the call "
            f"@unverified — do not widen the index by hand."
        )
    unknown = sorted(k for k in params if k not in declared)
    if unknown:
        raise UnknownQueryParameterError(
            f"{key} does not declare {unknown}. Rentvine IGNORES an unrecognised "
            f"filter and returns the unfiltered collection with a 200, so this "
            f"would have produced a confident answer about the wrong scope. "
            f"Declared here: {sorted(declared)}"
        )
    return params


def page_params(page: int, page_size: int) -> dict[str, int]:
    """Build the pagination pair. Both are always sent; neither is defaulted."""
    if page < 1:
        raise RentvineTransportError(f"page is 1-based in Rentvine; got {page}")
    if page_size < 1:
        raise RentvineTransportError(f"page_size must be positive; got {page_size}")
    return {PAGE_PARAM: page, PAGE_SIZE_PARAM: page_size}


#: The pagination metadata Rentvine returns as RESPONSE HEADERS.
#:
#: ⚠ **THIS CLOSES A GAP FOUR SEPARATE DOCUMENTS CALLED OPEN.** `api-notes.md`,
#: ADR 0010's Known Gap #2, `PROGRESS.md`'s blocker list and this module's own
#: `page_looked_complete` docstring all said the spec documents no total, no
#: `hasMore` and no `Link` header, so a sync loop could not tell a last page
#: from a short one. It does — as headers, on **40 of the 44** paginating
#: operations. Every earlier scan looked at response BODY fields and at
#: `X-Total`; none looked at `responses.*.headers`.
#:
#: That is the sixth time a count over this spec came back zero because it was
#: pointed at the wrong place ($ref and allOf being the other five). The
#: standing rule now covers headers too.
PAGINATION_HEADERS = (
    "pagination-total-pages",
    "pagination-total-items",
    "pagination-current-page",
    "pagination-next-page",
    "pagination-last-page",
    "pagination-first-page",
    "pagination-before-page",
    "pagination-page-size",
)

#: The paginating operations that do NOT declare the headers and have NOT been
#: seen to send them — exports plus two others. A loop over these still needs
#: the full-page heuristic, so the heuristic is kept rather than deleted.
OPERATIONS_WITHOUT_PAGINATION_HEADERS = frozenset(
    {
        "GET /accounting/deposits",
        "GET /maintenance/inspections",
        "GET /properties/export",
        "GET /properties/units/export",
        "GET /screening/applications/export",
    }
)


#: Operations the SPEC declares no pagination headers for and the LIVE server
#: was measured sending them anyway — the same kind of record as
#: ``_row_envelopes.LIVE_OVERRIDES``: where the captured document is wrong,
#: with the evidence. ``client.iter_all`` reads whatever headers arrive, so
#: nothing here changes behaviour; it is what lets the lease sync claim a
#: SERVER-confirmed end, and ``test_rentvine_complete_leases.py`` holds both
#: halves (the spec still says none; the loop only trusts headers that came).
MEASURED_PAGINATION_HEADERS: dict[str, str] = {
    "GET /leases/export": (
        "2026-10-01, demoaccount41: pagination-total-items=28, total-pages=2, "
        "current/next/last-page and page-size on both pages (25 + 3 rows)"
    ),
}


def total_pages_from(headers: Mapping[str, str]) -> int | None:
    """Rentvine's own page count, or ``None`` when it did not say.

    ⚠ Returns ``None`` rather than a guess on anything unparseable. A sync that
    treats a missing or junk total as "1 page" silently truncates the customer's
    portfolio and reports the truncation as a total — this product's worst
    failure mode (gotchas 26/55). The caller falls back to
    :func:`page_looked_complete`, which is honest about being a heuristic.
    """
    raw = headers.get("pagination-total-pages")
    if raw is None:
        # HTTP headers are case-insensitive; a plain dict in a test may not be.
        lowered = {k.lower(): v for k, v in headers.items()}
        raw = lowered.get("pagination-total-pages")
    if raw is None:
        return None
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def page_looked_complete(rows_returned: int, page_size: int) -> bool:
    """Whether this page was FULL — i.e. another page may exist.

    ⚠ **A HEURISTIC, and only the FALLBACK now.** Prefer
    :func:`total_pages_from`: 40 of the 44 paginating operations return
    ``pagination-total-pages``, which answers the question outright. This
    remains because six operations — the exports among them, listed in
    :data:`OPERATIONS_WITHOUT_PAGINATION_HEADERS` — do not.

    Where it is the only signal: a short page is *evidence* of the end; a full
    page is *not* evidence of more, because a collection whose size is an exact
    multiple of ``page_size`` ends on a full page. So the loop keeps paging
    while pages come back full and stops on the first short or empty one,
    accepting one wasted request at the boundary. Do not infer a total from it.

    A sync must still carry a hard page ceiling on this path, so an unexpected
    answer past the end cannot loop forever.
    """
    if page_size < 1:
        raise RentvineTransportError(f"page_size must be positive; got {page_size}")
    return rows_returned >= page_size


class Failure(Enum):
    """What a non-2xx means, in terms a caller can act on."""

    #: 401 — the credential itself is wrong or revoked. Reconnecting helps.
    CREDENTIALS = "credentials"
    #: 403 — the credential is fine and THIS operation is not permitted. The
    #: customer's API key is bound to a role; the fix is the role, not the key.
    PERMISSION = "permission"
    #: 404 — no such record. Never evidence about the rest of the account.
    NOT_FOUND = "not_found"
    #: 400 — we sent something malformed. Ours to fix, never the customer's.
    BAD_REQUEST = "bad_request"
    #: Anything the spec does not describe, including 429 and 5xx.
    UNDOCUMENTED = "undocumented"
    #: Never a status Rentvine sent. Occupella's own circuit breaker
    #: (``client.py``, terms rule 25) refused to send because Rentvine kept
    #: failing. Nothing was sent, so nothing changed in Rentvine.
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class _Meaning:
    failure: Failure
    retryable: bool
    operator_action: bool


_STATUS_MEANING: dict[int, _Meaning] = {
    400: _Meaning(Failure.BAD_REQUEST, retryable=False, operator_action=False),
    401: _Meaning(Failure.CREDENTIALS, retryable=False, operator_action=True),
    403: _Meaning(Failure.PERMISSION, retryable=False, operator_action=True),
    404: _Meaning(Failure.NOT_FOUND, retryable=False, operator_action=False),
}


def classify_status(status: int) -> tuple[Failure, bool]:
    """Return ``(meaning, retryable)`` for a response status.

    ⚠ **401 and 403 are deliberately different, and conflating them is a
    documented product bug on the Buildium side** (CLAUDE.md gotcha 102): a 403
    means the credential was READ, ACCEPTED, and refused for this operation, so
    telling the customer to reconnect sends them to redo a connection that
    works. Rentvine makes this sharper — its API keys are bound to a role the
    customer chooses at creation, so a restricted key is an expected, supported
    state rather than a fault.

    ⚠ **Everything undocumented is retryable, and that includes 429.** The spec
    contains zero occurrences of ``429``, ``ratelimit`` or ``retry-after``.
    That is an absence of documentation, not a promise of no limit, so a status
    we cannot explain backs off rather than being treated as fatal.
    """
    meaning = _STATUS_MEANING.get(status)
    if meaning is not None:
        return meaning.failure, meaning.retryable
    return Failure.UNDOCUMENTED, True
