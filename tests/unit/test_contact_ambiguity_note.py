"""When a name matches two people, the tool must not invite a choice.

`buildium_get_tenant` learned this in gotcha 41's fix: its ambiguity list ends
by telling the model to name ALL of them and ask, because *"stating one
confidently would be a guess presented as a fact"*. That fix followed a reply
which put Marcus Whitfield's name on Elena Vasquez's address.

`contact_lookup` never got the same line. It is the MIRROR tool — the one the
model reaches for first, and the one gotcha 51 was found in — and it printed a
list of matching people followed by a freshness footer. A list with no
instruction is an invitation to pick, and two tenants sharing a surname is
exactly the shape (checklist #104).

⚠ **The rows themselves were already right.** Gotchas 41/51 made each line
self-contained and tense-correct, so this is not "make the data better" — it is
the closing line that tells the model what to DO with two rows it can no longer
mis-join. Both halves are needed and only one was present.

⚠ **Silent on a single match, and that is what keeps it read.** A
disambiguation instruction on every lookup is one the model learns to skip —
gotcha 52's cry-wolf, pointed at a prompt instead of a regex. Every assertion
below has its negative twin.
"""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helixis.agents import mirror_tools

_MIRROR = Path("src/helixis/agents/mirror_tools.py")
_BUILDIUM = Path("src/helixis/agents/buildium_tools.py")


def _module_constant(path: Path, name: str) -> str:
    """A module-level string constant, read from the AST.

    ⚠ AST rather than a grep: ``ruff format`` splits a long string across
    several implicitly-concatenated lines, so a substring scan sees fragments
    and a line-anchored one sees nothing (gotcha 40).
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        targets = (
            node.targets
            if isinstance(node, ast.Assign)
            else [node.target]
            if isinstance(node, ast.AnnAssign)
            else []
        )
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            value = node.value
            assert value is not None
            return str(ast.literal_eval(value))
    raise AssertionError(f"{name} is not a module-level constant in {path}")


def _impl(path: Path, name: str) -> ast.AsyncFunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {path}")


# ── the instruction itself ───────────────────────────────────────────


def test_the_note_tells_the_model_to_ask_rather_than_choose() -> None:
    note = _module_constant(_MIRROR, "_MULTIPLE_PEOPLE_NOTE")
    assert "name ALL of them and ask" in note, (
        "the note no longer tells the model to ask — a list of matching "
        "people with no instruction is an invitation to pick one"
    )
    assert "guess presented as a fact" in note, (
        "the note lost the reason. 'Ask' without 'stating one would be a "
        "guess' reads as a style preference rather than a correctness rule"
    )


def test_the_note_also_forbids_re_joining_the_rows() -> None:
    """Gotcha 41's original defect was a name from one row with an address
    from another. Each line is self-contained now; the note says not to undo
    that."""
    note = _module_constant(_MIRROR, "_MULTIPLE_PEOPLE_NOTE")
    assert "Do NOT combine a name from one" in note


def test_both_surfaces_give_the_same_instruction() -> None:
    """⚠ Two wordings for one rule drift, and the drift here would be one
    surface telling the model to ask while the other lets it choose (gotcha
    73's "approved" in three modules, pointed at a prompt).

    Compared on the load-bearing clauses rather than byte-for-byte: the two
    lists differ in what a row can carry (a Buildium tenant has an address; a
    mirror row may be an owner or a vendor), so identical text would be a
    false pin.
    """
    mirror = _module_constant(_MIRROR, "_MULTIPLE_PEOPLE_NOTE")
    buildium = _BUILDIUM.read_text(encoding="utf-8")
    for clause in (
        "Each line above is one person, complete.",
        "name ALL of them and ask",
        "would be a guess presented as a fact",
    ):
        assert clause in mirror, f"the mirror note dropped {clause!r}"
        assert clause in buildium, (
            f"the Buildium note dropped {clause!r} — the two surfaces now give "
            "different instructions for the same situation"
        )


# ── when it fires ────────────────────────────────────────────────────


def _guard_test(node: ast.AST) -> ast.If:
    """The `if` whose body appends the note."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.If) and any(
            isinstance(n, ast.Name) and n.id == "_MULTIPLE_PEOPLE_NOTE" for n in ast.walk(sub)
        ):
            return sub
    raise AssertionError("contact_lookup never appends _MULTIPLE_PEOPLE_NOTE")


