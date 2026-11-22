"""The write harness names real tools, tiers them honestly, and hides the JWT.

`scripts/test_buildium_writes.py` is the only thing that exercises the whole
Occupella -> Buildium write path end to end, and it is run by hand against a
LIVE account. Three of its cases cannot be undone by any API. So the failure
that matters is not a crash -- it is a case that looks like it tested
something and did not.

The load-bearing guard is the first one. A case whose `tool` is a typo, or
names a tool that has since been renamed or unregistered, still emits a prompt,
still reaches SOME gate or none, and still prints a verdict. Nothing about the
run says the tool it claims to cover was never involved. That is gotcha 35's
"a case that has never run is not a passing case", pointed at a live-write
harness where the cost of believing a false pass is a write nobody checked.

⚠ This reads BOTH sides (gotcha 28): the harness's case list AND the live
registries the Buildium agent actually gets.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

from helixis.agents.buildium_tools import buildium_tools
from helixis.agents.sms_tools import sms_tools

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
_HARNESS = _SCRIPTS / "test_buildium_writes.py"


def _load():
    """Import the script by path -- it lives in scripts/, not the package."""
    sys.path.insert(0, str(_SCRIPTS))
    try:
        spec = importlib.util.spec_from_file_location("_write_harness", _HARNESS)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(str(_SCRIPTS))


_FX = {
    "property": "500 Woodlane St",
    "unit": "Unit 1",
    "vendor": "Volt & Vine Electric",
    "tenant": "Doyle Prather",
}


# ── the both-sides guard ──────────────────────────────────────────────


def test_every_case_names_a_tool_that_is_actually_registered() -> None:
    """A renamed or unregistered tool must fail HERE, not silently pass live.

    The harness never calls these names -- it sends natural-language prompts
    and lets the orchestrator route. So the name is a CLAIM about what the
    case covers, and a claim in a string cannot fail on its own.
    """
    mod = _load()
    registered = {t.name for t in (*buildium_tools(), *sms_tools())}
    named = {c["tool"] for c in mod.build_cases(_FX)}
    unknown = sorted(named - registered)
    assert not unknown, (
        f"the harness claims to cover tools that are not registered: {unknown}\n"
        "Each of these cases will still run, still print a verdict, and still "
        "prove nothing. Fix the name, or delete the case if the tool is gone."
    )


def test_the_guard_can_see_a_bad_name() -> None:
    """The self-check the guard above needs. If `registered` were ever empty
    or misspelled, the assertion above would pass vacuously for every input --
    which is exactly the shape of defect it exists to catch."""
    registered = {t.name for t in (*buildium_tools(), *sms_tools())}
    assert "buildium_post_charge" in registered, "the registry read is broken"
    assert "buildium_post_charg" not in registered


def test_the_irreversible_three_are_the_ones_with_no_undo() -> None:
    """Tier is a safety claim, not a label. These three are irreversible for
    reasons recorded against live probes: a charge cannot be deleted, a
    payment cannot be edited or deleted, a work-order close latches one-way
    (gotchas 20/21). Demoting one to `additive` would let the default-adjacent
    `--tier additive` run spend a permanent write."""
    mod = _load()
    by_tier: dict[str, set[str]] = {}
    for c in mod.build_cases(_FX):
        by_tier.setdefault(c["tier"], set()).add(c["tool"])
    assert by_tier["irreversible"] == {
        "buildium_post_charge",
        "buildium_record_payment",
        "buildium_close_work_order",
    }
    # And nothing in the irreversible tier may claim an undo it does not have.
    for c in mod.build_cases(_FX):
        if c["tier"] == "irreversible":
            assert c["undo"] is None, f"{c['tool']} claims an undo; none exists"


def test_tiers_are_cumulative_and_default_is_the_safe_one() -> None:
    mod = _load()
    cases = mod.build_cases(_FX)
    rank = mod._TIER_RANK
    assert rank["reversible"] < rank["additive"] < rank["irreversible"]
    reversible = [c for c in cases if rank[c["tier"]] <= rank["reversible"]]
    everything = [c for c in cases if rank[c["tier"]] <= rank["irreversible"]]
    assert len(reversible) < len(everything) == len(cases)
    assert mod.build_parser().parse_args(["--company-id", "x", "--fixtures", "y"]).tier == (
        "reversible"
    ), "the default tier must be the one whose every write has a probed undo"


def test_nothing_is_written_without_execute() -> None:
    """`--execute` is the only thing that turns an approval on. The parser
    default is the whole safety property: a run that forgets the flag declines
    every gate."""
    args = mod_args = _load().build_parser().parse_args(["--company-id", "x", "--fixtures", "y"])
    assert args.execute is False
    assert mod_args.i_accept_irreversible is False


# ── the JWT never reaches argv ────────────────────────────────────────


def test_the_token_is_not_a_command_line_argument() -> None:
    """A JWT passed as an argument is visible in the process list and lands in
    shell history. The PowerShell predecessor took `-Token` and had to be
    changed; this pins that it cannot come back.

    Asserted on the AST, not a substring: `ruff format` moves arguments across
    lines freely, and a comment in this file legitimately contains the string
    `--token` while explaining the rule (gotcha 40).
    """
    tree = ast.parse(_HARNESS.read_text(encoding="utf-8"))
    added: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_argument"
        ):
            for a in node.args:
                if isinstance(a, ast.Constant) and isinstance(a.value, str):
                    added.append(a.value)
    assert added, "no add_argument calls found -- the AST scan is not reading the parser"
    leaked = [a for a in added if "token" in a.lower() or "secret" in a.lower()]
    assert not leaked, (
        f"credential-shaped command-line argument(s): {leaked} -- read it from the "
        "environment instead; argv is visible in the process list and in history"
    )


# ── verdicts ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("gate", "text", "error", "execute", "expected"),
    [
        (True, "", None, False, "GATED-DECLINED"),
        # An EMPTY reply is not a confirmation. Under the old fail-open
        # reading it was WROTE, which is precisely how a write that said
        # nothing at all got a clean label.
        (True, "", None, True, "WROTE-UNCONFIRMED"),
        (True, "Posted it in Buildium.", None, True, "WROTE"),
        (False, "", None, False, "NO-GATE"),
        (False, "", "boom", False, "ERROR"),
        # The role floor refuses BEFORE any gate. That is correct behaviour on
        # a member JWT, and reporting it as NO-GATE would send the operator
        # hunting a routing bug that does not exist.
        (False, "This needs a Manager or Admin.", None, False, "REFUSED-BY-DESIGN"),
        (False, "Texting is not enabled.", None, True, "REFUSED-BY-DESIGN"),
    ],
)
def test_verdicts(gate: bool, text: str, error: str | None, execute: bool, expected: str) -> None:
    mod = _load()
    r = mod.TurnResult()
    r.gate_seen, r.final_text, r.error = gate, text, error
    assert mod.verdict_for(result=r, execute=execute) == expected


def test_an_error_outranks_a_refusal_shaped_reply() -> None:
    """The other direction of the refusal branch. A transport failure whose
    message happens to contain 'permission' is an ERROR -- laundering it into
    REFUSED-BY-DESIGN would report a broken run as correct behaviour, which is
    gotcha 35's asymmetry rule (never let an outage launder a finding)."""
    mod = _load()
    r = mod.TurnResult()
    r.gate_seen = False
    r.final_text = "manager"
    r.error = "ConnectError: permission denied"
    assert mod.verdict_for(result=r, execute=False) == "ERROR"


