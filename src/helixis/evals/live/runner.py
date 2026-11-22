"""CLI entry: run the live release-gate suites, seed, report, and log to Langfuse.

    python -m helixis.evals.live.runner [--suite N ...] [--seed] [--reset]
                                        [--report PATH] [--no-langfuse]

Exit codes (mirror the golden runner): 0 = every run suite PASS, 1 = a suite
FAIL/ERROR, 2 = nothing gating ran (all SKIPPED). A SKIP is never a pass.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from helixis.evals.live.langfuse_live import LiveEvalLangfuse
from helixis.evals.live.report import render_report, report_filename
from helixis.evals.live.suites.base import RunContext, Status, Suite, SuiteResult
from helixis.evals.live.suites.suite02_cross_tenant import CrossTenantSuite
from helixis.evals.live.suites.suite04_redaction import RedactionSuite
from helixis.evals.live.suites.suite05_hallucinated_success import HallucinatedSuccessSuite
from helixis.evals.live.suites.suite08_personas import PersonaSuite
from helixis.evals.live.suites.suite09_dirty_data import DirtyDataSuite
from helixis.evals.live.suites.suite10_rate_limit import RateLimitSuite
from helixis.settings import get_settings

logger = logging.getLogger(__name__)

_SUITES: dict[int, type[Suite]] = {
    2: CrossTenantSuite,
    4: RedactionSuite,
    5: HallucinatedSuccessSuite,
    8: PersonaSuite,
    9: DirtyDataSuite,
    10: RateLimitSuite,
}
_REPORTS_DIR = Path("eval-reports")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="helixis.evals.live.runner")
    p.add_argument(
        "--suite",
        type=int,
        action="append",
        choices=sorted(_SUITES),
        default=None,
        help="run only this suite (repeatable); default all",
    )
    p.add_argument("--seed", action="store_true", help="seed the two companies before running")
    p.add_argument("--reset", action="store_true", help="reset (wipe A/B) before seeding")
    p.add_argument(
        "--no-live-a",
        action="store_true",
        help="skip live Buildium provisioning for company A during seed",
    )
    p.add_argument("--report", type=str, default=None, help="report path (default eval-reports/…)")
    p.add_argument("--no-langfuse", action="store_true", help="disable Langfuse dataset logging")
    p.add_argument("--json", action="store_true", help="print a machine-readable summary")
    return p.parse_args(argv)


async def run(argv: list[str]) -> int:
    args = _parse_args(argv)
    settings = get_settings()
    ctx = RunContext.build(settings)
    logger.info("live-eval run rc=%s caps=%s", ctx.rc_sha, ctx.caps)

    if args.seed or args.reset:
        await _seed(ctx, reset=args.reset, live_a=not args.no_live_a)

    suite_ids = args.suite or sorted(_SUITES)
    results: list[SuiteResult] = []
    try:
        for sid in suite_ids:
            suite = _SUITES[sid]()
            logger.info("running suite %d — %s", sid, suite.name)
            result = await suite.run(ctx)
            results.append(result)
            print(f"[suite {sid:>2}] {result.status.value:8} {suite.name}")
    finally:
        await ctx.aclose()

    _log_langfuse(ctx, results, disabled=args.no_langfuse)
    lf_active = ctx.caps.langfuse and not args.no_langfuse
    report = render_report(results, rc_sha=ctx.rc_sha, langfuse_active=lf_active)
    path = _write_report(report, args.report, ctx.rc_sha)
    print(f"\nreport -> {path}")
    _print_bug_stubs(results)

    return _exit_code(results)


async def _seed(ctx: RunContext, *, reset: bool, live_a: bool) -> None:
    from helixis.evals.live.seeder import TwoCompanySeeder, open_pool

    if not ctx.caps.db_url:
        print("cannot seed: HELIXIS_SUPABASE__DB_URL unset")
        return
    pool = await open_pool(ctx.settings)
    try:
        seeder = TwoCompanySeeder(pool, ctx.settings)
        res = await seeder.seed(reset=reset, live_company_a=live_a)
        print(
            f"[seed] companies={res.companies_ensured} mirror_rows={res.mirror_rows_written} "
            f"a_live={res.company_a_live} note={res.company_a_note!r}"
        )
        for err in res.errors:
            print(f"[seed][error] {err}")
    finally:
        await pool.close()


def _log_langfuse(ctx: RunContext, results: list[SuiteResult], *, disabled: bool) -> None:
    if disabled or not ctx.caps.langfuse:
        return
    sink = LiveEvalLangfuse(ctx.settings)
    records = [
        o.to_record(r.slug, {"description": o.description}) for r in results for o in r.outcomes
    ]
    if records:
        n = sink.log_run(ctx.rc_sha, records)
        print(f"[langfuse] logged {n} scenarios as dataset run rc-{ctx.rc_sha}")


def _write_report(report: str, explicit: str | None, rc_sha: str) -> Path:
    if explicit:
        path = Path(explicit)
    else:
        _REPORTS_DIR.mkdir(exist_ok=True)
        path = _REPORTS_DIR / report_filename(rc_sha)
    path.write_text(report, encoding="utf-8")
    return path


def _print_bug_stubs(results: list[SuiteResult]) -> None:
    bugs = [b for r in results for b in r.bugs]
    if not bugs:
        return
    print(f"\n{len(bugs)} bug(s) to file (label: bug):")
    for b in bugs:
        print(f"  - {b.title}")


def _exit_code(results: list[SuiteResult]) -> int:
    if any(r.status in (Status.FAIL, Status.ERROR) for r in results):
        return 1
    # ⚠ EMPTY IS ALSO "nothing gating ran", and the old ``results and all(…)``
    # returned 0 for it — the skip-floor inverted on the one function that IS
    # the skip-floor. Unreachable through the CLI today (``--suite`` carries
    # ``choices`` so ``suite_ids`` is never empty), so this changes no current
    # output; it is here because a caller that one day passes an empty list
    # must not read "nothing ran" as "everything passed".
    if not results or all(r.status is Status.SKIPPED for r in results):
        return 2
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(asyncio.run(run(sys.argv[1:])))


if __name__ == "__main__":
    main()
