"""Company knowledge MD scaffold (ADR 0006).

Covers the source rollup rendering, the distiller's degrade-gracefully behavior
(no-signal placeholder / LLM-failure fallback / happy path), the per-day
idempotency guard, and the daily cycle's enabled-flag gate + per-company
iteration. No real DB or LLM — a fake pool + an injected ``completion_fn``.
"""

from __future__ import annotations

import datetime
from typing import Any

import pytest

from helixis.company_knowledge import generator, sources
from helixis.company_knowledge.generator import (
    build_profile_markdown,
    generate_for_company,
    run_company_knowledge_cycle,
)
from helixis.company_knowledge.sources import CompanyFacts, facts_to_prompt_block
from helixis.db.pms_coverage import Coverage
from helixis.settings import Settings

TODAY = datetime.date(2026, 7, 4)


def _settings(*, enabled: bool = True) -> Settings:
    return Settings(
        env="test",
        company_knowledge={"enabled": enabled, "inject": True, "activity_days": 30},
    )


# ---- sources ---------------------------------------------------------------


def test_has_any_signal() -> None:
    assert not CompanyFacts().has_any_signal()
    assert CompanyFacts(lease_count=3).has_any_signal()
    assert CompanyFacts(documents=[("Lease", "lease")]).has_any_signal()
    assert CompanyFacts(activity_by_category={"maintenance": 2}).has_any_signal()


def test_facts_to_prompt_block_is_deterministic_and_sorted() -> None:
    facts = CompanyFacts(
        lease_count=40,
        work_order_count=12,
        open_work_order_count=3,
        activity_by_category={"leasing": 2, "maintenance": 9},
        documents=[("Standard Lease", "lease"), ("Pet Addendum", "addendum")],
    )
    block = facts_to_prompt_block(facts)
    # Activity is sorted by count desc → maintenance (9) before leasing (2).
    assert block.index("maintenance: 9") < block.index("leasing: 2")
    assert "Leases on file (mirrored): 40" in block
    assert "12 (3 open)" in block
    assert "Standard Lease (lease)" in block


# ---- distiller (build_profile_markdown) ------------------------------------


async def test_build_profile_no_signal_returns_placeholder() -> None:
    md = await build_profile_markdown(CompanyFacts(), _settings())
    assert "still learning" in md.lower()


async def test_build_profile_uses_llm_when_signal_present() -> None:
    async def fake_completion(**_: Any) -> Any:
        class _Msg:
            content = "## Portfolio\n\nA tidy 40-lease residential book."

        class _Choice:
            message = _Msg()

        class _Resp:
            choices = [_Choice()]

        return _Resp()

    md = await build_profile_markdown(
        CompanyFacts(lease_count=40), _settings(), completion_fn=fake_completion
    )
    assert "tidy 40-lease" in md


async def test_build_profile_passes_generous_max_tokens() -> None:
    """Regression (#461 dogfood): Gemini 2.5 Flash is a thinking model — a small
    max_tokens (1200) got eaten by reasoning tokens and truncated the visible
    profile (finish_reason=length), dropping the prioritized policy facts. The
    distiller must request a generous budget so the profile isn't cut off."""
    captured: dict[str, Any] = {}

    async def fake_completion(**kwargs: Any) -> Any:
        captured.update(kwargs)

        class _Msg:
            content = "## Portfolio\n\nfine."

        class _Choice:
            message = _Msg()

        class _Resp:
            choices = [_Choice()]

        return _Resp()

    await build_profile_markdown(
        CompanyFacts(lease_count=40), _settings(), completion_fn=fake_completion
    )
    assert captured["max_tokens"] == generator._PROFILE_MAX_TOKENS
    assert captured["max_tokens"] >= 3000  # headroom over the thinking overhead


async def test_build_profile_falls_back_when_llm_fails() -> None:
    async def boom(**_: Any) -> Any:
        raise RuntimeError("model down")

    md = await build_profile_markdown(
        CompanyFacts(lease_count=40, documents=[("Lease", "lease")]),
        _settings(),
        completion_fn=boom,
    )
    # Deterministic facts rendering, not a crash.
    assert "Company profile" in md
    assert "Leases on file (mirrored): 40" in md


# ---- fake pool for generate_for_company / cycle ----------------------------


class _FakePool:
    """Minimal asyncpg-pool stand-in for the generator path.

    ``last_generated`` seeds the per-day guard; empty source tables (fetchval→0,
    fetch→[]) exercise the no-signal branch. ``executed`` captures upserts.
    """

    def __init__(
        self,
        *,
        company_ids: list[str],
        last_generated: dict[str, datetime.date] | None = None,
    ):
        self.company_ids = company_ids
        self.last_generated = last_generated or {}
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchval(self, sql: str, *args: Any) -> int:
        return 0  # empty mirror tables → no portfolio signal

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        if "FROM public.companies" in sql:
            return [{"id": cid} for cid in self.company_ids]
        return []  # empty activity / documents

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        # repo.generated_on / repo.load both query company_knowledge.
        if "company_knowledge" in sql:
            cid = args[0]
            gen = self.last_generated.get(cid)
            return {"generated_on": gen} if gen is not None else None
        return None

    async def execute(self, sql: str, *args: Any) -> str:
        self.executed.append((sql, args))
        return "INSERT 0 1"


