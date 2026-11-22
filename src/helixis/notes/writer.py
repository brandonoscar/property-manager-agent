"""The Buildium half of the autonomous-note path — the WRITER seam
``notes/policy.py`` declared as a Protocol and never bound to anything.

``policy.py`` deliberately refused to guess an endpoint (gotcha 15) and left
the write as a Protocol. It has stayed unbound ever since, which made the whole
module unreachable. This binds it — and the binding is mostly a statement of
**what Buildium actually supports**, because the answer is narrower than
``policy.EntityRef`` implied.

Live write-probes, 2026-07-05, recorded in the endpoint modules themselves:

===============  =================================================  ==========
kind             mechanism                                          additive?
===============  =================================================  ==========
``vendor``       ``POST /v1/vendors/{id}/notes`` — a REAL note       yes
                 endpoint, ``{"Note": ...}``, 65,535-char cap
``task``         no ``/notes`` route exists. The note rides the      no
                 ``Message`` field of ``PUT /tasks/todorequests/
                 {id}`` and lands as a TASK HISTORY entry — a
                 full-replace PUT done read-merge-write
``work_order``   no ``/notes`` route exists. The note is appended    no
                 to ``WorkDetails`` via ``PUT /workorders/{id}`` —
                 full-replace, and 422s when the WO has no vendor
===============  =================================================  ==========

Everything else — lease, owner, tenant, property — has **no probed endpoint**,
so this writer raises rather than inventing one. That is the whole reason the
Protocol existed.

⚠ **"Additive" is why :data:`AUTONOMOUS_KINDS` is one entry, not three.** A
human approving a note through ``buildium_add_note`` has looked at the record;
an autonomous pass has not, and two of the three mechanisms above are
full-replace PUTs on a live record whose merge, if it drifts, damages fields
nobody asked to touch (gotcha 20 documents that class silently 200-ing). The
unattended path therefore starts at the one endpoint that can only ever append.
Widening it is a one-line change and a deliberate decision, not an oversight —
the confirm-gated tool already reaches all three.
"""

from __future__ import annotations

import logging
from typing import Any

from helixis.notes.policy import EntityRef

logger = logging.getLogger(__name__)


class UnsupportedNoteTargetError(Exception):
    """Raised for an entity kind with no live-probed Buildium note mechanism.

    Deliberately an exception rather than a silent no-op: a note the caller
    believes it wrote and Buildium never received is the fabricated-record
    shape this repo keeps paying for (gotchas 26 / 55 / 57). Failing loudly
    keeps the dedup key unwritten, so a later pass retries.
    """


#: Kinds this writer can persist at all — the three with a probed mechanism.
WRITABLE_KINDS: frozenset[str] = frozenset({"vendor", "task", "work_order"})

#: Kinds the UNATTENDED pass may write. See the module docstring: only the
#: genuinely append-only endpoint. The confirm-gated ``buildium_add_note``
#: tool is unaffected and still reaches everything in :data:`WRITABLE_KINDS`.
AUTONOMOUS_KINDS: frozenset[str] = frozenset({"vendor"})


class BuildiumNoteWriter:
    """Persist an accepted note body to its Buildium entity.

    Satisfies ``policy.NoteWriter``. ``api`` is an opened
    :class:`~helixis.buildium.api.BuildiumApi`; the caller owns the client
    lifecycle (the same arrangement every agent tool uses), so this class holds
    no connection state and is safe to construct per pass.
    """

    def __init__(self, api: Any) -> None:
        self._api = api

    async def write(self, target: EntityRef, body: str) -> str:
        """Write ``body`` to ``target``. Returns a handle for the caller's log.

        Raises :class:`UnsupportedNoteTargetError` for a kind with no probed
        mechanism, and lets ``BuildiumApiError`` propagate — the caller records
        the dedup key only on success, so a failed write is retried rather than
        silently swallowed.
        """
        kind = (target.kind or "").strip().lower()
        if kind not in WRITABLE_KINDS:
            raise UnsupportedNoteTargetError(
                f"Buildium has no probed note mechanism for {kind!r} "
                f"(supported: {', '.join(sorted(WRITABLE_KINDS))})"
            )
        target_id = int(target.id)
        if kind == "vendor":
            created = await self._api.vendors.add_note(target_id, note=body)
            return str(created.get("Id") or created.get("id") or f"vendor:{target_id}")
        if kind == "work_order":
            await self._api.work_orders.add_note(target_id, note=body)
            return f"work_order:{target_id}"
        await self._api.tasks.add_note(target_id, note=body)
        return f"task:{target_id}"


def autonomous_writable(target: EntityRef) -> bool:
    """True when the UNATTENDED pass may write to ``target``.

    Stricter than :data:`WRITABLE_KINDS` on purpose — see the module docstring.
    """
    return (target.kind or "").strip().lower() in AUTONOMOUS_KINDS


__all__ = [
    "AUTONOMOUS_KINDS",
    "WRITABLE_KINDS",
    "BuildiumNoteWriter",
    "UnsupportedNoteTargetError",
    "autonomous_writable",
]
