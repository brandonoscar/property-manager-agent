"""Async event bus — per-session fan-out queues for streaming agent events.

Multiplayer rewrite (live-watch PR): the original bus held ONE queue per
session, which made the stream single-consumer — a second subscriber would
steal events from the first. Each session now keeps:

* a bounded **replay log** of every published event, so a subscriber that
  attaches late (the driver's SSE response starting a beat after the run
  begins, or a teammate joining mid-run via ``GET /agent/watch``) first
  receives everything published so far, then goes live; and
* one bounded queue **per subscriber**, so N clients each get the full
  stream and a slow watcher only ever drops its OWN oldest events — never
  the driver's.

Duplication/loss safety: ``subscribe`` registers its queue and snapshots the
log in the same synchronous block (no ``await`` between them, and asyncio is
single-threaded), so an event published after registration reaches the new
subscriber exactly once via its queue, and one published before reaches it
exactly once via the snapshot.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import AsyncGenerator

from helixis.common.events import HelixisEvent

logger = logging.getLogger(__name__)

_SENTINEL = object()


class _Session:
    __slots__ = ("log", "next_subscriber_id", "published", "subscribers")

    def __init__(self, log_limit: int) -> None:
        self.log: deque[HelixisEvent] = deque(maxlen=log_limit)
        self.subscribers: dict[int, asyncio.Queue] = {}
        self.next_subscriber_id = 0
        # Monotonic count of events EVER published — never reset, never
        # decremented by the deque evicting an old entry. It is what makes
        # a resume cursor stable: an index into ``log`` would silently
        # shift under the caller every time the log rolled over.
        self.published = 0


class EventBus:
    """In-process pub/sub for agent event streaming.

    Each agent session fans out to any number of subscribers (the driver's
    SSE response plus zero or more watchers). The pre-fan-out API surface
    (``create_session`` / ``publish`` / ``subscribe`` / ``close_session`` /
    ``has_session``) is unchanged so no caller needed rework.
    """

    def __init__(self, max_queue_size: int = 1000, replay_log_limit: int = 1000):
        self._sessions: dict[str, _Session] = {}
        self._max_queue_size = max_queue_size
        # Matches the old single-queue buffer capacity: pre-fan-out, up to
        # 1000 events could pile up before the (sole) subscriber attached.
        self._replay_log_limit = replay_log_limit

    def create_session(self, session_id: str) -> None:
        if session_id not in self._sessions:
            self._sessions[session_id] = _Session(self._replay_log_limit)

    async def publish(self, session_id: str, event: HelixisEvent) -> None:
        session = self._sessions.get(session_id)
        if session is None:
            logger.warning("publish to unknown session %s, creating it", session_id)
            self.create_session(session_id)
            session = self._sessions[session_id]

        session.log.append(event)
        session.published += 1
        # The sequence rides WITH the event. A subscriber counting locally
        # would desync the moment ``_put_dropping_oldest`` shed one of its
        # events under backpressure — and a wrong cursor is worse than no
        # cursor, because a resume would then skip or duplicate silently.
        for queue in session.subscribers.values():
            self._put_dropping_oldest(queue, (session.published, event), session_id)

    def _put_dropping_oldest(self, queue: asyncio.Queue, item: object, session_id: str) -> None:
        if queue.full():
            # Per-subscriber backpressure: drop THAT subscriber's oldest
            # event. Other subscribers (and the replay log) are unaffected.
            try:
                queue.get_nowait()
                logger.warning("dropped oldest event for session %s (backpressure)", session_id)
            except asyncio.QueueEmpty:
                pass
        queue.put_nowait(item)

    async def subscribe(self, session_id: str) -> AsyncGenerator[HelixisEvent, None]:
        """Yield the session's replay log, then live events, until closed.

        The sequence-free view of :meth:`subscribe_seq`, kept because most
        callers only want the events.

        ``aclose`` on the inner generator is NOT optional. ``async for`` does
        not propagate GeneratorExit into a delegated async generator, so
        closing this one would leave ``subscribe_seq`` suspended with its
        ``finally`` unrun — the subscriber stays registered forever and
        ``publish`` keeps filling a queue nobody reads.
        """
        inner = self.subscribe_seq(session_id)
        try:
            async for _seq, event in inner:
                yield event
        finally:
            await inner.aclose()

    async def subscribe_seq(
        self, session_id: str, after: int | None = None
    ) -> AsyncGenerator[tuple[int, HelixisEvent], None]:
        """Same stream, each event paired with its monotonic sequence number.

        ``after`` resumes: only events with a sequence STRICTLY greater are
        replayed. A reconnecting client passes the last id it rendered and
        picks up from there instead of re-rendering the turn from event
        zero — which is what a plain re-subscribe does, and why a resume
        without a cursor looks like a bug.

        ``after`` older than the retained log is NOT an error: the deque is
        bounded, so a client gone long enough gets whatever survives. It
        may therefore have a gap. That is strictly better than the current
        behaviour, which is losing the stream entirely.
        """
        session = self._sessions.get(session_id)
        if session is None:
            self.create_session(session_id)
            session = self._sessions[session_id]

        queue: asyncio.Queue = asyncio.Queue(maxsize=self._max_queue_size)
        subscriber_id = session.next_subscriber_id
        session.next_subscriber_id += 1
        session.subscribers[subscriber_id] = queue
        # Snapshot in the same synchronous block as registration (see module
        # docstring) — this pairing is what makes catch-up exactly-once.
        # ``published`` is read in the SAME block for the same reason: it and
        # the snapshot must describe one instant, or the sequence numbers we
        # hand out would be off by however many events landed in between.
        snapshot = list(session.log)
        # The oldest retained event's sequence. Events evicted by the deque's
        # maxlen are gone, so this floor rises over the session's life.
        base_seq = session.published - len(snapshot) + 1

        try:
            for offset, event in enumerate(snapshot):
                seq = base_seq + offset
                if after is not None and seq <= after:
                    continue  # the client already rendered this one
                yield seq, event
            while True:
                item = await queue.get()
                if item is _SENTINEL:
                    break
                seq, event = item
                # No ``after`` filter here, on purpose. Registration and the
                # log snapshot happen in one synchronous block, so an event in
                # this queue is always NEWER than the snapshot — it can never
                # duplicate a replayed one, and filtering buys nothing.
                # It would also cost something real: a cursor ahead of the
                # session (a stale client, a copied url) would swallow every
                # live event and the stream would hang silently forever.
                # Ignoring a nonsense cursor is the fail-safe direction.
                yield seq, event
        finally:
            # The session may have been closed (and popped) while we streamed;
            # look it up fresh rather than holding the old reference.
            current = self._sessions.get(session_id)
            if current is not None:
                current.subscribers.pop(subscriber_id, None)

    async def close_session(self, session_id: str) -> None:
        session = self._sessions.get(session_id)
        if session is not None:
            for queue in session.subscribers.values():
                self._put_dropping_oldest(queue, _SENTINEL, session_id)
            # Don't remove immediately — let subscribers drain.
            await asyncio.sleep(0.1)
            self._sessions.pop(session_id, None)

    def has_session(self, session_id: str) -> bool:
        return session_id in self._sessions

    def subscriber_count(self, session_id: str) -> int:
        """Live subscriber count (driver + watchers). 0 for unknown sessions."""
        session = self._sessions.get(session_id)
        return len(session.subscribers) if session is not None else 0
