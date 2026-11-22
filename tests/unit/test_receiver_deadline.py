"""The receiver's synchronous work is bounded, or the subscription dies.

Buildium gives a webhook receiver ten seconds and suspends the subscription
after 20 consecutive failures. A suspended subscription stops ALL webhook
ingestion until someone re-enables it in the Buildium console, and nothing
alarms on it — the Inbox just goes quiet.

Nothing bounded that work. The module comment estimated "enrichment (~800ms) +
routing (~50ms) + floor builder (~5ms) + legacy dispatch (already in path) —
comfortably under 10s", and every slow term there is an ESTIMATE of work that
makes live Buildium calls: the enricher fetches the task, its unit, its
property and its requester, and each dispatch handler re-fetches its own entity
(``queue.py``'s own comment: "mirror handlers hold the window open for seconds
because they fetch from Buildium"). Add the client's account-wide 10 req/s
token bucket and a slow upstream, and an estimate is not a bound.

⚠ **These guards RUN the code.** A missing deadline is invisible to static
analysis by construction — source that waits forever looks exactly like source
that waits correctly (gotcha 43, where 24 static probe guards all stayed green
through a hang). Every test here drives the real receiver against a phase that
sleeps past the budget, under its own ``asyncio.wait_for`` so a regression
fails in seconds rather than hanging CI.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from helixis.webhooks import router as router_mod

# ── the budget itself ────────────────────────────────────────────────


def test_the_budget_leaves_room_inside_buildiums_deadline() -> None:
    """The work budget is a FRACTION of the SLA, not a round number — so it
    moves if Buildium's deadline ever does, and the remainder still has to
    cover signature verification, the enqueue, routing, the floor build, the
    claim resolution and the response, none of which are free."""
    assert router_mod.RECEIVER_WORK_BUDGET_SECONDS < router_mod.BUILDIUM_ACK_SLA_SECONDS
    headroom = router_mod.BUILDIUM_ACK_SLA_SECONDS - router_mod.RECEIVER_WORK_BUDGET_SECONDS
    assert headroom >= 3.0, f"only {headroom:.1f}s left for everything outside the two slow phases"


def test_the_deadline_is_shared_not_per_phase() -> None:
    """Two separate timeouts would let the worst case be their SUM, and the
    sum is the number that has to stay under the SLA. Whatever enrichment
    does not spend must still be available to dispatch."""
    deadline = router_mod._ReceiverDeadline(budget=1.0)
    first = deadline.remaining()
    time.sleep(0.05)
    second = deadline.remaining()
    assert second < first, "the budget does not decrease — each phase gets a fresh one"


def test_an_exhausted_budget_reports_zero_not_negative() -> None:
    """``asyncio.wait_for`` treats a negative timeout as "already expired",
    which happens to be right — but a negative number reads as a bug to the
    next person and invites a "fix" that removes the clamp."""
    deadline = router_mod._ReceiverDeadline(budget=-5.0)
    assert deadline.remaining() == 0.0


# ── the wrapper actually bounds a slow phase ─────────────────────────


#: The outer safety net, so a regression fails fast instead of hanging CI.
#: ⚠ It must be FAR larger than the budget under test — see ``_assert_cut_off``.
_SAFETY_NET_SECONDS = 5.0
_TEST_BUDGET_SECONDS = 0.05


async def _assert_cut_off(coro_factory: object) -> None:
    """Assert the RECEIVER'S deadline cut the phase off — not the test's own.

    ⚠ The obvious version of this passed against a receiver with NO deadline
    at all. Both timeouts raise ``TimeoutError``, so ``pytest.raises`` cannot
    tell which one fired: with the inner deadline deleted, the outer safety
    net fired at 5s and the test went green (measured — the only tell was the
    suite taking 14s instead of 4s). A guard that cannot distinguish the
    control working from the control being absent is not a guard (gotcha 33:
    assert the probe actually RAN before trusting its verdict).

    So the assertion is on ELAPSED TIME. The receiver's budget is 0.05s and
    the safety net is 5s — two orders of magnitude apart, which is what makes
    them separable.
    """
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(coro_factory, timeout=_SAFETY_NET_SECONDS)  # type: ignore[arg-type]
    elapsed = time.monotonic() - started
    assert elapsed < _SAFETY_NET_SECONDS / 2, (
        f"cut off after {elapsed:.2f}s — that is the TEST's safety net firing, not "
        f"the receiver's {_TEST_BUDGET_SECONDS}s budget. The receiver has no deadline."
    )


@pytest.mark.asyncio
async def test_a_slow_phase_is_cut_off() -> None:
    """THE property, driven rather than read."""
    deadline = router_mod._ReceiverDeadline(budget=_TEST_BUDGET_SECONDS)

    async def _slow() -> None:
        await asyncio.sleep(30)

    await _assert_cut_off(router_mod._within_deadline(_slow(), deadline))


@pytest.mark.asyncio
async def test_a_fast_phase_is_untouched() -> None:
    """Both directions (gotcha 38). A deadline that fired on everything would
    push every event to the worker and make the receiver's inline path
    pointless — the Inbox would stop lighting up before the worker's next
    poll, which is the whole reason the floor is built inline."""
    deadline = router_mod._ReceiverDeadline(budget=5.0)
    ran = []

    async def _fast() -> None:
        ran.append(True)

    await router_mod._within_deadline(_fast(), deadline)
    assert ran == [True]


@pytest.mark.asyncio
async def test_the_enrichment_variant_returns_its_result() -> None:
    """⚠ A shared helper that discarded the return value would be gotcha 42's
    ``.as_tool(max_turns=)`` bug: a sub-run that already found what it needed,
    thrown away. The enricher's output IS the enrichment."""

    class _Handler:
        class enricher:  # noqa: N801 — mirrors the real attribute shape
            @staticmethod
            async def enrich(envelope: dict[str, object], company_id: str) -> str:
                return f"enriched:{company_id}"

    deadline = router_mod._ReceiverDeadline(budget=5.0)
    got = await router_mod._await_enrichment(_Handler(), {}, "co-1", deadline)
    assert got == "enriched:co-1"


