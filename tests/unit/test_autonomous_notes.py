"""Guards for the autonomous-note path (notes/writer.py + notes/runner.py).

``notes/policy.py`` shipped with both live seams as unbound Protocols, so the
whole module was reachable from nothing. These guards cover the binding, and
in particular the three ways this feature could quietly become dangerous:

- shipping ON (it must be dark, and the DEFAULT is the thing to assert);
- inventing a Buildium endpoint for an entity kind nobody probed;
- an unattended full-replace PUT into a live record.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from helixis.notes.policy import EntityRef, NoteCandidate, NoteClass
from helixis.notes.runner import NOTE_ACTION_TYPE, run_note_pass
from helixis.notes.writer import (
    AUTONOMOUS_KINDS,
    WRITABLE_KINDS,
    BuildiumNoteWriter,
    UnsupportedNoteTargetError,
)
from helixis.settings import NotesSettings

COMPANY = "5ca1ab1e-0000-4000-8000-000000000002"


def _candidate(kind: str = "vendor", text: str = "Owner won't cover electrical in 2026"):
    return NoteCandidate(
        note_class=NoteClass.FINANCIAL_RESPONSIBILITY,
        text=text,
        target=EntityRef(kind=kind, id=4610),
        source="email from owner 2026-08-29",
        confidence=0.95,
    )


def _store(won: bool = True) -> MagicMock:
    store = MagicMock()
    store.claim = AsyncMock(return_value=MagicMock(won=won, row=MagicMock()))
    store.finalize_success = AsyncMock()
    store.finalize_retryable = AsyncMock()
    return store


def _writer(handle: str = "vendor:4610") -> MagicMock:
    writer = MagicMock()
    writer.write = AsyncMock(return_value=handle)
    return writer


# ── dark by default ──────────────────────────────────────────────────


def test_autonomous_notes_are_off_by_default() -> None:
    """The flag IS the safety property. An unasked write into a customer's
    system of record must require a deploy-time decision, so the default is
    what this asserts — not that the flag is readable."""
    assert NotesSettings().autonomous_enabled is False


@pytest.mark.asyncio
async def test_disabled_writes_nothing_and_says_so() -> None:
    """A dark feature that logs nothing is indistinguishable from a broken one
    (the detector/reminder-visibility lesson), so every candidate comes back
    with a named reason rather than silently vanishing."""
    writer, store = _writer(), _store()
    run = await run_note_pass(
        [_candidate()], company_id=COMPANY, writer=writer, store=store, enabled=False
    )
    assert run.written == []
    assert [reason for _, reason in run.dropped] == ["autonomous_notes_disabled"]
    writer.write.assert_not_awaited()
    store.claim.assert_not_awaited()  # nobody pays for a claim that cannot write


# ── the happy path ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_accepted_vendor_fact_is_written_and_recorded() -> None:
    writer, store = _writer(), _store()
    run = await run_note_pass(
        [_candidate()], company_id=COMPANY, writer=writer, store=store, enabled=True
    )
    assert len(run.written) == 1
    body = writer.write.await_args.args[1]
    assert body.startswith("[Occupella] Financial responsibility — ")
    store.finalize_success.assert_awaited_once()
    # Permanent dedup: a fact is noted once per LIFETIME, like the risk
    # bridge's bridged_at. An expiring window would re-note it later.
    assert store.claim.await_args.kwargs["window"] is None
    assert store.claim.await_args.kwargs["action_type"] == NOTE_ACTION_TYPE


@pytest.mark.asyncio
async def test_a_held_claim_is_not_rewritten() -> None:
    writer, store = _writer(), _store(won=False)
    run = await run_note_pass(
        [_candidate()], company_id=COMPANY, writer=writer, store=store, enabled=True
    )
    assert run.written == []
    assert [reason for _, reason in run.dropped] == ["duplicate"]
    writer.write.assert_not_awaited()


# ── the unattended narrowing ─────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["task", "work_order"])
async def test_read_merge_write_kinds_are_refused_unattended(kind: str) -> None:
    """Both of these write through a FULL-REPLACE PUT on a live record. The
    confirm-gated tool may do that because a human looked at it first; an
    unattended pass has not, and gotcha 20 records that this class of PUT
    fails by silently 200-ing. Nothing is claimed and nothing is written."""
    writer, store = _writer(), _store()
    run = await run_note_pass(
        [_candidate(kind=kind)], company_id=COMPANY, writer=writer, store=store, enabled=True
    )
    assert run.written == []
    assert [reason for _, reason in run.dropped] == ["not_autonomous_writable"]
    writer.write.assert_not_awaited()


def test_the_unattended_set_is_narrower_than_what_the_writer_can_reach() -> None:
    """If these two ever coincide, the narrowing above has been deleted
    without anyone deciding to delete it."""
    assert AUTONOMOUS_KINDS < WRITABLE_KINDS
    assert AUTONOMOUS_KINDS == {"vendor"}


# ── never invent an endpoint ─────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["lease", "owner", "tenant", "property"])
async def test_unprobed_kinds_raise_rather_than_guessing(kind: str) -> None:
    """These four are exactly what EntityRef's docstring used to claim. None
    has a probed note route, and guessing one is gotcha 15's whole rule."""
    api = MagicMock()
    with pytest.raises(UnsupportedNoteTargetError):
        await BuildiumNoteWriter(api).write(EntityRef(kind=kind, id=1), "body")


