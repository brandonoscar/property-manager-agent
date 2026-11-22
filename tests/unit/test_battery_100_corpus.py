"""The 100-question battery: the corpus and the harness must agree.

WHY THIS CORPUS EXISTS
----------------------
Batteries v1-v5 ran TWELVE questions. Twelve is enough to find a specific
defect and nowhere near enough to answer "does the product work" — the bar for
the 2026-09-01 release. So: **33 capabilities x 3 questions, plus the flagship
portfolio review as #100.**

Three, not five, is deliberate. Five per capability buys a K-of-N signal (the
browser sweep's >=4/5) but only twenty capabilities. Three buys thirty-three,
and the signal is recovered a different way: a set is ~7 minutes and ~$1, so a
set that scores 2/3 is re-run on the spot to separate flake from defect.

⚠ AND IT IS WHY THE CORPUS IS GROUPED AT ALL
---------------------------------------------
A Supabase access token lives ~1 hour. 100 questions at the measured ~150s
median is ~4.2 hours, so an unattended full pass CANNOT complete — it dies at
``_TokenExpiredError`` partway and reports a third of a run. Sets of three fit
inside one token with room to spare. The grouping is not presentation; it is
what makes the corpus runnable at all without giving ``run_battery.py`` its own
token-refresh path.

⚠ EVERY QUESTION NAMES A REAL BLUE RIDGE RECORD
------------------------------------------------
Gotcha 47's lesson, applied before the fact. The previous cross-source corpus
named Garden Row, Premier Air and Hank the Handyman — all real, all in Buildium
account 682052, and the chat runs on 695763. Four questions were therefore
unanswerable by construction, the agent said so correctly, and a day and a half
went into reading that as a product failure. So the anchors here (the
state-capital properties, the three vendors, the seeded task titles, the
delinquency ladder) were each verified present in Blue Ridge's mirror first.

Set 29 still names those absent records — but as an HONESTY set, where "no such
record" is the correct answer, not a capability set where it is a failure.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
from typing import Any

QUESTIONS = pathlib.Path("scripts/battery_questions_100.json")
SETS = pathlib.Path("scripts/battery_sets_100.json")
CANONICAL_12 = pathlib.Path("scripts/battery_questions.json")

#: Verbatim from run_battery. Held here as a literal on purpose: if the runner
#: renames its pre-flight question, this file should fail rather than follow.
PREFLIGHT = "What's my total unpaid vendor bill exposure right now?"


def _questions() -> list[str]:
    return json.loads(QUESTIONS.read_text(encoding="utf-8"))


def _meta() -> list[dict]:
    return json.loads(SETS.read_text(encoding="utf-8"))


def _score_battery(alias: str) -> Any:
    """Load ``score_battery.py`` by path — it is a script, not a package.

    Each caller passes its own ``alias`` because the module registers itself
    in ``sys.modules`` under that name; sharing one alias across tests would
    let a half-initialised import from a failed test leak into the next.
    """
    spec = importlib.util.spec_from_file_location(alias, "scripts/score_battery.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


def test_the_corpus_is_the_shape_it_claims() -> None:
    """33 sets of 3, plus the flagship. A set that quietly grew to 4 breaks the
    ~7-minute / one-token property the whole grouping exists for."""
    qs, meta = _questions(), _meta()
    assert len(qs) == 100, f"{len(qs)} questions, expected 100"
    assert len(meta) == 100, f"{len(meta)} sidecar entries, expected 100"

    sizes: dict[int, int] = {}
    for m in meta:
        sizes[int(m["set"])] = sizes.get(int(m["set"]), 0) + 1
    threes = {s: n for s, n in sizes.items() if n == 3}
    assert len(threes) == 33, f"expected 33 sets of three, got {len(threes)}"
    assert sizes[34] == 1, "the flagship portfolio review must be a set of one"


def test_the_sidecar_lines_up_with_the_questions() -> None:
    """⚠ BOTH sides (gotcha 28). The sidecar addresses questions by NUMBER; if
    the two files drift, ``--set 17`` silently runs somebody else's three."""
    qs, meta = _questions(), _meta()
    assert [m["n"] for m in meta] == list(range(1, len(qs) + 1)), (
        "sidecar numbering is not 1..N against the questions file — --set would "
        "select the wrong questions with no error"
    )
    for m in meta:
        assert m["scoring"] in {"computed", "judge", "assert"}, m
        assert m["set_name"], m


def test_the_preflight_question_is_present() -> None:
    """The runner gates a whole run on this question answering with a non-zero
    dollar figure. It no longer has to sit at index 1 — but it does have to
    exist, or every run of this file dies at startup."""
    qs = _questions()
    assert qs.count(PREFLIGHT) == 1, (
        f"{PREFLIGHT!r} must appear EXACTLY once — the runner takes the first "
        f"match, so a duplicate makes which one it means unknowable"
    )


