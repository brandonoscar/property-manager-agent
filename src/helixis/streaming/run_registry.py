"""In-process registry of sessions with an agent run in flight.

The live-watch PR makes a session's stream shareable (bus fan-out), which
exposes a race the single-player UI used to hide: nothing stopped two users
(or one user double-submitting) from POSTing ``/agent/run`` with the same
``session_id`` concurrently — two orchestrator runs would interleave events
into one stream and their turns into one transcript. The route claims the
session here before spawning the run and 409s when it's already held, so a
thread has AT MOST one driving run at a time; teammates join it via
``GET /agent/watch`` instead of starting a second run.

In-process on purpose: the deployment is a single web instance (one uvicorn
worker), and the claim window (check → create_task) has no ``await``, so the
claim is atomic under asyncio. The durable ``orchestrator_runs.status``
row remains the cross-process/audit view; this set is the fast live lock.
Release is wired as a done-callback on the run task, so it fires on success,
exception, AND cancellation — a crashed run can't wedge its thread.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_active: set[str] = set()


def claim(session_id: str) -> bool:
    """Claim ``session_id`` for a new run. False if a run already holds it."""
    if session_id in _active:
        return False
    _active.add(session_id)
    return True


def release(session_id: str) -> None:
    """Release a claim. Tolerates an unheld id (direct callers never claim)."""
    _active.discard(session_id)


def is_active(session_id: str) -> bool:
    return session_id in _active


__all__ = ["claim", "is_active", "release"]
