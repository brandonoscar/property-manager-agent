"""Present-key-absent-value, made checkable.

Four times in one month a Buildium field arrived as a key with a null value
and the code read it as populated — TotalAmount, Status, bill ownership, and
IsPaid. Every one produced a plausible wrong answer rather than a crash, and
every one survived unit tests because the tests supplied the field the code
wanted. Only a live row settles it.

These pin the pure core: the counting, the verdicts, and the completeness rule
that forces the question when a schema grows a field.
"""

from __future__ import annotations

import pytest

from helixis.buildium.field_shapes import (
    EXPECTATIONS,
    USUALLY_FLOOR,
    Expect,
    analyse,
    expected_aliases,
    unclassified,
    verdict,
)
from helixis.buildium.schemas import Bill, Task

# ── counting: present and non-null are different questions ────────────────


def test_present_but_null_is_not_non_null() -> None:
    """THE distinction. A defaulted getattr protects against ABSENCE and not
    against a null — which is exactly why `getattr(b, "is_paid", True)` never
    fired its default and `None is False` threw away all 67 bills."""
    rows = [{"IsPaid": None}, {"IsPaid": None}, {"IsPaid": True}]
    stat = analyse(rows, ["IsPaid"])["IsPaid"]
    assert stat.total == 3
    assert stat.present == 3, "the key is there on every row"
    assert stat.non_null == 1, "the value is not"


def test_an_absent_key_counts_as_neither() -> None:
    stat = analyse([{"a": 1}, {}], ["b"])["b"]
    assert (stat.present, stat.non_null) == (0, 0)


def test_rows_may_be_dicts_or_models() -> None:
    """Live callers pass pydantic rows; tests pass dicts. Both must work or
    the thing that runs in production is not the thing under test."""
    model_row = Bill.model_validate({"Id": 1, "TotalAmount": None})
    assert analyse([model_row], ["Id"])["Id"].non_null == 1


# ── verdicts ──────────────────────────────────────────────────────────────


def test_an_always_field_that_is_ever_null_is_a_finding() -> None:
    stats = analyse([{"Id": 1}, {"Id": None}], ["Id"])
    found = verdict("bill", stats)
    assert len(found) == 1
    assert "null on 1/2" in str(found[0])


def test_a_usually_field_null_on_every_row_is_a_finding() -> None:
    """All-null is the signal. A field that is NEVER populated is effectively
    absent, whatever the schema promises — TotalAmount was declared, typed,
    and null on all 67 rows."""
    stats = analyse([{"VendorId": None}] * 20, ["VendorId"])
    found = verdict("bill", stats)
    assert found and "ALL 20 rows" in str(found[0])


def test_a_usually_field_populated_on_most_rows_is_fine() -> None:
    """⚠ RE-POINTED. This asserted that ANY partial population is fine — 1 of
    2 rows — which is the defect stated as a requirement: the judge tested
    ``non_null == 0``, so a scoping key could fall to 1/179 and stay green.

    The property it was reaching for is real (a sparse-but-healthy field must
    not be reported, or the sweep becomes noise). What it needed was a rate
    that is actually "most".
    """
    stats = analyse([{"VendorId": 7}] * 9 + [{"VendorId": None}], ["VendorId"])
    assert verdict("bill", stats) == []


def test_a_usually_field_that_has_collapsed_to_a_minority_is_a_finding() -> None:
    """The shape the old rule could not see, and the one that fails silently.

    ``_only_for_property`` fails OPEN when NO row carries the field (gotcha
    55), so 0% over-reports visibly. A field at 5% filters instead, matches
    the populated row, and reports every other row as ABSENT.
    """
    stats = analyse([{"VendorId": 7}] + [{"VendorId": None}] * 19, ["VendorId"])
    found = verdict("bill", stats)
    assert found, "a field populated on 1 of 20 rows is not 'usually' populated"
    assert "1/20" in str(found[0])
    assert "5%" in str(found[0]), "the rate is what makes the finding judgeable"


