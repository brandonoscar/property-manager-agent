"""Per-person standing agents v1 (upgrade 3) — dark by default.

Each employee can have STANDING TASKS: a stored prompt their agent runs
on a schedule (v1: daily at ``hour_utc``), with the result delivered to
their Inbox — the multiplayer vision's second brick, built directly on
the first (the ``Helixis.TaskRequest`` team-task pipeline from the
teammate-task PR). Nothing here sends anything external: the output of a
standing run lands ONLY in the owner's Inbox as a reviewable card, same
as a teammate-sent task.

Dark until the worker env sets ``HELIXIS_STANDING_AGENTS_ENABLED=true``
(same os.getenv gate as email ingest — a worker-process flag, not a
Settings field, so flipping it can't affect the web service).

Duplicate-run discipline: a due task is CLAIMED (``last_run_at`` updated)
*before* its LLM run starts — a crash mid-run costs that day's run
(logged loudly) rather than double-billing the run on the next poll.
The claim UPDATE is guarded by the same due-predicate it was selected
with, so two overlapping poll cycles can't both claim one task.

Rows live in ``standing_tasks`` (migration 0059 — applied manually, no
runner: gotcha 8).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from helixis.agents.team_task_tools import member_label
from helixis.db.repositories.members import list_members
from helixis.memory.context import memory_context
from helixis.settings import Settings
from helixis.webhooks.team_tasks import enqueue_team_task

logger = logging.getLogger(__name__)

STANDING_POLL_INTERVAL_SECONDS = 300


def standing_agents_enabled() -> bool:
    """Worker-process opt-in — same shape as HELIXIS_EMAIL_INGEST_ENABLED."""
    return os.getenv("HELIXIS_STANDING_AGENTS_ENABLED", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


@dataclass(frozen=True)
class StandingTask:
    """One row of ``standing_tasks`` — a user's scheduled agent prompt."""

    id: int
    company_id: str
    user_id: str
    title: str
    prompt: str
    hour_utc: int
    last_run_at: datetime | None


def is_due(task: StandingTask, now: datetime) -> bool:
    """Pure due-predicate: today's scheduled hour has passed and the task
    hasn't run today (UTC calendar-day semantics).

    A task created with ``hour_utc`` later than the current hour waits for
    that hour; one that already ran today waits for tomorrow. A task that
    has NEVER run is due as soon as its hour passes — including on the day
    it's created.
    """
    if now.hour < task.hour_utc:
        return False
    if task.last_run_at is None:
        return True
    return task.last_run_at.astimezone(UTC).date() < now.astimezone(UTC).date()


async def fetch_due_tasks(pool: Any, now: datetime) -> list[StandingTask]:
    """All enabled standing tasks currently due, oldest-claimed first."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, company_id, user_id, title, prompt, hour_utc, last_run_at
              FROM standing_tasks
             WHERE enabled
             ORDER BY last_run_at NULLS FIRST, id
            """
        )
    tasks = [
        StandingTask(
            id=r["id"],
            company_id=str(r["company_id"]),
            user_id=str(r["user_id"]),
            title=r["title"],
            prompt=r["prompt"],
            hour_utc=r["hour_utc"],
            last_run_at=r["last_run_at"],
        )
        for r in rows
    ]
    return [t for t in tasks if is_due(t, now)]


async def claim_task(pool: Any, task: StandingTask, now: datetime) -> bool:
    """Atomically claim a due task by stamping ``last_run_at``.

    The WHERE clause re-checks the due-predicate server-side, so two
    overlapping poll cycles can't both claim the same task; the loser
    gets rowcount 0 and skips.
    """
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE standing_tasks
               SET last_run_at = $2
             WHERE id = $1
               AND enabled
               AND (last_run_at IS NULL OR last_run_at::date < $2::date)
            """,
            task.id,
            now,
        )
    return bool(result == "UPDATE 1")


async def deliver_result(pool: Any, task: StandingTask, result_text: str) -> bool:
    """Land the run's output in the owner's Inbox via the team-task
    pipeline — the exact rails a teammate-sent task rides, so the Inbox
    card, planner enrichment, and dedup identity all come for free."""
    to_name = f"user {task.user_id[:8]}"
    try:
        members = await list_members(pool, company_id=task.company_id)
        for m in members:
            if m.user_id == task.user_id:
                to_name = member_label(m)
                break
    except Exception:  # noqa: BLE001 — display-name lookup is cosmetic
        logger.debug("standing_agents: member lookup failed", exc_info=True)

    return await enqueue_team_task(
        pool,
        company_id=task.company_id,
        from_user_id=f"standing-agent:{task.id}",
        from_name=f"Your Occupella agent — {task.title}",
        to_user_id=task.user_id,
        to_name=to_name,
        title=task.title,
        details=result_text,
    )


async def run_standing_task(
    pool: Any,
    settings: Settings,
    task: StandingTask,
    composio_client: Any = None,
) -> bool:
    """Run one standing task's prompt through the full orchestrator and
    deliver the answer to the owner's Inbox.

    The run is scoped exactly like a chat turn by that user: memory
    context binds (company_id, user_id), so recall/extraction stay
    company-partitioned, and any write tool the agent reaches is still
    confirm-gated — with no one present to approve, gated writes abort,
    which is precisely the standing-agent contract (read, analyze,
    report; never act unattended).
    """
    from agents import Runner  # lazy: keeps worker import cheap when dark

    from helixis.agents.orchestrator import create_orchestrator

    with memory_context(
        company_id=task.company_id,
        user_id=task.user_id,
        session_id=f"standing-{task.id}",
    ):
        orchestrator = create_orchestrator(
            settings,
            composio_client=composio_client,
            company_id=task.company_id,
            composio_user_id=task.user_id,
        )
        result = await Runner.run(
            orchestrator,
            task.prompt,
            max_turns=settings.agent.max_iterations,
        )
    text = str(getattr(result, "final_output", "") or "").strip()
    if not text:
        logger.warning("standing_agents: task id=%s produced no output", task.id)
        return False
    return await deliver_result(pool, task, text)


async def run_standing_cycle(
    pool: Any,
    settings: Settings,
    composio_client: Any = None,
    now: datetime | None = None,
) -> int:
    """One poll cycle: claim + run every due standing task. Per-task
    failures are contained (one bad prompt never starves the rest);
    returns the number of tasks that delivered."""
    now = now or datetime.now(UTC)
    delivered = 0
    for task in await fetch_due_tasks(pool, now):
        try:
            if not await claim_task(pool, task, now):
                continue  # another cycle won the claim
            if await run_standing_task(pool, settings, task, composio_client):
                delivered += 1
        except (Exception, SystemExit):  # noqa: BLE001 — gotcha 7 discipline
            logger.exception(
                "standing_agents: task id=%s failed — day's run skipped (claimed)",
                task.id,
            )
    return delivered


__all__ = [
    "STANDING_POLL_INTERVAL_SECONDS",
    "StandingTask",
    "claim_task",
    "deliver_result",
    "fetch_due_tasks",
    "is_due",
    "run_standing_cycle",
    "run_standing_task",
    "standing_agents_enabled",
]