# ── the deadline answer arrives as `done`, not as tokens ──────────────


def test_a_deadline_answer_is_not_reported_as_silence() -> None:
    """When a run hits the soft deadline, `deadline_synthesis` writes the answer
    and it ships as `done_event(...)` -- it NEVER streams as tokens. Reading
    tokens alone reported two real 1,000+ character answers as an empty reply,
    which reads as a delivery loss and sent the operator hunting the wrong bug
    (`add_work_order_note`, 2026-08-29: final_len 1081, manifest said empty)."""
    mod = _load()
    r = mod.TurnResult()
    r.gate_seen = False
    r.done_summary = "The note wasn't added -- the lookups didn't finish."
    r.salvaged = True
    assert r.answer == r.done_summary, "an answer that did not stream is still an answer"
    assert r.final_text == "", "the token buffer must stay honest about what streamed"


def test_a_deadline_run_is_not_the_same_finding_as_a_missing_gate() -> None:
    """DEADLINE-NO-WRITE means the write surface is UNTESTED for that case, not
    broken. Collapsing it into NO-GATE is what made a 174s lookup failure read
    as a possible gate bypass."""
    mod = _load()
    r = mod.TurnResult()
    r.gate_seen = False
    r.done_summary = "Couldn't establish which work order you meant."
    r.salvaged = True
    assert mod.verdict_for(result=r, execute=False) == "DEADLINE-NO-WRITE"

    # Without the salvage flag it is still the serious finding.
    r.salvaged = False
    assert mod.verdict_for(result=r, execute=False) == "NO-GATE"


