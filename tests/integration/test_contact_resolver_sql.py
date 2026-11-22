"""Live-Postgres verification of the comms resolver's SQL digit-normalization.

The unit suite mocks the pool, so the expression
``right(regexp_replace(phone, '\\D', '', 'g'), 10)`` in
``comms/contact_resolver.py`` is a string never sent to a real database by any
default-run test — a typo or Postgres regex-flavor surprise would pass CI and
fail on the first real inbound SMS. This test closes that seam: it runs the
real expression on a live connection and asserts it agrees with the Python
side (``phone_match_key``) on the exact corpus ``test_comms_phone.py`` pins.

First verified live against helixis-test on 2026-08-15 (all 12 corpus cases in
agreement; every real mirror row with a phone — 43 tenants / 4 owners /
22 vendors — produced a full 10-digit key). This test makes that verification
repeatable.

Skipped by default (integration marker + credential guard), same two-layer
pattern as the other integration tests. Read-only: a VALUES probe, no table
access.
"""

from __future__ import annotations

import os

import pytest

from helixis.comms.phone import phone_match_key

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("HELIXIS_SUPABASE__DB_URL"),
        reason="needs HELIXIS_SUPABASE__DB_URL (live DB)",
    ),
]

# The same corpus the unit suite pins for phone_match_key, plus junk.
_CORPUS = [
    "+15551234567",
    "(555) 123-4567",
    "555.123.4567",
    "555-123-4567",
    "1-555-123-4567",
    "15551234567",
    "  +1 (555) 123-4567  ",
    "5551234567",
    "555-1234",  # 7 digits — must NOT produce a 10-digit key
    "abc",
    "",
    "+44 20 7946 0000",  # international — last-10 semantics on both sides
]


@pytest.mark.asyncio
async def test_sql_match_key_agrees_with_python_on_live_postgres() -> None:
    import asyncpg

    conn = await asyncpg.connect(os.environ["HELIXIS_SUPABASE__DB_URL"])
    try:
        rows = await conn.fetch(
            r"""
            select p as input, right(regexp_replace(p, '\D', '', 'g'), 10) as sql_key
            from unnest($1::text[]) as t(p)
            """,
            _CORPUS,
        )
    finally:
        await conn.close()

    for row in rows:
        raw = row["input"]
        sql_key = row["sql_key"] or ""
        py_key = phone_match_key(raw)
        if py_key is None:
            # Python refuses (<10 national digits) — SQL must not have
            # produced a full 10-digit key that could false-match.
            assert len(sql_key) < 10, f"SQL made a matchable key from {raw!r}: {sql_key}"
        else:
            assert sql_key == py_key, f"divergence on {raw!r}: sql={sql_key} py={py_key}"
