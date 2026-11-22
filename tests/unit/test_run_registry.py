"""Session run-lock registry (live-watch PR) — one driving run per thread."""

from __future__ import annotations

from helixis.streaming import run_registry


def test_claim_release_lifecycle() -> None:
    sid = "test-session-registry-1"
    try:
        assert not run_registry.is_active(sid)
        assert run_registry.claim(sid) is True
        assert run_registry.is_active(sid)
        # Second claim while held → refused (the 409 path).
        assert run_registry.claim(sid) is False
        run_registry.release(sid)
        assert not run_registry.is_active(sid)
        # Reclaimable after release.
        assert run_registry.claim(sid) is True
    finally:
        run_registry.release(sid)


def test_release_tolerates_unheld_session() -> None:
    # Direct run_agent_streaming callers (evals) never claim — release of an
    # unheld id must be a no-op, not an error.
    run_registry.release("never-claimed-session")