def test_the_note_is_conditional_not_unconditional() -> None:
    """⚠ The assertion that keeps it worth reading. Appended every time, this
    is a line the model sees on every single contact lookup and learns to skip
    — and then it is absent exactly when it matters (gotcha 52)."""
    guard = _guard_test(_impl(_MIRROR, "_contact_lookup_impl"))
    source = ast.unparse(guard.test)
    assert "> 1" in source, (
        f"the note's condition is {source!r} — it must fire only when more than one person matched"
    )


def test_the_condition_counts_every_family() -> None:
    """A tenant and an owner sharing a name is the same ambiguity as two
    tenants. Counting only one list would let the commonest cross-family case
    through silently."""
    guard = _guard_test(_impl(_MIRROR, "_contact_lookup_impl"))
    source = ast.unparse(guard.test)
    for family in ("tenants", "owners", "vendors"):
        assert family in source, (
            f"{family} are not counted toward ambiguity, so a name matching a "
            f"{family[:-1]} and someone else reads as a single confident hit"
        )


# ── what the model actually reads ────────────────────────────────────
#
# ⚠ The AST guards above prove the append EXISTS and is conditional. They
# cannot see it sitting in a branch nothing reaches, which is gotcha 66's
# shape — a structural fix bound where the measurement never runs. These
# two drive the real tool and read the string it returns.


class _AcquireCM:
    def __init__(self, conn: object) -> None:
        self._conn = conn

    async def __aenter__(self) -> object:
        return self._conn

    async def __aexit__(self, *_a: object) -> bool:
        return False


def _tenant(first: str, last: str, prop: str) -> dict:
    """A row in the shape `contact_lookup` actually selects.

    ⚠ `lease_status` / `ended_on` are gotcha 51's columns — a fixture that
    omitted them would exercise a shape the tool no longer produces.
    """
    return {
        "first_name": first,
        "last_name": last,
        "email": f"{first.lower()}@t.example",
        "phone": None,
        "properties": prop,
        "lease_status": "Active",
        "ended_on": None,
        "link_status": "Current",
    }


async def _lookup(tenants: list[dict]) -> str:
    conn = MagicMock()
    # tenants, then owners, then vendors — the three families in order.
    conn.fetch = AsyncMock(side_effect=[tenants, [], []])
    conn.fetchrow = AsyncMock(return_value={"latest": None})
    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=lambda: _AcquireCM(conn))
    ctx = MagicMock()
    ctx.company_id = "co-1"
    with (
        patch.object(mirror_tools, "current_memory_context", return_value=ctx),
        patch.object(mirror_tools, "get_pool", return_value=pool),
    ):
        return await mirror_tools._contact_lookup_impl("Whitfield")


@pytest.mark.asyncio
async def test_two_people_sharing_a_surname_get_the_note() -> None:
    """Checklist #104, and gotcha 41's own defect: two rows, one surname, and
    the model picking whichever it read last."""
    out = await _lookup(
        [
            _tenant("Marcus", "Whitfield", "1200 Dexter Ave, Montgomery"),
            _tenant("Elena", "Whitfield", "1700 W Washington St, Phoenix"),
        ]
    )
    assert "Marcus Whitfield" in out and "Elena Whitfield" in out
    assert _module_constant(_MIRROR, "_MULTIPLE_PEOPLE_NOTE") in out, (
        "two people matched and the result reads as a list to pick from — "
        "which is how one person's name reached another's address"
    )


@pytest.mark.asyncio
async def test_a_single_match_is_left_alone() -> None:
    """⚠ The half that keeps the note worth reading. On every lookup it is a
    line the model learns to skip, and then it is absent exactly when it
    matters (gotcha 52)."""
    out = await _lookup([_tenant("Marcus", "Whitfield", "1200 Dexter Ave, Montgomery")])
    assert "Marcus Whitfield" in out
    assert _module_constant(_MIRROR, "_MULTIPLE_PEOPLE_NOTE") not in out
    assert "name ALL of them" not in out


def test_the_guard_can_say_no() -> None:
    """Proof the walker distinguishes a conditional append from an
    unconditional one — otherwise it passes against the defect it targets
    (gotcha 45)."""
    conditional = ast.parse(
        "async def f():\n    if n > 1:\n        lines.append(_MULTIPLE_PEOPLE_NOTE)\n"
    )
    assert isinstance(_guard_test(conditional), ast.If)

    unconditional = ast.parse("async def f():\n    lines.append(_MULTIPLE_PEOPLE_NOTE)\n")
    try:
        _guard_test(unconditional)
    except AssertionError:
        pass
    else:  # pragma: no cover - only reached when the walker is broken
        raise AssertionError("the walker treats an unconditional append as guarded")
