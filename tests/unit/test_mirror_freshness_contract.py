"""Every mirror-backed read must say how old its data is.

The mirror answers from Postgres instead of Buildium, which is why it is
instant — and why it can be wrong in a way the live API cannot. Measured
2026-08-26 on ``helixis_mirror_sync_state``: the live company's tables were
**1.4 hours** behind. That is fine for "how's the portfolio doing" and wrong
for "did the Johnson payment come through", and **the user cannot tell a
stale answer from an incorrect one** — it is the absence-claim failure of
gotchas 26/42 with a timestamp instead of an empty list.

``_freshness_line`` already solves it properly: it reads
``helixis_mirror_sync_state`` per table, reports the age of the freshest CORE
table, and names the stalest one once it is >26h behind. Eleven of the
thirteen registered read tools call it.

What did NOT exist is the CONTRACT. Eleven of thirteen is a coincidence, not
a rule — the fourteenth mirror tool can ship without a freshness line and
nothing fails. This file makes it a rule.

⚠ Reads BOTH sides (gotcha 28): the ``_MIRROR_READS`` registry that
``call_budget`` treats as free-and-local, AND the implementations behind those
names. A test over either alone passes while they disagree — which is exactly
how the eval cost gate went five weeks classifying 139 billed cases as free
(gotcha 45).

⚠ **THIS GUARD HAD A ONE-LINE HOLE UNTIL 2026-09-09, and it accepted the exact
form the rule exists to replace.** ``_emits_freshness`` read
``{"_freshness_line", "_as_of_line"} & called`` — so a bare
``_as_of_line(await _latest_updated(...))`` satisfied "reports its age". Those
two functions are not interchangeable and differ on the only thing that
matters: ``_as_of_line`` prints a timestamp and passes no judgement, while
``_freshness_line`` names the stalest CORE table once it is >26h behind AND
adds "there is no Buildium connection on file, so this will NOT catch up on
its own". ``_latest_updated`` is worse still — a ``GREATEST`` over three
tables, which is verbatim the §1.11 gap migration 0057 was added to close: a
property row synced five minutes ago prints "updated 5 min ago" over a
week-stale balances table.

Six call sites were passing on that hole, and they were the worst six
available: every "nothing to report" early-return —

    "No outstanding balances — every lease is current."
    "No unpaid bills on file."
    "No open tasks — the board is clear."
    "No active leases expire in the next N days."
    "No billed spend in the last N days."

plus ``delinquency_report``'s main path. So each tool WARNED when it found
something and FLATTERED when it said there was nothing — exactly backwards,
because an assertion of absence is the claim that needs the caveat most
(gotcha 26), and on the delinquency path it is the claim that sends a late
notice to a real person.

The lesson is gotcha 59's, one level over: **a guard on "does it report X"
must also pin WHICH implementation of X**, when two exist and they disagree
precisely where it counts.
"""

from __future__ import annotations

import ast
import pathlib

from helixis.agents.call_budget import _MIRROR_READS

SOURCE = pathlib.Path("src/helixis/agents/mirror_tools.py")

#: Registered mirror reads that legitimately carry no mirror freshness, each
#: with the reason it is exempt. An entry here is a claim that the tool's
#: answer does not come from the Buildium mirror at all — not "we did not get
#: round to it".
EXEMPT: dict[str, str] = {
    # Static jurisdiction reference data (county/state rules), not synced
    # Buildium rows — a Buildium sync age would be a meaningless number
    # attached to an answer it does not describe.
    "jurisdiction_lookup": "static reference data, not mirrored Buildium rows",
    # Reads `helixis_property_intel.fetched_at` — Superfund / FMR / hazard
    # feeds on their own cadence, so `_as_of_line` over ITS OWN timestamps is
    # the honest footer and a Buildium sync verdict would be a warning about a
    # table this tool never touches. Exempt from `_freshness_line`, NOT from
    # reporting an age: `test_an_exempt_tool_still_reports_some_age` holds it.
    "property_hazards": "property-intel fetch times, not the Buildium mirror",
}


def _impl_bodies() -> dict[str, ast.AsyncFunctionDef]:
    """``tool_name -> the _<tool>_impl AST node``.

    ⚠ AST, not substring matching (gotcha 40). A guard that greps for
    ``_freshness_line`` cannot survive ``ruff format`` reflowing a call across
    lines, and it matches the word inside a comment explaining the guard. A
    comment is never a ``Call`` node.
    """
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    out: dict[str, ast.AsyncFunctionDef] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name.startswith("_"):
            if node.name.endswith("_impl"):
                out[node.name[1:-5]] = node
    return out


