"""Same-company / cross-company isolation tests for MemoryManager.

These prove the company-scope contract:
  - User A and User B at the same company share memory.
  - User C at a different company does not see either's memory.

The Mem0 ``user_id`` partition is the boundary; we exercise it via a fake
in-memory Mem0 backend that mirrors the real partition behaviour without
needing pgvector. This is a real test of the partition logic — not just
"did we call the API with these args?" — because the fake stores rows by
the partition key and search() filters on it.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from helixis.memory.manager import MemoryManager
from helixis.settings import Settings


class FakeMem0:
    """Minimal Mem0 stand-in that respects the user_id partition.

    Enough surface area for add/search/get_all to round-trip through
    MemoryManager. No semantic search — just substring matching, which
    is enough to prove the partition isolates correctly.
    """

    def __init__(self) -> None:
        # rows keyed by (partition_key, content) so we can dedupe on re-add
        self._rows: list[dict[str, Any]] = []
        self._next_id = 1

    async def add(
        self,
        content: Any,
        *,
        user_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # In real Mem0, content can be a string or a list of message dicts.
        text = content if isinstance(content, str) else str(content)
        memory_id = f"mem_{self._next_id}"
        self._next_id += 1
        self._rows.append(
            {
                "id": memory_id,
                "memory": text,
                "user_id": user_id,
                "metadata": metadata or {},
            }
        )
        return {"results": [{"event": "ADD", "memory": text, "id": memory_id}]}

    async def search(
        self,
        query: str,
        *,
        filters: dict[str, Any],
        top_k: int = 10,
    ) -> dict[str, Any]:
        partition = filters.get("user_id")
        # Filter to the right partition first, then crude substring match.
        matches = [
            {
                "id": r["id"],
                "memory": r["memory"],
                "metadata": r["metadata"],
            }
            for r in self._rows
            if r["user_id"] == partition and query.lower() in r["memory"].lower()
        ]
        return {"results": matches[:top_k]}

    async def get_all(self, *, filters: dict[str, Any]) -> dict[str, Any]:
        partition = filters.get("user_id")
        return {
            "results": [
                {
                    "id": r["id"],
                    "memory": r["memory"],
                    "metadata": r["metadata"],
                }
                for r in self._rows
                if r["user_id"] == partition
            ]
        }


@pytest.fixture
def manager() -> MemoryManager:
    mgr = MemoryManager(Settings())
    mgr._mem0 = FakeMem0()
    mgr._initialized = True
    return mgr


@pytest.mark.asyncio
async def test_user_b_reads_what_user_a_wrote_in_same_company(
    manager: MemoryManager,
) -> None:
    """Brandon writes; Ryan (same company) reads it back."""
    company = "company_acme"
    user_a = "11111111-1111-1111-1111-111111111111"  # Brandon
    user_b = "22222222-2222-2222-2222-222222222222"  # Ryan

    # User A stores
    with patch("helixis.memory.manager.write_audit"):
        await manager.add(
            "Unit 4B tenant is Sarah Chen, lease expires 2026-09-30",
            company_id=company,
            stored_by=user_a,
            category="tenant",
        )

    # User B searches the same company — must find it
    with patch("helixis.memory.manager.write_audit"):
        hits = await manager.search("Sarah Chen", company_id=company, searched_by=user_b)

    assert len(hits) == 1
    assert "Sarah Chen" in hits[0]["memory"]


@pytest.mark.asyncio
async def test_user_c_at_different_company_cannot_read(
    manager: MemoryManager,
) -> None:
    """Ryan (different company) does NOT see Brandon's memory."""
    company_a = "company_acme"
    company_b = "company_beta"
    user_a = "11111111-1111-1111-1111-111111111111"
    user_c = "33333333-3333-3333-3333-333333333333"

    with patch("helixis.memory.manager.write_audit"):
        await manager.add(
            "Unit 4B tenant is Sarah Chen, lease expires 2026-09-30",
            company_id=company_a,
            stored_by=user_a,
            category="tenant",
        )

        # User C at a different company must not see it
        hits = await manager.search("Sarah Chen", company_id=company_b, searched_by=user_c)

    assert hits == []


@pytest.mark.asyncio
async def test_get_all_respects_partition(manager: MemoryManager) -> None:
    """get_all() must only return rows for the requested company."""
    company_a = "company_acme"
    company_b = "company_beta"
    user_a = "11111111-1111-1111-1111-111111111111"
    user_c = "33333333-3333-3333-3333-333333333333"

    with patch("helixis.memory.manager.write_audit"):
        await manager.add("acme fact 1", company_id=company_a, stored_by=user_a)
        await manager.add("acme fact 2", company_id=company_a, stored_by=user_a)
        await manager.add("beta fact 1", company_id=company_b, stored_by=user_c)

    acme = await manager.get_all(company_id=company_a)
    beta = await manager.get_all(company_id=company_b)
    assert len(acme) == 2
    assert len(beta) == 1
    assert all("acme" in r["memory"] for r in acme)
    assert all("beta" in r["memory"] for r in beta)


@pytest.mark.asyncio
async def test_empty_company_id_is_refused_not_shared(manager: MemoryManager) -> None:
    """No company scope → NO memory op. The old behavior fell back to one
    shared 'default' partition, which every company-less caller read and
    wrote across tenants — a leak vector, now removed."""
    with patch("helixis.memory.manager.write_audit"):
        result = await manager.add(
            "orphan fact",
            company_id="",
            stored_by="11111111-1111-1111-1111-111111111111",
        )
        hits = await manager.search(
            "orphan",
            company_id="",
            searched_by="22222222-2222-2222-2222-222222222222",
        )
        everything = await manager.get_all(company_id="")
        extracted = await manager.extract_and_store(
            [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}],
            company_id="",
        )

    assert result is None
    assert hits == []
    assert everything == []
    assert extracted == []
    # Nothing ever reached the backing store — no shared partition exists.
    assert manager._mem0._rows == []
