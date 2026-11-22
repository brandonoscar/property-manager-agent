"""Has the cost of a turn drifted past its baseline? (finding #60's missing consumer)

`evals/cost_budgets.py` could flag a cost regression from the day it landed and
never has, because nothing called it: every baseline was null AND no code path
produced the task-class string it takes. `evals/cost_drift.py` is the producer;
this is the runner.

    python scripts/cost_drift_check.py --days 14          # needs the DB URL
    python scripts/cost_drift_check.py --days 14 --json

Exit 0 = within budget (or no data). Exit 1 = a metric is more than
`over_budget_threshold` above its baseline. Read-only — one SELECT.

⚠ It compares the WINDOW's median against the baseline median, never a single
run against it: a per-run check against a p50 fires on half of all runs and
teaches the reader to ignore it. The regression this exists to catch is
gotcha 37's shape — a 3x change that hid for a month because the number came
off one day — and that is only visible in aggregate.

⚠ The query is DELIBERATELY GLOBAL (gotcha 47). Latency and tool count are
properties of the model and the harness, not of a tenant, and no single tenant
has adequate n. It reads timings and tool NAMES only — never `user_query`,
never `final_text`, only whether prose exists.

⚠ Baselines are Anthropic-era (gotcha 18). If the provider changes again the
numbers in `config/eval_budgets.yaml` describe a system that no longer exists —
re-measure rather than reading the drift as a regression. That is gotcha 48's
expired-justification shape pointed at a cost control.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from helixis.evals.cost_budgets import check_all, load_budgets  # noqa: E402
from helixis.evals.cost_drift import RunRecord, summarise  # noqa: E402

#: Turns before this ran on Gemini and describe a different system (gotcha 18).
ANTHROPIC_ERA_START = "2026-07-21"

#: ⚠ Per-run cost/tokens live in ``raw->'usage'`` (llm/turn_cost), NOT in
#: ``helixis_llm_usage`` — that one is keyed (company_id, day) and cannot be
#: joined to a run. Reading only the daily ledger is how this module first
#: shipped claiming per-run cost did not exist. A run before 2026-08-26 has no
#: usage object and yields NULLs, which ``summarise`` skips rather than zeroes.
#: ``cost_is_floor`` marks a total LiteLLM could not fully price; it is dropped
#: rather than averaged, because a floor presented as a total is gotcha 25's
#: $0.00 bill.
_QUERY = """
select r.latency_ms,
       coalesce(length(trim(r.final_text)), 0) > 0 as has_prose,
       coalesce(
         (select array_agg(e->>'name') from jsonb_array_elements(r.tool_calls) e),
         '{}'
       ) as tool_names,
       case when coalesce((r.raw->'usage'->>'cost_is_floor')::boolean, false)
            then null else (r.raw->'usage'->>'cost_usd')::float end as usd,
       case when coalesce((r.raw->'usage'->>'cost_is_floor')::boolean, false)
            then null
            else (r.raw->'usage'->>'prompt_tokens')::int
                 + (r.raw->'usage'->>'completion_tokens')::int end as tokens
  from orchestrator_runs r
 where r.status = 'succeeded'
   and r.latency_ms is not null
   and r.tool_calls is not null
   and r.started_at >= greatest($1::timestamptz, now() - ($2 || ' days')::interval)
"""


async def _fetch(db_url: str, days: int) -> list[RunRecord]:
    import asyncpg

    conn = await asyncpg.connect(db_url)
    try:
        rows = await conn.fetch(_QUERY, ANTHROPIC_ERA_START, str(days))
    finally:
        await conn.close()
    return [
        RunRecord(
            latency_ms=r["latency_ms"],
            tool_names=tuple(r["tool_names"] or ()),
            has_prose=bool(r["has_prose"]),
            usd=r["usd"],
            tokens=r["tokens"],
        )
        for r in rows
    ]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=14, help="window size (default 14)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    db_url = os.environ.get("HELIXIS_SUPABASE__DB_URL", "").strip()
    if not db_url:
        # NOT exit 0. A cost check that could not run is not a cost check that
        # passed — the LED-013 skip-floor rule (gotcha 35).
        print("HELIXIS_SUPABASE__DB_URL is not set — cannot measure.", file=sys.stderr)
        return 2

    records = asyncio.run(_fetch(db_url, args.days))
    measured = summarise(records)
    budgets = load_budgets()
    violations = check_all(measured, budgets)

    if args.json:
        print(
            json.dumps(
                {
                    "window_days": args.days,
                    "runs": len(records),
                    "measured": measured,
                    "violations": [str(v) for v in violations],
                },
                indent=2,
            )
        )
    else:
        print(f"{len(records)} runs in the last {args.days} days (Anthropic era only)")
        if not measured:
            print("no classifiable answered runs — nothing to compare")
        for tc, metrics in sorted(measured.items()):
            pretty = ", ".join(f"{m}={v:g}" for m, v in sorted(metrics.items()))
            print(f"  {tc}: {pretty}")
        for v in violations:
            print(f"OVER BUDGET  {v}")

    if not measured:
        # No qualifying runs is not evidence of health, but it is also not a
        # regression — say so and pass, the way the smoke checks treat a dark
        # dependency. A LOUD zero would cry wolf on any quiet fortnight.
        return 0
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
