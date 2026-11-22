"""A card is a photograph; the record moves on.

An Inbox card is built when its webhook arrives and then waits for a click.
In between, the task can be completed IN BUILDIUM — where Occupella never sees
the click — and the handler would post "just following up, this is still
outstanding" onto a job that finished on Tuesday.

That output is the dangerous kind: well-formed, correctly addressed, and false.
Nothing about it looks wrong to the person who receives it.

⚠ The two assertions at the bottom are the ones most likely to rot. Both check
WIRING rather than logic, and both fail silently in the direction of "looks
fine": reading the wrong status field returns ``None`` on a completed task and
waves it through, and leaving 409 out of the pre-side-effect set turns the
guard into a permanent brick on the card it was protecting.
"""

from __future__ import annotations

import pytest

from helixis.agent.handlers.staleness import (
    NOT_APPLICABLE,
    compare_task_status,
    revalidate_task,
)

# ── the pure verdict ────────────────────────────────────────────────────────


@pytest.mark.parametrize("live", ["Completed", "Closed", "completed", "CLOSED"])
@pytest.mark.parametrize("card", ["New", "InProgress", "Deferred", None, ""])
def test_open_when_written_and_finished_now_is_stale(card: str | None, live: str) -> None:
    verdict = compare_task_status(card, live)
    assert verdict.stale is True
    assert verdict.checked is True
    assert "no longer holds" in verdict.reason


@pytest.mark.parametrize("live", ["New", "InProgress", "Deferred"])
def test_a_task_still_open_is_not_stale(live: str) -> None:
    verdict = compare_task_status("New", live)
    assert verdict.stale is False
    assert verdict.checked is True


def test_a_card_written_about_finished_work_is_not_stale() -> None:
    """It was already closed when the card was written, so nothing changed
    underneath anyone. That is a planner question — should this card exist —
    not a freshness one, and blocking here would refuse legitimate
    close-the-loop notes on completed work."""
    verdict = compare_task_status("Completed", "Completed")
    assert verdict.stale is False
    assert verdict.checked is True


@pytest.mark.parametrize("live", [None, "", "   "])
def test_an_unreadable_live_status_is_unchecked_not_fresh(live: str | None) -> None:
    """⚠ The gotcha-26 shape, as a verdict.

    Reading the wrong field yields ``None``, and ``None`` is not terminal, so
    a naive check would conclude "still open" and pass every stale card
    through while looking like it worked. Refusing to draw a conclusion is the
    only safe reading of no data.
    """
    verdict = compare_task_status("New", live)
    assert verdict.stale is False
    assert verdict.checked is False, "no data must never read as a passing check"


def test_stale_and_unchecked_are_different_statements() -> None:
    """``stale=False, checked=False`` says we never found out. ``stale=False,
    checked=True`` says we looked and it was fine. Collapsing them into one
    boolean is how a check that never runs starts reading as one that passed —
    the same failure the probe's "0 leaks over zero answer" had."""
    never_looked = compare_task_status("New", None)
    looked = compare_task_status("New", "New")
    assert never_looked.stale == looked.stale
    assert never_looked.checked != looked.checked


# ── the re-read ─────────────────────────────────────────────────────────────


class _Task:
    def __init__(self, **kw: object) -> None:
        self.__dict__.update(kw)


class _Client:
    def __init__(self, task: object | Exception) -> None:
        self.tasks = self
        self._task = task
        self.calls = 0

    async def get(self, task_id: int) -> object:
        self.calls += 1
        if isinstance(self._task, Exception):
            raise self._task
        return self._task


@pytest.mark.asyncio
async def test_the_re_read_uses_task_status_not_status() -> None:
    """⚠ The single most important line in this file.

    ``GET /tasks/{id}`` returns the value under ``TaskStatus`` and leaves
    ``Status`` NULL on every production row — verified across all 179
    (gotcha 26). A check reading ``.status`` finds ``None`` on a genuinely
    completed task, concludes "not terminal", and passes it through. It would
    be a working-looking guard that never catches anything.

    So the fixture sets ONLY ``task_status``, exactly as production does.
    """
    client = _Client(_Task(task_status="Completed", status=None))
    verdict = await revalidate_task(client, 39503, "New")
    assert client.calls == 1, "VACUOUS: the task was never re-read"
    assert verdict.stale is True


