"""Rentvine wraps most rows, and reading the wrapper as the record is silent.

THE DEFECT THIS FILE EXISTS FOR
    ``client._rows_from`` accepted a bare array and a single-list-key object.
    The shape the spec actually documents is a third one: an array whose
    ELEMENTS are each a wrapper around the record. Such a row passes the
    ``isinstance(r, dict)`` filter intact, reaches the mapper, yields no id,
    and is skipped — for every row, of five of the seven collections the sync
    reads. Measured against the Phase 4 sync: ``rows_by_table`` all zero,
    ``failed={}``, ``result.ok=True``, ``confirmed_complete`` naming all five,
    and zero INSERTs. A complete, successful sync of an empty portfolio.

    Not a live outage — ``rentvine.enabled`` defaults false and
    ``sync_company`` has no caller yet — so this is the last moment it is
    cheap to fix.

WHY THE LITERALS BELOW ARE TYPED OUT BY HAND
    ⚠ The obvious test regenerates the map and compares. That checks the
    committed file matches the GENERATOR, which is worth having and is the
    drift test at the bottom — but it cannot see a generator that is wrong,
    because both sides come from the same rule. Two rules were tried before
    this one and both produced a plausible, wrong map:

      * "$ref means record" — ``/vendors`` INLINES its contact schema, so it
        fell out of the map entirely, and an absent entry reads exactly like
        "bare". The silent direction, on a collection the sync reads.
      * the same rule read the eleven ``/accounting/diagnostics/*`` rows as
        wrapped, because they ``$ref`` an INTEGER ENUM (``ledger_types``).

    So the seven collections the sync depends on are pinned as literals, and
    the guards below reproduce each wrong rule and require it to fail. A test
    that only regenerates would have passed against both (gotcha 45: a control
    whose failure mode is "silently permits more" has to be measured against
    something it did not produce).

AND A THIRD RULE FAILED — the one that says the spec is right
    ⚠ Five of those seven literals are read off the spec by eye. TWO are not,
    and could not be: ``/owners`` and ``/properties/{propertyID}/units``
    declare a bare ``$ref`` and RETURN a wrapper. A generator cannot see that,
    and neither can a transcription — only a live call can. Those two now say
    what the first Phase 8 sync (2026-09-23) actually received, and the
    override layer that carries them is the generator's ``LIVE_OVERRIDES``.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from helixis.rentvine import client as rvclient
from helixis.rentvine import transport
from helixis.rentvine._row_envelopes import (
    LIVE_OVERRIDDEN,
    MEASURED_SUBJECT,
    ROW_COMPANIONS,
    ROW_ENVELOPE,
    SPEC_SHA256,
)

REPO = Path(__file__).resolve().parents[2]
SPEC_PATH = REPO / "docs" / "rentvine" / "vendor" / "docs.rentvine.com" / "openapi.json"


def _generator():
    path = REPO / "scripts" / "rentvine_row_envelopes.py"
    spec = importlib.util.spec_from_file_location("_rv_envelopes", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_rv_envelopes"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def gen():
    return _generator()


@pytest.fixture(scope="module")
def spec():
    return json.loads(SPEC_PATH.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# the seven the sync reads, read off the spec by hand
# --------------------------------------------------------------------------

#: ``None`` = the array's items ARE the record. Anything else = the key the
#: record sits under. Transcribed from the 200 response schema of each path,
#: EXCEPT the two marked LIVE below.
#:
#: ⚠ **TWO OF THESE WERE RE-POINTED ON 2026-09-23 AND THE OLD VALUES WERE NOT
#: WRONG TRANSCRIPTIONS.** ``/owners`` and ``/properties/{propertyID}/units``
#: both declare ``items`` as a bare ``$ref`` to the record, so ``None`` was a
#: faithful reading of the capture — and the first Phase 8 sync against a real
#: sandbox received ``{"contact": …}`` and ``{"unit": …, "token": …}``. The
#: spec is what disagrees with the server, and these literals now say what the
#: server said. Owners 5 → 0 and units 37 → 0 in the mirror is what the old
#: reading cost, with ``failed={}`` and the sync reporting success.
#:
#: An assertion holds a defect in place exactly as firmly as it catches one:
#: while these two read ``None``, this file was the reason a correction looked
#: like a regression.
MEASURED = {
    "GET /owners": "contact",  # LIVE 2026-09-23; spec says $ref -> contact
    "GET /properties/{propertyID}/units": "unit",  # LIVE 2026-09-23; spec says $ref
    "GET /tenants": "contact",
    "GET /vendors": "contact",  # schema INLINED, no $ref anywhere
    "GET /properties": "property",  # beside a `token` string
    "GET /leases": "lease",  # beside `unit`
    "GET /maintenance/work-orders": "workOrder",  # beside a nullable `contact`
}


@pytest.mark.parametrize("operation,key", sorted(MEASURED.items(), key=lambda kv: kv[0]))
def test_the_collections_the_sync_reads_have_the_measured_envelope(operation, key):
    assert operation in ROW_ENVELOPE, f"{operation} fell out of the map entirely"
    assert ROW_ENVELOPE[operation] == key


def test_bare_collections_exist_and_none_of_the_seven_the_sync_reads_is_one():
    """Both directions, RE-POINTED 2026-09-23.

    ⚠ This test used to name ``/owners`` and ``/units`` as its bare examples,
    which is what the live run falsified — so the "other direction" now comes
    from operations that are genuinely bare and always were. Without it, a map
    that unwrapped EVERYTHING would pass every assertion above: the seven the
    sync reads are now all wrapped, so they can no longer supply the negative.

    The diagnostics rows are flat by construction — ``ledger_types`` is an
    integer enum, which is the counterexample the generator's second rejected
    rule was built on (see the module docstring there).
    """
    bare = {op for op, k in ROW_ENVELOPE.items() if k is None}
    assert "GET /accounting/diagnostics/escrow-mismatch" in bare
    assert "GET /chat/messages" in bare
    assert bare.isdisjoint(set(MEASURED)), (
        "every collection the sync reads carries a wrapper; a bare one here "
        "means a row is being handed to the mapper unwrapped"
    )


def test_every_collection_the_sync_reads_is_in_the_map():
    """The sync's own table, so a new collection cannot be added without one.

    ⚠ ``/vendors`` is the one that motivates this: the first rule dropped it
    silently, and nothing anywhere would have said so.
    """
    from helixis.rentvine import sync

    paths = [path for _table, path in sync.SOURCES]
    # Leases come from the export since 2026-10-01; `getLease` is a single
    # record, not a collection, so it is deliberately absent from the map.
    paths += [sync.LEASES_EXPORT_PATH, sync.UNITS_PATH]
    assert len(paths) >= 7, "the sync reads fewer collections than expected"
    for path in paths:
        key = transport.operation_key("GET", path)
        assert key in ROW_ENVELOPE, f"{key} is not in the row-envelope map"


# --------------------------------------------------------------------------
# the two rules that produced a wrong map — reproduced, and required to fail
# --------------------------------------------------------------------------


def test_a_ref_keyed_rule_would_have_dropped_vendors(spec):
    """``/vendors`` inlines its schema. Measured, not argued."""
    items = spec["paths"]["/vendors"]["get"]["responses"]["200"]["content"]
    items = next(iter(items.values()))["schema"]["items"]
    contact = items["properties"]["contact"]
    assert "$ref" not in contact, "if this gains a $ref the regression is no longer live"
    assert contact.get("properties"), "the schema is inlined here"
    # And the real map still finds it.
    assert ROW_ENVELOPE["GET /vendors"] == "contact"


def test_an_integer_enum_is_not_a_record(gen, spec):
    """``ledger_types`` is a ``$ref`` to ``{"type": "integer", "enum": [...]}``.

    Reading a ``$ref`` as proof of recordhood turned eleven flat diagnostic
    rows into wrapped ones and would have unwrapped each to a single integer.
    """
    schemas = spec["components"]["schemas"]
    assert schemas["ledger_types"]["type"] == "integer"
    assert gen._resolves_to_record({"$ref": "#/components/schemas/ledger_types"}, schemas) is False
    assert ROW_ENVELOPE["GET /accounting/diagnostics/escrow-mismatch"] is None


def test_composition_is_followed_or_two_collections_move_to_the_wrong_bucket(gen, spec):
    """``property`` and ``work_order`` are ``allOf`` compositions.

    Stopping at the wrapper reads them as zero-field, which made ``/properties``
    measure as BARE and ``/maintenance/work-orders`` measure as ``contact`` —
    the wrong answer rather than no answer (gotcha 63's shape).
    """
    schemas = spec["components"]["schemas"]
    for name in ("property", "work_order"):
        assert "allOf" in schemas[name], f"{name} is no longer a composition"
        assert not schemas[name].get("properties"), "reads as zero fields without allOf"
        assert gen._resolves_to_record({"$ref": f"#/components/schemas/{name}"}, schemas) is True


def _declared_order(spec, operation):
    path = operation.removeprefix("GET ")
    content = spec["paths"][path]["get"]["responses"]["200"]["content"]
    return list(next(iter(content.values()))["schema"]["items"]["properties"])


def test_where_several_records_share_a_row_the_first_declared_one_wins(gen, spec):
    """The one inference, and the property it rests on, stated as a test.

    ⚠ JSON Schema does not make property order meaningful, so this is T4. What
    holds it up is that it agrees with the record on every ambiguous operation
    — checked here across all of them, not asserted for the two I looked at.

    ⚠ RE-POINTED 2026-09-23, never relaxed. This asserted the rule over ALL 28
    ambiguous operations and probe K measured the 28th disagreeing, so it was
    holding the product to a reading the sandbox had refuted (gotchas
    89/93/95/150). The rule still governs everything ``SUBJECT_OF`` does not
    name, and the operations it DOES name are asserted the other way below —
    the exemption is not a hole, it is the other half of the claim.
    """
    envelope, companions = gen.build_index(spec)
    assert companions, "no ambiguous operations found — check the walk"
    inferred = [op for op in companions if op not in gen.SUBJECT_OF]
    assert len(inferred) == len(companions) - len(gen.SUBJECT_OF)
    for operation in inferred:
        rest = companions[operation]
        chosen = envelope[operation]
        assert chosen not in rest
        props = _declared_order(spec, operation)
        assert props.index(chosen) < min(props.index(c) for c in rest)


def test_every_measured_subject_still_contradicts_the_tie_break(gen, spec):
    """A subject that AGREES with first-declared is a licence nobody needs.

    ⚠ Gotcha 63's staleness direction, and the reason this is not folded into
    the ``LIVE_OVERRIDES`` guard next to it: that one asserts the spec is wrong
    about the row's SHAPE, this one asserts the spec is right and the
    generator's tie-break picked the wrong record off it. An entry that stopped
    differing would read as a measurement contradicting the document when it no
    longer does.

    It also pins the half that makes the correction worth having: the displaced
    record stays in ``ROW_COMPANIONS``, so the alternative is still written
    down and a wrong pick is still one line to correct.
    """
    envelope, companions = gen.build_index(spec)
    assert gen.SUBJECT_OF, "the subject table is empty; delete this test with it"
    for operation, subject in gen.SUBJECT_OF.items():
        props = _declared_order(spec, operation)
        assert envelope[operation] == subject
        assert props.index(subject) > 0, (
            f"{operation}: {subject!r} is declared first, so the first-declared "
            f"rule already picks it — remove it from SUBJECT_OF"
        )
        displaced = props[0]
        assert displaced in companions[operation], (
            f"{operation}: {displaced!r} was displaced and is not in the "
            f"companions — a corrected pick must leave the alternative written down"
        )


def test_a_subject_and_a_live_override_cannot_name_the_same_operation(gen, spec, monkeypatch):
    """They are incompatible claims, so resolving them by order would make
    whichever ran second win silently."""
    operation = next(iter(gen.LIVE_OVERRIDES))
    monkeypatch.setattr(gen, "SUBJECT_OF", {operation: "whatever"})
    with pytest.raises(KeyError, match="BOTH"):
        gen.build_index(spec)


def test_a_subject_that_is_not_on_the_row_raises_rather_than_being_written(gen, spec):
    """Declaring a key the rows do not carry makes ``_unwrap_rows`` raise on
    EVERY row of a live call — a typo trading a silent wrong record for a loud
    total failure. It must not reach the generated module.

    ⚠ MATCHED ON THE EXPLANATION, NOT ON THE NAME, and a mutation is why. With
    the ``operation not in envelope`` branch deleted, the very next line still
    raises ``KeyError`` — an incidental one from the dict lookup, carrying the
    operation as its whole message. A ``match=`` on the operation name is
    satisfied by both, so the battery scored the deliberate refusal ALIVE while
    the guard was doing nothing. What the branch buys is the SENTENCE, so the
    sentence is what the test reads.
    """
    operation = next(iter(gen.SUBJECT_OF))
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(gen, "SUBJECT_OF", {operation: "nosuchrecord"})
        with pytest.raises(KeyError, match="records actually on the row"):
            gen.build_index(spec)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(gen, "SUBJECT_OF", {"GET /not-a-collection": "thing"})
        with pytest.raises(KeyError, match="cannot create a collection"):
            gen.build_index(spec)


def test_the_companions_are_recorded_rather_than_discarded():
    """A wrong pick must be one line to correct, with the alternatives written
    down — and ``unit`` beside a lease is relationship material, not noise."""
    assert ROW_COMPANIONS["GET /leases"] == ("unit",)
    assert ROW_COMPANIONS["GET /maintenance/work-orders"] == ("contact",)
    # A single-key wrapper has no companions and must not invent one.
    assert "GET /tenants" not in ROW_COMPANIONS
    assert "GET /owners" not in ROW_COMPANIONS


# --------------------------------------------------------------------------
# what the client does with it
# --------------------------------------------------------------------------


def test_a_wrapped_collection_is_unwrapped_to_the_records():
    rows = rvclient._rows_from([{"lease": {"leaseID": 1}, "unit": {"unitID": 9}}], "GET /leases")
    assert rows == [{"leaseID": 1}]


def test_a_bare_collection_is_left_exactly_as_it_arrived():
    """Unwrapping a bare row is the same bug pointed the other way.

    ⚠ Re-pointed 2026-09-23: this used ``GET /owners``, which the live run
    proved is wrapped. A bare example has to come from an operation that is
    actually bare, or the test is asserting the defect.
    """
    body = [{"ledgerTypeID": 7, "amount": "12.00"}]
    assert rvclient._rows_from(body, "GET /accounting/diagnostics/escrow-mismatch") == body


def test_a_row_missing_its_declared_key_raises_and_names_everything_needed():
    """The headline: this must NOT hand the wrapper on, and must NOT skip it.

    Either quiet option gives the mapper an object with no id, and the sync
    then reports a clean complete fetch of an empty portfolio.
    """
    with pytest.raises(rvclient.RentvineAPIError) as exc:
        rvclient._rows_from([{"propertyID": 4610, "name": "Redbud"}], "GET /properties")
    message = str(exc.value)
    assert "'property'" in message, "must name the key it expected"
    assert "propertyID" in message, "must name the keys that actually arrived"
    assert "row 0" in message, "must name which row"
    assert exc.value.failure is transport.Failure.UNDOCUMENTED
    assert exc.value.retryable is False


def test_a_null_record_under_the_right_key_also_raises():
    """``{"lease": null}`` is not a lease. Passing ``None`` to the mapper is the
    same empty-mirror outcome with a ``TypeError`` somewhere further on."""
    with pytest.raises(rvclient.RentvineAPIError):
        rvclient._rows_from([{"lease": None}], "GET /leases")


def test_the_failing_row_is_named_by_its_index_not_just_the_first():
    with pytest.raises(rvclient.RentvineAPIError, match="row 2"):
        rvclient._rows_from(
            [{"lease": {"leaseID": 1}}, {"lease": {"leaseID": 2}}, {"unit": {}}],
            "GET /leases",
        )


def test_an_empty_collection_is_empty_rather_than_an_error():
    """An account with no vendors is a real answer, and must not look like a
    broken envelope."""
    assert rvclient._rows_from([], "GET /vendors") == []


def test_no_operation_leaves_the_rows_alone(caplog):
    """The body-envelope half still works on its own, and says nothing."""
    body = [{"lease": {"leaseID": 1}}]
    assert rvclient._rows_from(body) == body
    assert "row envelope" not in caplog.text


def test_an_operation_absent_from_the_map_is_warned_about_not_silently_passed(caplog):
    """⚠ ``ROW_ENVELOPE.get()`` answers ``None`` for both absent and bare, so
    without the membership test the two collapse — and they are different
    claims. Absent means the rows are being passed through on no evidence,
    which is exactly how ``/vendors`` was missed.
    """
    body = [{"whatever": {"id": 1}}]
    with caplog.at_level("WARNING"):
        assert rvclient._rows_from(body, "GET /not-a-collection") == body
    assert "GET /not-a-collection" in caplog.text
    assert "whatever" in caplog.text, "the keys that arrived are the only way to settle it"


def test_the_body_envelope_and_the_row_envelope_both_apply():
    """A wrapped body AND wrapped rows. Handling one and not the other is the
    bug in either direction."""
    body = {"items": [{"contact": {"contactID": 3}}]}
    assert rvclient._rows_from(body, "GET /tenants") == [{"contactID": 3}]


# --------------------------------------------------------------------------
# the generated file
# --------------------------------------------------------------------------


def test_the_committed_map_matches_the_spec(gen):
    """Drift. Weaker than the literals above, and still worth having."""
    assert gen.main(["--check"]) == 0


def test_the_map_is_keyed_to_the_captured_spec(gen):
    assert SPEC_SHA256 == gen.EXPECTED_SHA256


def test_the_generated_file_is_already_formatter_clean():
    """⚠ The drift check compares bytes, so a generator emitting something the
    formatter would rewrite makes ``--check`` fail permanently the first time
    anybody runs ``make format`` — and the obvious repair at that point is to
    regenerate, which puts it straight back. Measured rather than assumed: the
    emitted shape was arrived at by formatting the file and reading the result.
    """
    import shutil
    import subprocess

    if shutil.which("ruff") is None:  # pragma: no cover - ruff is a [dev] dep
        pytest.skip("ruff not installed")
    done = subprocess.run(
        ["ruff", "format", "--check", "src/helixis/rentvine/_row_envelopes.py"],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_map_covers_every_array_returning_get(gen, spec):
    """Not a sample. A collection absent from the map is passed through
    unwrapped, so a gap here is a silently-empty table later."""
    expected = set()
    for path, item in spec["paths"].items():
        op = (item or {}).get("get")
        if not op:
            continue
        content = ((op.get("responses") or {}).get("200") or {}).get("content") or {}
        schema = (next(iter(content.values()), {}) or {}).get("schema") or {}
        if schema.get("type") == "array":
            expected.add(f"GET {path}")
    assert expected, "found no array-returning GETs — the walk is wrong"
    assert set(ROW_ENVELOPE) == expected


# --------------------------------------------------------------------------
# the live-override layer — where a measurement outranks the capture
# --------------------------------------------------------------------------


def test_every_live_override_still_contradicts_the_spec(gen, spec, monkeypatch):
    """A stale override is a licence nobody needs (gotcha 63).

    ⚠ The comparison runs the REAL walk with the table emptied, rather than
    re-reading ``items`` by hand here. A second implementation of the rule in
    the test would drift from the generator's, and then this would be
    comparing two guesses instead of the spec against a measurement.

    The day Rentvine corrects its document, the override stops differing and
    this fails — which is the signal to DELETE the entry, not to weaken this.
    """
    overrides = dict(gen.LIVE_OVERRIDES)
    assert overrides, "the override table is empty; delete this test with it"

    monkeypatch.setattr(gen, "LIVE_OVERRIDES", {})
    from_spec, _ = gen.build_index(spec)

    for operation, measured in overrides.items():
        assert operation in from_spec, f"{operation} is not an array-returning GET"
        assert from_spec[operation] != measured, (
            f"{operation}: the spec now reads {from_spec[operation]!r}, which "
            f"agrees with the live measurement — remove it from LIVE_OVERRIDES"
        )


def test_an_override_for_a_non_collection_raises_rather_than_inventing_one(gen, spec, monkeypatch):
    """⚠ A plain assignment would ADD the key, and an absent key is the map's
    third state ("not a collection"). So a typo would invent a collection out
    of nothing and the runtime would start unwrapping rows of something that
    is not one — the failure mode is silently-does-more (gotcha 45).
    """
    monkeypatch.setattr(gen, "LIVE_OVERRIDES", {"GET /not-a-collection": "thing"})
    with pytest.raises(KeyError, match="not-a-collection"):
        gen.build_index(spec)


def test_the_committed_module_names_the_same_overrides_the_generator_applies():
    """Both sides (gotcha 28). ``LIVE_OVERRIDDEN`` is what a reader of the
    committed map — and any future runtime check — can see; the generator's
    table is what actually moved the values. A module listing an override it
    did not receive reads as a disagreement with the spec that is not there.
    """
    gen = _generator()
    assert LIVE_OVERRIDDEN == frozenset(gen.LIVE_OVERRIDES)
    for operation in LIVE_OVERRIDDEN:
        assert ROW_ENVELOPE[operation] == gen.LIVE_OVERRIDES[operation]


def test_the_two_collections_the_live_run_corrected_are_the_ones_it_named():
    """The measurement itself, pinned so a regeneration cannot quietly drop it.

    Phase 8's first live sync (2026-09-23) mirrored 5 of 7 families and left
    owners at 0 of 5 and units at 0 of 37 — both because the row handed to the
    mapper was a wrapper the map said was a record.
    """
    assert ROW_ENVELOPE["GET /owners"] == "contact"
    assert ROW_ENVELOPE["GET /properties/{propertyID}/units"] == "unit"


def test_the_committed_module_names_the_same_subjects_the_generator_corrects():
    """Both sides (gotcha 28), and kept SEPARATE from ``LIVE_OVERRIDDEN``.

    A reader of the committed map has to be able to tell "the spec was wrong
    about this row" from "the spec was right and we picked the wrong record off
    it". Collapsing them into one set would make the second read as the first,
    and the first is the one nobody may "correct" back by reading the document.
    """
    gen = _generator()
    assert MEASURED_SUBJECT == frozenset(gen.SUBJECT_OF)
    assert not (MEASURED_SUBJECT & LIVE_OVERRIDDEN)
    for operation in MEASURED_SUBJECT:
        assert ROW_ENVELOPE[operation] == gen.SUBJECT_OF[operation]


def test_the_units_export_is_a_units_export_and_carries_the_property_as_a_join():
    """Probe K's measurement, pinned — and the half that stops the obvious
    "collapse the N+1" being the next silent zero.

    ⚠ ``sync.UNITS_PATH``'s comment now rests on BOTH facts: the export's
    subject is the unit, and ``unit_export`` carries no ``propertyID`` so the
    link is only on the sibling record. Prose cannot fail (gotcha 48), so the
    second fact is read off the captured spec here rather than believed.
    """
    operation = "GET /properties/units/export"
    assert ROW_ENVELOPE[operation] == "unit"
    assert ROW_COMPANIONS[operation] == ("property",)

    spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    unit_export = spec["components"]["schemas"]["unit_export"]["properties"]
    assert "propertyID" not in unit_export, (
        "the export's unit now carries the property link — sync.UNITS_PATH's "
        "comment says it does not, and the N+1 it justifies can go"
    )
    # The CONTROL: the per-property route's record does carry it, which is the
    # whole reason that route is the one the sync reads.
    assert "propertyID" in spec["components"]["schemas"]["unit"]["properties"]


def test_a_changed_spec_is_refused_rather_than_regenerated_against(gen, tmp_path):
    """The map is only meaningful against the spec it was read from."""
    other = tmp_path / "openapi.json"
    other.write_text('{"paths": {}}', encoding="utf-8")
    original = gen.SPEC
    try:
        gen.SPEC = other
        assert gen.main(["--check"]) == 1
    finally:
        gen.SPEC = original
