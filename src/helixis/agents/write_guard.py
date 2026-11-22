"""A human-approved write must not be interrupted by our own deadline.

The soft-deadline stop is right for READS — stop looking things up, write an
answer from what you have (gotcha 37's 40% recovery). It is wrong the moment a
person has approved a confirm gate, because the one window you must never cut
is between the approval and the write completing.

Measured 2026-08-30, the reversible tier of the write harness. All three writes
came back ``abandoned_unknown / ambiguous:RuntimeError``:

    error_type  SoftDeadlineSynthesized
    err         "wall-clock deadline 240s exceeded (stopped at the 168s soft
                 cutoff to write an answer)"

168s is ``SOFT_DEADLINE_FRACTION * 240``. The stop fired while ``run_guarded``
was suspended in ``await execute()``, its ``except Exception`` reaped the claim
as ambiguous, and ``abandoned_unknown`` is **never retried by design** — a
duplicate write in a landlord's records is worse than a missing one. So the
operator was left unable to say whether a move-out had been recorded against a
real tenant's lease, and the harness's own undo could not clean up after it.

WHAT THIS DOES, AND WHAT IT DELIBERATELY DOES NOT
-------------------------------------------------
While a write is in flight the SOFT stops are suppressed: the cost ceiling, the
refusal ceiling, and the soft wall clock. Those all exist to end a run that is
*reading* unproductively, and none of their reasoning survives contact with a
half-executed write.

The HARD wall clock is NOT suppressed, only stretched. A genuinely wedged write
still has to die, or one hung HTTP call pins a worker forever — the same
"stretched, not removed" bargain the watchdog already strikes for a tool in
flight. The stretch is bounded and the hard path still raises.

⚠ **The marker must span the AWAIT, not the dispatch** (gotcha 39). The write
happens while the coroutine is SUSPENDED; a flag set and cleared around the
call is false for the entire window it exists to describe.

⚠ **The state is a MUTABLE dict reached through a ContextVar, never a plain
``ContextVar.set``** (gotchas 34/42). ``run_guarded`` may execute inside a
nested ``.as_tool()`` sub-run, and a Task COPIES the context — a ``set`` in
there is invisible to the watchdog polling in the parent. Mutating a shared
dict crosses that boundary; rebinding does not.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator
from contextvars import ContextVar, Token
from typing import Any

logger = logging.getLogger(__name__)

#: How much longer than the hard deadline a run may take when a write is in
#: flight. Bounded on purpose: a write deserves room to finish, not immunity.
WRITE_GRACE_MULTIPLIER = 1.5

_write_state: ContextVar[dict[str, Any] | None] = ContextVar("helixis_write_state", default=None)


def bind_write_marker() -> tuple[dict[str, Any], Token]:
    """Bind a per-run marker. Returns the SHARED dict and its reset token.

    The caller keeps the dict and hands it (or an accessor over it) to the
    watchdog, so both sides read the same object rather than two copies.
    """
    state: dict[str, Any] = {"in_flight": 0}
    return state, _write_state.set(state)


def unbind_write_marker(token: Token) -> None:
    with contextlib.suppress(ValueError):
        _write_state.reset(token)


def write_in_flight() -> bool:
    """True while at least one approved write is executing."""
    state = _write_state.get()
    return bool(state and state.get("in_flight", 0) > 0)


@contextlib.contextmanager
def executing_write(action_type: str) -> Iterator[None]:
    """Mark an approved write as in flight for the whole of ``execute()``.

    A COUNTER rather than a boolean: the SDK runs a turn's tool calls
    concurrently (gotcha 25), so two gated writes can overlap and a boolean
    would be cleared by whichever finished first, un-protecting the other
    mid-flight.

    Unbound (tests, the webhook worker, any path with no orchestrator run) this
    is a no-op — the marker simply does not exist and nothing changes.
    """
    state = _write_state.get()
    if state is None:
        yield
        return
    state["in_flight"] = state.get("in_flight", 0) + 1
    # Recorded for the run row: a run that was extended should say so, or the
    # next person reading a 300s latency has no way to tell a protected write
    # from a regression.
    state["protected"] = sorted({*state.get("protected", []), action_type})
    try:
        yield
    finally:
        state["in_flight"] = max(0, state.get("in_flight", 0) - 1)


__all__ = [
    "WRITE_GRACE_MULTIPLIER",
    "bind_write_marker",
    "executing_write",
    "unbind_write_marker",
    "write_in_flight",
]
