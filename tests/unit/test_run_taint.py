"""An injected run is told apart from an ordinary one, at the write gate.

Fencing lowers the odds the model is steered by attacker-written text. It
cannot lower the BLAST RADIUS when it is. A run that read a tenant-authored
work-order description reached ``confirm_or_abort`` with exactly the same
title, details and role floor as one that read only our own rollups — the gate
is keyed to the ACTION (payment / charge / close, gotcha 21) and never to the
run's input provenance.

⚠ **The Task boundary is the whole reason this is an object and not a bool.**
A specialist consult runs as a Task, a Task COPIES the context, so a
``ContextVar.set(True)`` inside the sub-run is invisible to the parent
(gotcha 42, measured). The buildium specialist is where nearly all Buildium
free text is read — so a naive bool would have been raised in exactly the place
the gate could never see it from, and would have read False on every genuinely
tainted run. A control that looks present and is absent.

That test runs its OWN ``asyncio.run`` and is mutation-verified against the
bool implementation, because a test that cannot observe the failure is not
covering it (gotcha 36).
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar

from helixis.agents.taint import (
    bind_run_taint,
    current_run_taint,
    mark_untrusted_read,
    taint_provenance_note,
)
from helixis.security.prompt_delimit import wrap_untrusted

# ── the flag itself ──────────────────────────────────────────────────


def test_a_clean_run_is_not_tainted() -> None:
    """Both directions, and this is the one that matters commercially: most
    runs read only our own rollups, and a note on every confirm card is a note
    nobody reads."""
    bind_run_taint()
    assert taint_provenance_note() is None


def test_an_unbound_context_is_silent() -> None:
    """The planner runs in the worker with no agent run around it, and the
    eval harness builds prompts directly. Neither may raise, and neither
    should mark anything."""
    ContextVar("throwaway")  # keep the import honest about what this exercises
    from helixis.agents import taint

    taint._taint.set(None)
    mark_untrusted_read("web page")
    assert taint_provenance_note() is None


def test_fencing_content_taints_the_run() -> None:
    """THE wiring. Raised inside ``wrap_untrusted`` so fencing a new surface
    marks the run automatically — the fence and the marker cannot drift into
    disagreeing about what counts as untrusted."""
    bind_run_taint()
    wrap_untrusted("water everywhere", label="task description, written by the requester")
    note = taint_provenance_note()
    assert note is not None
    assert "task description" in note


def test_the_note_names_the_source() -> None:
    """ "This run is tainted" is not actionable; "this run read a tenant-written
    work-order description" is. Gotcha 39's lesson — a message has to be
    written for whoever actually reads it, and the person at a confirm card can
    do exactly one thing with this."""
    bind_run_taint()
    wrap_untrusted("x", label="inbound email body")
    wrap_untrusted("y", label="web page")
    note = taint_provenance_note()
    assert note is not None
    assert "inbound email body" in note
    assert "web page" in note


def test_the_source_list_is_bounded() -> None:
    """A run that reads fifty fenced fields must not grow a note the human
    cannot read — an unreadable warning is the same as no warning."""
    bind_run_taint()
    for i in range(50):
        wrap_untrusted("x", label=f"source {i}")
    record = current_run_taint()
    assert record is not None
    assert len(record.sources) <= 8


def test_marking_never_raises() -> None:
    """A security MARKER that can break a turn gets removed the first time it
    does. Junk in, no exception out."""
    bind_run_taint()
    mark_untrusted_read("")
    mark_untrusted_read("x" * 5000)
    assert taint_provenance_note() is not None


# ── the Task boundary — the load-bearing property ────────────────────


def test_a_mark_made_inside_a_task_is_visible_to_the_parent() -> None:
    """⚠ THE reason this is a mutable object.

    ``.as_tool()`` runs a specialist as a Task and a Task copies the context,
    so a ``ContextVar.set`` inside is invisible outside. Nearly all Buildium
    free text is read inside the buildium specialist, so a bool flag would
    have been raised exactly where the gate could never see it.

    Runs its own ``asyncio.run`` and asserts from OUTSIDE the task, which is
    the only vantage point that can tell the two implementations apart
    (gotcha 36's rule: a test that cannot observe the failure is not covering
    it).
    """

    async def _specialist_consult() -> None:
        # Exactly what a specialist does: fences a Buildium field it read.
        wrap_untrusted("leak in unit 4B", label="task description, written by the requester")

    async def _run() -> str | None:
        bind_run_taint()
        await asyncio.create_task(_specialist_consult())  # the .as_tool() shape
        return taint_provenance_note()  # read from the PARENT context

    note = asyncio.run(_run())
    assert note is not None, (
        "a mark made inside a specialist sub-run did not reach the parent — "
        "the gate would read clean on every genuinely tainted run"
    )
    assert "task description" in note


def test_two_runs_do_not_share_a_record() -> None:
    """Bound per run. One tenant's fenced content must not taint the next
    manager's confirm card — a warning that appears on unrelated runs is how a
    real signal gets ignored."""

    async def _first() -> None:
        bind_run_taint()
        wrap_untrusted("x", label="web page")
        assert taint_provenance_note() is not None

    async def _second() -> str | None:
        bind_run_taint()
        return taint_provenance_note()

    asyncio.run(_first())
    assert asyncio.run(_second()) is None


# ── it reaches the confirm card ──────────────────────────────────────


def test_the_confirm_gate_reads_the_provenance() -> None:
    """Reads the real call site: a flag nothing consults is worth nothing.
    Also pins that it is ADDITIVE — the gate must not start blocking, because
    a refusal on taint would break legitimate work on most runs (almost any
    run that reads a work-order description)."""
    import ast
    import inspect

    from helixis.agents import tools

    src = inspect.getsource(tools.confirm_or_abort)
    assert "taint_provenance_note" in src
    tree = ast.parse(inspect.cleandoc(src))
    returns_false_on_taint = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.If) and "taint" in ast.unparse(n.test) and "return" in ast.unparse(n)
    ]
    assert not returns_false_on_taint, (
        "the gate refuses on taint — that blocks legitimate work on nearly "
        "every run and the control gets switched off"
    )