@pytest.mark.asyncio
async def test_a_failed_re_read_does_not_block_the_action() -> None:
    """Failing closed would turn any provider blip into "nothing in the Inbox
    works" — a far larger outage than the one being prevented, on actions that
    are already human-confirmed and manager-gated."""
    verdict = await revalidate_task(_Client(RuntimeError("buildium 503")), 39503, "New")
    assert verdict.stale is False
    assert verdict.checked is False, "a failed check must not report as a passed one"


@pytest.mark.asyncio
@pytest.mark.parametrize("task_id", [None, "", "not-a-number", {}])
async def test_an_action_with_no_task_has_nothing_to_revalidate(task_id: object) -> None:
    """``create_followup_task`` makes a new record and ``reply_to_email`` never
    touches Buildium, so there is no photograph to go stale."""
    assert await revalidate_task(_Client(_Task(task_status="Completed")), task_id, "New") == (
        NOT_APPLICABLE
    )


@pytest.mark.asyncio
async def test_a_missing_client_is_not_applicable() -> None:
    assert await revalidate_task(None, 39503, "New") == NOT_APPLICABLE


# ── the wiring, which fails silently in the reassuring direction ────────────


def test_the_open_status_list_is_shared_not_copied() -> None:
    """A second hand-maintained copy of a Buildium enum is exactly the drift
    ``id_hygiene`` was written to clean up after: add a status, one table
    learns it, the other keeps answering from memory."""
    import inspect

    from helixis.agent.handlers import staleness
    from helixis.agents.buildium_tools import _OPEN_TASK_STATUSES

    source = inspect.getsource(staleness)
    assert "_OPEN_TASK_STATUSES" in source, "the open-status list must be imported"
    for status in _OPEN_TASK_STATUSES:
        assert compare_task_status("New", status).stale is False, (
            f"{status} is open per the shared list but this module calls it terminal"
        )


def test_a_stale_card_leaves_the_claim_retryable() -> None:
    """⚠ Reads BOTH sides of the seam (gotcha 28).

    The staleness check raises ``HandlerError(409)`` from BEFORE any Buildium
    write, so it provably fired nothing. If 409 is not in
    ``_PRE_SIDE_EFFECT_STATUSES`` the guard classes it ambiguous, records
    ``abandoned_unknown``, and that action can never be retried — a freshness
    guard that permanently bricks the card it was protecting. The user's whole
    remedy is "refresh and try again", which that would remove.

    Testing the raise and the status set separately would let them drift into
    disagreeing while each looked correct on its own — so this finds the ACTUAL
    raise in the route and checks THAT code against the set, rather than
    asserting a hardcoded 409 twice and calling it two sides.
    """
    import ast
    import inspect

    from helixis.api.routes import buildium as route_mod
    from helixis.api.routes.buildium import _PRE_SIDE_EFFECT_STATUSES

    tree = ast.parse(inspect.getsource(route_mod))
    codes: list[int] = []
    for node in ast.walk(tree):
        # `if verdict.stale: raise HandlerError(reason, status_code=...)`
        if not isinstance(node, ast.If):
            continue
        if "verdict.stale" not in ast.unparse(node.test):
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Raise) or not isinstance(inner.exc, ast.Call):
                continue
            for kw in inner.exc.keywords:
                if kw.arg == "status_code" and isinstance(kw.value, ast.Constant):
                    codes.append(kw.value.value)

    assert codes, "no `raise ... status_code=` found under a `verdict.stale` branch"
    for code in codes:
        assert code in _PRE_SIDE_EFFECT_STATUSES, (
            f"the staleness guard raises {code}, which the executor treats as AMBIGUOUS — "
            "the action would be recorded abandoned_unknown and could never be retried"
        )
