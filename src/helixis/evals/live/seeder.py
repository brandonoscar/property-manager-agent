"""Two-company sandbox seeder — idempotent, with a ``--reset``.

Provisions the fixed A/B companies in helixis-test and gives each the data the
live suites need:

  Company A (live-backed)  ─ its own Buildium sandbox credential row, then the
      REAL provisioning path: ``synthetic_traffic`` writes work orders / tenants
      / bills into the live sandbox, and ``backfill_company`` (the same code the
      webhook worker's mirror-sync runs) pulls them into A's mirror. So A's
      mirror holds genuine Buildium-synced state, not hand-written rows.

  Company B (shadow)       ─ a disjoint, KNOWN dataset written straight into the
      mirror (:mod:`fixtures`). One sandbox account maps to one Buildium
      ``AccountId`` and webhook sync routes by it, so a second live company on
      the same sandbox is impossible; B living only in our mirror is also what
      makes the cross-tenant assertion decidable (every B row is known).

Plus the dirty-data records (suite 9) and the redaction seed (suite 4), both on
company A so its own turns surface them.

Idempotent: fixed UUIDs + ``ON CONFLICT`` upserts, so re-running converges
without duplicating. ``reset()`` tombstones every A/B row first.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from helixis.evals.live import config, fixtures
from helixis.settings import Settings, get_settings

if TYPE_CHECKING:
    import asyncpg

logger = logging.getLogger(__name__)

# Conflict target per mirror table. Grounded on the REAL upsert code
# (db/repositories/entities.py), NOT the 0038 migration text: migration 0049
# (mirror_tenancy_unique) made the unique key composite (company_id, buildium_id),
# so ON CONFLICT (buildium_id) alone has no matching constraint on the live DB.
_CONFLICT_KEYS = {
    "helixis_properties": ("company_id", "buildium_id"),
    "helixis_units": ("company_id", "buildium_id"),
    "helixis_tenants": ("company_id", "buildium_id"),
    "helixis_lease_transactions": ("company_id", "buildium_id"),
    "helixis_bills": ("company_id", "buildium_id"),
    "helixis_lease_balances": ("company_id", "lease_id"),
}

# Every mirror table an A/B row can land in — the reset surface.
_MIRROR_TABLES = (
    "helixis_bills",
    "helixis_lease_balances",
    "helixis_lease_transactions",
    "helixis_lease_tenants",
    "helixis_tenants",
    "helixis_units",
    "helixis_properties",
)


@dataclass
class SeedResult:
    reset: bool = False
    companies_ensured: list[str] = field(default_factory=list)
    company_a_live: bool = False
    company_a_note: str = ""
    mirror_rows_written: int = 0
    synthetic_events: int = 0
    backfill_summary: str = ""
    errors: list[str] = field(default_factory=list)

    def ok(self) -> bool:
        return not self.errors


class TwoCompanySeeder:
    """Owns a pool and seeds/resets the two companies."""

    def __init__(self, pool: asyncpg.Pool, settings: Settings | None = None):
        self._pool = pool
        self._settings = settings or get_settings()

    # ── public API ───────────────────────────────────────────────────────

    async def seed(self, *, reset: bool = False, live_company_a: bool = True) -> SeedResult:
        result = SeedResult(reset=reset)
        if reset:
            await self.reset()
        await self._ensure_companies(result)
        await self._seed_shadow_b(result)
        await self._seed_dirty_and_redaction(result)
        if live_company_a:
            await self._provision_company_a_live(result)
        else:
            result.company_a_note = "live provisioning skipped (live_company_a=False)"
        return result

    async def reset(self) -> None:
        """Delete every A/B row: mirror rows, credentials, then the companies.

        Companies cascade to most mirror tables, but we delete explicitly (and
        first) so a partial schema still resets cleanly, and so a re-seed never
        collides with a leftover unique buildium_id."""
        ids = (config.COMPANY_A_ID, config.COMPANY_B_ID)
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                for table in _MIRROR_TABLES:
                    await conn.execute(
                        f"delete from public.{table} where company_id = any($1::uuid[])", list(ids)
                    )
                await conn.execute(
                    "delete from public.company_buildium_credentials "
                    "where company_id = any($1::uuid[])",
                    list(ids),
                )
                await conn.execute(
                    "delete from public.companies where id = any($1::uuid[])", list(ids)
                )
        logger.info("live-eval seeder reset A/B companies")

    # ── steps ────────────────────────────────────────────────────────────

    async def _ensure_companies(self, result: SeedResult) -> None:
        rows = [
            (config.COMPANY_A_ID, config.COMPANY_A_NAME),
            (config.COMPANY_B_ID, config.COMPANY_B_NAME),
        ]
        async with self._pool.acquire() as conn:
            for cid, name in rows:
                await conn.execute(
                    "insert into public.companies (id, name) values ($1, $2) "
                    "on conflict (id) do update set name = excluded.name",
                    cid,
                    name,
                )
                result.companies_ensured.append(cid)

    async def _seed_shadow_b(self, result: SeedResult) -> None:
        rows = fixtures.company_b_rows(config.COMPANY_B_ID, config.COMPANY_B_ACCOUNT_ID)
        n = await self._upsert_rows(rows, result)
        result.mirror_rows_written += n

    async def _seed_dirty_and_redaction(self, result: SeedResult) -> None:
        # Dirty + redaction records live on Company A so A's own turns surface
        # them (suite 9 drives A; suite 4 renders A's tenant).
        rows: list[fixtures.MirrorRow] = []
        for case in fixtures.dirty_cases(config.COMPANY_A_ID, config.COMPANY_B_ACCOUNT_ID):
            rows.extend(case.rows)
        rows.append(fixtures.redaction_seed_row(config.COMPANY_A_ID, config.COMPANY_B_ACCOUNT_ID))
        n = await self._upsert_rows(rows, result)
        result.mirror_rows_written += n

    async def _provision_company_a_live(self, result: SeedResult) -> None:
        caps = config.resolve_capabilities(self._settings)
        reason = caps.skip_reason("buildium_sandbox")
        if reason:
            result.company_a_note = f"NOT live-backed: {reason}"
            return
        try:
            await self._store_company_a_credentials()
            requested = 3
            exit_code = await self._fire_synthetic_traffic(count=requested)
            if exit_code != 0:
                result.errors.append(f"synthetic_traffic exited {exit_code}")
            result.synthetic_events = requested if exit_code == 0 else 0
            summary = await self._backfill_company_a_mirror()
            result.backfill_summary = summary
            result.company_a_live = True
            result.company_a_note = "live-backed (creds stored, sandbox seeded, mirror backfilled)"
        except Exception as exc:  # noqa: BLE001 — record, don't abort the whole seed
            logger.exception("company A live provisioning failed")
            result.errors.append(f"company A live provisioning: {exc!r}")
            result.company_a_note = f"live provisioning errored: {exc!r}"

    async def _store_company_a_credentials(self) -> None:
        """Write A's sandbox creds the same way the API route does (Fernet +
        upsert into company_buildium_credentials)."""
        from helixis.security import CredentialCipher

        b = self._settings.buildium
        cipher = CredentialCipher(self._settings)
        await self._pool.execute(
            """
            insert into public.company_buildium_credentials (
                company_id, encrypted_client_id, encrypted_client_secret,
                client_id_last4, base_url, last_tested_at
            ) values ($1, $2, $3, $4, $5, now())
            on conflict (company_id) do update set
                encrypted_client_id     = excluded.encrypted_client_id,
                encrypted_client_secret = excluded.encrypted_client_secret,
                client_id_last4         = excluded.client_id_last4,
                base_url                = excluded.base_url,
                last_tested_at          = now()
            """,
            config.COMPANY_A_ID,
            cipher.encrypt(b.client_id),
            cipher.encrypt(b.client_secret),
            cipher.last4(b.client_id),
            b.base_url,
        )

    async def _fire_synthetic_traffic(self, count: int = 3) -> int:
        """Seed the live sandbox via the shipped generator. Import + call its
        ``main`` (returns a process exit code: 0 = ok) so we stay in-process and
        share the same sandbox settings + its ``_assert_sandbox`` rail."""
        from scripts import synthetic_traffic

        return await synthetic_traffic.main(count=count, kind="mix")

    async def _backfill_company_a_mirror(self) -> str:
        """Pull the sandbox into A's mirror with the real worker code path."""
        from helixis.buildium.backfill import backfill_company

        res = await backfill_company(
            self._settings.buildium,
            self._pool,
            company_id=config.COMPANY_A_ID,
            account_id=config.COMPANY_A_ACCOUNT_ID,
        )
        return (
            f"properties={res.properties} units={res.units} tenants={res.tenants} "
            f"leases={res.leases} applicants={res.applicants} errors={len(res.errors)}"
        )

    # ── upsert helper ────────────────────────────────────────────────────

    async def _upsert_rows(self, rows: list[fixtures.MirrorRow], result: SeedResult) -> int:
        written = 0
        async with self._pool.acquire() as conn:
            for row in rows:
                try:
                    await self._upsert_one(conn, row)
                    written += 1
                except Exception as exc:  # noqa: BLE001
                    result.errors.append(f"{row.table}: {exc!r}")
                    logger.warning("seed upsert failed for %s: %s", row.table, exc)
        return written

    async def _upsert_one(self, conn: asyncpg.Connection, row: fixtures.MirrorRow) -> None:
        conflict = _CONFLICT_KEYS.get(row.table)
        if conflict is None:
            raise ValueError(f"no conflict key configured for table {row.table!r}")
        cols = list(row.columns)
        placeholders: list[str] = []
        values: list[object] = []
        for i, col in enumerate(cols, start=1):
            val = row.columns[col]
            if isinstance(val, (dict, list)):
                placeholders.append(f"${i}::jsonb")
                values.append(json.dumps(val))
            else:
                placeholders.append(f"${i}")
                values.append(val)
        updatable = [c for c in cols if c not in conflict]
        set_clause = ", ".join(f"{c} = excluded.{c}" for c in updatable) or (
            f"{conflict[0]} = excluded.{conflict[0]}"
        )
        sql = (
            f"insert into public.{row.table} ({', '.join(cols)}) "
            f"values ({', '.join(placeholders)}) "
            f"on conflict ({', '.join(conflict)}) do update set {set_clause}"
        )
        await conn.execute(sql, *values)


async def open_pool(settings: Settings | None = None) -> asyncpg.Pool:
    """Open a small pool against helixis-test from settings.supabase.db_url."""
    import asyncpg

    s = settings or get_settings()
    dsn = s.supabase.db_url
    if not dsn:
        raise RuntimeError("HELIXIS_SUPABASE__DB_URL is not set — cannot reach helixis-test")
    return await asyncpg.create_pool(dsn, min_size=1, max_size=4)