@pytest.mark.asyncio
async def test_a_slow_enrichment_is_also_cut_off() -> None:
    class _Handler:
        class enricher:  # noqa: N801
            @staticmethod
            async def enrich(envelope: dict[str, object], company_id: str) -> str:
                await asyncio.sleep(30)
                return "never"

    deadline = router_mod._ReceiverDeadline(budget=_TEST_BUDGET_SECONDS)
    await _assert_cut_off(router_mod._await_enrichment(_Handler(), {}, "co-1", deadline))


# ── SystemExit must not escape the Task boundary ─────────────────────


def test_systemexit_inside_a_bounded_phase_does_not_kill_the_loop() -> None:
    """gotcha 36, and it is why the conversion happens INSIDE the wrapper.

    ``asyncio.wait_for`` creates a Task, and asyncio re-raises ``SystemExit``
    into the running loop as well as to the awaiter — so catching it outside
    lets the handler return its degraded value exactly as designed and the
    process die a moment later. An optional ML dep can still ``sys.exit(1)``
    at runtime (gotcha 7) and this is a request path.

    ⚠ This runs its OWN ``asyncio.run`` and asserts the runner RETURNS. Under
    pytest-asyncio the loop teardown happens outside the test's assertion, so
    the obvious version passes against the broken code — a test that cannot
    observe the failure is not covering it.
    """

    async def _exits() -> None:
        raise SystemExit(1)

    async def _main() -> str:
        deadline = router_mod._ReceiverDeadline(budget=5.0)
        try:
            await router_mod._within_deadline(_exits(), deadline)
        except Exception as exc:  # noqa: BLE001 — the point is that it is an Exception now
            return f"contained: {type(exc).__name__}"
        return "no error"

    assert asyncio.run(_main()).startswith("contained: RuntimeError")
