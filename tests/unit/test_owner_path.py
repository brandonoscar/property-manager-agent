"""Who owns this property, and can we reach them.

Three defects on one path, all measured before any change (2026-08-28).

**1. "Who owns MA — 24 Beacon St?" had no local answer.** One recorded run
spent 176.7s across five Buildium lookups and concluded *"I could not
determine which person or vendor owns 24 Beacon St."* The mirror knew:
``helixis_owners.property_ids`` covers **48 of Blue Ridge's 50 properties**
and nothing joined it. Fifth instance of gotchas 41/50/51/53 — synced,
never read.

**2. A full name matched no owner.** ``_get_owner_impl`` tested the WHOLE
query against each field separately, so ``"margaret whitlock" in "whitlock"``
is False and so is ``in "margaret"``. The phrasing a user is most likely to
type returned "No rental owner matches". Every existing test passed a single
word, which is why it survived.

**3. An owner with no contact details said nothing about it.** The card simply
omitted the Email line, which reads as "not worth printing" rather than "not
on file" — and the reply then offers to reach out.

⚠ CONTACT DETAILS ARE NOT UNIFORMLY ABSENT, and the audit that found this said
they were. Live: **Blue Ridge 0 of 4 owners, My Company 4 of 4.** A hardcoded
"no contact on file" would be a fresh false claim for half the product —
the same over-generalisation as marking every tenant FORMER (gotcha 51). Both
directions are tested below for exactly that reason.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from helixis.agents import buildium_tools, mirror_tools


def _owner(first="", last="", company=None, email=None, phones=None) -> SimpleNamespace:
    return SimpleNamespace(
        first_name=first,
        last_name=last,
        company_name=company,
        email=email,
        phone_numbers=phones or [],
        address=None,
        property_ids=[1, 2],
    )


# ── the full-name matcher ────────────────────────────────────────────────

OWNERS = [
    _owner(first="Margaret", last="Whitlock"),
    _owner(first="Sanjay", last="Bhatt"),
    _owner(company="Ocean Properties LLC"),
    _owner(first="Margaret", last="Chen"),
]


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("margaret whitlock", ["Whitlock"]),  # THE bug: two words, one owner
        ("Margaret Whitlock", ["Whitlock"]),  # case-insensitive
        ("whitlock", ["Whitlock"]),  # single word still works
        ("bhatt", ["Bhatt"]),
        ("ocean", []),  # company-only match has no last name
        ("sanjay bhatt", ["Bhatt"]),
    ],
)
def test_a_full_name_finds_the_owner(query: str, expected: list[str]) -> None:
    """⚠ The two-word cases are the whole point. Under the old rule every one
    of them returned nothing, and the single-word cases — which is all the
    existing tests used — passed identically then and now."""
    got = [o.last_name for o in buildium_tools._owners_matching(OWNERS, query) if o.last_name]
    assert got == expected, f"{query!r} -> {got}"


def test_a_shared_first_name_does_not_bury_the_exact_match() -> None:
    """Two Margarets. "margaret whitlock" scores 2 on Whitlock and 1 on Chen;
    only the best band is returned, so the answer is unambiguous rather than a
    two-name "which one?" the user already disambiguated."""
    matches = buildium_tools._owners_matching(OWNERS, "margaret whitlock")
    assert [o.last_name for o in matches] == ["Whitlock"]


def test_a_bare_first_name_still_returns_both_margarets() -> None:
    """The other direction — genuine ambiguity must stay ambiguous. Narrowing
    this to one would be a confident guess about which person was meant."""
    matches = buildium_tools._owners_matching(OWNERS, "margaret")
    assert sorted(o.last_name for o in matches) == ["Chen", "Whitlock"]


def test_a_company_name_matches_on_any_word() -> None:
    matches = buildium_tools._owners_matching(OWNERS, "Ocean Properties")
    assert [o.company_name for o in matches] == ["Ocean Properties LLC"]


def test_an_unrelated_query_matches_nothing() -> None:
    assert buildium_tools._owners_matching(OWNERS, "zzzz") == []
    assert buildium_tools._owners_matching(OWNERS, "   ") == []


# ── the contact-details line ─────────────────────────────────────────────


def test_an_owner_with_no_contact_details_says_so() -> None:
    card = buildium_tools._format_owner_card(_owner(first="Sanjay", last="Bhatt"))
    assert "No email or phone on file" in card, card


def test_an_owner_with_an_email_is_not_told_it_has_none() -> None:
    """⚠ The direction the audit got wrong. Half the owners in the product
    carry contact details; a hardcoded caveat would lie about every one."""
    card = buildium_tools._format_owner_card(
        _owner(first="Ada", last="Byron", email="ada@example.com")
    )
    assert "ada@example.com" in card
    assert "No email or phone on file" not in card, card


def test_an_owner_with_only_a_phone_is_not_told_it_has_none() -> None:
    card = buildium_tools._format_owner_card(
        _owner(first="Ada", last="Byron", phones=[{"Number": "555-0100", "Type": "Home"}])
    )
    assert "555-0100" in card
    assert "No email or phone on file" not in card, card


# ── the property -> owner join ───────────────────────────────────────────


def _pool(rows: list[dict]) -> MagicMock:
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=rows)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool


def _row(**over) -> dict:
    row = {
        "first_name": "Sanjay",
        "last_name": "Bhatt",
        "company_name": None,
        "email": None,
        "phone": None,
    }
    row.update(over)
    return row


@pytest.mark.asyncio
async def test_the_owner_is_named_from_the_mirror() -> None:
    line = await mirror_tools._owner_of_property(_pool([_row()]), "co-1", 4610)
    assert line == "- Owner: Sanjay Bhatt — no email or phone on file"


@pytest.mark.asyncio
async def test_an_owner_with_contact_details_says_so_without_sending_them() -> None:
    """Re-pointed 2026-09-25 (Rentvine terms rule 20: the model gets the
    minimum a task needs). A snapshot says a contact IS on file, never "no
    email or phone on file", and names where to get it, but the values
    themselves only come from contact_lookup."""
    line = await mirror_tools._owner_of_property(
        _pool([_row(email="s@example.com", phone="555-0100")]), "co-1", 4610
    )
    assert "email and phone on file" in line and "contact_lookup" in line
    assert "no email or phone on file" not in line
    assert "s@example.com" not in line and "555-0100" not in line


@pytest.mark.asyncio
async def test_an_owner_with_only_an_email_says_only_email() -> None:
    line = await mirror_tools._owner_of_property(
        _pool([_row(email="s@example.com", phone=None)]), "co-1", 4610
    )
    assert "(email on file" in line and "phone" not in line


@pytest.mark.asyncio
async def test_no_owner_row_prints_nothing_rather_than_asserting_no_owner() -> None:
    """⚠ Two of Blue Ridge's 50 properties are genuinely uncovered by
    ``property_ids``. That is a gap in what Buildium sent us, NOT evidence the
    property is unowned — so the tool stays silent and lets the model say it
    does not know, rather than asserting an absence (gotchas 26/55)."""
    assert await mirror_tools._owner_of_property(_pool([]), "co-1", 4610) == ""


@pytest.mark.asyncio
async def test_co_owners_are_all_named() -> None:
    line = await mirror_tools._owner_of_property(
        _pool([_row(), _row(first_name="Ada", last_name="Byron")]), "co-1", 4610
    )
    assert line.startswith("- Owners:")
    assert "Sanjay Bhatt" in line and "Ada Byron" in line


def test_property_snapshot_actually_calls_the_owner_join() -> None:
    """⚠ Everything above tests ``_owner_of_property`` in isolation, so all of
    it would still pass with the snapshot never calling it — which is the
    shipped state this change exists to fix. Mutation caught exactly that:
    replacing the call with ``owner_line = ''`` left the whole file green.

    AST rather than a substring (gotcha 40).
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(mirror_tools._property_snapshot_impl))
    called = {
        n.func.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "_owner_of_property" in called, (
        "property_snapshot no longer joins the owner — 'who owns this?' goes "
        "back to costing a specialist consult that cannot answer it"
    )


@pytest.mark.asyncio
async def test_the_owner_query_is_company_scoped() -> None:
    """gotcha 47 — a buildium property id is only unique within a company."""
    pool = _pool([_row()])
    await mirror_tools._owner_of_property(pool, "co-1", 4610)
    sql = pool.acquire.return_value.__aenter__.return_value.fetch.await_args.args[0]
    assert "company_id = $1::uuid" in sql
    assert "= ANY(property_ids)" in sql