def test_nothing_unattended_runs_by_default() -> None:
    """Ten questions must not execute on a normal run, for two different
    reasons — and neither is 'they are bad questions'.

    ``requires_attachment``: /agent/run carries text, so the document sets
    cannot be ASKED over this harness. Running them scores the product on a
    question it never received.

    ``sends_real_email``: the compose set sends REAL mail to REAL people on
    approval. Nobody should find that out from a battery report.
    """
    meta = _meta()
    skipped = [m for m in meta if m.get("requires_attachment") or m.get("sends_real_email")]
    assert len(skipped) == 10, f"expected 10 unattended-skip questions, got {len(skipped)}"
    # Both reasons must actually be represented — a corpus where one flag went
    # missing would still total 10 by accident and skip the wrong things.
    assert any(m.get("requires_attachment") for m in skipped)
    assert any(m.get("sends_real_email") for m in skipped)


def test_the_canonical_twelve_still_loads_unchanged() -> None:
    """⚠ The 100 is ADDITIVE. ``battery_questions.json`` is what v1-v5 ran and
    what every prior report indexes against; it has no sidecar, so the set-aware
    paths must no-op on it rather than fail."""
    twelve = json.loads(CANONICAL_12.read_text(encoding="utf-8"))
    assert len(twelve) == 12
    assert twelve[1] == PREFLIGHT, (
        "the canonical file's q02 is still the pre-flight question — the runner "
        "no longer REQUIRES position, but nothing here should have moved it"
    )
    assert not CANONICAL_12.with_name("battery_sets.json").exists(), (
        "a sidecar appeared next to the canonical 12 — it would start skipping "
        "questions in a corpus that has none of the flags"
    )


def test_every_scoring_kind_is_actually_used() -> None:
    """⚠ Assert the taxonomy is real (gotcha 33's shape). A corpus where every
    set drifted to 'judge' would still pass the shape test above while quietly
    abandoning the computed answer key — which is the half that makes the score
    mean 'the answer was TRUE' rather than 'an answer arrived'."""
    kinds = {m["scoring"] for m in _meta()}
    assert kinds == {"computed", "judge", "assert"}, f"only {kinds} in use"


def test_computed_means_there_is_actually_something_to_check() -> None:
    """⚠ BOTH sides (gotcha 28): the sidecar's LABEL against the answer key's
    contents.

    The first cut of this corpus labelled 33 questions ``computed`` while only
    17 carried a checkable fact. That gap is the dangerous kind — the report
    would say "33 questions have a right answer" and quietly judge 16 of them
    on the rubric alone, which is the difference between "the answer was TRUE"
    and "an answer arrived". Nothing would have failed.

    So the label now means exactly one thing, enforced in both directions: a
    ``computed`` question HAS facts, and a question with facts IS labelled
    computed. Widening the computed set is real work (bank balances, per-lease
    end dates and the bill totals are not cheaply derivable from the seed) —
    but it has to move the label and the key together.
    """
    sb = _score_battery("sb")

    gt = sb.build_ground_truth(sb._load_seed())
    key = sb.expectations_100(gt)
    qs, meta = _questions(), _meta()

    # ⚠ Scoped to the non-binary questions. An ``assert`` question may ALSO
    # carry a fact and still be binary: q88 ("is there a water-heater task
    # anywhere?") has a checkable needle AND is scored on whether it read a
    # truncated list as absence. Forcing it into one column would either drop a
    # free arithmetic check or misreport an honesty test as arithmetic.
    binary = {m["n"] for m in meta if m["scoring"] == "assert"}
    labelled = {m["n"] for m in meta if m["scoring"] == "computed"}
    with_facts = {
        m["n"]
        for m in meta
        if m["n"] not in binary and (key.get(qs[m["n"] - 1]) or sb.Expectation()).facts
    }
    assert labelled == with_facts, (
        f"label and key disagree — labelled-only {sorted(labelled - with_facts)}, "
        f"facts-only {sorted(with_facts - labelled)}. A 'computed' question with "
        f"no fact is scored by the judge while the report claims arithmetic."
    )
    assert len(labelled) >= 15, (
        f"only {len(labelled)} questions carry a checkable fact — too few for the "
        f"score to mean the product WORKS rather than that it replied."
    )


