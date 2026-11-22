"""One autonomous-note pass: classify → gate → write, with a durable
dedup ledger.

``policy.py`` owns the deterministic middle and ``writer.py`` owns the Buildium
half; this is the loop that runs them, and the piece that makes the ``seen``
set real. ``policy.select_new`` takes a ``seen`` set and deliberately does not
mutate it — "the caller adds keys only after the WRITER actually persists".
There was no caller, so there was no ledger.

**The ledger is the existing idempotency store, not a new table.**
``helixis_action_idempotency`` is already a per-company "have we done this
exact thing" record with an atomic single-winner claim, which is precisely what
a note dedup key needs. Claims are taken with ``window=None`` → ``expires_at``
NULL → **permanent**: the same fact about the same entity is noted once per
lifetime, the same call the risk bridge makes with ``bridged_at``. No
migration, and nothing new to purge on disconnect.

⚠ **A crashed write is never retried, on purpose.** If the process dies between
claim and write, ``reap_stale`` moves the row to ``abandoned_unknown`` — "the
side effect may have fired; we cannot know". A later pass loses the claim and
skips. For a WRITE into a customer's system of record that is the correct
direction: a missing note is recoverable by a human, a duplicate note in a
landlord's records is not, and nobody is watching an unattended pass to notice
either.

**Dark by default.** ``settings.notes.autonomous_enabled`` is False, and
:func:`run_note_pass` refuses before classifying — nobody pays for an LLM call
that cannot produce a write. Flipping it makes Occupella write into Buildium
*without being asked*, which is why it is a deploy-time decision and not a
code path anyone can reach accidentally.
"""

from __future__ import annotations

import logging
from typing import Any

from helixis.notes.policy import NoteCandidate, NoteRun, dedup_key, format_note_body, select_new
from helixis.notes.writer import UnsupportedNoteTargetError, autonomous_writable

logger = logging.getLogger(__name__)

#: ``action_type`` recorded on every ledger row this pass claims. Distinct from
#: the confirm-gated tool's ``buildium_add_*_note`` types so the two paths are
#: separable in an audit — "did a human approve this note?" must be answerable
#: from the row alone.
NOTE_ACTION_TYPE = "notes.autonomous"


async def run_note_pass(
    candidates: list[NoteCandidate],
    *,
    company_id: str,
    writer: Any,
    store: Any,
    enabled: bool,
) -> NoteRun:
    """Write the accepted, novel, autonomously-writable subset of ``candidates``.

    ``writer`` satisfies ``policy.NoteWriter`` (normally
    :class:`~helixis.notes.writer.BuildiumNoteWriter` over an opened API);
    ``store`` satisfies ``IdempotencyStore``. Both are injected so this is
    testable with no network and no database.

    Returns a :class:`~helixis.notes.policy.NoteRun` — written dedup keys plus
    every drop paired with its reason, so a tuning pass can see WHY a fact was
    not recorded. Never raises: an unattended pass that takes down its caller
    is worse than one that writes nothing.
    """
    run = NoteRun()
    if not enabled:
        # Refuse BEFORE the classifier's caller spends anything. Every
        # candidate is reported dropped rather than silently discarded — a
        # dark feature that logs nothing is indistinguishable from a broken
        # one (the reminder-sweep / detector-visibility lesson).
        run.dropped.extend((c, "autonomous_notes_disabled") for c in candidates)
        if candidates:
            logger.info(
                "autonomous notes: disabled — %d candidate(s) not written for company=%s",
                len(candidates),
                company_id,
            )
        return run

    to_write, dropped = select_new(candidates, seen=set())
    run.dropped.extend(dropped)

    for candidate in to_write:
        key = dedup_key(candidate)
        if not autonomous_writable(candidate.target):
            # A kind the confirm-gated tool can write but an unattended pass
            # must not (writer.AUTONOMOUS_KINDS). Named distinctly from
            # "unsupported" so tuning can tell a policy choice from a missing
            # Buildium endpoint.
            run.dropped.append((candidate, "not_autonomous_writable"))
            continue
        try:
            claim = await store.claim(
                company_id=company_id,
                idem_key=key,
                action_type=NOTE_ACTION_TYPE,
                window=None,  # permanent — noted once per lifetime
            )
        except (Exception, SystemExit):
            logger.exception("autonomous notes: claim failed for company=%s — skipping", company_id)
            run.dropped.append((candidate, "claim_failed"))
            continue
        if not claim.won:
            run.dropped.append((candidate, "duplicate"))
            continue

        body = format_note_body(candidate)
        try:
            handle = await writer.write(candidate.target, body)
        except UnsupportedNoteTargetError:
            await _release(store, company_id, key, "unsupported target kind")
            run.dropped.append((candidate, "unsupported_target"))
            continue
        except (Exception, SystemExit) as exc:
            # Release the claim so a later pass can retry — the write did NOT
            # land. This is the one failure we can prove, which is why it is
            # allowed to retry where a crash mid-write is not.
            await _release(store, company_id, key, str(exc)[:200])
            logger.exception("autonomous notes: write failed for company=%s", company_id)
            run.dropped.append((candidate, "write_failed"))
            continue

        try:
            await store.finalize_success(company_id=company_id, idem_key=key, result=handle)
        except (Exception, SystemExit):
            # The note IS in Buildium; only the bookkeeping failed. Log loudly
            # — the claim row stays in_progress and will reap to
            # abandoned_unknown, which correctly blocks a re-write.
            logger.exception(
                "autonomous notes: wrote %s but could not finalize the ledger row", handle
            )
        run.written.append(key)

    if run.written:
        logger.info(
            "autonomous notes: company=%s wrote=%d dropped=%d",
            company_id,
            len(run.written),
            len(run.dropped),
        )
    return run


async def _release(store: Any, company_id: str, key: str, error: str) -> None:
    """Mark a claim retryable after a write we know did not happen."""
    try:
        await store.finalize_retryable(company_id=company_id, idem_key=key, error=error)
    except (Exception, SystemExit):
        logger.exception("autonomous notes: releasing claim %s failed", key)


__all__ = ["NOTE_ACTION_TYPE", "run_note_pass"]