def _section_tables() -> dict[str, set[str]]:
    """Module-level constants that hold ``*_impl`` references.

    ``portfolio_review`` composes its sections through such a table —
    ``*(impl() for _, impl in _PORTFOLIO_REVIEW_SECTIONS)`` — so the name it
    *calls* is the loop variable, and a naive "does it call an ``_impl``"
    check reads it as bare. Resolving the table is what tells delegation
    apart from omission, and getting that wrong would either exempt a real
    gap or flag a tool that is already correct.
    """
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    tables: dict[str, set[str]] = {}
    for node in tree.body:
        # ⚠ BOTH assignment forms. The real table is annotated —
        # ``_PORTFOLIO_REVIEW_SECTIONS: tuple[tuple[str, Any], ...] = (...)``
        # — which is an ``AnnAssign``, not an ``Assign``. Handling only the
        # latter made this walker find nothing and report a compliant tool as
        # bare; any future typed table would have slipped the same way.
        if isinstance(node, ast.Assign):
            targets: list[ast.expr] = list(node.targets)
            value = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        impls = {
            n.id
            for n in ast.walk(value)
            if isinstance(n, ast.Name) and n.id.startswith("_") and n.id.endswith("_impl")
        }
        if not impls:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                tables[target.id] = impls
    return tables


def _called_names(node: ast.AST) -> set[str]:
    out: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            name = getattr(sub.func, "id", None) or getattr(sub.func, "attr", None)
            if name:
                out.add(name)
    return out


def _emits_freshness(node: ast.AST, tables: dict[str, set[str]] | None = None) -> bool:
    """Does this implementation report its data's age with a VERDICT —
    directly, or by composing implementations that do?

    ⚠ ``_as_of_line`` deliberately does NOT count any more. It is a timestamp
    with no judgement attached; ``_freshness_line`` is the same timestamp plus
    the stalest-table warning and the "no Buildium connection, this will not
    catch up" branch. Accepting both is what let six absence claims ship with
    the flattering number — see this module's docstring.
    """
    tables = {} if tables is None else tables
    called = _called_names(node)
    referenced = {sub.id for sub in ast.walk(node) if isinstance(sub, ast.Name)}

    if "_freshness_line" in called:
        return True
    # Direct delegation to another *_impl coroutine.
    if any(n.startswith("_") and n.endswith("_impl") for n in called):
        return True
    # Table-driven delegation (the portfolio_review shape).
    return any(name in tables for name in referenced)


def _uses_the_legacy_footer(node: ast.AST) -> bool:
    """The specific defect: an implementation reaching for the pre-0057 pair.

    ⚠ Scoped to ``*_impl`` bodies rather than the file (gotchas 40/59).
    ``_freshness_line`` itself calls both — that is its documented
    pre-migration fallback — and this module's own prose names them in order
    to explain the rule, so a file-wide scan would fire on the correct code
    and on the sentence describing it.
    """
    return bool({"_as_of_line", "_latest_updated"} & _called_names(node))


def test_every_registered_mirror_read_reports_its_age() -> None:
    """⚠ The rule, not the coincidence.

    A mirror answer with no age is presented to the user as current fact. On
    a 1.4-hour-old row that is a confident, unfalsifiable, wrong answer — and
    `bank_balances` is in this set, so the failure mode is a stale MONEY
    figure stated flatly.
    """
    impls = _impl_bodies()
    tables = _section_tables()
    missing = []
    for tool in sorted(_MIRROR_READS):
        if tool in EXEMPT:
            continue
        node = impls.get(tool)
        if node is None:
            continue  # covered by the registry test below
        if not _emits_freshness(node, tables):
            missing.append(tool)

    assert not missing, (
        f"these mirror reads answer from data that can be hours old and never say so: "
        f"{missing}. Add `_freshness_line(pool, company_id)` to the returned text, or "
        f"add an entry to EXEMPT stating why the tool's answer is not mirror-derived."
    )


def test_no_rollup_reaches_for_the_pre_0057_footer() -> None:
    """The absence paths are the reason this test exists.

    ``_as_of_line(await _latest_updated(...))`` is a ``GREATEST`` over three
    tables wearing a freshness footer's clothes, and every call site it had was
    a sentence asserting that nothing is there. An exempt tool may still call
    ``_as_of_line`` over ITS OWN timestamps — what is banned is a mirror rollup
    doing so instead of taking the verdict.
    """
    impls = _impl_bodies()
    offenders = [
        tool
        for tool in sorted(_MIRROR_READS)
        if tool not in EXEMPT and tool in impls and _uses_the_legacy_footer(impls[tool])
    ]
    assert not offenders, (
        f"{offenders} report an age with no verdict. `_as_of_line` prints a "
        "timestamp; `_freshness_line` prints the same timestamp AND names the "
        "stalest core table and whether the connection is gone. Pass a more "
        "specific timestamp as `_freshness_line(pool, company_id, as_of=...)` "
        "if the tool has one — do not drop back to the legacy pair."
    )


