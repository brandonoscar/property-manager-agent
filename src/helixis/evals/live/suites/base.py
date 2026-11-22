"""Shared suite scaffolding: run context, result types, and the base class.

Every suite answers one question — *does the release candidate hold this safety
property against a live sandbox?* — and returns a uniform :class:`SuiteResult`
the runner turns into a report section and (optionally) a Langfuse dataset run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from helixis.evals.live import config
from helixis.evals.live.driver import AgentDriver
from helixis.evals.live.langfuse_live import ScenarioRecord
from helixis.evals.live.passk import PassKResult
from helixis.settings import Settings, get_settings

if TYPE_CHECKING:
    import asyncpg

    from helixis.evals.live.scoring import MirrorProbe

logger = logging.getLogger(__name__)


class Status(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    SKIPPED = "SKIPPED"
    ERROR = "ERROR"


@dataclass
class ScenarioOutcome:
    """One scenario's gated result — the row that lands in the report table."""

    scenario_id: str
    description: str
    gated_pass: bool
    k: int
    safety_pass_count: int
    safety_failures: list[str] = field(default_factory=list)
    quality_pass_count: int = 0
    quality_notes: list[str] = field(default_factory=list)
    detail: dict[str, object] = field(default_factory=dict)

    @classmethod
    def from_passk(cls, description: str, res: PassKResult) -> ScenarioOutcome:
        """Flatten k runs into the row the report renders.

        ⚠ ``RunOutcome.detail`` IS STILL DROPPED HERE, and that is recorded
        rather than fixed. Each of the k runs carries one — suite 2's holds
        ``{"b_state": …, "leaked": [...]}``, which is the evidence behind a
        cross-tenant verdict — and ``ScenarioOutcome.detail`` is singular, so
        folding them needs a RULE: the last run's, the first safety-failing
        one, or a list of all k. Its only consumer is ``to_record`` →
        Langfuse (the report never renders it), so picking one is a design
        decision about what a dataset row should carry, not a line to change
        while fixing the notes column (gotcha 149's reasoning: wiring a value
        without deciding what its consumer does with it just moves the unread
        value one level out).
        """
        return cls(
            scenario_id=res.scenario,
            description=description,
            gated_pass=res.gated_pass,
            k=res.k,
            safety_pass_count=res.safety_pass_count,
            safety_failures=(res.all_safety_failures() or ([res.error] if res.error else [])),
            quality_pass_count=res.quality_pass_count,
            # ⚠ WITHOUT THIS THE QUALITY REASONS ARE COMPUTED AND THROWN AWAY.
            # ``report.py`` renders ``safety_failures or quality_notes`` in the
            # Notes column, and every suite that builds a ScenarioOutcome BY
            # HAND (5, 8) fills quality_notes — only the two that come through
            # here (2, 9) did not, so a clean run showed the case description
            # where it should have shown "reply cited a dollar figure for a
            # NULL-rent unit". suite09 even carried a comment asking for this
            # fold with no code under it (gotcha 48: a comment cannot fail).
            quality_notes=res.all_quality_failures(),
        )

    def to_record(self, suite: str, input_: dict) -> ScenarioRecord:
        return ScenarioRecord(
            suite=suite,
            scenario_id=self.scenario_id,
            safety_passed=self.gated_pass,
            quality_passed=self.quality_pass_count >= self.k if self.k else self.gated_pass,
            k=self.k,
            input=input_,
            detail=self.detail,
        )


@dataclass
class BugFinding:
    """A real defect the suite surfaced — becomes a GitHub issue (label bug)."""

    title: str
    body: str
    scenario_id: str


@dataclass
class SuiteResult:
    number: int
    name: str
    pass_criterion: str
    status: Status
    outcomes: list[ScenarioOutcome] = field(default_factory=list)
    skip_reason: str = ""
    notes: list[str] = field(default_factory=list)
    bugs: list[BugFinding] = field(default_factory=list)

    @property
    def slug(self) -> str:
        return f"suite{self.number:02d}"

    def finalize(self) -> SuiteResult:
        """Derive PASS/FAIL from the outcomes (unless already SKIPPED/ERROR)."""
        if self.status in (Status.SKIPPED, Status.ERROR):
            return self
        if not self.outcomes:
            self.status = Status.ERROR
            self.notes.append("no scenarios ran")
        else:
            self.status = Status.PASS if all(o.gated_pass for o in self.outcomes) else Status.FAIL
        return self


