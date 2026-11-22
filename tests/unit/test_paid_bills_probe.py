"""The paid-bills probe shipped with three defects. This is the floor.

A parallel session ran it against Blue Ridge and it answered nothing. All three
faults were in the probe, not the account, and each one is the kind that reads
as a result rather than as a break:

1. **The per-company credential fix was defeated.** ``load_buildium_settings_
   for_company`` needs a DB pool and the probe never opened one, so it raised
   "Postgres pool not initialized" on EVERY call, the ``except`` swallowed it
   as "no stored row is a normal answer", and it fell back to env every single
   time. The comment above it described the bug the code still had.
2. **A crash on the error path.** ``_probe_window``'s error return omitted
   ``confirmed_paid`` while the roll-up read it unconditionally, so ONE failed
   window raised ``KeyError`` — and the docstring promises an honest exit 2
   where that path delivered exit 1 and a traceback.
3. **An unevenly-applied caveat.** The env-credential warning reached one of
   four verdict branches. A qualifier that applies to some verdicts and not
   others is worse than none, because the uncaveated ones read as trustworthy.

⚠ The through-line: every one of these fails *quietly and plausibly*. The probe
still printed windows, still printed a verdict, still exited. Only running it
against a real account surfaced them — which is why this file exercises the
pure paths that do not need credentials, so the next break costs a test rather
than a day with no API access left.
"""

from __future__ import annotations

import datetime
import inspect

from scripts.paid_bills_probe import _probe_window, _slices, _verdict, probe

_CASES = [
    (0, 0, []),  # nothing found
    (0, 0, [{"error": "422"}]),  # every window errored
    (10, 3, []),  # rows returned, some unconfirmed
    (10, 10, []),  # real paid bills found
]


# ── defect 2: the error path must not crash the roll-up ───────────────


def test_both_return_paths_carry_every_key_the_rollup_reads() -> None:
    """The roll-up sums ``count`` and ``confirmed_paid`` across ALL windows,
    including errored ones. A key present on only the success path is a
    KeyError that fires exactly when something else has already gone wrong."""
    src = inspect.getsource(_probe_window)
    for key in ("count", "confirmed_paid", "priced", "total"):
        assert src.count(f'"{key}"') >= 2, (
            f"{key!r} appears on only one return path of _probe_window — a "
            "window failure will raise KeyError in the summary"
        )


# ── defect 3: the caveat is all-or-nothing ────────────────────────────


def test_every_verdict_is_caveated_when_read_with_env_credentials() -> None:
    for found, confirmed, errored in _CASES:
        verdict = _verdict(found, confirmed, errored, source="env")
        assert "ENV credentials" in verdict, (
            f"verdict for found={found} is NOT caveated under env credentials — "
            "it reads as attributable to the named company when it is not"
        )


def test_no_verdict_is_caveated_when_read_with_company_credentials() -> None:
    """The other direction. A warning on every verdict is a warning on none."""
    for found, confirmed, errored in _CASES:
        assert "ENV credentials" not in _verdict(found, confirmed, errored, source="company")


def test_the_four_verdicts_are_actually_distinct() -> None:
    """They exist to separate 'genuinely none' from 'window' from 'erroring'.
    If two collapsed, the probe would run and distinguish nothing — which is
    what it did on its first outing."""
    seen = {_verdict(f, c, e, source="company") for f, c, e in _CASES}
    assert len(seen) == len(_CASES), "two verdict branches produce identical text"


# ── defect 1: the company path must be reachable ──────────────────────


def test_the_company_credential_path_opens_a_pool() -> None:
    """Without this the ``--company-id`` flag only LABELS the output.

    An AST-free source check is enough here and deliberately so: the failure
    was a MISSING call, and the cheapest true statement about a missing call is
    that its name does not appear.
    """
    src = inspect.getsource(probe)
    assert "init_pool" in src, (
        "probe() no longer opens a pool, so load_buildium_settings_for_company "
        "raises on every call and --company-id silently reverts to env creds"
    )
    assert "pool.close()" in src, "the pool is opened and never closed"


def test_the_env_fallback_is_still_reachable() -> None:
    """Both directions: the fix must not make a credential-less run crash.
    A probe that cannot fall back is a probe that cannot run on a fresh
    checkout, which is where it will next be used (a brand-new account)."""
    src = inspect.getsource(probe)
    assert 'source = "env"' in src
    assert "settings.buildium" in src


# ── the window walk, which nothing else covers ────────────────────────


def test_windows_walk_backward_without_overlapping() -> None:
    """Overlapping windows would double-count a paid bill and turn a clean
    'none found' into a fabricated number.

    ⚠ The first version of this test destructured ``(_, newer_start)`` from a
    ``(start, end)`` tuple, so it compared the older window's end against the
    newer window's END — always true, and blind to overlap. A mutation that
    made the windows overlap by 30 days passed it. Bind the fields by name.
    """
    today = datetime.date(2026, 8, 30)
    windows = _slices(3, today)
    assert windows[0][1] == today
    for newer, older in zip(windows, windows[1:], strict=False):
        newer_start = newer[0]
        older_start, older_end = older
        assert older_end < newer_start, (
            f"windows overlap: {older_start}..{older_end} runs past {newer_start}, "
            "so a bill paid in the overlap is counted twice"
        )
        assert older_start < older_end


def test_at_least_one_window_even_for_zero_years() -> None:
    """`--years 0` must not silently probe nothing and report 'no paid bills' —
    that would be a fabricated absence, this product's worst failure mode."""
    assert len(_slices(0, datetime.date(2026, 8, 30))) == 1
