"""The standing-agents runner had no writer, so it has never run anything.

``webhooks/standing_agents.py`` polls every 300s, claims a due row, runs its
prompt scoped to (company, user) and delivers to that person's Inbox. It is
wired into the worker. Migration 0059 states the gap in as many words — *"No
API writes to this table exist yet (v1 rows are inserted manually / by a future
settings UI)"* — and the table holds 0 rows across every company. A scheduler
with nothing to schedule.

Three decisions are pinned here because each could reasonably have gone the
other way, and a future reader deserves to know they were decisions:

⚠ **Self-only.** A standing task runs AS its owner and delivers to THEIR
Inbox, so creating one for somebody else means making another person's agent
run a prompt they did not write. ``user_id`` is the caller's, never a body
field — and reads are scoped too, because these are stored prompts and one
person's are not another's to read.

⚠ **No role floor**, which is only defensible BECAUSE of the scoping above.

⚠ **The dark flag is reported, not guessed.** The runner is off unless the
WORKER process sets ``HELIXIS_STANDING_AGENTS_ENABLED``, and the web process
cannot read the worker's environment — so the answer is tri-state and
"unknown" is a real value. Guessing "live" from the wrong process would be the
exact failure this whole branch keeps finding.
"""

from __future__ import annotations

import ast
import inspect

import pytest

from helixis.api.routes import standing_tasks as st


def _src(fn: object) -> str:
    return inspect.getsource(fn)  # type: ignore[arg-type]


# ── it exists at all ─────────────────────────────────────────────────


def test_the_runner_finally_has_a_writer() -> None:
    from helixis.main import app

    paths = {p for p in app.openapi()["paths"] if "standing-tasks" in p}
    assert "/api/v1/standing-tasks" in paths
    assert "/api/v1/standing-tasks/{task_id}" in paths


def test_all_four_verbs_are_present() -> None:
    """A create with no delete is a table that only grows, and each row is a
    scheduled LLM run."""
    from helixis.main import app

    spec = app.openapi()["paths"]
    assert set(spec["/api/v1/standing-tasks"]) >= {"get", "post"}
    assert set(spec["/api/v1/standing-tasks/{task_id}"]) >= {"patch", "delete"}


# ── self-only, on every verb ─────────────────────────────────────────


@pytest.mark.parametrize(
    "fn",
    [
        st.list_standing_tasks,
        st.create_standing_task,
        st.update_standing_task,
        st.delete_standing_task,
    ],
)
def test_every_route_scopes_to_the_caller(fn: object) -> None:
    """⚠ BOTH keys on every verb. Company alone would let a colleague read or
    delete your stored prompts; user alone would cross tenants. This is
    gotcha 47 on a table whose rows are private text."""
    src = _src(fn)
    assert "company_id = $1::uuid" in src, f"{getattr(fn, '__name__', fn)} is not company-scoped"
    assert "user_id = $2::uuid" in src, f"{getattr(fn, '__name__', fn)} is not user-scoped"


def test_the_owner_is_never_taken_from_the_request_body() -> None:
    """⚠ The decision that makes "no role floor" safe. If ``user_id`` could
    come from the body, anyone could schedule a daily prompt to run as a
    colleague and land in their Inbox."""
    assert "user_id" not in st.StandingTaskIn.model_fields
    assert "user_id" not in st.StandingTaskPatch.model_fields
    assert "company_id" not in st.StandingTaskIn.model_fields
    assert "user.user_id" in _src(st.create_standing_task)


def test_last_run_at_is_not_client_editable() -> None:
    """⚠ It is the duplicate-run defence: the worker stamps it BEFORE running,
    so a crash costs a day rather than double-billing. A client that could
    clear it could re-run a task arbitrarily often, each run a billed call."""
    assert "last_run_at" not in st.StandingTaskPatch.model_fields


def test_the_patch_columns_come_from_an_allowlist() -> None:
    """The SET clause is built by interpolation, which is safe only while the
    names come from declared model fields — and is asserted rather than
    assumed, because "it can't happen" is how a dynamic SET becomes
    injectable the next time somebody widens the model."""
    tree = ast.parse(inspect.cleandoc(_src(st.update_standing_task)))
    asserts = [n for n in ast.walk(tree) if isinstance(n, ast.Assert)]
    assert asserts, "no allowlist assertion guards the interpolated SET clause"
    assert any("title" in ast.unparse(n) and "prompt" in ast.unparse(n) for n in asserts)


# ── the dark flag is reported honestly ───────────────────────────────


def test_an_unset_flag_reads_as_unknown_not_as_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠ THE honesty point. This is the WEB process; the runner's flag lives
    in the WORKER's environment. Unset here means "cannot tell from here", and
    reporting that as ``False`` would be a confident claim made from the wrong
    process."""
    monkeypatch.delenv("HELIXIS_STANDING_AGENTS_ENABLED", raising=False)
    assert st._runner_enabled() is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("true", True), ("1", True), ("yes", True), ("false", False), ("", False), ("no", False)],
)
def test_an_explicit_flag_is_read_the_same_way_the_worker_reads_it(
    raw: str, expected: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⚠ Both sides (gotcha 28). If this parsed the flag differently from
    ``standing_agents_enabled`` the status line would contradict the runner —
    a status that disagrees with reality is worse than no status."""
    from helixis.webhooks.standing_agents import standing_agents_enabled

    monkeypatch.setenv("HELIXIS_STANDING_AGENTS_ENABLED", raw)
    assert st._runner_enabled() is expected
    assert standing_agents_enabled() is expected


def test_the_list_response_carries_the_runner_state() -> None:
    """A field nothing populates is a field nobody can act on."""
    assert "runner_enabled" in st.StandingTasksOut.model_fields
    assert "_runner_enabled()" in _src(st.list_standing_tasks)


# ── bounds ───────────────────────────────────────────────────────────


def test_there_is_a_per_person_cap() -> None:
    """Each row is a daily LLM run, so an unbounded list is an unbounded daily
    bill — and nobody meaningfully supervises fifty scheduled prompts.

    ⚠ Asserts that the comparison GUARDS the refusal, not that the constant
    appears in the source. The substring version passed against ``if False:``
    — because ``MAX_TASKS_PER_USER`` is also named in the 409 message, so the
    dead branch still mentioned it. Third time on this branch that a
    source-substring guard has been satisfied by the very text explaining the
    thing it was checking.
    """
    assert st.MAX_TASKS_PER_USER > 0
    tree = ast.parse(inspect.cleandoc(_src(st.create_standing_task)))
    guards = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.If)
        and "MAX_TASKS_PER_USER" in ast.unparse(n.test)
        and any(isinstance(b, ast.Raise) for b in ast.walk(n))
    ]
    assert guards, "no live comparison against the cap raises — the limit is decorative"
    assert "409" in ast.unparse(guards[0]), "hitting the cap must refuse, not silently no-op"


def test_the_cadence_allowlist_matches_the_database_constraint() -> None:
    """⚠ Migration 0059 has ``check (cadence in ('daily'))``. Accepting
    "weekly" here would pass pydantic and fail at the database as a 500 —
    a validation error dressed as a server fault."""
    sql = (
        __import__("pathlib")
        .Path(__file__)
        .resolve()
        .parents[2]
        .joinpath("supabase/migrations/0059_standing_tasks.sql")
        .read_text(encoding="utf-8")
    )
    for cadence in st._CADENCES:
        assert f"'{cadence}'" in sql, f"{cadence} is accepted by the API and rejected by the DB"