def test_every_question_has_an_answer() -> None:
    """⚠ ALL 100, no exceptions — and the entry must SAY something.

    An unkeyed question is not neutral. It still appears in the report, still
    counts toward the score, and is graded by the rubric alone — which asks
    whether an answer arrived, not whether it was true. So a corpus where a
    third of the questions carry no key produces a report that reads as
    "100 questions verified" while a third of it verified nothing. That is
    the same shape as gotcha 35's "a case that has never run is not a passing
    case": the suite counts it, so the gap is worse than an omission.

    An EMPTY ``Expectation()`` is caught too — it satisfies "the key has this
    question" while carrying neither a fact to check nor a trap to tell the
    judge what the answer turns on, which is an unkeyed question wearing the
    key's costume.
    """
    sb = _score_battery("sb_all")
    key = sb.expectations_100(sb.build_ground_truth(sb._load_seed()))
    qs = _questions()

    missing = [n for n, q in enumerate(qs, 1) if q not in key]
    assert not missing, (
        f"{len(missing)} questions have no answer-key entry: {missing}. They "
        f"would still be scored — by the rubric alone — and the report would not "
        f"say so."
    )
    hollow = [n for n, q in enumerate(qs, 1) if not (key[q].facts or key[q].trap)]
    assert not hollow, (
        f"questions {hollow} have an entry with neither facts nor a trap — "
        f"present in the key, absent in effect."
    )


def test_the_answer_key_addresses_real_questions() -> None:
    """Every key entry must match a question verbatim. The key is TEXT-keyed
    precisely so it cannot drift onto the wrong question — but a typo would
    make an entry silently unreachable instead, which is the same defect
    wearing a quieter costume."""
    sb = _score_battery("sb2")

    key = sb.expectations_100(sb.build_ground_truth(sb._load_seed()))
    orphans = [q for q in key if q not in set(_questions())]
    assert not orphans, f"answer-key entries matching no question: {orphans}"


def test_the_rendered_answer_sheet_carries_every_question() -> None:
    """``--dump-key`` is what a human actually reads, so a renderer that drops
    entries produces an answer sheet that LOOKS complete and is not — and
    nothing else in this file would notice, because the key itself would still
    be whole. Same both-sides rule as gotcha 28, applied to the human surface
    rather than to a producer/consumer pair in code."""
    sb = _score_battery("sb3")
    sheet = sb.render_answer_key(sb.build_ground_truth(sb._load_seed()))
    qs = _questions()

    absent = [n for n, q in enumerate(qs, 1) if q not in sheet]
    assert not absent, f"questions missing from the rendered answer sheet: {absent}"
    # And every set heading, so a set cannot vanish wholesale with its three
    # questions still individually present under someone else's heading.
    for m in _meta():
        assert f"## Set {m['set']} — {m['set_name']}" in sheet, m


# ── the v13 adversarial set ─────────────────────────────────────────────────
#
# Fifteen questions aimed at surfaces the 100 never touched: cross-tenant
# leakage with a REAL colliding surname, superlatives that must and must not
# hedge, absence-vs-unknown, former tenants, and the duplicate trap inverted.


def _v13_questions() -> list[str]:
    return json.loads(
        pathlib.Path("scripts/battery_questions_v13.json").read_text(encoding="utf-8")
    )


def test_every_v13_question_has_an_answer_key_entry() -> None:
    """⚠ Both sides (gotcha 28). A question with no entry is scored on nothing
    and reports zero factual errors — which reads as a pass. That is the
    failure mode this whole file exists to prevent, and the merge line in
    ``main()`` is one edit away from silently reintroducing it."""
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location("sb_v13", "scripts/score_battery.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["sb_v13"] = module
    spec.loader.exec_module(module)

    gt = module.build_ground_truth(module._load_seed())
    key = module.expectations_v13(gt)
    missing = [q for q in _v13_questions() if q not in key]
    assert not missing, f"v13 questions with no answer-key entry: {missing}"
    extra = [q for q in key if q not in _v13_questions()]
    assert not extra, f"answer-key entries matching no v13 question (typo?): {extra}"


def test_the_v13_set_is_mixed_not_uniformly_hedged() -> None:
    """⚠ A battery where every correct answer is "it depends" cannot tell a
    careful agent from an evasive one. The set deliberately contains
    superlatives that MUST resolve to one name and superlatives that MUST
    report a tie; if the hedging ones ever outnumber the resolving ones, the
    set has drifted into rewarding evasion."""
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location("sb_v13b", "scripts/score_battery.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["sb_v13b"] = module
    spec.loader.exec_module(module)

    key = module.expectations_v13(module.build_ground_truth(module._load_seed()))
    must_not_hedge = [q for q, e in key.items() if "Hedging" in e.trap or "Single answer" in e.trap]
    must_hedge = [q for q, e in key.items() if "tied" in e.trap.lower()]
    assert must_not_hedge, "no question pins a single unambiguous answer"
    assert must_hedge, "no question pins a tie"
    assert len(must_not_hedge) >= len(must_hedge), (
        f"{len(must_hedge)} hedge-required vs {len(must_not_hedge)} resolve-required — "
        f"a set weighted toward hedging rewards evasion"
    )
