"""A uuid column does not come back as a string, and three routes compared it
to one.

``orchestrator_runs.company_id`` is a Postgres ``uuid``. asyncpg decodes that
to ``asyncpg.pgproto.pgproto.UUID`` — a ``uuid.UUID`` subclass. Every caller
of ``get_company_for_session`` compares the result against
``AuthenticatedUser.company_ids``, which ``auth.py`` builds from a **PostgREST
JSON** response and is therefore a list of plain ``str``.

``UUID(x) in ["<x as text>"]`` is False. So the membership test could not
succeed, and the three checks failed in the two ways their operators allow:

* ``POST /agent/run`` — ``owner is not None and owner not in …`` — 404
  "session not found" on every turn in a thread that ALREADY had a run row.
  A first turn creates no row yet, so new threads worked; that is why this
  read as intermittent instead of total.
* ``GET /agent/watch`` and ``POST /agent/cancel`` — ``owner is None or owner
  not in …`` — 404 unconditionally. The dropped-stream reattach and the stop
  button could never work.

⚠ **The existing ownership suite passed against all of it.** Its fake pool
returns ``{"company_id": "<a str>"}``, so it measured a shape production never
produces — a fake answers whatever it is handed (gotchas 69/79). These tests
hand over the REAL decoded type, which is the only thing that separates the
two implementations.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from asyncpg.pgproto import pgproto

from helixis.db.repositories import orchestrator_runs

# The same value in both spellings — the whole point is that they are equal as
# text and NOT interchangeable as list members.
_COMPANY_TEXT = "224c6a28-d4ac-41be-8d75-b1ac6c58bafe"
_COMPANY_DECODED = pgproto.UUID(bytes.fromhex(_COMPANY_TEXT.replace("-", "")))


class _AcquireCM:
    def __init__(self, conn: object) -> None:
        self._conn = conn

    async def __aenter__(self) -> object:
        return self._conn

    async def __aexit__(self, *_: object) -> bool:
        return False


def _pool_returning(row: object) -> MagicMock:
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=row)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AcquireCM(conn))
    return pool


def test_the_premise_holds_asyncpg_decodes_a_uuid_column_to_uuid_not_str() -> None:
    """The floor. If this ever stops being true the fix above is unnecessary.

    Asserted rather than assumed (gotcha 47): the whole diagnosis rests on
    asyncpg's codec, so it is measured here instead of remembered. No database
    is needed — the decoder is importable.
    """
    assert isinstance(_COMPANY_DECODED, uuid.UUID)
    assert not isinstance(_COMPANY_DECODED, str)
    assert str(_COMPANY_DECODED) == _COMPANY_TEXT
    # ⚠ THE production comparison, in one line.
    assert _COMPANY_DECODED not in [_COMPANY_TEXT], (
        "a uuid.UUID is not a member of a list of strings — if this assertion "
        "ever fails, Python changed and the routes were fine all along"
    )


@pytest.mark.asyncio
async def test_the_owner_comes_back_as_text_so_a_membership_test_can_work() -> None:
    """The fix, stated as the property the callers actually need.

    Not `== _COMPANY_TEXT`: `pgproto.UUID == str` is False, but so is
    `str(PgUUID) != text`, and an assertion that only compares equality would
    pass against a returned UUID via the subclass's own __eq__ in some
    orderings. What every caller does is a LIST MEMBERSHIP test, so that is
    what is asserted.
    """
    pool = _pool_returning({"company_id": _COMPANY_DECODED})
    owner = await orchestrator_runs.get_company_for_session(pool, "s1")

    assert isinstance(owner, str), f"owner came back as {type(owner).__name__}"
    assert owner in [_COMPANY_TEXT], (
        "the ownership check is `owner not in user.company_ids`, and "
        "user.company_ids is a list of str from a PostgREST read"
    )


@pytest.mark.asyncio
async def test_a_string_column_still_passes_through_unchanged() -> None:
    """Both directions. `str()` on an already-text value must be a no-op —
    not every deployment's column is uuid, and a normalisation that mangled a
    text id would break the case that currently works."""
    pool = _pool_returning({"company_id": _COMPANY_TEXT})
    assert await orchestrator_runs.get_company_for_session(pool, "s1") == _COMPANY_TEXT


@pytest.mark.asyncio
async def test_absent_and_null_both_stay_none() -> None:
    """`None` is load-bearing and means something DIFFERENT on each route.

    On `/agent/run` it means "a session nobody has run yet" and must be
    ALLOWED; on `/agent/watch` and `/agent/cancel` it denies. Turning a NULL
    into the string "None" would let a run route accept, and the other two
    reject, on a value that means neither.
    """
    assert await orchestrator_runs.get_company_for_session(_pool_returning(None), "s1") is None
    assert (
        await orchestrator_runs.get_company_for_session(_pool_returning({"company_id": None}), "s1")
        is None
    )
