"""What "open" means for a work order or a task. One definition, two readers.

The project had TWO spellings and they disagreed (gotcha 50 recorded it and
deliberately did not fix it, because reconciling needs the real Buildium
vocabulary rather than a guess):

* ``buildium_tools`` allowlisted OPEN — work orders ``("New", "InProgress")``,
  tasks ``("New", "InProgress", "Deferred")``.
* the mirror denylisted CLOSED — ``("Completed", "Closed", "Cancelled")``.

Two consequences, and the second is the one that matters:

1. **A ``Deferred`` work order was open in the mirror and closed in the live
   tool.** So ``portfolio_overview`` and ``buildium_open_maintenance_by_unit``
   could answer the same question with different numbers, and nothing said so.

2. **An allowlist and a denylist drift in OPPOSITE directions.** Under an
   allowlist an unrecognised status is CLOSED; under a denylist it is OPEN. So
   the day Buildium adds a status — and its vocabularies do change (gotcha 15:
   never take a Buildium vocabulary from memory or the docs) — the API path
   would start silently DROPPING that work from every "what's open" answer,
   while the mirror kept it. Under-reporting looks like a clean answer and
   over-reporting looks like a number to argue with; this product's worst
   failure mode is the first (gotcha 26).

So CLOSED is the single source of truth and open is its complement. An
unrecognised status is OPEN everywhere, by construction rather than by
remembering to keep two lists in step.

⚠ **The server-side filter is unavoidably an allowlist**, because
``GET /workorders?statuses=`` takes the values to INCLUDE — you cannot send
Buildium a denylist. :data:`OPEN_STATUSES` is therefore computed from the two
sets below rather than written out, so adding a status to ``KNOWN_STATUSES``
updates the API request and the client-side filter together. And every
client-side decision goes through :func:`is_open`, which asks "is this NOT
closed" — so a status the server filter did not know to request is still
counted correctly once the rows are in hand.

Live-verified 2026-08-29 across the whole mirror: only ``New``, ``InProgress``
and ``Completed`` actually occur (work orders 88/1/4, tasks 171/4/4). That
confirms what OCCURS and NOT the full enum — absence from the rows is not
evidence of absence from the vocabulary (gotcha 26 again), which is exactly
why the fix is built to be safe under an unknown value instead of waiting for
certainty about the enum.
"""

from __future__ import annotations

from typing import Any

#: Statuses that mean the work is FINISHED. The one list to edit.
#:
#: ``Closed`` and ``Cancelled`` do not occur on any live row today and are kept
#: because the mirror has always excluded them and removing an exclusion on the
#: strength of "we have not seen one" is how a finished job starts being
#: reported as outstanding.
CLOSED_STATUSES: tuple[str, ...] = ("Completed", "Closed", "Cancelled")

#: Every status this codebase knows about, closed ones included. Used ONLY to
#: build the server-side filter below — never to decide whether a row in hand
#: is open, because that decision must stay safe for values not listed here.
KNOWN_STATUSES: tuple[str, ...] = (
    "New",
    "InProgress",
    "Deferred",
    *CLOSED_STATUSES,
)

#: The values to ASK Buildium for. Computed, so it cannot drift from the
#: closed set — this is what made a ``Deferred`` work order invisible to the
#: live tool while the mirror counted it.
OPEN_STATUSES: tuple[str, ...] = tuple(s for s in KNOWN_STATUSES if s not in CLOSED_STATUSES)


def is_open(status: Any) -> bool:
    """Is this maintenance record still outstanding?

    ⚠ Asks "is it NOT closed", never "is it one of the open ones". An
    unrecognised status therefore counts as OPEN, which over-reports rather
    than under-reports — a visible, arguable number instead of a clean-looking
    answer that quietly omitted work (gotcha 26).

    ``None`` is open too: a row whose status Buildium did not send is unknown,
    not finished. Same principle as gotcha 55 — a filter that cannot see must
    decline to exclude.
    """
    if status is None:
        return True
    return str(status).strip() not in CLOSED_STATUSES


def closed_statuses_sql() -> str:
    """``CLOSED_STATUSES`` as a SQL literal list, for a mirror query's
    ``NOT IN (...)``.

    ⚠ A FUNCTION, not a formatted constant, for the reason gotcha 50 records:
    ``repr(tuple)[1:-1]`` looks right until the tuple has one entry, where it
    emits a trailing comma and Postgres answers 42601 on a live chat turn. This
    builds the list explicitly so the one-element case is not a special case.
    """
    return ", ".join(f"'{s}'" for s in CLOSED_STATUSES)


def open_status_sql(column: str) -> str:
    """:func:`is_open` as a SQL condition on ``column``: true for every row not
    known to be closed, an unknown (NULL) status included.

    ⚠ ``status NOT IN (closed)`` alone is never true for NULL, so a work order
    whose status a system did not send, or sent in words Occupella does not
    map (``rentvine.mapper`` writes NULL for an unknown primary status), fell
    out of every count while ``open_work_orders`` listed it: the dashboard said
    fewer were open than the list showed, and an unknown read as closed (rule
    14). Every SQL reader of "open" goes through here
    (``test_open_status_has_one_definition``). ``column`` is an identifier
    written in code, never input.
    """
    return f"({column} IS NULL OR {column} NOT IN ({closed_statuses_sql()}))"


__all__ = [
    "CLOSED_STATUSES",
    "KNOWN_STATUSES",
    "OPEN_STATUSES",
    "closed_statuses_sql",
    "is_open",
    "open_status_sql",
]
