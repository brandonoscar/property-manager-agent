"""Unit tests for the golden end-state regression engine (evals/golden_regression.py)."""

from __future__ import annotations

from helixis.evals.golden_regression import (
    canonicalize,
    compare_end_states,
    diff_states,
    is_ignored_key,
)


def test_is_ignored_key() -> None:
    for k in ("id", "created_at", "task_id", "work_order_id", "LastUpdatedDateTime", "posted_at"):
        assert is_ignored_key(k), k
    for k in ("status", "amount", "unit", "category", "description"):
        assert not is_ignored_key(k), k


def test_canonicalize_drops_volatile_and_normalizes() -> None:
    state = {
        "id": 42,
        "created_at": "2026-07-13T10:00:00Z",
        "task_id": 99,
        "amount": "2450",
        "status": "New",
        "nested": {"work_order_id": 5, "keep": 1.0},
    }
    got = canonicalize(state)
    assert got == {"amount": 2450, "status": "New", "nested": {"keep": 1}}


def test_identical_states_no_diff() -> None:
    a = {"status": "Completed", "charges": [{"amount": 100}, {"amount": 50}]}
    # Volatile fields differ but are ignored → still no diff.
    b = {"status": "Completed", "id": 7, "charges": [{"amount": 100}, {"amount": 50, "id": 9}]}
    assert diff_states(a, b) == []


def test_changed_scalar_flagged() -> None:
    diffs = diff_states({"status": "New"}, {"status": "Completed"})
    assert len(diffs) == 1
    assert diffs[0].path == "status" and diffs[0].kind == "changed"
    assert diffs[0].stored == "New" and diffs[0].current == "Completed"


def test_added_and_removed_keys() -> None:
    diffs = {d.path: d.kind for d in diff_states({"a": 1, "b": 2}, {"a": 1, "c": 3})}
    assert diffs == {"b": "removed", "c": "added"}


def test_nested_path() -> None:
    diffs = diff_states(
        {"work_order": {"unit": "2A", "status": "New"}},
        {"work_order": {"unit": "2A", "status": "Closed"}},
    )
    assert len(diffs) == 1 and diffs[0].path == "work_order.status"


def test_list_element_and_length() -> None:
    diffs = diff_states(
        {"c": [{"amount": 100}, {"amount": 50}]}, {"c": [{"amount": 100}, {"amount": 75}]}
    )
    assert len(diffs) == 1 and diffs[0].path == "c[1].amount"

    len_diffs = diff_states({"c": [1, 2, 3]}, {"c": [1, 2]})
    kinds = {d.kind for d in len_diffs}
    assert "length_changed" in kinds


def test_numeric_normalization_no_false_diff() -> None:
    # "2450" vs 2450 vs 2450.0 all normalize equal — no spurious regression.
    assert diff_states({"amount": "2450"}, {"amount": 2450}) == []
    assert diff_states({"amount": 2450.0}, {"amount": 2450}) == []


def test_type_change_flagged() -> None:
    diffs = diff_states({"x": {"a": 1}}, {"x": [1]})
    assert len(diffs) == 1 and diffs[0].kind == "type_changed"


def test_compare_end_states_wraps() -> None:
    clean = compare_end_states("040_task", {"status": "New"}, {"status": "New", "id": 1})
    assert not clean.regressed and clean.task_id == "040_task"
    regressed = compare_end_states("040_task", {"status": "New"}, {"status": "Done"})
    assert regressed.regressed and len(regressed.diffs) == 1


def test_diff_str_is_readable() -> None:
    d = diff_states({"status": "New"}, {"status": "Done"})[0]
    assert str(d) == "status: 'New' -> 'Done'"


def test_extra_ignore() -> None:
    # A caller can drop additional noisy keys (e.g. a computed display string).
    a = {"status": "New", "display": "foo"}
    b = {"status": "New", "display": "bar"}
    assert diff_states(a, b) != []
    assert diff_states(a, b, extra_ignore=frozenset({"display"})) == []
