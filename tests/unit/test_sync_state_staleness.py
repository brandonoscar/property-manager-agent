"""A stranded ``syncing`` row must stop claiming progress it isn't making.

``run_initial_sync`` is fired and forgotten from ``POST /buildium/test``, so a
process killed mid-crawl never reaches ``mark_done`` or ``mark_failed`` and the
row sits at ``syncing`` for the life of the account. The banner that polls
``GET /buildium/sync-status`` would then say "still syncing…" forever.

⚠ Both directions, every time (gotcha 38). Calling a SLOW sync dead is a fresh
false claim about every big account's first crawl, and it is the failure mode
that would make someone delete this check rather than fix it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from helixis.db.repositories.sync_state import (
    STALE_AFTER_SECONDS,
    SyncState,
    is_stale,
)

_NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


def _state(status: str, started_delta_s: float | None) -> SyncState:
    started = None if started_delta_s is None else _NOW - timedelta(seconds=started_delta_s)
    return SyncState(
        company_id="c1",
        status=status,
        started_at=started,
        finished_at=None,
        counts={},
        last_error=None,
    )


# ── the thing it must catch ─────────────────────────────────────────────────


def test_a_syncing_row_older_than_the_threshold_is_stale() -> None:
    assert is_stale("syncing", _NOW - timedelta(seconds=STALE_AFTER_SECONDS + 1), now=_NOW)


# ── the things it must spare ────────────────────────────────────────────────


def test_a_sync_that_is_merely_slow_is_not_stale() -> None:
    """The false-positive direction, and the one that matters most.

    A large account's first crawl is many sequential paged reads. Reporting it
    dead while it is still working would be gotcha 41's confident false claim,
    pointed at the new-customer onboarding screen.
    """
    assert not is_stale("syncing", _NOW - timedelta(seconds=STALE_AFTER_SECONDS - 60), now=_NOW)


def test_the_threshold_boundary_is_not_stale() -> None:
    """Exactly at the limit is still running — strictly greater, like
    ``_truncate_footer``'s "exactly at the limit is not truncated"."""
    assert not is_stale("syncing", _NOW - timedelta(seconds=STALE_AFTER_SECONDS), now=_NOW)


def test_terminal_states_are_never_stale() -> None:
    """``done`` and ``failed`` already finished. An old ``done`` row is a
    company that synced last month, not a problem."""
    old = _NOW - timedelta(days=30)
    for status in ("done", "failed", "pending", "unknown"):
        assert not is_stale(status, old, now=_NOW), status


def test_a_row_with_no_start_time_is_not_stale() -> None:
    """Nothing to measure against. Guessing here would flag every row that
    predates ``started_at`` being written."""
    assert not is_stale("syncing", None, now=_NOW)


# ── the wire shape ──────────────────────────────────────────────────────────


def test_to_public_reports_stale_without_touching_status() -> None:
    """``stale`` is ADDITIVE. ``status`` keeps its stored value so a client
    reading only ``status`` behaves exactly as it did before."""
    out = _state("syncing", STALE_AFTER_SECONDS + 60).to_public(now=_NOW)
    assert out["status"] == "syncing", "status must not be rewritten"
    assert out["stale"] is True


def test_to_public_on_a_healthy_sync() -> None:
    out = _state("syncing", 60).to_public(now=_NOW)
    assert out["status"] == "syncing"
    assert out["stale"] is False


def test_a_naive_timestamp_does_not_raise() -> None:
    """asyncpg can hand back a naive datetime. Comparing naive to aware raises
    TypeError, which on this read path would 500 the banner instead of
    degrading it — so the comparison normalises both sides."""
    naive = SyncState(
        company_id="c1",
        status="syncing",
        started_at=datetime(2026, 9, 2, 10, 0),  # noqa: DTZ001 — the point
        finished_at=None,
        counts={},
        last_error=None,
    )
    assert naive.to_public(now=_NOW)["stale"] is True


def test_every_endpoint_branch_returns_the_same_keys() -> None:
    """Reads BOTH sides (gotcha 28): the ``unknown`` early-returns in the route
    are hand-built dicts, not ``to_public()`` output, so they drift silently.
    A client switching on ``stale`` would get ``undefined`` from one branch and
    a boolean from another — the producer/consumer split that took chat down
    in gotcha 28.
    """
    import ast
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[2] / "src" / "helixis" / "api" / "routes" / "buildium.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)

    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef) and n.name == "sync_status"
    )
    literal_returns = [
        node.value
        for node in ast.walk(fn)
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict)
    ]
    assert literal_returns, "no dict literal returns found — scanner broken or route rewritten"

    expected = set(
        SyncState("c", "unknown", None, None, {}, None).to_public(now=_NOW),
    )
    for dict_node in literal_returns:
        keys = {k.value for k in dict_node.keys if isinstance(k, ast.Constant)}
        assert keys == expected, (
            f"a sync-status branch returns {sorted(keys)} but to_public() returns "
            f"{sorted(expected)} — the banner would read a missing key as undefined"
        )


def test_counts_survive_a_json_string_column() -> None:
    """``counts`` arrives as jsonb; asyncpg may hand it back as text. The
    banner renders these numbers, so a string would show as characters."""
    parsed = json.loads('{"properties": 12}')
    out = SyncState("c", "done", None, None, parsed, None).to_public(now=_NOW)
    assert out["counts"]["properties"] == 12