@pytest.mark.asyncio
async def test_each_writable_kind_hits_its_own_probed_endpoint() -> None:
    api = MagicMock()
    api.vendors.add_note = AsyncMock(return_value={"Id": 77})
    api.tasks.add_note = AsyncMock(return_value={})
    api.work_orders.add_note = AsyncMock(return_value={})
    writer = BuildiumNoteWriter(api)

    assert await writer.write(EntityRef(kind="vendor", id=5), "b") == "77"
    api.vendors.add_note.assert_awaited_once_with(5, note="b")
    await writer.write(EntityRef(kind="task", id=6), "b")
    api.tasks.add_note.assert_awaited_once_with(6, note="b")
    await writer.write(EntityRef(kind="work_order", id=7), "b")
    api.work_orders.add_note.assert_awaited_once_with(7, note="b")
    # No crossed wires: the vendor call must not have touched the others.
    assert api.tasks.add_note.await_count == 1


# ── failure directions ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_failed_write_releases_the_claim_for_a_later_pass() -> None:
    """The one failure we can PROVE did not write. Holding the claim would
    make a transient Buildium error permanent."""
    store = _store()
    writer = MagicMock()
    writer.write = AsyncMock(side_effect=RuntimeError("502 from Buildium"))
    run = await run_note_pass(
        [_candidate()], company_id=COMPANY, writer=writer, store=store, enabled=True
    )
    assert run.written == []
    assert [reason for _, reason in run.dropped] == ["write_failed"]
    store.finalize_retryable.assert_awaited_once()
    store.finalize_success.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_written_note_counts_even_if_the_ledger_write_fails() -> None:
    """The note IS in Buildium at that point. Reporting it unwritten would
    invite a second pass to write it again — the duplicate is the worse
    outcome, so the claim stays taken and the run reports success."""
    store = _store()
    store.finalize_success = AsyncMock(side_effect=RuntimeError("db gone"))
    run = await run_note_pass(
        [_candidate()], company_id=COMPANY, writer=_writer(), store=store, enabled=True
    )
    assert len(run.written) == 1


@pytest.mark.asyncio
async def test_a_low_confidence_fact_is_dropped_by_the_policy_gate() -> None:
    candidate = _candidate()
    candidate.confidence = 0.4
    writer, store = _writer(), _store()
    run = await run_note_pass(
        [candidate], company_id=COMPANY, writer=writer, store=store, enabled=True
    )
    assert [reason for _, reason in run.dropped] == ["low_confidence"]
    writer.write.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_same_fact_twice_in_one_batch_is_written_once() -> None:
    writer, store = _writer(), _store()
    run = await run_note_pass(
        [_candidate(), _candidate()],
        company_id=COMPANY,
        writer=writer,
        store=store,
        enabled=True,
    )
    assert len(run.written) == 1
    assert writer.write.await_count == 1
