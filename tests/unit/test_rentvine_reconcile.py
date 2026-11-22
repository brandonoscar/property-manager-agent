"""Phase 8's reconciliation must not pass by having learned nothing.

The gate is "the mirror's row count must equal the same count read back from
the API, and a re-run must be idempotent". The naive implementation compares
two integers — and a failed API read and an empty account both produce zero,
so ``0 == 0`` reports RECONCILED off the back of a read that did not happen.
That is this product's most-recorded failure shape (gotchas 26/55/57).

So the tests below are weighted towards the ways this could pass WRONGLY,
because a reconciliation that cries wolf gets fixed and one that green-lights
silently does not.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _module():
    path = REPO / "scripts" / "rentvine_reconcile.py"
    spec = importlib.util.spec_from_file_location("_rv_reconcile", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_rv_reconcile"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def rec():
    return _module()


# --------------------------------------------------------------------------
# the ways it could pass wrongly
# --------------------------------------------------------------------------


def test_an_unread_api_never_reconciles_even_when_the_mirror_is_empty(rec):
    """THE headline case: unread vs empty must not collapse into 0 == 0."""
    out = rec.reconcile({"properties": None}, {"properties": 0})
    assert out.families[0].verdict is rec.Verdict.UNKNOWN
    assert out.ok is False
    assert "NOT a match" in out.families[0].detail


def test_an_unread_mirror_never_reconciles_either(rec):
    out = rec.reconcile({"properties": 0}, {"properties": None})
    assert out.families[0].verdict is rec.Verdict.UNKNOWN
    assert out.ok is False


def test_both_unread_is_unknown_not_a_match(rec):
    out = rec.reconcile({"properties": None}, {"properties": None})
    assert out.families[0].verdict is rec.Verdict.UNKNOWN
    assert out.ok is False


def test_a_genuine_zero_on_both_sides_does_reconcile_and_says_why(rec):
    """The distinction has to cut both ways or it is just strictness.

    An account with no vendors really does reconcile at zero — and the detail
    says the API was actually read, which is what separates this from the case
    above.
    """
    out = rec.reconcile({"vendors": 0}, {"vendors": 0})
    assert out.families[0].verdict is rec.Verdict.RECONCILED
    assert out.ok is True
    assert "BECAUSE the API was" in out.families[0].detail


def test_an_empty_run_is_not_a_pass(rec):
    """``all([])`` is True; a reconciliation over nothing has checked nothing."""
    assert rec.reconcile({}, {}).ok is False


def test_a_family_missing_from_one_side_is_unknown_not_zero(rec):
    """A family the sync never attempted has not been shown to be empty."""
    out = rec.reconcile({"leases": 12}, {})
    assert out.families[0].verdict is rec.Verdict.UNKNOWN
    assert out.ok is False


# --------------------------------------------------------------------------
# real mismatches, and the two directions are not the same
# --------------------------------------------------------------------------


def test_a_short_mirror_names_every_cause_and_picks_none(rec):
    """⚠ **RE-POINTED 2026-09-23 — this asserted the wrong half of its own
    module's rule.** It was called ``..._names_pagination_as_the_likely_cause``
    and required exactly that, while the module's header is about never
    fabricating a reading. The first live Phase 8 run produced two MISSING
    families and paging was not involved in either: the rows arrived and the
    WRITER dropped them, because the row envelope called a wrapper a record.
    An operator following the old sentence would have gone after the paging
    loop, which was working.

    ⚠ **RE-POINTED AGAIN 2026-09-23 — there are THREE now, not two.** Making
    ``_sync_units`` record-and-continue (gotcha 160) created a shortfall this
    comparison had no cause for: part of the family was never fetched, and the
    sync's own ``failed`` says so. It is DECISIVE and free to check, unlike the
    other two, so it is named first — and this test is what stops the detail
    drifting back to a shorter list as new causes appear.

    This comparison is pure and genuinely cannot tell them apart, so the
    property is that it names every one and sends the reader to what can.
    """
    out = rec.reconcile({"properties": 120}, {"properties": 100})
    f = out.families[0]
    assert f.verdict is rec.Verdict.MISSING
    assert "short by 20" in f.detail
    # (c) the vendor's gap — still named, because it is real.
    assert "stop condition" in f.detail
    # (b) ours at the WRITER — the cause the first live run had, and the field
    # that distinguishes it.
    assert "skipped_no_id" in f.detail.lower() or "skipped" in f.detail.lower()
    assert "keys" in f.detail, "the skip warning's keys are what separate (b) from (c)"
    # (a) ours at the FETCH — a partial per-property walk. Named first because
    # it is the only one settled by reading a field rather than a log.
    assert "failed" in f.detail.lower(), "a partially-fetched family must be a listed cause"


def test_a_mirror_holding_more_is_not_treated_as_harmless(rec):
    """Over-count means duplicates or an unscoped read, not a rounding issue."""
    out = rec.reconcile({"properties": 100}, {"properties": 140})
    f = out.families[0]
    assert f.verdict is rec.Verdict.EXTRA
    assert "40 MORE" in f.detail
    assert "scope" in f.detail


def test_one_bad_family_fails_the_whole_run(rec):
    out = rec.reconcile({"properties": 10, "leases": 5}, {"properties": 10, "leases": 4})
    assert out.ok is False
    assert [f.family for f in out.unresolved] == ["leases"]


def test_families_are_compared_in_a_stable_order(rec):
    out = rec.reconcile({"units": 1, "leases": 1}, {"units": 1, "leases": 1})
    assert [f.family for f in out.families] == ["leases", "units"]


# --------------------------------------------------------------------------
# idempotency
# --------------------------------------------------------------------------


def test_a_second_sync_that_inserts_nothing_passes(rec):
    out = rec.idempotency(500, 500)
    assert out.ok is True and out.inserted == 0


def test_a_second_sync_that_inserts_fails_and_names_the_cause(rec):
    out = rec.idempotency(500, 507)
    assert out.ok is False and out.inserted == 7
    assert "upsert key" in out.detail


def test_a_second_sync_that_deletes_is_called_out_as_worse(rec):
    """Losing rows on a re-run is not a lesser version of duplicating them."""
    out = rec.idempotency(500, 480)
    assert out.ok is False and out.inserted == -20
    assert "worse" in out.detail


def test_an_unread_count_makes_idempotency_prove_nothing(rec):
    for before, after in ((None, 500), (500, None), (None, None)):
        out = rec.idempotency(before, after)
        assert out.ok is False, (before, after)
        assert "proved nothing" in out.detail


# --------------------------------------------------------------------------
# the mirror-side scoping the count must carry
# --------------------------------------------------------------------------


def test_the_count_sql_carries_both_predicates(rec):
    """Forgetting either is a defect, and they fail in opposite directions.

    Without ``pms`` the count includes Buildium rows and can never match.
    Without ``company_id`` it reads another tenant's rows — which moves the
    numbers CLOSER, so it is the one that hides.
    """
    sql = rec.mirror_count_sql("helixis_properties")
    assert "company_id = $1" in sql
    assert "pms = $2" in sql
    assert "helixis_properties" in sql


# --------------------------------------------------------------------------
# the report
# --------------------------------------------------------------------------


def test_the_report_states_unread_rather_than_printing_none(rec):
    text = rec.render(rec.reconcile({"properties": None}, {"properties": 0}))
    assert "unread" in text
    assert "None" not in text
    assert "NOT RECONCILED" in text


def test_the_report_says_nothing_was_checked_on_an_empty_run(rec):
    text = rec.render(rec.reconcile({}, {}))
    assert "nothing was checked" in text
    assert "NOT RECONCILED" in text


def test_a_clean_run_with_idempotency_reads_as_reconciled(rec):
    text = rec.render(rec.reconcile({"units": 3}, {"units": 3}), rec.idempotency(3, 3))
    assert text.rstrip().endswith("VERDICT: RECONCILED")


def test_counts_matching_but_idempotency_failing_is_not_reconciled(rec):
    """Both halves of Phase 8's gate, and either one alone is not the gate."""
    text = rec.render(rec.reconcile({"units": 3}, {"units": 3}), rec.idempotency(3, 9))
    assert "NOT RECONCILED" in text
