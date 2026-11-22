"""A bug report has to be diagnosable without a conversation.

`helixis_feedback` stored category + message + view. When chat fails the SSE
response is already a 200, so the user sees "something went wrong" and the
traceback lives only in Render's logs — gotcha 28 is this product taking chat
out for every substantive turn while 4308 tests stayed green. A report saying
"it broke" costs a round trip to the customer before diagnosis can start.

⚠ The two properties that matter are BOTH about where the run id comes from:
it is resolved on the SERVER, and the lookup is scoped to the caller's company.
A client-supplied id is a claim; an unscoped lookup attaches another tenant's
run to this company's report (gotcha 47) — in the row an operator will read.
"""

from __future__ import annotations

import ast
import pathlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from helixis.api.routes import feedback as feedback_mod
from helixis.api.routes.feedback import FeedbackIn, submit_feedback

_SOURCE = pathlib.Path(feedback_mod.__file__).read_text(encoding="utf-8")


class _User:
    user_id = "11111111-1111-1111-1111-111111111111"
    company_ids = ["22222222-2222-2222-2222-222222222222"]


def _pool(run_row: dict[str, Any] | None) -> tuple[MagicMock, list[tuple[str, tuple[Any, ...]]]]:
    """A pool that records what it was asked.

    ⚠ Reaches into the auto-generated AsyncMock tree rather than hand-building
    the connection (gotcha 69): `acquire().__aenter__()` already yields an
    AsyncMock whose every method awaits, and replacing it with a plain
    MagicMock breaks every other await in the route with a TypeError far from
    the change that caused it.
    """
    calls: list[tuple[str, tuple[Any, ...]]] = []
    pool = MagicMock()
    conn = pool.acquire.return_value.__aenter__.return_value

    # ⚠ Split on the STATEMENT, not on the method. The insert moved from
    # `execute` to `fetchrow ... RETURNING id` when the row id became the
    # pointer a Sentry notice carries, so one fake now answers two different
    # questions and returning the run row to both would hand the notice a
    # feedback id that is really a run id.
    async def _fetchrow(sql: str, *args: Any) -> dict[str, Any] | None:
        calls.append((sql, args))
        if "INSERT INTO helixis_feedback" in sql:
            return {"id": "44444444-4444-4444-4444-444444444444"}
        return run_row

    async def _execute(sql: str, *args: Any) -> str:
        calls.append((sql, args))
        return "INSERT 0 1"

    conn.fetchrow = AsyncMock(side_effect=_fetchrow)
    conn.execute = AsyncMock(side_effect=_execute)
    return pool, calls


async def _submit(monkeypatch: pytest.MonkeyPatch, payload: FeedbackIn, run_row: Any) -> list[Any]:
    pool, calls = _pool(run_row)
    monkeypatch.setattr(feedback_mod, "get_pool", lambda: pool)
    monkeypatch.setattr(feedback_mod, "ph_capture", lambda *a, **k: None)
    await submit_feedback(payload, user=_User())  # type: ignore[arg-type]
    return calls


# ── the run is resolved, and it is scoped ────────────────────────────


async def test_the_run_is_resolved_from_the_thread_and_stored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = "33333333-3333-3333-3333-333333333333"
    calls = await _submit(
        monkeypatch,
        FeedbackIn(category="bug", message="chat returned nothing", session_id="t_9"),
        {"id": run},
    )
    insert = next(args for sql, args in calls if "INSERT INTO helixis_feedback" in sql)
    assert run in insert, "the resolved run id never reached the row"