def test_an_unconfirmed_write_is_not_reported_as_written() -> None:
    """`WROTE` used to mean "a gate appeared and we approved it" -- never that
    the write landed. On 2026-08-30 all three reversible writes printed WROTE
    while the guard had reaped them `abandoned_unknown / ambiguous`, the agents
    said in plain words they could not confirm, and two of three undos failed.
    A manifest that overstates exactly where accuracy matters most is worse
    than no manifest."""
    mod = _load()
    for reply in (
        "I tried to post the charge but couldn't confirm it went through, so I'm "
        "flagging it for review.",
        "I attempted the update, but the Buildium Agent could not confirm the write landed.",
        "I couldn't safely process this right now.",
    ):
        r = mod.TurnResult()
        r.gate_seen = True
        r.final_text = reply
        assert mod.verdict_for(result=r, execute=True) == "WROTE-UNCONFIRMED", (
            f"reported as confirmed: {reply!r}"
        )


def test_the_phrasings_that_actually_slipped_through() -> None:
    """Verbatim from the 03:09 live run. The first cut listed the phrases that
    mean FAILURE and defaulted to WROTE, so anything one word outside the list
    read as a clean write -- "the write could not be confirmed as successful"
    missed "could not confirm" by a single word, three times, and the non-zero
    exit that exists so this cannot be skimmed past never fired."""
    mod = _load()
    for reply in (
        "Buildium didn't return a clear confirmation that the write actually landed",
        "hit a connection error, so I can't be certain the note was actually added",
        "the write could not be confirmed as successful",
        "Multiple attempts were blocked by a safety guard with no specific reason.",
    ):
        r = mod.TurnResult()
        r.gate_seen = True
        r.final_text = reply
        assert mod.verdict_for(result=r, execute=True) == "WROTE-UNCONFIRMED", (
            f"still reads as a confirmed write: {reply!r}"
        )


def test_a_clean_write_is_still_reported_as_written() -> None:
    """The other direction. If every write reads as unconfirmed the verdict
    carries no information and the operator stops reading it (gotcha 38's
    both-directions rule)."""
    mod = _load()
    r = mod.TurnResult()
    r.gate_seen = True
    r.final_text = "Posted a $1.00 charge to Doyle Prather at unit 1 in Buildium."
    assert mod.verdict_for(result=r, execute=True) == "WROTE"


def test_an_unconfirmed_write_is_never_undone_automatically() -> None:
    """Undoing a write we cannot prove landed is a SECOND unverified write on
    top of an unknown state. The undo must key on WROTE alone."""
    import ast

    src = Path(_load().__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    compared: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Name)
            and node.left.id == "v"
        ):
            compared += [
                c.value
                for c in node.comparators
                if isinstance(c, ast.Constant) and isinstance(c.value, str)
            ]
    assert compared, "no `v == ...` comparison found -- the scan is not reading the undo gate"
    assert "WROTE-UNCONFIRMED" not in compared, (
        "the undo path accepts an unconfirmed write -- that is a second "
        "unverified write onto an unknown state"
    )


