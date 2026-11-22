"""Generating, hashing and spending two-factor backup codes.

⚠ THESE TESTS RUN IN THE DEV CONTAINER AND THE ROUTE TESTS DO NOT. That is
not a coincidence — it is why the logic lives in `helixis.auth.
mfa_backup_codes` rather than inside the route handlers. The module imports
`hashlib`, `hmac`, `logging` and `secrets` and nothing else, so it is
reachable wherever Python is; `api/routes/mfa.py` pulls in `helixis.api.auth`
→ `pyjwt` → `cryptography`, whose Rust bindings panic in this container. The
security-carrying half is the half that can be exercised.

⚠ No `pytest-asyncio` here either, so every async assertion runs its own
`asyncio.run`. Gotcha 36 recommends that shape anyway: a test that borrows a
loop it did not create cannot observe what happens to that loop.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from helixis.auth.mfa_backup_codes import (
    CODE_BITS,
    CODE_COUNT,
    disable_all_factors,
    generate_codes,
    hash_code,
    new_salt,
    normalise,
    remaining_count,
    replace_codes,
    spend_code,
)


def _scopes_to_user(sql: str) -> bool:
    """Does this statement actually say `user_id = $n`?"""
    return re.search(r"user_id\s*=\s*\$\d", sql) is not None


def _requires_unspent(sql: str) -> bool:
    """Does this statement actually say `used_at is null`?"""
    return re.search(r"used_at\s+is\s+null", sql) is not None


class FakeConn:
    """The asyncpg calls the module makes, over a visible row store.

    ⚠ **THIS FAKE READS THE PREDICATES OUT OF THE SQL, and the first version
    did not — which made the two most important tests in this file vacuous.**
    It originally filtered on `used_at is None` and `user_id ==` in Python
    whatever the statement said, so it agreed with itself: deleting
    `and used_at is null` from the real UPDATE (a double-spend, i.e. a
    permanent 2FA bypass) and dropping the user scope from the candidate
    fetch (one person's code working against another's account) BOTH left all
    27 tests green. Measured, not guessed — the mutations were run.

    That is the repo's own recorded lesson twice over: gotcha 59's "a guard on
    a hardcoded list must also pin the OPERATOR that consumes it", and gotcha
    72's "a fake that hardcoded the intent would agree with itself whichever
    operator the route used". So the predicates below are DERIVED from the
    statement text, and removing one from the module changes what this fake
    does.
    """

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.factors: list[dict] = []
        self._next_id = 1

    def _visible(self, sql: str, user_id: str) -> list[dict]:
        rows = self.rows
        if _scopes_to_user(sql):
            rows = [r for r in rows if r["user_id"] == user_id]
        if _requires_unspent(sql):
            rows = [r for r in rows if r["used_at"] is None]
        return rows

    async def execute(self, sql: str, *args) -> None:
        await asyncio.sleep(0)
        if sql.lstrip().lower().startswith("delete from public.helixis_mfa_backup_codes"):
            (user_id,) = args
            doomed = {id(r) for r in self._visible(sql, user_id)}
            self.rows = [r for r in self.rows if id(r) not in doomed]
            return
        if "insert into public.helixis_mfa_backup_codes" in sql:
            user_id, code_hash, salt = args
            self.rows.append(
                {
                    "id": str(self._next_id),
                    "user_id": user_id,
                    "code_hash": code_hash,
                    "salt": salt,
                    "used_at": None,
                }
            )
            self._next_id += 1
            return
        raise AssertionError(f"unexpected execute: {sql}")

    async def fetch(self, sql: str, *args) -> list[dict]:
        # ⚠ A REAL AWAIT POINT. asyncpg calls suspend, and the atomic claim
        # in the UPDATE exists for exactly the window that opens here — see
        # `test_two_requests_racing_the_same_code`. A fake that never yields
        # cannot express the race the predicate defends against.
        await asyncio.sleep(0)
        if "select id, code_hash, salt" in sql:
            (user_id,) = args
            return list(self._visible(sql, user_id))
        if "delete from auth.mfa_factors" in sql:
            (user_id,) = args
            scoped = _scopes_to_user(sql)
            gone = [f for f in self.factors if not scoped or f["user_id"] == user_id]
            doomed = {id(f) for f in gone}
            self.factors = [f for f in self.factors if id(f) not in doomed]
            return gone
        raise AssertionError(f"unexpected fetch: {sql}")

    async def fetchval(self, sql: str, *args):
        # ⚠ A REAL AWAIT POINT. asyncpg calls suspend, and the atomic claim
        # in the UPDATE exists for exactly the window that opens here — see
        # `test_two_requests_racing_the_same_code`. A fake that never yields
        # cannot express the race the predicate defends against.
        await asyncio.sleep(0)
        if "select count(*)" in sql:
            (user_id,) = args
            return len(self._visible(sql, user_id))
        if "update public.helixis_mfa_backup_codes" in sql:
            (row_id,) = args
            # `id = $1` is the addressing; `and used_at is null` is the atomic
            # claim, and whether it is honoured comes from the statement.
            unspent_only = _requires_unspent(sql)
            for r in self.rows:
                if r["id"] != row_id:
                    continue
                if unspent_only and r["used_at"] is not None:
                    return None
                r["used_at"] = "now"
                return r["id"]
            return None
        raise AssertionError(f"unexpected fetchval: {sql}")


def test_the_fake_is_not_agreeing_with_itself() -> None:
    """Guard the guard: prove the predicate readers can say NO.

    Without this, a scanner bug that made `_requires_unspent` always true (a
    stray `return True`, a regex that matches everything) would silently
    restore the vacuous fake and every mutation below would go back to
    surviving — with all 27 tests still green, which is the direction that
    manufactures confidence.
    """
    assert _requires_unspent("update x set used_at = now() where used_at is null")
    assert not _requires_unspent("update x set used_at = now() where id = $1")
    assert _scopes_to_user("select 1 from x where user_id = $1::uuid")
    assert not _scopes_to_user("select 1 from x where used_at is null")


USER = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"


def test_a_batch_is_ten_distinct_codes() -> None:
    codes = generate_codes()
    assert len(codes) == CODE_COUNT
    assert len(set(codes)) == CODE_COUNT


def test_codes_are_grouped_for_reading_off_paper() -> None:
    # People transcribe these from a printout into a phone. An unbroken
    # 20-character string is where transcription errors come from.
    for code in generate_codes(3):
        assert re.fullmatch(r"[0-9A-Z]{5}(-[0-9A-Z]{5}){3}", code), code


def test_the_alphabet_excludes_the_characters_people_mistype() -> None:
    # I/L/O/U are absent on purpose: 1-vs-I and 0-vs-O are exactly the
    # confusion that costs somebody their account recovery, and U is what
    # turns a random string into an accidental word.
    joined = "".join(generate_codes(20)).replace("-", "")
    assert not (set(joined) & set("ILOU")), sorted(set(joined) & set("ILOU"))


def test_the_entropy_claim_the_hash_choice_rests_on() -> None:
    # ⚠ This is not decoration. Migration 0094 justifies a FAST hash (sha256,
    # no bcrypt) on the grounds that these are server-generated at 100 bits,
    # so there is no dictionary and no meaningful offline search. Shorten the
    # codes and that reasoning expires — this test is what notices.
    assert CODE_BITS >= 100


def test_normalise_forgives_how_a_person_types_it() -> None:
    code = generate_codes(1)[0]
    assert normalise(code.lower()) == normalise(code)
    assert normalise(code.replace("-", " ")) == normalise(code)
    assert normalise(f"  {code}  ") == normalise(code)


def test_normalise_reduces_junk_to_nothing() -> None:
    # The route rejects an empty normalisation before touching the database.
    assert normalise("----") == ""
    assert normalise("   ") == ""


def test_the_same_code_hashes_differently_under_different_salts() -> None:
    # Per-row salt: two users who somehow held the same code must not share a
    # hash, or the table leaks which accounts have a code in common.
    code = generate_codes(1)[0]
    assert hash_code(code, new_salt()) != hash_code(code, new_salt())


def test_hashing_is_stable_for_one_salt() -> None:
    code, salt = generate_codes(1)[0], new_salt()
    assert hash_code(code, salt) == hash_code(code, salt)


def test_hashing_goes_through_normalise() -> None:
    code, salt = generate_codes(1)[0], new_salt()
    assert hash_code(code.lower().replace("-", ""), salt) == hash_code(code, salt)


def test_issuing_stores_hashes_and_never_the_code() -> None:
    conn = FakeConn()
    codes = asyncio.run(replace_codes(conn, USER))
    stored = {r["code_hash"] for r in conn.rows}
    assert len(conn.rows) == CODE_COUNT
    # The plaintext must not appear anywhere in the row, in any form.
    for code in codes:
        assert code not in stored
        assert normalise(code) not in stored
        for row in conn.rows:
            assert normalise(code) not in str(row)


def test_regenerating_kills_the_old_sheet() -> None:
    # ⚠ THE POINT OF THE WHOLE FUNCTION. A code printed a year ago and left in
    # a drawer must stop working the moment somebody regenerates, or
    # "regenerate" is a button that lies.
    conn = FakeConn()
    first = asyncio.run(replace_codes(conn, USER))
    asyncio.run(replace_codes(conn, USER))
    assert asyncio.run(spend_code(conn, USER, first[0])) is False
    assert len(conn.rows) == CODE_COUNT


def test_regenerating_leaves_other_users_alone() -> None:
    conn = FakeConn()
    theirs = asyncio.run(replace_codes(conn, OTHER))
    asyncio.run(replace_codes(conn, USER))
    assert asyncio.run(spend_code(conn, OTHER, theirs[0])) is True


def test_a_valid_code_is_spent_once() -> None:
    conn = FakeConn()
    codes = asyncio.run(replace_codes(conn, USER))
    assert asyncio.run(spend_code(conn, USER, codes[0])) is True
    assert asyncio.run(remaining_count(conn, USER)) == CODE_COUNT - 1


def test_a_code_cannot_be_spent_twice() -> None:
    # SINGLE USE is the whole security property: a code that still works
    # after being spent is a permanent 2FA bypass sitting on a piece of paper.
    conn = FakeConn()
    codes = asyncio.run(replace_codes(conn, USER))
    assert asyncio.run(spend_code(conn, USER, codes[0])) is True
    assert asyncio.run(spend_code(conn, USER, codes[0])) is False


def test_two_requests_racing_the_same_code_spend_it_once() -> None:
    """⚠ THE TEST THE `and used_at is null` PREDICATE EXISTS FOR.

    Deleting that predicate from the UPDATE changes nothing sequentially —
    the candidate fetch already excludes spent rows, so the second attempt
    never reaches the update at all. Per gotcha 58 that first reads as "the
    branch is redundant", and it is not: it defends a RACE. Two requests
    carrying the same code both pass the fetch (both see it unspent), and
    only one may win the claim.

    `FakeConn` suspends at every call the way asyncpg does, so `gather` here
    interleaves exactly that window. Without the predicate BOTH return True,
    which in production is one code disabling 2FA twice and — worse — a
    spend that was supposed to be atomic silently not being.

    This is gotcha 25's sentence in a third place: check-then-act around an
    `await` is not a claim.
    """
    conn = FakeConn()
    codes = asyncio.run(replace_codes(conn, USER))

    async def race() -> list[bool]:
        return list(
            await asyncio.gather(
                spend_code(conn, USER, codes[0]),
                spend_code(conn, USER, codes[0]),
            )
        )

    results = asyncio.run(race())
    assert sorted(results) == [False, True], results
    assert asyncio.run(remaining_count(conn, USER)) == CODE_COUNT - 1


def test_a_code_is_accepted_however_it_was_typed() -> None:
    conn = FakeConn()
    codes = asyncio.run(replace_codes(conn, USER))
    typed = codes[3].lower().replace("-", " ")
    assert asyncio.run(spend_code(conn, USER, typed)) is True


def test_a_wrong_code_spends_nothing() -> None:
    conn = FakeConn()
    asyncio.run(replace_codes(conn, USER))
    assert asyncio.run(spend_code(conn, USER, "AAAAA-BBBBB-CCCCC-DDDDD")) is False
    assert asyncio.run(remaining_count(conn, USER)) == CODE_COUNT


def test_one_users_code_is_worthless_against_another_account() -> None:
    # ⚠ The tenant boundary here is the auth USER, not a company — a second
    # factor belongs to the person and follows them across every workspace
    # (migration 0094 says the same from the storage side). A code that
    # worked against somebody else's account would be a complete takeover.
    conn = FakeConn()
    mine = asyncio.run(replace_codes(conn, USER))
    asyncio.run(replace_codes(conn, OTHER))
    assert asyncio.run(spend_code(conn, OTHER, mine[0])) is False
    assert asyncio.run(remaining_count(conn, USER)) == CODE_COUNT


def test_spending_compares_against_every_candidate_without_stopping_early() -> None:
    # ⚠ The loop deliberately does NOT break on a match, so runtime does not
    # depend on WHERE the code sits in the list. Asserting on timing would be
    # flaky, so this asserts the observable consequence: the LAST code works
    # exactly as well as the first, which is what a short-circuit on the
    # first row would break.
    conn = FakeConn()
    codes = asyncio.run(replace_codes(conn, USER))
    assert asyncio.run(spend_code(conn, USER, codes[-1])) is True


def test_counting_ignores_spent_codes() -> None:
    conn = FakeConn()
    codes = asyncio.run(replace_codes(conn, USER))
    for code in codes[:4]:
        asyncio.run(spend_code(conn, USER, code))
    assert asyncio.run(remaining_count(conn, USER)) == CODE_COUNT - 4


def test_counting_is_zero_for_somebody_who_never_generated() -> None:
    assert asyncio.run(remaining_count(FakeConn(), USER)) == 0


def test_spent_rows_are_kept_rather_than_deleted() -> None:
    # Keeping them is what makes "3 of 10 left" answerable and stops a spent
    # code ever being re-issued.
    conn = FakeConn()
    codes = asyncio.run(replace_codes(conn, USER))
    asyncio.run(spend_code(conn, USER, codes[0]))
    assert len(conn.rows) == CODE_COUNT


def test_disabling_removes_this_users_factors_and_reports_how_many() -> None:
    conn = FakeConn()
    conn.factors = [
        {"id": "f1", "user_id": USER},
        {"id": "f2", "user_id": USER},
        {"id": "f3", "user_id": OTHER},
    ]
    assert asyncio.run(disable_all_factors(conn, USER)) == 2
    assert [f["user_id"] for f in conn.factors] == [OTHER]


def test_disabling_an_account_with_no_factors_is_not_an_error() -> None:
    # A code spent against an account whose factor was already gone is a real
    # outcome, not a failure — the code is spent either way, and the route
    # reports the zero rather than raising.
    assert asyncio.run(disable_all_factors(FakeConn(), USER)) == 0


@pytest.mark.parametrize("junk", ["", "   ", "----", "!!!"])
def test_junk_never_matches_a_real_code(junk: str) -> None:
    conn = FakeConn()
    asyncio.run(replace_codes(conn, USER))
    assert asyncio.run(spend_code(conn, USER, junk)) is False
