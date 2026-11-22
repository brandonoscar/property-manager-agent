"""Probe G: does the identity-type probe answer the question, or just look like it?

ADR 0010 revision 3 puts BOTH PMSs' record ids in the identity column each
mirror table already has — `bigint` columns. Whether Rentvine's ids fit is
inferred from a spec that contradicts itself (126 of 127 ``*ID`` path
parameters are integer; most ``leaseID``/``propertyID`` schema declarations are
string-typed). Probe G is the instrument that settles it against a real server.

An instrument that reports the wrong verdict is worse than none, because this
one's answer decides whether a migration is safe. So the three outcomes are
tested separately, and the middle one — a NUMERIC STRING — is the one that
matters most: it coerces into a bigint silently, so nothing fails at write
time and the assumption is still wrong about the wire.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _probe_module():
    path = REPO / "scripts" / "rentvine_probe.py"
    spec = importlib.util.spec_from_file_location("_rv_probe", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_rv_probe"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def probe():
    return _probe_module()


@pytest.fixture
def creds(probe, monkeypatch):
    monkeypatch.setenv("RENTVINE_ACCOUNT_CODE", "acme")
    monkeypatch.setenv("RENTVINE_API_KEY", "key")
    monkeypatch.setenv("RENTVINE_API_SECRET", "secret")
    return probe.Creds()


def _serve(probe, monkeypatch, rows_by_path: dict[str, object], status: int = 200):
    """Answer every probe GET from a fixture instead of the network."""

    def fake_get(_creds, path, params=None, **kw):
        if path not in rows_by_path:
            return {"status": 404, "body": None, "error": None, "url": path}
        import json as _json

        return {
            "status": status,
            "body": _json.dumps({"rows": rows_by_path[path]}),
            "error": None,
            "url": path,
        }

    monkeypatch.setattr(probe, "_get", fake_get)


# --------------------------------------------------------------------------
# the type reader
# --------------------------------------------------------------------------


def test_bool_is_not_reported_as_int(probe):
    """``isinstance(True, int)`` is True in Python — gotcha 27's ordering trap.

    A flag misread as an integer id would be counted as evidence that ids are
    numeric, which is the exact conclusion this probe exists to test.
    """
    assert probe._json_type(True) == "bool"
    assert probe._json_type(1) == "int"
    assert probe._json_type("1") == "str"
    assert probe._json_type(None) == "null"
    assert probe._json_type(1.5) == "float"
    assert probe._json_type([1]) == "list"


def test_id_fields_unwraps_one_nesting_level_and_reads_only_identity_keys(probe):
    row = {"property": {"propertyID": 7, "name": "Elm St", "portfolioID": 3, "isActive": True}}
    got = dict(probe._id_fields(row))
    assert got == {"propertyID": 7, "portfolioID": 3}
    # Nothing that could carry a name, email or address is collected, so the
    # report is safe to paste into an issue.
    assert "name" not in got


def test_id_fields_handles_an_unwrapped_row(probe):
    assert dict(probe._id_fields({"leaseID": 1, "unitID": 2})) == {"leaseID": 1, "unitID": 2}


def test_id_fields_declines_on_a_shape_it_does_not_recognise(probe):
    assert probe._id_fields("not a row") == []
    assert probe._id_fields([1, 2]) == []


# --------------------------------------------------------------------------
# the three verdicts
# --------------------------------------------------------------------------


def test_integer_ids_confirm_the_assumption(probe, creds, monkeypatch):
    _serve(probe, monkeypatch, {p: [{"propertyID": 4471}] for p, _ in probe._IDENTITY_COLLECTIONS})
    out = probe.probe_identity_types(creds)
    assert out["verdict"].startswith("CONFIRMED")
    assert out["numeric_range"] == [4471, 4471]


def test_a_non_numeric_string_id_refutes_it_and_names_the_field(probe, creds, monkeypatch):
    _serve(
        probe,
        monkeypatch,
        {p: [{"propertyID": "abc-123"}] for p, _ in probe._IDENTITY_COLLECTIONS},
    )
    out = probe.probe_identity_types(creds)
    assert out["verdict"].startswith("REFUTED")
    assert "propertyID" in out["verdict"]
    # The verdict has to say what to DO, not just that something is wrong.
    assert "Decision (a)" in out["verdict"]


def test_a_numeric_string_id_is_qualified_not_confirmed(probe, creds, monkeypatch):
    """The subtle outcome, and the reason the probe reads types at all.

    ``"4471"`` coerces into a bigint without complaint, so a write-time check
    would report success. Only the JSON type distinguishes it.
    """
    _serve(
        probe, monkeypatch, {p: [{"propertyID": "4471"}] for p, _ in probe._IDENTITY_COLLECTIONS}
    )
    out = probe.probe_identity_types(creds)
    assert out["verdict"].startswith("QUALIFIED")
    assert "STRINGS" in out["verdict"]
    assert out["numeric_string_samples"][:1] == ["4471"]


def test_a_non_numeric_string_outranks_a_numeric_one(probe, creds, monkeypatch):
    """Both present: the refutation must win, not be averaged away."""
    _serve(
        probe,
        monkeypatch,
        {p: [{"propertyID": "4471", "portfolioID": "abc"}] for p, _ in probe._IDENTITY_COLLECTIONS},
    )
    assert probe.probe_identity_types(creds)["verdict"].startswith("REFUTED")


# --------------------------------------------------------------------------
# the collision half, and declining to answer
# --------------------------------------------------------------------------


def test_an_id_range_overlapping_buildium_is_called_out(probe, creds, monkeypatch):
    """Revision 3 made one-PMS-per-company load-bearing; the range says how much."""
    _serve(probe, monkeypatch, {p: [{"propertyID": 11316}] for p, _ in probe._IDENTITY_COLLECTIONS})
    out = probe.probe_identity_types(creds)
    assert "OVERLAPS Buildium's range" in out["verdict"]
    assert "a1" in out["verdict"]


def test_a_high_range_is_reported_differently(probe, creds, monkeypatch):
    _serve(
        probe,
        monkeypatch,
        {p: [{"propertyID": 99_000_000_000}] for p, _ in probe._IDENTITY_COLLECTIONS},
    )
    assert "above Buildium's usual range" in probe.probe_identity_types(creds)["verdict"]


def test_no_readable_rows_declines_rather_than_confirming(probe, creds, monkeypatch):
    """A probe that reached nothing must not read as evidence ids are numeric.

    This is the failure mode that would matter most: an empty account, a
    permissions-restricted key, or an unrecognised envelope all return zero
    rows, and zero rows is not an answer (gotchas 55/57).
    """
    _serve(probe, monkeypatch, {})
    out = probe.probe_identity_types(creds)
    assert out["verdict"] == probe._UNKNOWN
    assert out["unreadable"], "the report must say WHICH collections could not be read"


def test_one_unreachable_family_does_not_stop_the_others(probe, creds, monkeypatch):
    reachable = probe._IDENTITY_COLLECTIONS[0][0]
    _serve(probe, monkeypatch, {reachable: [{"propertyID": 12}]})
    out = probe.probe_identity_types(creds)
    assert out["verdict"].startswith("CONFIRMED")
    assert len(out["families_reached"]) == 1
    assert len(out["unreadable"]) == len(probe._IDENTITY_COLLECTIONS) - 1


# --------------------------------------------------------------------------
# wiring
# --------------------------------------------------------------------------


def test_every_probe_target_is_a_path_the_spec_documents(probe):
    """A guessed URL is what CLAUDE.md gotcha 79 records the cost of.

    If a target is not in the captured spec, a 404 would read as "Rentvine does
    not have this" when it actually means "we made the path up".
    """
    sys.path.insert(0, str(REPO / "src"))
    from helixis.rentvine._query_index import QUERY_PARAMS

    for path, _family in probe._IDENTITY_COLLECTIONS:
        assert f"GET {path}" in QUERY_PARAMS, f"{path} is not documented in the spec"


def test_the_identity_probe_is_actually_registered_in_main(probe):
    """A probe nothing calls is the `comms_triage` failure (gotcha 62).

    Behavioural tests above all invoke the function directly, so none of them
    can see that ``main`` never runs it.
    """
    import ast

    src = (REPO / "scripts" / "rentvine_probe.py").read_text(encoding="utf-8")
    main_fn = next(
        n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef) and n.name == "main"
    )
    names = {node.id for node in ast.walk(main_fn) if isinstance(node, ast.Name)}
    assert "probe_identity_types" in names, "main() does not run the identity probe"


# --------------------------------------------------------------------------
# Probes I / J / K — the three markers the probe could not previously answer
# --------------------------------------------------------------------------
#
# Each of these settles an @unverified marker by OBSERVATION, so the failure
# that matters is an instrument that reports a clean verdict it did not earn.
# Every test below therefore pins the DECLINE as hard as the answer.


def test_probe_i_reports_the_key_union_across_rows_not_just_the_first(probe, creds, monkeypatch):
    """An absence cannot be read off one row.

    A server that omits null fields shows a key on the row that has a value
    and hides it on the row that does not — so a probe that stopped at row one
    would report "no deposit field" on an account that plainly has one.
    """
    _serve(
        probe,
        monkeypatch,
        {
            "/leases": [
                {"leaseID": 1, "rentAmount": 900},
                {"leaseID": 2, "rentAmount": 950, "securityDeposit": 1800},
            ],
            "/owners/search": [{"ownerID": 7, "firstName": "A"}],
        },
    )
    out = probe.probe_row_shapes(creds)

    lease = next(f for f in out["findings"] if f["family"] == "lease")
    assert lease["matching_keys"] == ["securityDeposit"]
    assert lease["rows_sampled"] == 2
    assert "REFUTED" in out["verdict"]


def test_probe_i_matches_a_field_under_any_spelling(probe, creds, monkeypatch):
    """`deposit`, `securityDeposit`, `depositAmount` all refute the claim.

    Looking for one exact name is gotcha 79's error — an identifier somebody
    expects is not evidence about the wire.
    """
    for spelling in ("deposit", "depositAmount", "SecurityDepositHeld"):
        _serve(
            probe,
            monkeypatch,
            {
                "/leases": [{"leaseID": 1, spelling: 500}],
                "/owners/search": [{"ownerID": 7}],
            },
        )
        out = probe.probe_row_shapes(creds)
        lease = next(f for f in out["findings"] if f["family"] == "lease")
        assert lease["matching_keys"] == [spelling], spelling


def test_probe_i_states_a_clean_sample_as_a_sample_and_never_as_a_schema(probe, creds, monkeypatch):
    """The one wording that would make this instrument dangerous.

    "Rentvine has no deposit field" is not what a sampled page can support,
    and it is exactly the absence-claimed-over-a-truncated-read failure
    (gotcha 26) the connector exists to avoid making at the customer.
    """
    _serve(
        probe,
        monkeypatch,
        {"/leases": [{"leaseID": 1}], "/owners/search": [{"ownerID": 7}]},
    )
    out = probe.probe_row_shapes(creds)

    assert "CONSISTENT" in out["verdict"]
    lease = next(f for f in out["findings"] if f["family"] == "lease")
    assert "NOT proof" in lease["verdict"]
    assert lease["keys"] == ["leaseID"], "the whole key union is the artifact"


def test_probe_i_declines_when_neither_collection_answers(probe, creds, monkeypatch):
    _serve(probe, monkeypatch, {})
    out = probe.probe_row_shapes(creds)
    assert out["verdict"] == probe._UNKNOWN


def _units(probe, monkeypatch, rows):
    """Serve the units export only — the widest sample the probe tries first."""
    import json as _json

    def fake_get(_creds, path, params=None, **kw):
        if path == "/properties/units/export":
            return {"status": 200, "body": _json.dumps({"rows": rows}), "error": None, "url": path}
        return {"status": 404, "body": None, "error": None, "url": path}

    monkeypatch.setattr(probe, "_get", fake_get)


def test_probe_j_confirms_only_when_the_two_groups_differ(probe, creds, monkeypatch):
    """leaseID set on occupied units and clear on vacant ones is the finding."""
    _units(
        probe,
        monkeypatch,
        [
            {"unitID": 1, "primaryLeaseStatusID": 2, "leaseID": 11},
            {"unitID": 2, "primaryLeaseStatusID": 2, "leaseID": 12},
            {"unitID": 3, "primaryLeaseStatusID": 3, "leaseID": None},
        ],
    )
    out = probe.probe_unit_occupancy(creds)
    assert out["verdict"].startswith("CONFIRMED")
    assert out["active_with_lease_id"] == 2
    assert out["non_active_with_lease_id"] == 0


def test_probe_j_refutes_when_every_unit_carries_a_lease_id(probe, creds, monkeypatch):
    """⚠ THE CONTROL IS THE PROBE.

    Without comparing against occupied units, "vacant units carry a leaseID"
    and "every unit carries one" are the same observation — and only the
    second means the field says nothing about occupancy.
    """
    _units(
        probe,
        monkeypatch,
        [
            {"unitID": 1, "primaryLeaseStatusID": 2, "leaseID": 11},
            {"unitID": 2, "primaryLeaseStatusID": 3, "leaseID": 12},
        ],
    )
    out = probe.probe_unit_occupancy(creds)
    assert out["verdict"].startswith("REFUTED")
    assert "no occupancy information" in out["verdict"]


def test_probe_j_declines_when_the_sample_has_no_control_group(probe, creds, monkeypatch):
    """All-Active is not a confirmation — there is nothing to compare against."""
    _units(
        probe,
        monkeypatch,
        [
            {"unitID": 1, "primaryLeaseStatusID": 2, "leaseID": 11},
            {"unitID": 2, "primaryLeaseStatusID": 2, "leaseID": 12},
        ],
    )
    out = probe.probe_unit_occupancy(creds)
    assert out["verdict"].startswith(probe._UNKNOWN)
    assert "no vacant group" in out["verdict"]


def test_probe_j_qualifies_a_partial_correlation_rather_than_rounding_it(probe, creds, monkeypatch):
    """The middle outcome, which is the one an eager reader turns into a yes."""
    _units(
        probe,
        monkeypatch,
        [
            {"unitID": 1, "primaryLeaseStatusID": 2, "leaseID": 11},
            {"unitID": 2, "primaryLeaseStatusID": 3, "leaseID": 12},
            {"unitID": 3, "primaryLeaseStatusID": 3, "leaseID": None},
        ],
    )
    out = probe.probe_unit_occupancy(creds)
    assert out["verdict"].startswith("QUALIFIED")
    assert "CORRELATES with occupancy without determining it" in out["verdict"]


def test_probe_j_falls_back_to_per_property_units_when_the_export_does_not_answer(
    probe, creds, monkeypatch
):
    """The export is probe K's open question, so J must not depend on it."""
    import json as _json

    def fake_get(_creds, path, params=None, **kw):
        if path == "/properties/search":
            return {
                "status": 200,
                "body": _json.dumps({"rows": [{"propertyID": 5}]}),
                "error": None,
                "url": path,
            }
        if path == "/properties/5/units":
            return {
                "status": 200,
                "body": _json.dumps(
                    {
                        "rows": [
                            {"unitID": 1, "primaryLeaseStatusID": 2, "leaseID": 11},
                            {"unitID": 2, "primaryLeaseStatusID": 3, "leaseID": None},
                        ]
                    }
                ),
                "error": None,
                "url": path,
            }
        return {"status": 404, "body": None, "error": None, "url": path}

    monkeypatch.setattr(probe, "_get", fake_get)
    out = probe.probe_unit_occupancy(creds)

    assert out["source"] == "/properties/{propertyID}/units"
    assert out["verdict"].startswith("CONFIRMED")


