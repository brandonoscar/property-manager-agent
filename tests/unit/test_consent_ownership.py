"""A person who leaves must stop receiving the company's alert texts.

THE DEFECT
----------
The manager digest (`reminders/sms_digest.py`) texts every live
`recipient_kind='manager'` consent row after each reminder sweep — "3 items
need attention; top: <the top card's title>", which is portfolio content.
`remove_company_member` deleted the membership, wrote an activity row and
re-synced seats, and touched that ledger nowhere. A departed manager kept
getting them.

It could not have done otherwise before migration 0096: nothing on a consent
row said whose number it was. `display_name` is free text and
`evidence->>'recorded_by'` is whoever TYPED it — for a tenant's number, an
admin. Neither is an owner.

WHAT IS PINNED, AND THE SHAPE OF IT
-----------------------------------
Three separate things, because they fail separately:

1. the SQL does what it claims (revokes, never deletes; manager-kind only;
   owned rows only);
2. an unreadable ledger reports `None` and never 0 — the difference between
   "they had claimed none" and "nothing was checked";
3. **the routes actually CALL it.** A module-level test proving a helper is
   correct cannot see a caller that ignores it — gotchas 62/66/69 are three
   separate instances of exactly that, so the call-site checks are AST walks
   over both departure paths.

⚠ This container cannot run the route tests (the Agents SDK will not install),
so the call-site guards are static by necessity as well as by design. The SQL
itself was verified behaviourally against helixis-test — including that the
FK's SET NULL actually fires — inside a DO block that RAISES so it rolled back
by construction (gotchas 70/81). Recorded in CLAUDE.md.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from helixis.comms import consent_ownership as own

_ROOT = Path(__file__).resolve().parents[2]
_COMPANY_ROUTE = _ROOT / "src" / "helixis" / "api" / "routes" / "company.py"

COMPANY = "5ca1ab1e-0000-4000-8000-000000000004"
USER = "6b1f0d2c-1111-4a4a-9999-0f0f0f0f0f0f"


def _pool(*, fetch: Any = None, fetchrow: Any = None) -> MagicMock:
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=fetch if fetch is not None else [])
    pool.fetchrow = AsyncMock(return_value=fetchrow)
    return pool


# ── the revocation itself ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_it_revokes_rather_than_deletes() -> None:
    """The ledger is the TCPA record and has to prove BOTH the grant and the
    revocation (0068). A DELETE would destroy the evidence that the person
    ever consented, which is the half a dispute turns on."""
    pool = _pool(fetch=[{"id": "1"}])
    assert await own.revoke_for_departing_member(pool, company_id=COMPANY, user_id=USER) == 1
    sql = pool.fetch.await_args.args[0]
    assert "UPDATE" in sql and "DELETE" not in sql.upper()
    assert "revoked_at" in sql and "revocation_source" in sql


@pytest.mark.asyncio
async def test_it_records_why_in_the_ledger() -> None:
    """`staff_departure` distinguishes an offboarding from a STOP text and
    from somebody using the settings screen. An auditor reading the row can
    tell which happened; a single generic source cannot say."""
    pool = _pool(fetch=[])
    await own.revoke_for_departing_member(pool, company_id=COMPANY, user_id=USER)
    assert own.REVOCATION_SOURCE in pool.fetch.await_args.args


@pytest.mark.asyncio
async def test_it_touches_only_this_persons_claimed_staff_numbers() -> None:
    """Four bounds, and each one is a way this could silence the wrong person.

    ⚠ The tenancy bound is the least obvious and the most defensible:
    employment and renting are different relationships, so a property manager
    who also rents from the company must not lose texts about their own home
    because they changed jobs.
    """
    pool = _pool(fetch=[])
    await own.revoke_for_departing_member(pool, company_id=COMPANY, user_id=USER)
    sql = pool.fetch.await_args.args[0]
    assert "company_id" in sql, "an unscoped revoke reaches another tenant (gotcha 47)"
    assert "owner_user_id" in sql, "without this it cannot tell whose number it is"
    assert "recipient_kind" in sql, "a tenancy must survive an employment ending"
    assert "revoked_at IS NULL" in sql, "re-revoking would move an existing revocation date"
    assert own.STAFF_KIND in pool.fetch.await_args.args


@pytest.mark.asyncio
async def test_an_unclaimed_row_is_left_alone() -> None:
    """The whole design in one assertion: NULL means "we do not know", and a
    guess would silence a colleague on somebody else's departure. The SQL
    matches on equality, so a NULL owner cannot be swept up."""
    sql = own._REVOKE_SQL
    assert "owner_user_id  = $2::uuid" in sql
    assert "owner_user_id IS NULL" not in sql, (
        "a revoke that matched unattributed rows would silence everybody"
    )


@pytest.mark.asyncio
async def test_an_unreadable_ledger_reports_none_and_never_zero() -> None:
    """Zero says "they had claimed none". None says "nothing was checked".
    Collapsing the second into the first tells an admin the offboarding is
    complete when it was never looked at (gotcha 57's unknown-must-not-become
    -zero, and gotcha 70's unverifiable-is-never-a-pass)."""
    pool = _pool()
    pool.fetch = AsyncMock(side_effect=RuntimeError("pool down"))
    assert await own.revoke_for_departing_member(pool, company_id=COMPANY, user_id=USER) is None

    pool2 = _pool()
    pool2.fetchrow = AsyncMock(side_effect=RuntimeError("pool down"))
    assert await own.unattributed_manager_numbers(pool2, company_id=COMPANY) is None


@pytest.mark.asyncio
async def test_a_failure_never_aborts_the_departure() -> None:
    """The membership row is already gone by the time this runs. Raising here
    would 500 a removal that HAPPENED, leaving the caller believing it did
    not — strictly worse than a stale consent row, which is recoverable."""
    pool = _pool()
    pool.fetch = AsyncMock(side_effect=RuntimeError("boom"))
    await own.revoke_for_departing_member(pool, company_id=COMPANY, user_id=USER)  # no raise


# ── the honest half: what we could NOT settle ─────────────────────────


@pytest.mark.asyncio
async def test_it_counts_the_numbers_no_departure_can_act_on() -> None:
    pool = _pool(fetchrow={"n": 3})
    assert await own.unattributed_manager_numbers(pool, company_id=COMPANY) == 3
    sql = pool.fetchrow.await_args.args[0]
    assert "owner_user_id IS NULL" in sql
    assert "revoked_at IS NULL" in sql, "a revoked row is not a live exposure"
    assert "company_id" in sql, "gotcha 47 — every helixis_* query is scoped"


# ── the routes call it (gotchas 62 / 66 / 69) ────────────────────────


def _calls_in(func_name: str) -> set[str]:
    """Every function called inside one route, by name."""
    tree = ast.parse(_COMPANY_ROUTE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == func_name:
            return {
                inner.func.id
                for inner in ast.walk(node)
                if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name)
            }
    raise AssertionError(f"{func_name} not found — re-point this guard, do not delete it")


@pytest.mark.parametrize("route", ["remove_company_member", "leave_company"])
def test_both_departure_paths_revoke(route: str) -> None:
    """BOTH, or the forgotten one is silently exempt.

    ⚠ `leave_company` is the likelier of the two to be used by somebody who
    has already changed jobs — which is exactly the person who should stop
    receiving the digest. A guard covering only the admin-initiated path would
    read as coverage while missing the common case.
    """
    assert "revoke_for_departing_member" in _calls_in(route), (
        f"{route} removes the membership and leaves their alert texts live"
    )


def test_the_admin_path_also_reports_what_it_could_not_settle() -> None:
    """Revoking what we know about is half the job; saying how much we do NOT
    know is the half that lets an admin close the gap."""
    assert "unattributed_manager_numbers" in _calls_in("remove_company_member")


def test_the_claim_is_never_inferred_from_the_submitter() -> None:
    """THE ONE THAT MATTERS MOST.

    The obvious shortcut is to record whoever submitted the form as the owner.
    That is wrong for every number entered on somebody else's behalf, and it
    turns a colleague's departure into a silent revocation of a number that
    was never theirs. Ownership is only ever written from an EXPLICIT claim.
    """
    from helixis.api.routes.sms import ConsentIn

    assert ConsentIn.model_fields["is_own_number"].default is False, (
        "defaulting the claim to true infers ownership from whoever typed it"
    )

    source = (_ROOT / "src" / "helixis" / "api" / "routes" / "sms.py").read_text(encoding="utf-8")
    assert "user.user_id if payload.is_own_number else None" in source, (
        "the owner must be gated on the explicit claim, never passed unconditionally"
    )
    # ⚠ Re-consent must not ERASE a claim: a START text and an admin re-adding
    # a number both land on the same upsert, and EXCLUDED alone would null the
    # ownership out — after which no departure can revoke it, silently.
    assert "COALESCE(EXCLUDED.owner_user_id" in source


def test_the_migration_states_a_delete_rule_and_it_is_set_null() -> None:
    """A consent row must survive the deletion of the person it names — it is
    the record that proves the grant AND the revocation, and a TCPA defence
    needs it after they are gone. CASCADE would destroy the evidence.

    (`test_fk_delete_rules.py` enforces that SOME rule is stated; this pins
    WHICH one, because the two failures look nothing alike.)
    """
    sql = (_ROOT / "supabase" / "migrations" / "0096_sms_consent_owner.sql").read_text(
        encoding="utf-8"
    )
    statement = sql.split("alter table", 1)[1].split(";", 1)[0]
    assert "on delete set null" in statement.lower()
    assert "cascade" not in statement.lower()