def test_a_role_refusal_is_recognised_in_the_done_summary_too() -> None:
    """The role floor's message reaches the reply by whichever path the run
    used. Matching only streamed tokens would misfile a salvaged refusal as a
    gate failure -- the same one-reader bug, one layer over."""
    mod = _load()
    r = mod.TurnResult()
    r.gate_seen = False
    r.done_summary = "That needs a Manager or Admin."
    r.salvaged = True
    assert mod.verdict_for(result=r, execute=False) == "REFUSED-BY-DESIGN"


# ── the ledger read, which failed silently ────────────────────────────


def test_the_ledger_query_does_not_cast_company_id_to_uuid() -> None:
    """`helixis_action_idempotency.company_id` is **text**, live-verified.

    The first cut wrote `WHERE company_id = $1::uuid`. Postgres raised
    `operator does not exist: text = uuid`, the handler swallowed it, None meant
    "ledger unavailable", and every verdict fell back to blanket fail-closed
    doubt -- eight cases reported UNCONFIRMED including two that had genuinely
    SUCCEEDED. The fail-closed design was right; it fired on a bug in our own
    SQL rather than on real doubt.

    ⚠ Pins the OPERATOR, not just the column name (gotcha 59): a guard that
    checks which columns appear is blind to what is done with them.
    """
    tree = ast.parse(_HARNESS.read_text(encoding="utf-8"))
    # ⚠ SQL STATEMENTS ONLY. A whole-file scan fires on the comment that
    # documents this very bug, which legitimately quotes the broken form --
    # gotcha 40's trap, and gotcha 59's "scope an SQL guard to the statements".
    #
    # ⚠ Naming the TABLE was not a narrow enough filter, and that is the second
    # time this trap has landed here. `_BuildiumReadBack`'s docstring explains
    # what the ledger does and does not prove -- prose that mentions
    # `helixis_action_idempotency` and contains no SQL at all -- and the scan
    # read it as a query with a missing company filter. Requiring SELECT ...
    # FROM keeps the guard on statements. Prose describing a query is not one.
    sql = [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and "helixis_action_idempotency" in n.value
        and "select" in n.value.lower()
        and "from" in n.value.lower()
    ]
    assert sql, "the ledger query is gone -- re-point this guard"
    # Self-check: the filter above must still be finding the REAL statement, not
    # merely finding nothing and reporting green (gotcha 49).
    assert any("select status" in q.lower() for q in sql), (
        "the scan no longer matches the ledger's own SELECT -- it is measuring "
        "something else and would pass whatever the query said"
    )
    for q in sql:
        assert "company_id = $1::uuid" not in q, (
            "company_id is TEXT; casting the parameter to uuid makes the query raise, "
            "the read return None, and every verdict silently degrade to prose"
        )
        assert "company_id = $1" in q, "the ledger read is no longer company-scoped (gotcha 47)"


def test_a_broken_ledger_query_is_reported_not_swallowed() -> None:
    """A broken query and an unreachable database must not look the same.

    Both returned None, so a bug in our SQL was indistinguishable from a
    database we could not reach -- and the run said nothing either way. That is
    gotcha 45's shape: a control whose failure mode is silent degradation needs
    to say when it degraded.
    """
    import ast

    tree = ast.parse(_HARNESS.read_text(encoding="utf-8"))
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "guard_outcome"
    )
    handlers = [n for n in ast.walk(fn) if isinstance(n, ast.ExceptHandler)]
    assert handlers, "no except handler found -- the scan is not reading guard_outcome"
    for h in handlers:
        reports = any(
            isinstance(c, ast.Call) and getattr(c.func, "id", "") == "_ledger_unavailable"
            for c in ast.walk(h)
        )
        assert reports, (
            "an except handler in guard_outcome returns None without saying why -- "
            "a bug in our own SQL then reads exactly like an unreachable database"
        )
