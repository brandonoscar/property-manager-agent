""" "Connected" has to mean the keys work, not that a row exists.

The Buildium connector card said **Connected** whenever
``company_buildium_credentials`` had a row for the company. The variable was
even named ``buildium_configured`` and was assigned straight into
``is_connected``. So the card read Connected when the keys were mistyped,
revoked in Buildium, expired, or undecryptable after a data-key rotation —
every state in which the product cannot do a single thing the card promises.

This is the same error three times now, and worth naming as a class:

* gotcha 70 — a 2xx is not the write;
* gotcha 67 — ``configured`` ("credentials exist") is not ``enabled``
  ("we are allowed to send");
* here — "we stored a secret" is not "the secret works".

Each time, a fact we happened to have stood in for the fact we needed.

The fix uses evidence by EFFECT, which costs no API call on page load:
``last_tested_at`` is written only on a SUCCESSFUL credential test (and NULLed
when the keys are replaced), and a mirror sync timestamp exists only because a
fetch actually returned data. Either one, inside the window, proves the keys
worked.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from helixis.api.routes.connectors import (
    _PMS_PROOF_WINDOW,
    pms_connection_state,
)

_NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


def _state(**kw):
    kw.setdefault("last_tested_at", None)
    kw.setdefault("last_sync_at", None)
    return pms_connection_state(now=_NOW, **kw)


# ── the defect ────────────────────────────────────────────────────────


def test_stored_keys_that_never_worked_do_not_read_as_connected() -> None:
    """The bug, stated directly: a credentials row on its own is not a
    connection. This is the state a revoked or mistyped key lands in."""
    connected, needs_reauth = _state(configured=True)
    assert connected is False, (
        "a stored credentials row is being reported as Connected -- that is true "
        "of keys that are revoked, expired, mistyped or undecryptable"
    )
    assert needs_reauth is True, "the card should offer Reconnect, not Connect"


def test_keys_that_stopped_working_stop_reading_as_connected() -> None:
    """The live case: it worked once, then the key was revoked in Buildium and
    every sync since has failed. Proof ages out; the card follows."""
    stale = _NOW - _PMS_PROOF_WINDOW - timedelta(days=1)
    connected, needs_reauth = _state(configured=True, last_tested_at=stale, last_sync_at=stale)
    assert connected is False
    assert needs_reauth is True


def test_a_months_old_proof_is_not_proof() -> None:
    """⚠ Pins the POLICY with an absolute age, not one derived from the
    constant.

    The test above computes its input as ``window + 1 day``, so widening the
    window moves its own input along with it — mutation confirmed that setting
    the window to 100000 days left the whole file green. A test whose input
    tracks the value under test can only ever agree with itself (gotcha 60's
    shape: a decision taken from a measurement needs a test that re-measures).

    Six months is unambiguous. A key that has not demonstrably worked since
    then must not read as Connected whatever the window is tuned to.
    """
    ancient = _NOW - timedelta(days=180)
    connected, needs_reauth = _state(configured=True, last_tested_at=ancient, last_sync_at=ancient)
    assert connected is False, (
        "the proof window is wide enough that a six-month-old credential test "
        "still reads as Connected -- that is the original bug with extra steps"
    )
    assert needs_reauth is True


# ── the other direction, which matters just as much ───────────────────


@pytest.mark.parametrize("proof", ["last_tested_at", "last_sync_at"])
def test_either_proof_alone_is_enough(proof: str) -> None:
    """⚠ Two INDEPENDENT proofs, and either suffices. A company that connected
    long ago and never re-ran the manual test is proved by its syncs; one that
    just tested and has not synced yet is proved by the test. Requiring both
    would report healthy accounts as broken."""
    connected, needs_reauth = _state(configured=True, **{proof: _NOW - timedelta(hours=2)})
    assert connected is True
    assert needs_reauth is False


def test_a_working_connection_is_never_told_to_reconnect() -> None:
    """⚠ gotcha 55, pointed at this page. Turning an over-claim into a false
    alarm is not an improvement -- it moves the lie. The window is generous
    precisely so a healthy account cannot trip it."""
    just_inside = _NOW - _PMS_PROOF_WINDOW + timedelta(hours=1)
    connected, _ = _state(configured=True, last_sync_at=just_inside)
    assert connected is True


def test_no_credentials_is_connect_not_reconnect() -> None:
    """A company that has never connected must be offered Connect. Reconnect
    implies something to restore and is a confusing first-run state."""
    assert _state(configured=False) == (False, False)


def test_no_credentials_stays_disconnected_even_with_stray_proof() -> None:
    """⚠ Ordering guard. If the row is gone the connection is gone, whatever
    stale sync rows survive it -- a purge deletes credentials, and mirror
    freshness outliving them must not resurrect a Connected card."""
    assert _state(configured=False, last_sync_at=_NOW) == (False, False)


# ── the trap that would 500 a page load ───────────────────────────────


def test_a_naive_timestamp_does_not_raise() -> None:
    """⚠ Comparing a naive datetime against an aware one raises TypeError, and
    this runs on a page load that must never 500. A naive stamp is read as UTC
    rather than dropped: discarding it would flag a working account, which is
    the failure this whole guard exists to avoid."""
    connected, _ = _state(configured=True, last_tested_at=_NOW.replace(tzinfo=None))
    assert connected is True


def test_the_two_states_are_mutually_exclusive() -> None:
    """Connected and needs-reauth must never both be true: the settings card
    only renders the Reconnect affordance when NOT connected, so a both-true
    result would silently render as plain Connected and lose the warning."""
    for configured in (True, False):
        for stamp in (None, _NOW, _NOW - timedelta(days=400)):
            connected, needs_reauth = _state(configured=configured, last_sync_at=stamp)
            assert not (connected and needs_reauth)