async def test_the_run_lookup_is_scoped_to_the_callers_company(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠ `session_id` comes from the client. Unscoped, this attaches another
    tenant's run to this company's bug report — gotcha 47, in the row an
    operator would then go and read."""
    calls = await _submit(
        monkeypatch,
        FeedbackIn(category="bug", message="something broke", session_id="t_9"),
        {"id": "33333333-3333-3333-3333-333333333333"},
    )
    sql, args = next((s, a) for s, a in calls if "orchestrator_runs" in s)
    assert "company_id" in sql, "the run lookup is not company-scoped"
    assert _User.company_ids[0] in args


async def test_a_report_with_no_thread_still_files(monkeypatch: pytest.MonkeyPatch) -> None:
    """Feedback from a settings page has no chat thread. The message is the
    part a person wrote — never lose it for want of a diagnostic."""
    calls = await _submit(
        monkeypatch,
        FeedbackIn(category="idea", message="add a dark mode toggle", view="settings"),
        None,
    )
    assert any("INSERT INTO helixis_feedback" in sql for sql, _ in calls)
    assert not any("orchestrator_runs" in sql for sql, _ in calls), (
        "a report with no session should not query for a run at all"
    )


async def test_an_unresolvable_run_does_not_lose_the_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The thread is real, the run is gone (retention, or the turn never wrote
    a row). Still a report."""
    calls = await _submit(
        monkeypatch,
        FeedbackIn(category="bug", message="it hung", session_id="t_stale"),
        None,
    )
    insert = next(args for sql, args in calls if "INSERT INTO helixis_feedback" in sql)
    assert "it hung" in insert
    assert None in insert  # run_id


async def test_a_failing_run_lookup_does_not_break_the_submission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠ The asymmetry, stated as a test: a diagnostic that can break the thing
    it observes is worse than no diagnostic. Same rule `_warn_if_nothing_matched`
    takes in billing."""
    pool = MagicMock()
    conn = pool.acquire.return_value.__aenter__.return_value
    inserted: list[tuple[Any, ...]] = []

    # ⚠ Only the RUN LOOKUP fails. Making every `fetchrow` raise would break
    # the insert too and the test would pass for the wrong reason — it would
    # prove the route survives a dead database, not that it survives a dead
    # diagnostic, which is the asymmetry being pinned.
    async def _fetchrow(sql: str, *args: Any) -> dict[str, Any] | None:
        if "INSERT INTO helixis_feedback" in sql:
            inserted.append(args)
            return {"id": "44444444-4444-4444-4444-444444444444"}
        raise RuntimeError("orchestrator_runs is unreachable")

    conn.fetchrow = AsyncMock(side_effect=_fetchrow)
    monkeypatch.setattr(feedback_mod, "get_pool", lambda: pool)
    monkeypatch.setattr(feedback_mod, "ph_capture", lambda *a, **k: None)

    result = await submit_feedback(
        FeedbackIn(category="bug", message="broke again", session_id="t_1"),
        user=_User(),  # type: ignore[arg-type]
    )
    assert result.ok
    assert inserted, "the report was lost because a diagnostic join failed"


# ── what must NOT be accepted, and what must NOT be sent out ─────────


def test_the_client_cannot_supply_a_run_id() -> None:
    """A run id from the browser is a claim about which turn to attribute a
    complaint to. It is also reliably ABSENT in the failure case this feature
    exists for — the envelope that carries one is emitted only in loop mode
    with a final output."""
    assert "run_id" not in FeedbackIn.model_fields
    payload = FeedbackIn(category="bug", message="a real report", run_id="deadbeef")  # type: ignore[call-arg]
    assert not hasattr(payload, "run_id"), "a client-sent run id is being kept"


def test_the_user_agent_is_length_bounded() -> None:
    """Client-supplied free text landing in a row an operator reads."""
    field = FeedbackIn.model_fields["user_agent"]
    limits = [m for m in field.metadata if getattr(m, "max_length", None)]
    assert limits, "user_agent has no length bound"
    assert limits[0].max_length <= 400


def test_the_message_is_not_sent_to_posthog() -> None:
    """⚠ Third-party telemetry. A user's own words about their portfolio do not
    belong in it — the same line codeexec/audit draws when it sends a code
    sha256 and never the source.

    Reads the AST so a reflow cannot hide the argument (gotcha 40): the check
    is on what is passed to `ph_capture`, not on nearby text.
    """
    tree = ast.parse(_SOURCE)
    captures = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "ph_capture"
    ]
    assert captures, "the scan found no ph_capture call — it is pointed at nothing"
    for call in captures:
        payload = next((a for a in call.args if isinstance(a, ast.Dict)), None)
        assert payload is not None, "ph_capture's properties are no longer a literal dict"
        sent = ast.dump(payload)
        assert "message" not in sent, "the user's message is being sent to PostHog"


def test_the_migration_adds_every_column_the_route_writes() -> None:
    """Both sides (gotcha 28). The route naming a column no migration creates
    fails at runtime, on the submission of the report that would have told us."""
    migration = (
        pathlib.Path(__file__).resolve().parents[2]
        / "supabase"
        / "migrations"
        / "0092_feedback_diagnostic_context.sql"
    ).read_text(encoding="utf-8")
    for column in ("session_id", "run_id", "app_version", "user_agent"):
        assert column in migration, f"{column} is written by the route and never created"


# ── the report has to reach a person (2026-09-08) ───────────────────


async def test_the_arrival_reaches_sentry_carrying_the_row_and_not_the_prose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠ Nothing read this table. `helixis_feedback` was written by this route
    and by NO other code — no admin endpoint, no UI, no job — so a customer
    who described a defect was told "we reply by the next business day" and
    reached a table nobody queried. The notice puts the ARRIVAL in the stream
    the operator already reads.

    ⚠ And it carries the POINTER, never the message. That is the line the
    PostHog capture already draws: Sentry is third-party telemetry, and a
    customer's own words about their portfolio do not belong in it. Reading
    the complaint means opening the row, deliberately.
    """
    pool, _calls = _pool(None)
    monkeypatch.setattr(feedback_mod, "get_pool", lambda: pool)
    monkeypatch.setattr(feedback_mod, "ph_capture", lambda *a, **k: None)
    seen: list[Any] = []
    monkeypatch.setattr(
        feedback_mod,
        "capture_notice",
        lambda message, **kw: seen.append((message, kw)),
    )

    await submit_feedback(
        FeedbackIn(category="bug", message="the delinquency table showed my other client"),
        user=_User(),  # type: ignore[arg-type]
    )

    assert seen, "a report arrived and nothing told anybody"
    message, kw = seen[0]
    assert "44444444-4444-4444-4444-444444444444" in message, "no row to go and read"
    assert "my other client" not in message
    assert "my other client" not in str(kw)
    assert kw["op"] == "feedback.submitted"
    # A bug rises above info noise; a feature request must not sit at the same
    # level as a 500 (the signal that has to stay loud stays loud).
    assert kw["level"] == "warning"
    assert kw["tags"]["company_id"] == _User.company_ids[0]


async def test_an_idea_is_not_reported_at_the_same_level_as_a_bug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, _calls = _pool(None)
    monkeypatch.setattr(feedback_mod, "get_pool", lambda: pool)
    monkeypatch.setattr(feedback_mod, "ph_capture", lambda *a, **k: None)
    seen: list[Any] = []
    monkeypatch.setattr(feedback_mod, "capture_notice", lambda m, **kw: seen.append(kw))

    await submit_feedback(
        FeedbackIn(category="idea", message="let me text vendors"),
        user=_User(),  # type: ignore[arg-type]
    )
    assert seen[0]["level"] == "info"


async def test_a_broken_notice_never_costs_the_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same asymmetry as the run lookup: a diagnostic that can break the thing
    it observes is worse than no diagnostic."""
    pool, calls = _pool(None)
    monkeypatch.setattr(feedback_mod, "get_pool", lambda: pool)
    monkeypatch.setattr(feedback_mod, "ph_capture", lambda *a, **k: None)

    def _boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("sentry down")

    monkeypatch.setattr(feedback_mod, "capture_notice", _boom)

    result = await submit_feedback(
        FeedbackIn(category="bug", message="still broken"),
        user=_User(),  # type: ignore[arg-type]
    )
    assert result.ok
    assert any("INSERT INTO helixis_feedback" in sql for sql, _ in calls)