def test_probe_k_finds_the_link_on_the_envelope_not_only_the_rows(probe, creds, monkeypatch):
    """The envelope is the whole question — the rows were already read."""
    import json as _json

    def fake_get(_creds, path, params=None, **kw):
        return {
            "status": 200,
            "body": _json.dumps({"propertyID": 5, "rows": [{"unitID": 1, "name": "4B"}]}),
            "error": None,
            "url": path,
        }

    monkeypatch.setattr(probe, "_get", fake_get)
    out = probe.probe_units_export_envelope(creds)

    assert out["verdict"].startswith("CHANGE THE SYNC")
    assert out["envelope_property_keys"] == ["propertyID"]
    assert out["row_property_keys"] == []


def test_probe_k_confirms_the_n_plus_1_when_nothing_carries_the_link(probe, creds, monkeypatch):
    import json as _json

    def fake_get(_creds, path, params=None, **kw):
        return {
            "status": 200,
            "body": _json.dumps({"rows": [{"unitID": 1, "name": "4B"}]}),
            "error": None,
            "url": path,
        }

    monkeypatch.setattr(probe, "_get", fake_get)
    out = probe.probe_units_export_envelope(creds)

    assert out["verdict"].startswith("KEEP THE N+1")


def test_probe_k_does_not_read_an_empty_page_as_a_no(probe, creds, monkeypatch):
    """200-with-no-rows leaves the question open; calling it "no link" would
    confirm the N+1 on evidence that says nothing."""
    import json as _json

    def fake_get(_creds, path, params=None, **kw):
        return {"status": 200, "body": _json.dumps({"rows": []}), "error": None, "url": path}

    monkeypatch.setattr(probe, "_get", fake_get)
    out = probe.probe_units_export_envelope(creds)

    assert out["verdict"].startswith(probe._UNKNOWN)


