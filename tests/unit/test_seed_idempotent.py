"""Idempotency test for ``scripts/seed.py``.

Skipped automatically when ``HELIXIS_TEST_DB_URL`` is unset (CI doesn't
have a Postgres instance to seed against). When a DB is available, runs
the seed twice and asserts row counts don't double.

Same skip pattern as ``tests/unit/test_buildium_rls.py``.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

DB_URL = os.environ.get("HELIXIS_TEST_DB_URL", "")

pytestmark = pytest.mark.skipif(
    not DB_URL,
    reason="HELIXIS_TEST_DB_URL not set; idempotency test requires a real Postgres",
)


def _load_seed_module():
    repo_root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "_helixis_seed", repo_root / "scripts" / "seed.py"
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_helixis_seed"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.asyncio
async def test_seed_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Running the seeder twice should not duplicate rows."""
    monkeypatch.setenv("HELIXIS_SEED", "1")
    monkeypatch.setenv("HELIXIS_SUPABASE__DB_URL", DB_URL)

    seed = _load_seed_module()

    import asyncpg

    pool = await asyncpg.create_pool(DB_URL, min_size=1, max_size=2)
    try:
        # Run 1
        rc1 = await seed._run(reset=True, force=True)
        assert rc1 == 0

        async with pool.acquire() as conn:
            counts1 = {
                "companies": await conn.fetchval(
                    "SELECT count(*) FROM public.companies WHERE id = $1",
                    seed.DEMO_COMPANY_ID,
                ),
                "leases": await conn.fetchval(
                    "SELECT count(*) FROM public.helixis_leases WHERE company_id = $1",
                    seed.DEMO_COMPANY_ID,
                ),
                "work_orders": await conn.fetchval(
                    "SELECT count(*) FROM public.helixis_work_orders WHERE company_id = $1",
                    seed.DEMO_COMPANY_ID,
                ),
                "events": await conn.fetchval(
                    "SELECT count(*) FROM public.buildium_events WHERE company_id = $1",
                    seed.DEMO_COMPANY_ID,
                ),
                # activity_log doesn't have an idempotency key — re-runs do
                # accumulate rows here. Track it to confirm that's the only
                # table that grows on re-seed (acceptable for demo data).
                "activity": await conn.fetchval(
                    "SELECT count(*) FROM public.helixis_activity_log WHERE company_id = $1",
                    seed.DEMO_COMPANY_ID,
                ),
            }

        # Run 2 — no --reset, must be idempotent on conflict-keyed tables.
        rc2 = await seed._run(reset=False, force=True)
        assert rc2 == 0

        async with pool.acquire() as conn:
            counts2 = {
                k: await conn.fetchval(
                    f"SELECT count(*) FROM public.{('helixis_' if k in ('leases', 'work_orders', 'activity') else '') + ('helixis_activity_log' if k == 'activity' else 'buildium_events' if k == 'events' else k)} WHERE "  # noqa: E501
                    + ("id = $1" if k == "companies" else "company_id = $1"),
                    seed.DEMO_COMPANY_ID,
                )
                for k in counts1
            }

        # All idempotency-keyed tables: exact equality.
        for k in ("companies", "leases", "work_orders", "events"):
            assert counts1[k] == counts2[k], (
                f"{k}: re-seed changed row count {counts1[k]} -> {counts2[k]}"
            )
        # activity_log: known to grow on re-seed; assert it grew by exactly
        # the seeded batch (10) so we catch any unintended doubling.
        assert counts2["activity"] - counts1["activity"] == 10
    finally:
        await pool.close()
