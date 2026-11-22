"""Suite 10 — RATE LIMIT & RECOVERY (live).

Simulate Buildium 429s and exercise the client's real backoff (#592) live:

  * 10a — transient 429s: the client backs off and EVENTUALLY SUCCEEDS (the
    retry loop actually ran: transport saw >1 call), and the guarded action
    records exactly one success row.
  * 10b — no double-write on retry: the HTTP retry re-sends, but the LOGICAL
    action lands EXACTLY ONCE — one row in ``helixis_action_idempotency``.
  * 10c — persistent 429: retries exhaust, the client raises, and the guard
    records a HONEST failure (failed_retryable), NEVER a phantom success.

State-based throughout — read the idempotency row back, don't trust any text.
The 429 is injected beneath the shipped ``BuildiumClient`` (a real sandbox won't
429 on cue); backoff sleeps are zeroed so the loop is exercised without the wait.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from helixis.agents.idempotency import (
    STATUS_FAILED_RETRYABLE,
    STATUS_SUCCEEDED,
    PreSideEffectError,
    derive_key,
    guarded_action,
)
from helixis.buildium.client import BuildiumApiError
from helixis.db.repositories.action_idempotency import PgIdempotencyStore
from helixis.evals.live import config, faults
from helixis.evals.live.suites.base import (
    BugFinding,
    RunContext,
    ScenarioOutcome,
    Status,
    Suite,
    SuiteResult,
)

_ACTION_TYPE = "live_eval.buildium_create_under_ratelimit"
_IDEM_TABLE = "helixis_action_idempotency"
# Below the client's _MAX_RETRIES (5) so the retry loop can recover.
_TRANSIENT_429S = 3


class RateLimitSuite(Suite):
    number = 10
    name = "Rate limit & recovery (live)"
    pass_criterion = (
        "Under injected 429s the client backs off and either succeeds (retry "
        "loop demonstrably ran) or, on persistent 429, fails HONESTLY "
        "(failed_retryable, no phantom success). On retry the logical action "
        "lands exactly once (one helixis_action_idempotency row). State-based."
    )
    requires = ("db_url", "buildium_creds")

    async def _run(self, ctx: RunContext) -> SuiteResult:
        result = self._new_result()
        pool = await ctx.ensure_db()
        if pool is None:
            result.status = Status.SKIPPED
            result.skip_reason = "no helixis-test pool"
            return result
        store = PgIdempotencyStore(pool)

        await self._scenario_transient_recovers(ctx, store, result)
        await self._scenario_persistent_honest_failure(ctx, store, result)
        return result

    async def _prep_key(self, ctx: RunContext, discriminator: str) -> str:
        value = {"scenario": discriminator, "unit": "A-202", "category": "Electrical"}
        key = derive_key(config.COMPANY_A_ID, _ACTION_TYPE, value)
        await ctx.db.execute(
            f"delete from public.{_IDEM_TABLE} where company_id = $1 and idem_key = $2",
            config.COMPANY_A_ID,
            key,
        )
        return key

    async def _idem_row(self, ctx: RunContext, key: str) -> dict[str, Any] | None:
        row = await ctx.db.fetchrow(
            f"select status from public.{_IDEM_TABLE} where company_id = $1 and idem_key = $2",
            config.COMPANY_A_ID,
            key,
        )
        return dict(row) if row else None

    async def _idem_count(self, ctx: RunContext, key: str) -> int:
        return int(
            await ctx.db.fetchval(
                f"select count(*) from public.{_IDEM_TABLE} "
                "where company_id = $1 and idem_key = $2",
                config.COMPANY_A_ID,
                key,
            )
        )

    async def _scenario_transient_recovers(
        self, ctx: RunContext, store: PgIdempotencyStore, result: SuiteResult
    ) -> None:
        key = await self._prep_key(ctx, "transient_429")
        state: dict[str, Any] = {"attempts": 0}

        async def execute() -> dict:
            fi = faults.faulty_buildium_client(
                ctx.settings.buildium,
                status=429,
                fail_first_n=_TRANSIENT_429S,
                success_payload={"Id": 556_001},
            )
            with faults.no_backoff_sleep():
                async with fi:
                    try:
                        resp = await fi.client.request(
                            "POST", "/workorders", body={"unit": "A-202"}
                        )
                    except BuildiumApiError as exc:
                        raise PreSideEffectError(f"buildium {exc.status}") from exc
            state["attempts"] = fi.counter.calls
            return resp or {"Id": 556_001}

        await guarded_action(
            store=store,
            company_id=config.COMPANY_A_ID,
            idem_key=key,
            action_type=_ACTION_TYPE,
            confirm=None,
            execute=execute,
            window=timedelta(minutes=5),
        )
        row = await self._idem_row(ctx, key)
        count = await self._idem_count(ctx, key)
        failures: list[str] = []
        if state["attempts"] <= 1:
            failures.append(f"backoff loop did not run (transport saw {state['attempts']} call)")
        if state["attempts"] != _TRANSIENT_429S + 1:
            failures.append(
                f"expected {_TRANSIENT_429S + 1} attempts (retried through 429s then 200), "
                f"saw {state['attempts']}"
            )
        if not row or row["status"] != STATUS_SUCCEEDED:
            failures.append(f"status={row and row['status']}, expected succeeded after backoff")
        if count != 1:
            failures.append(f"{count} idempotency rows, expected exactly 1 (no double-write)")
        self._record(
            result,
            "10a_transient_backoff_recovers",
            "Transient 429s → backoff recovers, exactly one row",
            failures,
        )

    async def _scenario_persistent_honest_failure(
        self, ctx: RunContext, store: PgIdempotencyStore, result: SuiteResult
    ) -> None:
        key = await self._prep_key(ctx, "persistent_429")

        async def execute() -> dict:
            fi = faults.faulty_buildium_client(ctx.settings.buildium, status=429, fail_first_n=None)
            with faults.no_backoff_sleep():
                async with fi:
                    try:
                        await fi.client.request("POST", "/workorders", body={"unit": "A-202"})
                    except BuildiumApiError as exc:
                        # Retries exhausted; the write never landed → pre-side-effect.
                        raise PreSideEffectError(f"buildium {exc.status} after retries") from exc
            return {"Id": 1}  # unreachable

        raised = False
        try:
            await guarded_action(
                store=store,
                company_id=config.COMPANY_A_ID,
                idem_key=key,
                action_type=_ACTION_TYPE,
                confirm=None,
                execute=execute,
                window=timedelta(minutes=5),
            )
        except PreSideEffectError:
            raised = True
        row = await self._idem_row(ctx, key)
        failures: list[str] = []
        if not raised:
            failures.append("guard did not surface the exhausted-retry failure")
        if not row or row["status"] != STATUS_FAILED_RETRYABLE:
            failures.append(
                f"status={row and row['status']}, expected failed_retryable (honest failure)"
            )
        self._record(
            result,
            "10b_persistent_honest_failure",
            "Persistent 429 → retries exhaust, honest failure, no phantom success",
            failures,
        )

    def _record(
        self, result: SuiteResult, scenario_id: str, desc: str, failures: list[str]
    ) -> None:
        result.outcomes.append(
            ScenarioOutcome(
                scenario_id=scenario_id,
                description=desc,
                gated_pass=not failures,
                k=1,
                safety_pass_count=0 if failures else 1,
                safety_failures=failures,
                quality_pass_count=1,
            )
        )
        if failures:
            result.bugs.append(
                BugFinding(
                    title=f"[live-eval][rate-limit] {desc} — recovery contract violated",
                    body=(
                        f"Suite 10 scenario `{scenario_id}` failed against helixis-test.\n\n"
                        + "\n".join(f"- {f}" for f in failures)
                        + "\n\nExercised the shipped BuildiumClient backoff (#592) with injected "
                        "429s; verdict read from `helixis_action_idempotency`."
                    ),
                    scenario_id=scenario_id,
                )
            )
