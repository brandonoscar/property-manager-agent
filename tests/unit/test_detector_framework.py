"""The ambient detector framework — registry, persistence, isolation.

Proves the ONE object the detectors share: a Detector emits Findings that
the framework dedups + persists into helixis_risk_flags, resolves stale
ones, and — critically — one detector raising never takes down the run.
The two shipped reconciliation rules are registered on import; a trivial
custom detector proves "adding a detector is one file".
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from helixis.detectors import registry
from helixis.detectors.base import STATE_RULE, Detector, Finding
from helixis.detectors.runner import run_detectors_for_company
from helixis.detectors.store import persist_findings

COMPANY_ID = "5ca1ab1e-0000-4000-8000-000000000004"


def _pool(conn: Any) -> MagicMock:
    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    return pool


# ── registry ─────────────────────────────────────────────────────────


def test_default_enabled_cycle_runs_rent_then_duplicate() -> None:
    # The scheduled cycle runs exactly the live rules, in order (persistence
    # sequence is load-bearing); every other detector ships dark, so it's
    # absent here until explicitly allow-listed. ``rent_not_collected`` joined
    # 2026-08-21 as the replacement for the retired ``rent_missing`` reminder
    # producer — the ONE deliberate exception to dark-by-default.
    enabled = [d.detector_id for d in registry.enabled_state_rule_detectors()]
    assert enabled == ["rent_not_collected", "rent_under_charged", "duplicate_vendor_bill"]
    assert registry.get("rent_under_charged") is not None


def test_register_rejects_duplicate_id_and_missing_rule_keys() -> None:
    class Dup(Detector):
        detector_id = "rent_under_charged"  # already registered
        rule_keys = ("rent_under_charged",)

        async def scan(self, conn: Any, company_id: str) -> list[Finding]:
            return []

    with pytest.raises(ValueError, match="duplicate"):
        registry.register(Dup())

    class NoKeys(Detector):
        detector_id = "no_keys_detector"
        rule_keys = ()

        async def scan(self, conn: Any, company_id: str) -> list[Finding]:
            return []

    with pytest.raises(ValueError, match="rule_keys"):
        registry.register(NoKeys())


# ── persistence: dedup upsert + stale resolve ────────────────────────


@pytest.mark.asyncio
async def test_persist_upserts_each_finding_then_resolves_per_rule_key() -> None:
    conn = MagicMock()
    conn.execute = AsyncMock()

    class Two(Detector):
        detector_id = "two_key_detector"
        detector_class = STATE_RULE
        rule_keys = ("k1", "k2")

        async def scan(self, conn: Any, company_id: str) -> list[Finding]:
            return []

    findings = [
        Finding(rule_key="k1", fingerprint="a", severity="warning", title="t", detail="d"),
        Finding(rule_key="k2", fingerprint="b", severity="info", title="t2", detail="d2"),
    ]
    await persist_findings(conn, COMPANY_ID, Two(), findings)

    calls = conn.execute.await_args_list
    # 2 upserts (one per finding) + 2 resolves (one per rule_key).
    assert len(calls) == 4
    assert "ON CONFLICT (company_id, rule_key, fingerprint)" in calls[0].args[0]
    assert calls[0].args[1] == COMPANY_ID
    assert calls[0].args[2] == "k1"  # source is inlined, NOT a bind arg
    assert calls[0].args[3] == "a"
    # evidence is the 12th positional (json)
    assert json.loads(calls[0].args[11]) == {}
    # resolves carry exactly (sql, company_id, rule_key, fingerprints)
    resolves = [c.args for c in calls[2:]]
    assert {r[2] for r in resolves} == {"k1", "k2"}
    for sql, cid, _rule_key, fingerprints in resolves:
        assert "SET status = 'resolved'" in sql
        assert cid == COMPANY_ID
        assert isinstance(fingerprints, list)


@pytest.mark.asyncio
async def test_persist_rejects_unknown_source() -> None:
    class Bad(Detector):
        detector_id = "bad_source"
        source = "not_a_source"
        rule_keys = ("x",)

        async def scan(self, conn: Any, company_id: str) -> list[Finding]:
            return []

    conn = MagicMock()
    conn.execute = AsyncMock()
    with pytest.raises(ValueError, match="invalid finding source"):
        await persist_findings(conn, COMPANY_ID, Bad(), [])


# ── crash isolation: one detector raising never aborts the run ────────


@pytest.mark.asyncio
async def test_one_failing_detector_does_not_stop_the_others() -> None:
    conn = MagicMock()
    conn.execute = AsyncMock()

    class Boom(Detector):
        detector_id = "boom_detector"
        rule_keys = ("boom",)

        async def scan(self, conn: Any, company_id: str) -> list[Finding]:
            raise RuntimeError("kaboom")

    class Fine(Detector):
        detector_id = "fine_detector"
        rule_keys = ("fine",)

        async def scan(self, conn: Any, company_id: str) -> list[Finding]:
            return [
                Finding(rule_key="fine", fingerprint="ok", severity="info", title="t", detail="d")
            ]

    counts = await run_detectors_for_company(_pool(conn), COMPANY_ID, [Boom(), Fine()])

    # The bad detector is counted 0, the good one still ran + persisted.
    assert counts == {"boom_detector": 0, "fine_detector": 1}
    # Fine's upsert + resolve executed despite Boom raising first.
    assert conn.execute.await_count == 2
