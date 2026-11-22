"""Did untrusted content enter this run, and does the gate know?

Fencing (``security.prompt_delimit``) reduces the odds the model is steered by
attacker-written text. It cannot reduce the BLAST RADIUS when it is. Today a
run that read a tenant-authored work-order description reaches
``confirm_or_abort`` with exactly the same title, details and role floor as one
that read nothing but our own rollups — the gate is keyed to the ACTION
(payment / charge / close, per gotcha 21) and has never been keyed to the run's
input PROVENANCE.

So: one flag per run, raised the moment any fenced payload is produced, read by
the write path.

**What it changes, deliberately narrowly.** A tainted run's confirm card says
so, in the card the human actually reads — that is the control, because the
human is the one who can tell "email the owner list" from something they asked
for. It does NOT block, and it does not silently alter the write.

⚠ **The flag is a MUTABLE OBJECT, not a ``ContextVar[bool]``, and that is the
whole reason this file is not three lines.** A specialist consult runs as a
Task, a Task COPIES the context, and so a ``ContextVar.set(True)`` inside the
sub-run is invisible to the parent (gotcha 42, measured — the same mechanism
that made ``LoopStats`` a mutated object rather than a re-set variable). The
buildium specialist is where nearly all Buildium free text is read, so a
naive bool would have been raised in exactly the place it could never be seen
from, and the gate would have read False on every genuinely tainted run: a
control that looks present and is absent, which is the shape this whole audit
kept finding.

⚠ It is also raised inside ``wrap_untrusted`` itself rather than at each call
site. Fencing a NEW surface then taints automatically — nobody has to remember
two edits — and the two can never drift into disagreeing about what counts as
untrusted, which is gotcha 25's rule applied to a security marker.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class RunTaint:
    """Whether this run has read externally-authored content, and from where.

    Mutated in place so the mark survives the ``.as_tool()`` Task boundary.
    """

    #: Labels passed to ``wrap_untrusted`` — "task description, written by the
    #: requester", "inbound email body", "web page". Kept so the confirm card
    #: can say WHICH source, not merely that something was untrusted: "this
    #: run read a tenant-written work-order description" is actionable, "this
    #: run is tainted" is not.
    sources: set[str] = field(default_factory=set)

    @property
    def tainted(self) -> bool:
        return bool(self.sources)

    def mark(self, label: str) -> None:
        # Bounded: a run that reads fifty fenced fields must not grow an
        # unbounded set that then renders into a confirm card the human
        # cannot read. The first few sources are the informative ones.
        if len(self.sources) < 8:
            self.sources.add((label or "untrusted content").strip()[:80])


_taint: ContextVar[RunTaint | None] = ContextVar("helixis_run_taint", default=None)


def bind_run_taint() -> RunTaint:
    """Start a fresh taint record for this run and return it.

    Bound beside the call ledger and the confirm emitter in
    ``run_agent_streaming``. Returns the object so a caller can read it after
    the run without going back through the ContextVar.
    """
    record = RunTaint()
    _taint.set(record)
    return record


def current_run_taint() -> RunTaint | None:
    return _taint.get()


def mark_untrusted_read(label: str) -> None:
    """Record that fenced content was produced in this run.

    A no-op when nothing is bound — the planner runs in the worker with no
    agent run around it, and the eval harness builds prompts directly. Never
    raises: a security MARKER that can break a turn would be removed the first
    time it did.
    """
    try:
        record = _taint.get()
        if record is not None:
            record.mark(label)
    except Exception:  # noqa: BLE001 — a marker must never cost the turn
        logger.debug("taint mark failed", exc_info=True)


def taint_provenance_note() -> str | None:
    """One line for the confirm card, or ``None`` for an untainted run.

    ⚠ Written for the HUMAN approving the write, not for the model. Gotcha 39
    is the lesson: a message aimed at the wrong reader is worse than none —
    the specialist there was told "the next thing you produce must be the
    ANSWER", an instruction only the orchestrator could carry out. The person
    reading a confirm card can do exactly one useful thing with this, which is
    ask whether they requested this action, so that is what it says.
    """
    record = _taint.get()
    if record is None or not record.tainted:
        return None
    sources = ", ".join(sorted(record.sources))
    return (
        "⚠ This run read text written by someone outside your company "
        f"({sources}). That text cannot instruct Occupella, but check that "
        "this action is one YOU asked for before approving."
    )


__all__ = [
    "RunTaint",
    "bind_run_taint",
    "current_run_taint",
    "mark_untrusted_read",
    "taint_provenance_note",
]
