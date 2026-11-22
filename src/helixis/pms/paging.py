"""Read a whole collection, and know whether it is whole.

:class:`Collection` carries the rows AND whether the vendor named the end.
``complete`` is True only when the vendor's own total was reached (and, for
page-number paging, no ``next`` link remains). A read that stopped any other
way (a total that moved, a page ceiling, a missing total) is incomplete: its
rows are real and are written, but the table is not stamped fresh, nothing is
pruned from it, and its watermark does not advance. A short list must never
read as the whole (gotchas 26/55; Rentvine's ``Collection`` and gotcha 149).

Two strategies, one per vendor style:

* :class:`PageNumberPaging` (Rent Manager): ``pageNumber``/``pageSize``, 200
  or 206, a ``Link`` header and ``X-Total-Results``. DoorLoop pages the same
  way with its own names, and states its total in the body
  (``{"total": 150, "data": [...]}``): ``total_key`` reads it there.
* :class:`OffsetPaging` (Propertyware): ``limit``/``offset`` and
  ``X-Total-Count``.

⚠ If the total changes between the first page and the last, rows moved under
the read: it is read once more, and if it moves again it is incomplete.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from helixis.pms.errors import Failure, PmsApiError
from helixis.pms.http import PmsHttpClient

logger = logging.getLogger(__name__)

#: Hard ceiling on pages for one collection, so an answer past the end can
#: never loop forever. Reaching it makes the read incomplete.
MAX_PAGES = 200

_NEXT_LINK = re.compile(r'<[^>]*>\s*;\s*rel="?next"?', re.IGNORECASE)


@dataclass(frozen=True)
class Collection:
    """Every row read, and whether the vendor confirmed that is all of them."""

    rows: list[dict[str, Any]]
    #: True only when the vendor named the end (see the module docstring).
    complete: bool
    #: The vendor's own row count, or ``None`` when it did not say.
    total: int | None


def _int_header(headers: Mapping[str, str], name: str) -> int | None:
    lowered = {k.lower(): v for k, v in headers.items()}
    raw = lowered.get(name.lower())
    if raw is None:
        return None
    try:
        value = int(raw.strip())
    except ValueError:
        return None
    return value if value >= 0 else None


def _body_total(body: Any, key: str) -> int | None:
    """A total the body states under ``key``: a whole number, never negative,
    never a boolean (``True`` is an ``int`` in Python). Anything else is no
    total, so the read can never be called complete on it."""
    value = body.get(key) if isinstance(body, Mapping) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    return int(value) if value >= 0 else None


def has_next_link(headers: Mapping[str, str]) -> bool:
    """Whether a ``Link`` header names a ``rel="next"`` page."""
    lowered = {k.lower(): v for k, v in headers.items()}
    return bool(_NEXT_LINK.search(lowered.get("link", "")))


def _rows_of(body: Any, rows_key: str | None, template: str) -> list[dict[str, Any]]:
    if body is None:
        return []
    if rows_key is not None and isinstance(body, Mapping):
        body = body.get(rows_key)
        if body is None:
            return []
    if not isinstance(body, list) or not all(isinstance(r, Mapping) for r in body):
        raise PmsApiError(
            200,
            Failure.UNDOCUMENTED,
            False,
            f"{template} answered with something that is not a list of records",
        )
    return [dict(r) for r in body]


@dataclass(frozen=True)
class PageNumberPaging:
    """``pageNumber``/``pageSize`` paging with a total header and ``Link``.

    ``empty_statuses``: statuses the vendor uses for "no rows" on a collection
    read (Rent Manager: 404 documented, 204 seen in clients; both
    ``@unverified``). Only on the FIRST page; on a later page a 404 is an
    error.
    """

    page_param: str = "pageNumber"
    size_param: str = "pageSize"
    page_size: int = 1000
    total_header: str = "X-Total-Results"
    rows_key: str | None = None
    empty_statuses: frozenset[int] = field(default_factory=frozenset)
    #: The body key that states the total, when the vendor states it there
    #: (DoorLoop's ``total``). Set, it is the only total read.
    total_key: str | None = None


@dataclass(frozen=True)
class OffsetPaging:
    """``limit``/``offset`` paging with a total header (Propertyware)."""

    limit_param: str = "limit"
    offset_param: str = "offset"
    limit: int = 500
    total_header: str = "X-Total-Count"
    rows_key: str | None = None
    empty_statuses: frozenset[int] = field(default_factory=frozenset)
    #: As :attr:`PageNumberPaging.total_key`.
    total_key: str | None = None


Strategy = PageNumberPaging | OffsetPaging


async def _read_once(
    client: PmsHttpClient,
    template: str,
    strategy: Strategy,
    path_params: Mapping[str, Any] | None,
    query: Mapping[str, Any] | None,
    max_pages: int,
) -> tuple[list[dict[str, Any]], bool, int | None, bool]:
    """One pass: ``(rows, complete, first total, total moved)``."""
    rows: list[dict[str, Any]] = []
    first_total: int | None = None
    moved = False
    previous: list[dict[str, Any]] | None = None
    repeated = False
    next_link = False
    for page in range(max_pages):
        params = dict(query or {})
        if isinstance(strategy, PageNumberPaging):
            params[strategy.page_param] = page + 1
            params[strategy.size_param] = strategy.page_size
            size = strategy.page_size
        else:
            params[strategy.offset_param] = len(rows)
            params[strategy.limit_param] = strategy.limit
            size = strategy.limit
        try:
            resp = await client.get(template, path_params=path_params, query=params)
        except PmsApiError as exc:
            if page == 0 and exc.status in strategy.empty_statuses:
                return [], True, 0, False
            raise
        if page == 0 and resp.status in strategy.empty_statuses:
            # A success status the vendor uses for "no rows" (204).
            return [], True, 0, False
        got = _rows_of(resp.body, strategy.rows_key, template)
        if got and got == previous:
            # ⚠ The same page twice: the vendor ignored the paging parameter
            # (a misspelled one is ignored, not refused). Counting it again
            # would fill the total with copies and call the read whole.
            logger.warning("%s: the same page came back twice; incomplete", template)
            repeated = True
            break
        previous = got
        total = (
            _body_total(resp.body, strategy.total_key)
            if strategy.total_key is not None
            else _int_header(resp.headers, strategy.total_header)
        )
        if page == 0:
            first_total = total
        elif total != first_total:
            moved = True
        rows.extend(got)
        next_link = isinstance(strategy, PageNumberPaging) and has_next_link(resp.headers)
        if not got:
            break
        if total is not None:
            if len(rows) >= total and not next_link:
                break
        elif not next_link and len(got) < size:
            break
    else:
        logger.warning("%s: stopped at the %d-page ceiling; incomplete", template, max_pages)
        return rows, False, first_total, moved
    complete = (
        not repeated
        and not moved
        and first_total is not None
        and len(rows) == first_total
        and not next_link
    )
    return rows, complete, first_total, moved


async def read_all(
    client: PmsHttpClient,
    template: str,
    *,
    strategy: Strategy,
    path_params: Mapping[str, Any] | None = None,
    query: Mapping[str, Any] | None = None,
    max_pages: int = MAX_PAGES,
) -> Collection:
    """Every row of a collection, with whether the vendor confirmed it whole.

    Errors propagate (a 404 on a list is an error unless the strategy names
    it as empty). A total that moved during the read is read once more.
    """
    rows, complete, total, moved = await _read_once(
        client, template, strategy, path_params, query, max_pages
    )
    if moved:
        logger.info("%s: the total moved during the read; reading it once more", template)
        rows, complete, total, moved = await _read_once(
            client, template, strategy, path_params, query, max_pages
        )
        if moved:
            logger.warning("%s: the total moved twice; incomplete", template)
    return Collection(rows=rows, complete=complete, total=total)


__all__ = [
    "MAX_PAGES",
    "Collection",
    "OffsetPaging",
    "PageNumberPaging",
    "Strategy",
    "has_next_link",
    "read_all",
]
