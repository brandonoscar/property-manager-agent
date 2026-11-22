"""The sweep's case ORDER, which decides what a timed-out sweep never measures.

`_load_cases` returns a stable `sorted()` name order. That is right for a
reproducible run and wrong for a sweep that gets killed at a timeout: the cut
lands in the same place every time, so the tail of the corpus is never
measured — and because a case that never ran prints nothing, the nightly
report could not distinguish it from a pass (see
`scripts/eval_flake_tracker.py::corpus_case_ids`, the reporting half).

Measured on main 2026-09-11..14: 3 of 3 sweeps hit the per-sweep cap every
night while the corpus grew by exactly one case (162 -> 163), so the suite did
not outgrow its budget — it stopped in the same spot. The tail is the
release-safety series (41x fair-housing, 42x cross-tenant, 43x
prompt-injection, 45x comms, 47x honesty).
"""

from __future__ import annotations

import random

from helixis.evals.runner import _apply_case_order

_CASES = [{"id": f"{i:03d}_case"} for i in range(1, 41)]


def _ids(cases):
    return [c["id"] for c in cases]


def test_order_is_untouched_when_the_env_var_is_unset(monkeypatch):
    """OFF by default. A PR run, a `--case` grid and a local reproduction all
    stay byte-for-byte deterministic; only the nightly asks for rotation."""
    monkeypatch.delenv("HELIXIS_EVAL_SHUFFLE", raising=False)
    assert _apply_case_order(_CASES) == _CASES


def test_blank_and_whitespace_are_also_off(monkeypatch):
    monkeypatch.setenv("HELIXIS_EVAL_SHUFFLE", "   ")
    assert _apply_case_order(_CASES) == _CASES


def test_shuffling_keeps_every_case_exactly_once(monkeypatch):
    """Rotation must move the cut, never shrink the suite. A shuffle that
    dropped or duplicated a case would buy coverage with coverage."""
    monkeypatch.setenv("HELIXIS_EVAL_SHUFFLE", "auto")
    out = _apply_case_order(_CASES)
    assert sorted(_ids(out)) == sorted(_ids(_CASES))
    assert len(out) == len(_CASES)


def test_a_plain_truthy_value_picks_a_fresh_seed_each_run(monkeypatch):
    """THE trap this guard exists for. If `HELIXIS_EVAL_SHUFFLE=1` were read as
    "seed 1", every night would draw the identical permutation and the fixed
    tail would come straight back — the original bug wearing the fix's name.
    Only an explicit `seed:` prefix reproduces."""
    monkeypatch.setenv("HELIXIS_EVAL_SHUFFLE", "1")
    orders = {tuple(_ids(_apply_case_order(_CASES))) for _ in range(8)}
    assert len(orders) > 1, "a truthy value must not pin one permanent order"


def test_seed_prefix_reproduces_the_same_order(monkeypatch):
    """The printed line promises reproducibility; this is that promise."""
    monkeypatch.setenv("HELIXIS_EVAL_SHUFFLE", "seed:4242")
    first = _ids(_apply_case_order(_CASES))
    second = _ids(_apply_case_order(_CASES))
    assert first == second


def test_different_seeds_give_different_orders(monkeypatch):
    monkeypatch.setenv("HELIXIS_EVAL_SHUFFLE", "seed:1")
    a = _ids(_apply_case_order(_CASES))
    monkeypatch.setenv("HELIXIS_EVAL_SHUFFLE", "seed:2")
    b = _ids(_apply_case_order(_CASES))
    assert a != b


def test_a_malformed_seed_still_shuffles_rather_than_crashing(monkeypatch):
    """A typo'd seed must cost reproducibility, never the night's run."""
    monkeypatch.setenv("HELIXIS_EVAL_SHUFFLE", "seed:not-a-number")
    out = _apply_case_order(_CASES)
    assert sorted(_ids(out)) == sorted(_ids(_CASES))


def test_shuffling_does_not_disturb_the_global_random_stream(monkeypatch):
    """The agent and tool code under test may use `random` itself. Seeding the
    GLOBAL stream here would silently make some other component deterministic
    (or not) as a side effect of an unrelated env var."""
    monkeypatch.setenv("HELIXIS_EVAL_SHUFFLE", "seed:7")
    random.seed(12345)
    expected = [random.random() for _ in range(3)]
    random.seed(12345)
    _apply_case_order(_CASES)
    assert [random.random() for _ in range(3)] == expected


def test_the_printed_line_names_the_string_that_reproduces_it(monkeypatch, capsys):
    """A seed you cannot type back is not reproducibility. The line has to
    carry the exact env value, not just the number."""
    monkeypatch.setenv("HELIXIS_EVAL_SHUFFLE", "seed:99")
    _apply_case_order(_CASES)
    assert "HELIXIS_EVAL_SHUFFLE=seed:99" in capsys.readouterr().out
