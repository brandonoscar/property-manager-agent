"""SSE endpoint handler — converts EventBus events to Server-Sent Events."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncGenerator

from helixis.streaming.bus import EventBus

logger = logging.getLogger(__name__)

KEEPALIVE_INTERVAL = 15  # seconds

#: Hard ceiling on ONE stream's lifetime.
#:
#: ⚠ CONTAINMENT, NOT A ROOT-CAUSE FIX — and it exists because the keepalive
#: that was added to stop proxies closing idle connections ALSO removed the
#: only thing that was bounding a broken one. Before keepalives, a stream that
#: stopped delivering died at the client's read timeout. After them, every 15s
#: comment resets that timer, so a stream delivering nothing can hold a
#: connection open indefinitely. Measured on battery v11 q96: the agent
#: finished in 176.1s with a 758-character answer, and the client sat on the
#: socket for **2,948 seconds** — 49 minutes — receiving keepalives and no
#: events, then died on ReadTimeout having captured nothing.
#:
#: WHY that stream delivered nothing is still unexplained; the ``done`` event
#: carrying 776 characters was published and never arrived. This does not fix
#: that. It converts an unbounded hang into a bounded one, so the failure costs
#: a connection and a clear terminator instead of an hour.
#:
#: Sized off the agent's own hard deadline (``reasoning_loop_deadline_s``,
#: 240s) plus room for the deadline rescue and the publish. A stream still
#: running well past the point its run can possibly be alive is not waiting
#: for anything.
MAX_STREAM_SECONDS = 420.0


async def sse_event_generator(
    bus: EventBus,
    session_id: str,
    after: int | None = None,
) -> AsyncGenerator[str, None]:
    """Subscribe to a session's event bus and yield SSE-formatted strings.

    Sends keepalive comments every 15 seconds to prevent proxy timeouts.
    """

    # ⚠ THE KEEPALIVE WAS NEVER REACHING THE WIRE. The previous shape built a
    # nested ``keepalive()`` async generator and drove it with
    # ``asyncio.create_task(gen.__anext__())`` — so its ``yield`` landed in the
    # TASK'S RESULT and was discarded. This function's only ``yield``s were the
    # event and the terminator; nothing ever wrote ": keepalive". Measured
    # against the real generator with the interval at 0.2s and 3s of silence
    # (15 intervals): one event chunk, ZERO keepalive chunks. The docstring
    # above claimed the opposite and no test asserted it, so it read as covered
    # for as long as nobody looked (gotcha 48's shape).
    #
    # The restart was dead twice over: ``keepalive_task.cancel()`` threw
    # CancelledError into the generator at its ``sleep``, ending it, so every
    # later ``__anext__()`` raised ``StopAsyncIteration`` immediately.
    #
    # Consequence: the stream emits NOTHING during a publish-free window, and a
    # specialist consult is one uninterrupted silent window — gotcha 37
    # measured 195 of 232 tool calls as nested ``buildium_agent`` runs. A
    # confirm gate is worse: ``agents/tools.py`` waits up to 300s. Battery
    # v10-100 q63 (172.5s) and q96 (392s wall vs 81.5s watchdog-active — a
    # ~310s gate wait) both came back blank at the client while the server held
    # the finished answer.
    #
    # Now a plain timer raced against the subscriber, so the string is yielded
    # by THIS generator and actually reaches the socket.
    subscription = bus.subscribe_seq(session_id, after=after)
    # ⚠ Created ONCE and re-waited, not re-created per pass. The first draft
    # rebuilt the future inside the loop and awaited it after a timeout, so a
    # silent window produced exactly ONE keepalive and then blocked — measured
    # at 1 instead of 5 across 1.2s at a 0.2s interval. A keepalive that fires
    # once is not a keepalive; the whole point is the repeat. Cancelling it
    # instead would be worse: it IS the pending queue read, so cancelling drops
    # whichever event arrives next.
    next_event = asyncio.ensure_future(subscription.__anext__())
    started = asyncio.get_running_loop().time()
    try:
        while True:
            done, _pending = await asyncio.wait(
                {next_event}, timeout=KEEPALIVE_INTERVAL, return_when=asyncio.FIRST_COMPLETED
            )
            if asyncio.get_running_loop().time() - started > MAX_STREAM_SECONDS:
                # ⚠ Checked AFTER the wait, so a stream that is actively
                # delivering still gets to finish the event in hand. Breaking
                # here falls through to the terminator below, so the client
                # sees a clean close and can tell this from a severed pipe.
                logger.warning(
                    "sse stream for session %s exceeded %.0fs — closing. The run it "
                    "belongs to cannot still be alive; see MAX_STREAM_SECONDS.",
                    session_id,
                    MAX_STREAM_SECONDS,
                )
                break
            if not done:
                # Nothing published within the interval. Write the comment —
                # SSE comments are ignored by every client and are the only
                # thing keeping an idle connection alive through a proxy.
                yield ": keepalive\n\n"
                continue
            try:
                seq, event = next_event.result()
            except StopAsyncIteration:
                break
            # The SSE ``id:`` field is the resume cursor. A client that
            # loses the connection mid-turn reconnects with the last id it
            # RENDERED, and picks up from there instead of replaying the
            # whole turn — which is what re-subscribing without one does.
            yield f"id: {seq}\ndata: {json.dumps(event.model_dump())}\n\n"
            next_event = asyncio.ensure_future(subscription.__anext__())
    except asyncio.CancelledError:
        pass
    finally:
        # ⚠ gotcha 31, re-introduced here and now closed. ``async for`` does
        # NOT close a delegated async generator, and this function drives
        # ``subscribe_seq`` directly — so without this every stream left its
        # inner generator suspended, its ``finally`` (which pops the
        # subscriber) never ran, and ``publish`` went on filling a queue
        # nobody read. One leaked subscriber per closed stream, on the path
        # every chat turn uses. ``bus.subscribe`` already carries this fix
        # with a docstring explaining it; this call site had missed it.
        #
        # ⚠ CANCEL THE PENDING READ FIRST, or aclose() raises
        # ``RuntimeError: aclose(): asynchronous generator is already running``.
        # An async generator cannot be closed while an ``__anext__`` is
        # in flight — and the keepalive branch leaves exactly that pending on
        # EVERY iteration, so any teardown mid-wait (client disconnect, the
        # stream cap, cancellation) hit it. The RuntimeError propagates out of
        # the finally, the generator dies abnormally, and the ``[DONE]``
        # terminator below never runs: the client is left holding a socket
        # with no events and no close. Caught by the stream-cap test, which is
        # the first thing that ever exercised a teardown from inside the
        # keepalive branch.
        #
        # Cancelling here is safe and is NOT the mistake the loop warns about
        # above: we are tearing down and will never read another event, so
        # there is nothing left to drop.
        next_event.cancel()
        with contextlib.suppress(BaseException):
            await next_event
        await subscription.aclose()

    # Outside the ``finally`` deliberately. A ``yield`` there runs during
    # GeneratorExit too, and yielding while closing raises
    # ``RuntimeError: async generator ignored GeneratorExit`` — so every
    # client disconnect logged an error instead of closing quietly. That was
    # survivable while a disconnect was rare; with resume it is routine, since
    # a reattach closes the old stream by design. Out here the terminator is
    # sent on normal completion and skipped when we're being torn down.
    yield "data: [DONE]\n\n"