def test_an_exempt_tool_still_reports_some_age() -> None:
    """EXEMPT means "the Buildium sync verdict does not describe my data" — it
    does not mean "say nothing about how old this is". ``property_hazards``
    keeps ``_as_of_line`` over its own fetch times; ``jurisdiction_lookup``
    reads static reference data and has no age to report, which is why it is
    named here rather than folded into one rule.
    """
    impls = _impl_bodies()
    node = impls.get("property_hazards")
    assert node is not None, "property_hazards no longer resolves — re-check its exemption"
    # ⚠ EITHER footer satisfies the rule, and this used to name `_as_of_line`
    # alone. That was the right rule pinned to the wrong thing: the property is
    # "this tool states the age of its own data", not "this tool calls one
    # particular helper". `_intel_as_of_line` is the STRICTER of the two here —
    # `_as_of_line` renders hours (a year-old FEMA row read "9600h ago") and
    # attributes the data to "Occupella's synced Buildium data", which is a
    # source that has never seen it. See tests/unit/test_intel_freshness.py.
    assert {"_as_of_line", "_intel_as_of_line"} & _called_names(node), (
        "property_hazards is exempt from the Buildium verdict because its data "
        "is not mirrored Buildium rows — not from saying how old that data is"
    )


def test_the_registry_and_the_module_agree() -> None:
    """Both sides. A name in ``_MIRROR_READS`` that has no implementation is a
    tool ``call_budget`` treats as free-and-local while it is neither — the
    memoization and the cap would both be pointed at nothing."""
    impls = _impl_bodies()
    # Geo tools live in their own module; only assert on names this file owns.
    here = {t for t in _MIRROR_READS if t in impls}
    assert here, "no registered mirror read resolves to mirror_tools — has the module moved?"
    for tool in sorted(here):
        assert impls[tool].name == f"_{tool}_impl"


def test_exemptions_are_real_registered_tools() -> None:
    """An EXEMPT entry for a tool that no longer exists is a stale excuse that
    would silently cover a future tool of the same name."""
    unknown = set(EXEMPT) - set(_MIRROR_READS)
    assert not unknown, f"EXEMPT names tools that are not registered mirror reads: {unknown}"


def test_the_guard_can_actually_fail() -> None:
    """⚠ Assert the probe RUNS (gotcha 33). A detector that cannot distinguish
    a compliant implementation from a bare one is not a guard — it is a test
    that always passes wearing a guard's costume."""
    compliant = ast.parse(
        "async def _x_impl():\n    return 'rows\\n' + await _freshness_line(pool, cid)\n"
    ).body[0]
    bare = ast.parse("async def _y_impl():\n    return 'rows'\n").body[0]
    delegating = ast.parse("async def _z_impl():\n    return await _open_tasks_impl()\n").body[0]
    table_driven = ast.parse(
        "async def _w_impl():\n    return [impl() for _, impl in _SECTIONS]\n"
    ).body[0]

    legacy = ast.parse(
        "async def _v_impl():\n"
        "    return 'none. ' + _as_of_line(await _latest_updated(pool, cid))\n"
    ).body[0]

    assert _emits_freshness(compliant)
    assert not _emits_freshness(bare), "the detector cannot see a missing freshness line"
    # ⚠ The mutation the OLD guard could not see. Until 2026-09-09 this
    # returned True, which is how six absence claims shipped with a timestamp
    # and no verdict.
    assert not _emits_freshness(legacy), (
        "a bare `_as_of_line` satisfies the contract again — that is the hole"
    )
    assert _uses_the_legacy_footer(legacy)
    assert not _uses_the_legacy_footer(compliant), (
        "the legacy detector fires on correct code, so it would have to be switched off (gotcha 52)"
    )
    assert _emits_freshness(delegating), "composition inherits its sections' footers"
    assert not _emits_freshness(table_driven), "an UNKNOWN table must not count as delegation"
    assert _emits_freshness(table_driven, {"_SECTIONS": {"_open_tasks_impl"}}), (
        "a resolved section table IS delegation — this is the portfolio_review shape"
    )
