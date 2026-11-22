"""Cross-EMPLOYEE recall on the real store — the headline "AI learns across a
company's team" guarantee, Buildium-grounded.

Distinct from ``test_memory_isolation.py`` (Task B): that proved same-user
recall + cross-COMPANY isolation. This proves the EMPLOYEE-A → EMPLOYEE-B loop:
one teammate stores a Buildium-shaped fact, and a DIFFERENT teammate in the
SAME company recalls it — because memory is partitioned by ``company_id``, not
by user. The unit tests assert this against a fake Mem0; this asserts it against
the real Mem0 + pgvector + gemini-embedding-001 + spaCy BM25.

Skipped by default; needs a real Gemini key + Supabase DB URL.
Run: ``pytest -m integration tests/integration/test_memory_cross_employee_recall.py``

NOTE (follow-up for the local/creds'd session): the *fully conversational*
version — drive ``run_agent_streaming`` for employee B and assert the streamed
REPLY surfaces A's fact via auto-inject (not just that search() returns it) —
needs an EventBus + real model run and belongs where Gemini is available. This
test covers the memory-layer cross-employee contract; that one covers the agent
surfacing it in chat.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from typing import Any

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("HELIXIS_LLM__GOOGLE_API_KEY")
        or not os.environ.get("HELIXIS_SUPABASE__DB_URL"),
        reason="cross-employee recall test needs a real Gemini key + Supabase DB URL",
    ),
]


def _memory_text(hit: dict[str, Any]) -> str:
    return str(hit.get("memory", hit.get("content", "")))


async def _search_until(
    mgr: Any,
    query: str,
    *,
    company_id: str,
    searched_by: str,
    attempts: int = 6,
    delay: float = 1.5,
) -> list[dict[str, Any]]:
    """Poll — Mem0's add() is eventually consistent (async extraction + embed)."""
    hits: list[dict[str, Any]] = []
    for _ in range(attempts):
        hits = await mgr.search(query, company_id=company_id, searched_by=searched_by)
        if hits:
            return hits
        await asyncio.sleep(delay)
    return hits


@pytest.mark.asyncio
async def test_employee_b_recalls_what_employee_a_stored() -> None:
    from helixis.memory.manager import MemoryManager
    from helixis.settings import Settings

    mgr = MemoryManager(Settings())
    await mgr.initialize()
    assert mgr.available, "Mem0 did not initialize — check memory.enabled + Gemini key"

    suffix = uuid.uuid4().hex[:10]
    company = f"itest_company_{suffix}"  # one company, two teammates
    employee_a = str(uuid.uuid4())
    employee_b = str(uuid.uuid4())  # a DIFFERENT human in the same company

    # A Buildium-shaped fact, the kind the agent would extract after a work-order
    # lookup. Memory is Buildium-grounded — everything Helixis remembers traces
    # back to the Buildium API/webhook data (developer.buildium.com).
    fact = (
        f"Work order #5501 (itest {suffix}): Unit 4B kitchen faucet leak, "
        f"tenant Sarah Chen, assigned to Northside Plumbing, ETA Thursday."
    )

    try:
        # Employee A stores it.
        stored = await mgr.add(
            fact, company_id=company, stored_by=employee_a, category="maintenance"
        )
        assert stored is not None, "add() returned None on the real store"

        # Employee B — a different user, same company — recalls it.
        b_hits = await _search_until(
            mgr,
            "what's going on with the unit 4B faucet work order",
            company_id=company,
            searched_by=employee_b,
        )
        assert b_hits, "Employee B could not recall the fact Employee A stored (same company)"
        joined = " ".join(_memory_text(h) for h in b_hits)
        # The recalled memory should carry the operational specifics A stored.
        assert "Sarah Chen" in joined or "5501" in joined or "Northside" in joined, (
            f"recall did not surface A's work-order fact; got: {joined!r}"
        )
    finally:
        # Best-effort cleanup of this throwaway company's memories.
        with contextlib.suppress(Exception):
            for mem in await mgr.get_all(company_id=company):
                mem_id = mem.get("id")
                if mem_id:
                    await mgr.delete(str(mem_id))