def test_the_report_write_creates_its_parent_directory(probe, tmp_path):
    """⚠ This runs AFTER every probe, on a live credentialed run.

    A missing parent raised FileNotFoundError on the last line and took the
    whole result with it — gotcha 49's shape, on a run nobody can repeat
    without the operator's own API credentials.
    """
    target = tmp_path / "does" / "not" / "exist" / "probe-report.json"
    probe._write(str(target), {"verdict": "x"})
    assert target.exists()


def test_probe_j_declines_an_all_vacant_sample_rather_than_reading_0_equals_0(
    probe, creds, monkeypatch
):
    """The vacuous green, and it is the one a mutation found rather than review.

    With no Active unit in the sample the refute branch's second half reads
    ``occupied_with == len(occupied)`` — which is ``0 == 0``, True — so
    dropping the missing-control decline produces a confident REFUTED from a
    sample that contains no control at all. `rentvine_reconcile.py`'s own
    docstring names that shape ("⚠ THE FAILURE THIS FILE IS MOSTLY ABOUT:
    0 == 0"), and it is reachable here: a small account with no active lease,
    or any page that happens to return only vacant units.
    """
    _units(
        probe,
        monkeypatch,
        [
            {"unitID": 1, "primaryLeaseStatusID": 3, "leaseID": 11},
            {"unitID": 2, "primaryLeaseStatusID": 3, "leaseID": 12},
        ],
    )
    out = probe.probe_unit_occupancy(creds)

    assert out["verdict"].startswith(probe._UNKNOWN), out["verdict"]
    assert "control is missing" in out["verdict"]
    assert out["active_units"] == 0