async def test_generate_skips_when_already_done_today() -> None:
    pool = _FakePool(company_ids=["c1"], last_generated={"c1": TODAY})
    did = await generate_for_company(pool, company_id="c1", settings=_settings(), today=TODAY)
    assert did is False
    assert pool.executed == []  # no upsert


async def test_generate_writes_when_stale() -> None:
    pool = _FakePool(company_ids=["c1"], last_generated={"c1": datetime.date(2026, 7, 1)})
    did = await generate_for_company(pool, company_id="c1", settings=_settings(), today=TODAY)
    assert did is True
    # One upsert into company_knowledge with today's date.
    upserts = [c for c in pool.executed if "company_knowledge" in c[0]]
    assert len(upserts) == 1
    assert TODAY in upserts[0][1]


async def test_cycle_disabled_is_noop() -> None:
    pool = _FakePool(company_ids=["c1", "c2"])
    n = await run_company_knowledge_cycle(pool, settings=_settings(enabled=False), today=TODAY)
    assert n == 0
    assert pool.executed == []


async def test_cycle_generates_all_companies() -> None:
    pool = _FakePool(company_ids=["c1", "c2", "c3"])
    n = await run_company_knowledge_cycle(pool, settings=_settings(), today=TODAY)
    assert n == 3
    assert len([c for c in pool.executed if "company_knowledge" in c[0]]) == 3


async def test_cycle_survives_one_company_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = _FakePool(company_ids=["c1", "c2"])
    calls = {"n": 0}
    real = generator.generate_for_company

    async def flaky(*args: Any, **kwargs: Any) -> bool:
        calls["n"] += 1
        if kwargs.get("company_id") == "c1":
            raise RuntimeError("boom")
        return await real(*args, **kwargs)

    monkeypatch.setattr(generator, "generate_for_company", flaky)
    n = await run_company_knowledge_cycle(pool, settings=_settings(), today=TODAY)
    # c1 raised, c2 still generated — the sweep didn't abort.
    assert n == 1
    assert calls["n"] == 2


# ── activity that is unknown is never "none recorded" (rule 14) ─────────


class _ActivityPool(_FakePool):
    """Records which tables are read; the webhook rollup can be made to fail."""

    def __init__(self, *, activity_fails: bool = False) -> None:
        super().__init__(company_ids=["co-1"])
        self.activity_fails = activity_fails
        self.read: list[str] = []

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        self.read.append(sql)
        if "buildium_events" in sql and self.activity_fails:
            raise RuntimeError("pool is gone")
        return await super().fetch(sql, *args)


def _on(monkeypatch: pytest.MonkeyPatch, pms: str | None) -> None:
    async def coverage(_pool: Any, _company_id: str) -> Coverage:
        return Coverage(pms)

    monkeypatch.setattr(sources, "coverage_for", coverage)


@pytest.mark.parametrize(
    ("pms", "name"), [("rentvine", "Rentvine"), ("rentmanager", "Rent Manager")]
)
async def test_activity_on_another_system_is_untracked_not_none(
    monkeypatch: pytest.MonkeyPatch, pms: str, name: str
) -> None:
    """Only Buildium's webhooks are read, so a Rentvine company's profile
    prompt said "none recorded", which the distiller could write down as "no
    activity"."""
    _on(monkeypatch, pms)
    pool = _ActivityPool()
    facts = await sources.gather_company_facts(pool, company_id="co-1")  # type: ignore[arg-type]
    block = facts_to_prompt_block(facts)
    assert f"not tracked for {name}" in block and "UNKNOWN" in block
    assert "none recorded" not in block
    assert not any("buildium_events" in sql for sql in pool.read), "nothing to count there"


async def test_a_failed_activity_read_is_unknown_not_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _on(monkeypatch, "buildium")
    facts = await sources.gather_company_facts(
        _ActivityPool(activity_fails=True),  # type: ignore[arg-type]
        company_id="co-1",
    )
    block = facts_to_prompt_block(facts)
    assert "could not be read just now" in block and "none recorded" not in block


async def test_a_quiet_buildium_month_is_still_none_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _on(monkeypatch, "buildium")
    pool = _ActivityPool()
    block = facts_to_prompt_block(
        await sources.gather_company_facts(pool, company_id="co-1")  # type: ignore[arg-type]
    )
    assert "none recorded" in block and "UNKNOWN" not in block
    assert any("buildium_events" in sql for sql in pool.read)


async def test_an_unknown_system_names_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _on(monkeypatch, None)
    block = facts_to_prompt_block(
        await sources.gather_company_facts(_ActivityPool(), company_id="co-1")  # type: ignore[arg-type]
    )
    assert "UNKNOWN" in block and "none recorded" not in block
