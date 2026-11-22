"""A gate promoted on a retired model is a gate with no justification.

WHAT HAPPENED
-------------
Three geo cases — 099_property_proximity_flow, 100_property_location_routes_to
_buildium, 101_find_vendor_near_property_flow — were promoted OUT of quarantine
on 2026-07-11, each note citing the same evidence: *"a §4 20x grid against live
Gemini scored 20/20"*. ``single_run_gate: true`` means "this case is stable
enough on the CI tier to red-gate an unrelated pull request", and that grid was
the whole basis for the claim.

**The Google account died on 2026-07-21** (gotcha 18) — ten days later. From
that morning the justification was void: none of the three had been validated
on Anthropic, and pull requests run the **Haiku PR-gate tier**, which gotcha 10
separately records as drifting from the production model. Nothing failed and
nothing was logged; the notes went on asserting proof that no longer existed.

It surfaced on 2026-08-27 when 101 red-gated PR #923 on a quality failure it
could not clear in three re-rolls — a PR whose diff cannot reach tool selection
at all.

⚠ WHY THIS IS A CORPUS TEST AND NOT A CODE TEST
------------------------------------------------
Gotcha 45's rule, one level up. ``_case_needs_gemini`` kept "working" for five
weeks against a key no golden file declared any more, and a test feeding it
hand-written inputs would have passed throughout — the function was
self-consistent and pointed at nothing. The same shape applies to a
JUSTIFICATION: it is prose in a JSON file, it cannot fail, and the only way to
notice it has expired is to read the corpus and compare against what the
project actually runs.

⚠ CORRECTED 2026-09-05 — AND THE CORRECTION IS THE POINT
---------------------------------------------------------
This section used to read: *"Eight of the eleven ``single_run_gate: true``
cases (006, 053-059) carry NO note whatsoever, so an unexplained gate is
currently the NORM."*

**That was false.** 053 and 055-059 all carry a justification — in
``description``. The claim was measured with ``_notes``, which read only
underscore-prefixed keys, so the author saw an empty string for each of them
and concluded the notes did not exist. The guard's blind spot produced a
finding ABOUT the corpus that was really a finding about the guard, and it was
written down as fact.

Six of the ten gating cases cite Gemini. Every one was invisible here until
053 red-gated PR #986 on 2026-09-05 — a pull request that touches no routing
code at all, exactly the failure mode of the three geo cases above, five weeks
later, past a guard built to prevent it.

The lesson is gotcha 45's, turned on this file: a control whose failure mode is
"silently permits more" must be checked against what it ACTUALLY permitted.
``_notes`` now reads every string field, and
``test_the_guard_reads_description_not_just_underscore_keys`` pins that,
because a test feeding hand-written inputs would naturally use the underscore
key the docstring described and would pass forever.

Still NOT asserted: that every gate carries a justification. 006 genuinely has
none. Requiring one is a repo-wide convention this file would be inventing and
back-filling — worth doing deliberately, not smuggled in.

⚠ AND THIS DOES NOT ASSERT THE GATE IS OFF
-------------------------------------------
Two of the three still gate, because both PASSED the run that caught this —
there is no evidence they are unstable, only that their stated proof expired.
The test asserts the NOTE is honest, not that the gate is absent. Turning a
stale justification into an automatic de-gate would trade one unexamined state
for another.
"""

from __future__ import annotations

import json
import pathlib

GOLDEN = pathlib.Path("src/helixis/evals/golden")

#: Providers this project no longer runs on. A promotion note citing one of
#: these is quoting a measurement that cannot be reproduced.
#:
#: ⚠ Add to this list the same day a provider is retired — that is the whole
#: mechanism. gotcha 18 retired Google on 2026-07-21 and the three notes below
#: went stale that morning with nothing to catch them for five weeks.
RETIRED_PROVIDERS = ("gemini", "google", "openai", "gpt-4")

#: The phrase every corrected note carries. Not a magic string for its own
#: sake: it is what lets a reader grep the corpus for "gates whose evidence
#: expired" and get an answer.
VOID_MARKER = "THAT EVIDENCE IS VOID"


def _cases() -> list[tuple[pathlib.Path, dict]]:
    out = []
    for f in sorted(GOLDEN.glob("*.json")):
        try:
            out.append((f, json.loads(f.read_text(encoding="utf-8"))))
        except json.JSONDecodeError as exc:  # a malformed case is its own bug
            raise AssertionError(f"{f.name} is not valid JSON: {exc}") from exc
    return out