def test_probe_i_reads_the_lease_inside_its_wrapper_not_the_wrapper(probe, creds, monkeypatch):
    """`/leases` rows arrive as ``{lease, unit}`` (``_row_envelopes``). Reading
    the wrapper's keys compared ``{lease, unit}`` with "deposit" and passed
    without looking at a lease (2026-10-03). The unit beside it carries the
    unit's DEFAULT deposit, and the lease's ``depositRefundDueDate`` is a
    deadline; neither is a deposit the mirror is throwing away."""
    _serve(
        probe,
        monkeypatch,
        {
            "/leases": [
                {
                    "lease": {"leaseID": 1, "depositRefundDueDate": "2026-11-01"},
                    "unit": {"unitID": 9, "deposit": "1500.00"},
                },
                {"lease": {"leaseID": 2}, "unit": {"unitID": 10, "deposit": "900.00"}},
            ],
            "/owners/search": [{"contact": {"contactID": 7, "name": "A"}}],
        },
    )
    out = probe.probe_row_shapes(creds)
    lease = next(f for f in out["findings"] if f["family"] == "lease")
    assert lease["keys"] == ["depositRefundDueDate", "leaseID"]
    assert lease["wrapped_rows"] == 2 and lease["subject"] == "lease"
    assert lease["matching_keys"] == []
    assert lease["not_amounts_seen"] == ["depositRefundDueDate"]
    assert "CONSISTENT" in out["verdict"]
    owner = next(f for f in out["findings"] if f["family"] == "owner")
    assert owner["keys"] == ["contactID", "name"]

    _serve(
        probe,
        monkeypatch,
        {
            "/leases": [{"lease": {"leaseID": 3, "securityDepositAmount": "1500.00"}}],
            "/owners/search": [{"contact": {"contactID": 7}}],
        },
    )
    out = probe.probe_row_shapes(creds)
    lease = next(f for f in out["findings"] if f["family"] == "lease")
    assert lease["matching_keys"] == ["securityDepositAmount"]
    assert "REFUTED" in out["verdict"]


def test_probe_i_subjects_are_the_generated_envelopes(probe) -> None:
    """The probe is stdlib-only, so it names each subject itself; this keeps
    them equal to the map generated from the spec (gotcha 158)."""
    from helixis.rentvine._row_envelopes import ROW_ENVELOPE

    for path, _family, _needles, _rests_on, subject, _not_amounts in probe._ABSENCE_CLAIMS:
        assert ROW_ENVELOPE[f"GET {path}"] == subject, path