def test_the_all_null_finding_keeps_its_own_wording() -> None:
    """Two remedies, so two sentences. All-null means stop reading the field;
    a minority means it is still there and has stopped being dependable, and
    collapsing them would send the reader to the wrong fix.
    """
    stats = analyse([{"VendorId": None}] * 20, ["VendorId"])
    assert "treat as absent" in str(verdict("bill", stats)[0])


def test_a_rate_exactly_on_the_floor_is_not_a_finding() -> None:
    """A tie is not a majority either way, and a check that cannot tell which
    way it falls should decline rather than manufacture something to close
    (gotchas 52/55/57). Pinned in BOTH directions so the comparison cannot
    quietly become ``<=`` — that would fire on every evenly-split field.
    """
    half = analyse([{"VendorId": 7}, {"VendorId": None}], ["VendorId"])
    assert half["VendorId"].non_null_rate == USUALLY_FLOOR
    assert verdict("bill", half) == []

    below = analyse([{"VendorId": 7}] + [{"VendorId": None}] * 2, ["VendorId"])
    assert below["VendorId"].non_null_rate < USUALLY_FLOOR
    assert verdict("bill", below) != []


def test_the_floor_is_a_majority() -> None:
    """Pinned as a RELATIONSHIP, not a literal (gotcha 96). ``USUALLY`` is
    documented as "most rows"; anything at or below half stops meaning that,
    and a floor above half starts reporting fields the registry records as
    healthy (``Property`` is 171/179).
    """
    assert 0 < USUALLY_FLOOR <= 0.5


def test_the_deliberate_unit_id_finding_still_fires() -> None:
    """⚠ THE REGRESSION THIS FIX MOST HAD TO AVOID.

    ``task.UnitId`` is classified USUALLY with a MEASURED 0/90 on purpose —
    the registry says so at length — so that the probe keeps asking whether
    the code scopes on a field production never populates. A widening that
    silenced a deliberate persistent finding would have removed the question
    while looking like an improvement.
    """
    stats = analyse([{"UnitId": None}] * 90, ["UnitId"])
    found = verdict("task", stats)
    assert found and "ALL 90 rows" in str(found[0])


def test_the_judge_reads_the_rate_rather_than_re_deriving_it() -> None:
    """``non_null_rate`` existed, computed the deciding number, and was read
    by NOTHING — which is how the enum's stated meaning and the implemented
    test came apart in the first place.

    A second ``non_null / total`` inline in the judge would be two homes for
    one rule (gotcha 25), and the one that drifts is the one nobody is
    looking at. Asserted the way gotcha 147 asserts ``exc.retryable``: the
    consumer reads the property, and keeps no arithmetic of its own.
    """
    import ast
    import inspect

    from helixis.buildium import field_shapes

    fn = next(
        node
        for node in ast.walk(ast.parse(inspect.getsource(field_shapes)))
        if isinstance(node, ast.FunctionDef) and node.name == "verdict"
    )
    reads = [n for n in ast.walk(fn) if isinstance(n, ast.Attribute) and n.attr == "non_null_rate"]
    assert reads, "verdict must consult the property, not a private copy of it"

    divisions = [n for n in ast.walk(fn) if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Div)]
    assert not divisions, (
        "verdict re-derives a rate inline — that is a second home for the rule "
        "`non_null_rate` already owns"
    )


def test_a_sometimes_field_is_never_a_finding() -> None:
    """PaidDate is absent by definition on an unpaid bill. Reporting it would
    train people to ignore the output, and an ignored check protects nothing."""
    stats = analyse([{"PaidDate": None}] * 50, ["PaidDate"])
    assert verdict("bill", stats) == []