def _notes(case: dict) -> str:
    """Every prose field on the case, joined.

    ⚠ This read the underscore-prefixed keys ONLY, and its own docstring said
    it did so "rather than keying on one and missing a differently-named
    justification" — while missing the plainest field name there is.
    **Six of the ten gating cases put their justification in ``description``**
    (053, 055-059), so the guard scanned an empty string for each of them and
    passed. It has been green since it was written, over a corpus where the
    majority of gates rest on evidence measured against a provider retired
    2026-07-21.

    That is gotcha 45's rule turned on the guard itself: a control whose
    failure mode is "silently permits more" has to be checked against what it
    actually permitted, and this one was only ever checked against
    hand-written inputs that happened to use an underscore key.

    So: read EVERY string value. A justification is prose wherever the author
    put it, and the set of field names an author might choose is not knowable
    in advance — which is the argument the old docstring was making and then
    did not follow.
    """
    return " ".join(v for v in case.values() if isinstance(v, str))


def test_no_gate_rests_on_a_retired_provider_without_saying_so() -> None:
    """⚠ THE guard. A case may cite a retired provider — the history is worth
    keeping — but it must ALSO carry the marker saying that evidence no longer
    holds, so the next reader cannot mistake it for a live justification.

    Scoped to ``single_run_gate: true`` because that is the flag that lets a
    model-driven case red-gate an unrelated PR. A quarantined case's note
    claiming stale proof costs nobody a merge.
    """
    offenders = []
    for path, case in _cases():
        if case.get("single_run_gate") is not True:
            continue
        note = _notes(case).lower()
        cited = [p for p in RETIRED_PROVIDERS if p in note]
        if cited and VOID_MARKER not in _notes(case):
            offenders.append((path.name, cited))

    assert not offenders, (
        "these cases gate a pull request on evidence measured against a provider "
        f"this project no longer runs: {offenders}. Either re-validate on the "
        f"CURRENT PR-gate tier and rewrite the note, or mark the old evidence "
        f"void with '{VOID_MARKER}'. A note asserting proof that cannot be "
        f"reproduced is worse than no note — it stops the next reader looking."
    )


def test_the_marker_is_actually_in_use() -> None:
    """⚠ Assert the probe RAN (gotcha 33's shape). If every note were rewritten
    to drop the provider names entirely, the test above would pass vacuously
    while the corpus still carried gates nobody had re-validated. At least one
    case must carry the marker, or this file is measuring nothing."""
    marked = [p.name for p, c in _cases() if VOID_MARKER in _notes(c)]
    assert marked, (
        f"no case carries '{VOID_MARKER}' — either the corpus was cleaned up "
        f"(in which case delete this file and say why) or the marker drifted"
    )


def test_case_ids_are_unique() -> None:
    """⚠ Found while diagnosing the above: TWO files are numbered 099
    (``099_buildium_auth_failed_honesty`` and ``099_property_proximity_flow``).
    Their IDS differ, so the runner is fine — but a human reading "099 gated
    the build" cannot tell which, and that ambiguity cost a diagnostic step.
    IDs must stay unique; the duplicated NUMBER is recorded here rather than
    renamed, because renaming a case breaks every historical report that
    indexes it."""
    ids = [c["id"] for _p, c in _cases()]
    dupes = {i for i in ids if ids.count(i) > 1}
    assert not dupes, f"duplicate case ids — the runner cannot address these separately: {dupes}"


def test_the_guard_reads_description_not_just_underscore_keys() -> None:
    """⚠ The blindness that let six gates through.

    ``_notes`` originally joined only keys starting with ``_``. Six of the ten
    gating cases (053, 055-059) carry their justification in ``description``,
    so the guard scanned an empty string for each and passed — green since it
    was written, over a corpus where most gates cited a provider retired
    2026-07-21.

    A test feeding hand-written inputs to ``_notes`` would not have caught it:
    such a test naturally uses the underscore key the docstring describes, and
    the function was self-consistent on those. What has to be asserted is the
    FIELD COVERAGE — gotcha 45's rule pointed at the guard rather than at the
    corpus.
    """
    case = {"description": "20/20 on the live-Gemini 20x grid", "single_run_gate": True}
    assert "gemini" in _notes(case).lower(), (
        "_notes does not read `description` — the field six of ten gating "
        "cases actually use. Restoring an underscore-only scan re-opens the "
        "hole this file exists to close."
    )


def test_gotcha_48s_recorded_count_is_reproducible() -> None:
    """CLAUDE.md gotcha 48 says "Eight of the eleven gating cases (006,
    053-059) carry NO justification at all". That was measured with the same
    underscore-only lens and is FALSE: 053 and 055-059 all carry one, in
    ``description``. Pinning the real numbers so the correction cannot drift
    back into folklore.

    ⚠ Not asserting exact totals — the corpus grows. Asserting the property:
    a gating case that cites a retired provider ANYWHERE must be visible to
    the guard.
    """
    gating = [c for _, c in _cases() if c.get("single_run_gate") is True]
    assert gating, "no gating cases found — the scan is pointed at nothing"
    citing = [c for c in gating if any(p in _notes(c).lower() for p in RETIRED_PROVIDERS)]
    assert citing, (
        "no gating case cites a retired provider, which contradicts the "
        "measured corpus — _notes has probably gone blind again"
    )
