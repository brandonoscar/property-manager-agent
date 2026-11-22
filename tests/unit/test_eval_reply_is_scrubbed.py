"""The eval harnesses must read the reply the USER gets, not the raw model text.

``run_agent_streaming`` scrubs the closing message before it is streamed or
stored (``final_output = scrub_reply_text(str(final_output))``). Both eval
harnesses assigned ``result.final_output`` straight from the SDK, so every
reply assertion measured bytes no client ever receives.

That is not cosmetic, because one reply key gates. A bare record id is a
DISCLOSURE needle (``_is_disclosure_needle``), which makes
``reply_must_not_contain`` unconditionally SAFETY — never re-rolled, gates
every roll, even on a quarantined case. So ``140_card_payload_is_scrubbed_and_
grounded`` red-gated CI whenever the model happened to write the lease number
that the live path removes, and passed whenever it happened not to. Same case,
same code, opposite verdicts — which is exactly what a leak looks like and
exactly what this was not.

⚠ The card drain in ``agent_flow`` already scrubbed, with a comment saying
why — one line BELOW the raw ``final_output`` assignment. Half a rule applied
is how this survived review.

Two sides, per gotcha 28:

1. **The call site.** Each harness assigns ``result.final_output`` from a
   ``scrub_reply_text(...)`` call. A behavioural test cannot see a harness that
   quietly goes back to reading raw text, so this is an AST check.
2. **The corpus.** Scrubbing can destroy an assertion as easily as it removes a
   leak — the failure the card comment warns about, pointed at prose: a
   ``reply_must_contain`` whose needle the scrubber deletes would fail forever
   on a correct reply. So every needle the corpus actually declares is run
   through the scrubber (gotcha 45: read the corpus, not the function).
"""

from __future__ import annotations

import ast
import json
import pathlib
import re

import pytest

from helixis.agents.reply_hygiene import scrub_reply_text
from helixis.evals.runner import GOLDEN_DIR

_SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "helixis" / "evals"
_HARNESSES = ("agent_flow.py", "orchestrator_routing.py")


def _final_output_assignments(module: str) -> list[ast.expr]:
    """Every value assigned to ``result.final_output`` in ``module``."""
    tree = ast.parse((_SRC / module).read_text(encoding="utf-8"))
    values: list[ast.expr] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if (
                isinstance(target, ast.Attribute)
                and target.attr == "final_output"
                and isinstance(target.value, ast.Name)
                and target.value.id == "result"
            ):
                values.append(node.value)
    return values


@pytest.mark.parametrize("module", _HARNESSES)
def test_the_harness_reads_the_scrubbed_reply(module: str) -> None:
    """Both harnesses must scrub, not just the one whose case was gating.

    Fixing only ``agent_flow`` would leave ``orchestrator_routing`` reading raw
    text with the same ``reply_must_not_contain`` key available to it —
    gotcha 25's rule (fix a field-shape bug at every layer that reads it).
    """
    values = _final_output_assignments(module)
    assert values, f"{module} no longer assigns result.final_output — re-point this guard"
    for value in values:
        assert (
            isinstance(value, ast.Call) and getattr(value.func, "id", "") == "scrub_reply_text"
        ), (
            f"{module}:{value.lineno} assigns result.final_output from raw model text. "
            "Every reply assertion then measures bytes the user never sees, and "
            "reply_must_not_contain is SAFETY-classed, so a scrubbed-away identifier "
            "red-gates CI on a coin flip. Wrap it in scrub_reply_text()."
        )


def _corpus_reply_expectations() -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    needles: list[tuple[str, str]] = []
    patterns: list[tuple[str, str]] = []
    for path in sorted(pathlib.Path(GOLDEN_DIR).glob("*.json")):
        expected = json.loads(path.read_text(encoding="utf-8")).get("expected") or {}
        for needle in expected.get("reply_must_contain") or []:
            needles.append((path.name, needle))
        raw = expected.get("reply_must_match_regex")
        for pattern in [raw] if isinstance(raw, str) else (raw or []):
            patterns.append((path.name, pattern))
    return needles, patterns


def test_the_scrubber_destroys_no_reply_assertion_in_the_corpus() -> None:
    """The other direction: scrubbing must not delete what a case asserts.

    A ``reply_must_contain`` needle the scrubber removes would fail on a
    perfectly correct reply, forever, for a reason nobody would look for. The
    risky shapes are the numeric ones — dates, money, times, unit labels — and
    those are exactly the ones the identifier rule has to spare (gotcha 38).
    """
    needles, patterns = _corpus_reply_expectations()
    # Measured 2026-09-05: 39 needles, 84 regexes. The floors sit well below
    # that so trimming a case does not red-gate, while a broken glob or a wrong
    # CWD — which collects zero and reports green — still fails (gotcha 49).
    assert len(needles) > 25, f"only {len(needles)} needles found — the corpus scan is broken"

    destroyed = [
        (case, needle)
        for case, needle in needles
        if needle not in scrub_reply_text(f"The answer: {needle} — end.")
    ]
    assert not destroyed, (
        "the reply scrubber deletes these reply_must_contain needles, so their "
        f"cases can never pass once the harness reads the scrubbed reply: {destroyed}"
    )
    assert len(patterns) > 60, f"only {len(patterns)} regexes found — the corpus scan is broken"


@pytest.mark.parametrize(
    ("reply", "pattern"),
    [
        # Every numeric shape the corpus asserts on. Dates (126), money
        # (122/054), percentages (123), clock times (043/058) and unit labels
        # (329) all sit in the same digit space as a Buildium record id, and
        # the scrubber has to keep them.
        (
            "The lease ends 2027-03-18 and started in 2026.",
            r"\b20\d{2}-\d{2}-\d{2}\b|\b(19|20)\d{2}\b",
        ),
        ("The open balance is $2,400 as of today.", r"2,?400"),
        ("The operating account balance is $48,215.44.", r"(?i)(operating|balance|48,215|\$)"),
        ("That is 120 units, about 5% of the portfolio.", r"(120|5\s?%|five percent)"),
        ("Walkthrough at 10:00, plumbing at 1:00, owner review 3:00.", r"(?i)(10:00|1:00|3:00)"),
        ("There is no unit 88Z at that property.", r"(?i)(no|not) (unit|88z|match|find|exist)"),
    ],
)
def test_the_scrubber_spares_the_numbers_a_reply_is_allowed_to_carry(
    reply: str, pattern: str
) -> None:
    assert re.search(pattern, scrub_reply_text(reply)), (
        f"scrub_reply_text({reply!r}) destroyed a legitimate figure: {scrub_reply_text(reply)!r}"
    )


def test_the_scrubber_removes_the_identifier_case_140_forbids() -> None:
    """The leak the gate exists for — case 140's own mocked row.

    Both halves matter: the record id goes, and the money and the person's
    name stay. A scrubber that took the amount with it would trade a leak for
    a wrong answer.
    """
    scrubbed = scrub_reply_text("Desmond Achebe (Lease #99797) owes $412.75.")
    assert "99797" not in scrubbed
    assert "Desmond Achebe" in scrubbed
    assert "412.75" in scrubbed