def test_a_distrusted_field_that_starts_working_is_reported() -> None:
    """The inverse finding, and the easy one to forget. If TotalAmount begins
    arriving populated, that is good news AND it means the workaround built
    around its absence deserves review. Silence would let the codebase keep
    compensating for a problem that no longer exists."""
    stats = analyse([{"TotalAmount": 12.0}] * 5, ["TotalAmount"])
    found = verdict("bill", stats)
    assert found and "now populated" in str(found[0])


def test_a_distrusted_field_still_null_is_silent() -> None:
    """The steady state. We already know IsPaid is null; saying so daily is
    noise."""
    stats = analyse([{"IsPaid": None}] * 67, ["IsPaid"])
    assert verdict("bill", stats) == []


def test_the_four_real_regressions_are_recorded() -> None:
    """Each cost a production bug. The registry is where that knowledge lives
    now, so it survives the next person who has not read the gotchas."""
    assert EXPECTATIONS["bill"]["TotalAmount"][0] is Expect.NEVER_TRUST
    assert EXPECTATIONS["bill"]["IsPaid"][0] is Expect.NEVER_TRUST
    assert EXPECTATIONS["task"]["Status"][0] is Expect.NEVER_TRUST
    assert EXPECTATIONS["task"]["TaskStatus"][0] is Expect.ALWAYS
    assert "Lines" in EXPECTATIONS["bill"], "ownership and amount both live here"


# ── completeness: the part that catches the NEXT one ──────────────────────


@pytest.mark.parametrize("entity,model", [("bill", Bill), ("task", Task)])
def test_every_declared_field_is_classified(entity: str, model: type) -> None:
    """Adding a field to a schema forces the question "does production
    actually populate this?" — instead of leaving it to be discovered by a $0
    report. Same shape as test_buildium_purge's table classification."""
    missing = unclassified(entity, model)
    assert not missing, (
        f"{entity} schema declares aliases with no expectation: {sorted(missing)}. "
        "Classify each: ALWAYS / USUALLY / SOMETIMES / NEVER_TRUST, with a reason."
    )


def test_aliases_are_read_from_the_schema_not_retyped() -> None:
    """The alias set must come from the model, or the registry drifts from the
    code it describes and checks a shape nothing reads."""
    assert "TotalAmount" in expected_aliases(Bill)
    assert "TaskStatus" in expected_aliases(Task)


# ── guard self-test ───────────────────────────────────────────────────────


def test_the_verdict_can_return_findings_and_silence() -> None:
    """Guard self-test, per the convention. Five guards in two days passed
    against code they were written to catch, so a checker nobody has watched
    discriminate is not evidence."""
    assert verdict("bill", analyse([{"Id": None}], ["Id"])), "must flag a real violation"
    assert verdict("bill", analyse([{"Id": 1}], ["Id"])) == [], "must stay silent when clean"


# ── the module has a caller (finding #41) ─────────────────────────────────


def test_field_shapes_is_called_from_outside_its_own_test() -> None:
    """⚠ For its whole life this module was imported by exactly ONE file — this
    one. A static table of expectations that is never compared against a
    production row cannot detect the NEXT null-valued field, which is gotcha
    53's "check population, not presence" pointed back at the module written to
    enforce it. `scripts/field_shape_probe.py` is the caller.

    An import scan, not a behavioural test: the defect was never that the code
    was wrong, it was that nothing ran it — and `comms_triage` proved a
    behavioural test cannot see "wired to nothing" (#944).
    """
    import ast
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    callers = []
    for path in list((repo / "src").rglob("*.py")) + list((repo / "scripts").rglob("*.py")):
        if path.name == "field_shapes.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("field_shapes"):
                callers.append(str(path.relative_to(repo)))
            elif isinstance(node, ast.Import) and any(
                a.name.endswith("field_shapes") for a in node.names
            ):
                callers.append(str(path.relative_to(repo)))

    assert callers, (
        "nothing under src/ or scripts/ imports field_shapes — it is an orphan again. "
        "The registry only earns its keep when something compares it against live rows; "
        "see scripts/field_shape_probe.py."
    )
