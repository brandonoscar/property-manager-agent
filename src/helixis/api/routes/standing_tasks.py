"""Standing tasks — the writer the runner never had.

``webhooks/standing_agents.py`` polls ``standing_tasks`` every 300s, claims a
due row, runs its prompt through the orchestrator scoped to (company, user)
and delivers the output to that person's Inbox. It is wired into the worker
and it has never run anything, because **nothing can create a row**: migration
0059 says so in as many words — *"No API writes to this table exist yet (v1
rows are inserted manually / by a future settings UI)"* — and the table holds
0 rows across every company.

This is that slice. Four routes, and three decisions worth stating because
each could reasonably have gone the other way:

⚠ **Self-only, deliberately.** A standing task runs AS its owner and delivers
to THEIR Inbox, so letting a manager create one for somebody else means making
another person's agent run a prompt they did not write. That is a different
feature with a different consent story; ``user_id`` is always the caller's,
never a body field. Reading, editing and deleting are equally scoped — a
company-wide list would let anyone read a colleague's stored prompts.

⚠ **No role floor, and that is the reason for the scoping above.** Anyone may
schedule work for themselves; the floor would only make sense if the target
were somebody else, and it cannot be.

⚠ **The status line tells the truth about the dark flag.** The runner is off
unless the WORKER process sets ``HELIXIS_STANDING_AGENTS_ENABLED``, so a task
created today may never fire. Accepting it silently would be this product's
recurring failure — a thing that looks configured and does nothing — so the
create response and the status endpoint both say whether the runner is live.
⚠ The web process cannot read the worker's env, so ``runner_enabled`` is
reported as UNKNOWN rather than guessed: claiming "live" from the wrong
process would be worse than saying we cannot tell.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from helixis.api.auth import AuthenticatedUser, get_current_user
from helixis.db.engine import get_pool

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/standing-tasks", tags=["standing-tasks"])

#: v1 cadence, matching the CHECK constraint in migration 0059. A body that
#: offered "weekly" would be accepted by pydantic and rejected by Postgres.
_CADENCES = ("daily",)

#: Per person. A standing task is a scheduled LLM run, so an unbounded list is
#: an unbounded daily bill — and nobody meaningfully supervises fifty.
MAX_TASKS_PER_USER = 10


def _company_id(user: AuthenticatedUser) -> str:
    if not user.company_ids:
        raise HTTPException(status_code=400, detail="User is not a member of any company")
    return user.company_ids[0]


class StandingTaskIn(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    prompt: str = Field(min_length=1, max_length=4000)
    hour_utc: int = Field(default=12, ge=0, le=23)
    cadence: str = "daily"
    enabled: bool = True


class StandingTaskPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    prompt: str | None = Field(default=None, min_length=1, max_length=4000)
    hour_utc: int | None = Field(default=None, ge=0, le=23)
    enabled: bool | None = None


class StandingTaskOut(BaseModel):
    id: int
    title: str
    prompt: str
    hour_utc: int
    cadence: str
    enabled: bool
    last_run_at: str | None = None


class StandingTasksOut(BaseModel):
    tasks: list[StandingTaskOut]
    #: ⚠ Tri-state on purpose. The runner's flag lives in the WORKER process,
    #: which this one cannot read, so "unknown" is the honest answer and the
    #: only alternative is a guess presented as fact.
    runner_enabled: bool | None = None
    max_per_user: int = MAX_TASKS_PER_USER


def _runner_enabled() -> bool | None:
    raw = os.getenv("HELIXIS_STANDING_AGENTS_ENABLED")
    if raw is None:
        return None
    return raw.strip().lower() in ("1", "true", "yes")


def _out(row: Any) -> StandingTaskOut:
    r: dict[str, Any] = dict(row)
    last = r.get("last_run_at")
    return StandingTaskOut(
        id=r["id"],
        title=r["title"],
        prompt=r["prompt"],
        hour_utc=r["hour_utc"],
        cadence=r["cadence"],
        enabled=r["enabled"],
        last_run_at=last.isoformat() if last else None,
    )


@router.get("", response_model=StandingTasksOut)
async def list_standing_tasks(
    user: AuthenticatedUser = Depends(get_current_user),
) -> StandingTasksOut:
    """The CALLER'S standing tasks. Never a colleague's — these are stored
    prompts, and one person's are not another's to read."""
    rows = await get_pool().fetch(
        "SELECT id, title, prompt, hour_utc, cadence, enabled, last_run_at "
        "FROM public.standing_tasks "
        "WHERE company_id = $1::uuid AND user_id = $2::uuid "
        "ORDER BY hour_utc, id",
        _company_id(user),
        user.user_id,
    )
    return StandingTasksOut(tasks=[_out(r) for r in rows], runner_enabled=_runner_enabled())


@router.post("", response_model=StandingTaskOut, status_code=201)
async def create_standing_task(
    body: StandingTaskIn, user: AuthenticatedUser = Depends(get_current_user)
) -> StandingTaskOut:
    """Schedule a prompt to run daily for YOURSELF."""
    if body.cadence not in _CADENCES:
        raise HTTPException(
            status_code=422,
            detail=f"cadence must be one of {', '.join(_CADENCES)} (v1 supports daily only)",
        )
    company_id, pool = _company_id(user), get_pool()
    existing = await pool.fetchval(
        "SELECT count(*) FROM public.standing_tasks "
        "WHERE company_id = $1::uuid AND user_id = $2::uuid",
        company_id,
        user.user_id,
    )
    if existing >= MAX_TASKS_PER_USER:
        raise HTTPException(
            status_code=409,
            detail=(
                f"You already have {existing} standing tasks (limit "
                f"{MAX_TASKS_PER_USER}). Delete one before adding another."
            ),
        )
    row = await pool.fetchrow(
        "INSERT INTO public.standing_tasks "
        "  (company_id, user_id, title, prompt, cadence, hour_utc, enabled) "
        "VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, $7) "
        "RETURNING id, title, prompt, hour_utc, cadence, enabled, last_run_at",
        company_id,
        user.user_id,
        body.title.strip(),
        body.prompt.strip(),
        body.cadence,
        body.hour_utc,
        body.enabled,
    )
    return _out(row)


@router.patch("/{task_id}", response_model=StandingTaskOut)
async def update_standing_task(
    task_id: int, body: StandingTaskPatch, user: AuthenticatedUser = Depends(get_current_user)
) -> StandingTaskOut:
    """Edit or pause one of YOUR standing tasks.

    ⚠ ``last_run_at`` is deliberately not editable. The worker claims a due
    row by stamping it BEFORE the run, which is the whole duplicate-run
    defence; letting a client clear it would hand anyone a way to re-run a
    task arbitrarily often, each run a billed LLM call.
    """
    fields = body.model_dump(exclude_none=True)
    if not fields:
        raise HTTPException(status_code=422, detail="Nothing to update.")
    # ⚠ Column names come from an ALLOWLIST, not from the request. They are the
    # keys of a pydantic model with four declared fields, so nothing a caller
    # sends can reach the SET clause — but the allowlist is asserted anyway,
    # because "it can't happen" is how a dynamic SET clause becomes injectable
    # the next time somebody widens the model.
    assert set(fields) <= {"title", "prompt", "hour_utc", "enabled"}
    sets = ", ".join(f"{name} = ${i + 3}" for i, name in enumerate(fields))
    sql = (
        f"UPDATE public.standing_tasks SET {sets} "  # noqa: S608 — allowlisted above
        f"WHERE company_id = $1::uuid AND user_id = $2::uuid AND id = ${len(fields) + 3} "
        "RETURNING id, title, prompt, hour_utc, cadence, enabled, last_run_at"
    )
    row = await get_pool().fetchrow(sql, _company_id(user), user.user_id, *fields.values(), task_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Standing task not found.")
    return _out(row)


@router.delete("/{task_id}", status_code=204)
async def delete_standing_task(
    task_id: int, user: AuthenticatedUser = Depends(get_current_user)
) -> None:
    result = await get_pool().execute(
        "DELETE FROM public.standing_tasks "
        "WHERE company_id = $1::uuid AND user_id = $2::uuid AND id = $3",
        _company_id(user),
        user.user_id,
        task_id,
    )
    if str(result).split()[-1] == "0":
        raise HTTPException(status_code=404, detail="Standing task not found.")