@dataclass
class RunContext:
    """Shared, lazily-initialised resources handed to every suite."""

    settings: Settings
    caps: config.Capabilities
    rc_sha: str
    driver: AgentDriver
    pool: asyncpg.Pool | None = None  # helixis-test; None if no db_url
    mirror: MirrorProbe | None = None

    @property
    def db(self) -> asyncpg.Pool:
        """Non-optional pool accessor — call only after ``ensure_db()`` returned
        a pool (every suite guards on that first)."""
        if self.pool is None:
            raise RuntimeError("RunContext.db accessed before ensure_db() opened a pool")
        return self.pool

    @property
    def mirror_probe(self) -> MirrorProbe:
        if self.mirror is None:
            raise RuntimeError("RunContext.mirror_probe accessed before ensure_db()")
        return self.mirror

    @classmethod
    def build(cls, settings: Settings | None = None) -> RunContext:
        s = settings or get_settings()
        return cls(
            settings=s,
            caps=config.resolve_capabilities(s),
            rc_sha=config.rc_sha(),
            driver=AgentDriver(s),
        )

    async def ensure_db(self) -> asyncpg.Pool | None:
        """Open the helixis-test pool + mirror probe on first use.

        Also initialises the PROCESS-GLOBAL pool (``helixis.db.init_pool``) that
        ``run_agent_streaming`` reaches for via ``get_pool()`` (company knowledge,
        memory, per-company creds). The app does this in ``main.lifespan``; a
        headless driver must replicate it or the first agent turn raises
        "Postgres pool not initialized". Idempotent — safe to call repeatedly.
        """
        if self.pool is not None:
            return self.pool
        if not self.caps.db_url:
            return None
        from helixis.db import init_pool
        from helixis.evals.live.scoring import MirrorProbe
        from helixis.evals.live.seeder import open_pool

        self.pool = await open_pool(self.settings)
        self.mirror = MirrorProbe(self.pool)
        await init_pool(self.settings.supabase.db_url)
        # helixis_chat_messages.user_id has a FK to auth.users, so the driver must
        # run turns as a REAL user or every turn's chat-history write violates it.
        # Resolve any existing member id (read-only); the rows still land in the
        # synthetic company, which that user isn't a member of (RLS-invisible).
        try:
            real_user = await self.pool.fetchval(
                "select user_id::text from public.company_users limit 1"
            )
            if real_user:
                self.driver.default_user_id = real_user
        except Exception:  # noqa: BLE001 — non-fatal; falls back to synthetic id
            logger.debug("could not resolve a real user id for the driver", exc_info=True)
        return self.pool

    async def member_of(self, company_id: str) -> str | None:
        """A member of ``company_id`` to run turns as, or ``None``.

        ⚠ For a REAL workspace (the Rentvine sandbox run), never the
        ``default_user_id`` above. That is any member of ANY company, so 547
        sandbox chat rows (2026-09-26 to 10-04) were filed under a user who
        does not belong to the sandbox workspace, and the weekly tenant-isolation
        guard has failed on them ever since, where a real leak would now land
        in a job that is already red.
        """
        if self.pool is None:
            return None
        member: str | None = await self.pool.fetchval(
            "select user_id::text from public.company_users "
            "where company_id = $1::uuid order by created_at, user_id limit 1",
            company_id,
        )
        return member

    async def aclose(self) -> None:
        if self.pool is not None:
            await self.pool.close()
            self.pool = None
        from helixis.db import shutdown_pool

        await shutdown_pool()


class Suite:
    """Base class. Subclasses set number/name/pass_criterion + implement _run."""

    number: int = 0
    name: str = ""
    pass_criterion: str = ""
    #: capabilities this suite requires; a missing one → SKIPPED.
    requires: tuple[str, ...] = ()

    def _new_result(self, status: Status = Status.PASS) -> SuiteResult:
        return SuiteResult(
            number=self.number,
            name=self.name,
            pass_criterion=self.pass_criterion,
            status=status,
        )

    async def run(self, ctx: RunContext) -> SuiteResult:
        reason = ctx.caps.skip_reason(*self.requires)
        if reason:
            r = self._new_result(Status.SKIPPED)
            r.skip_reason = reason
            return r
        try:
            result = await self._run(ctx)
        except Exception as exc:  # noqa: BLE001 — a suite crash is an ERROR, not a silent pass
            logger.exception("suite %s crashed", self.slug)
            r = self._new_result(Status.ERROR)
            r.notes.append(f"suite crashed: {exc!r}")
            return r
        return result.finalize()

    async def _run(self, ctx: RunContext) -> SuiteResult:  # pragma: no cover - abstract
        raise NotImplementedError

    @property
    def slug(self) -> str:
        return f"suite{self.number:02d}"
