"""Tests for MemoryContext ContextVar propagation."""

from __future__ import annotations

import asyncio

import pytest

from helixis.memory.context import (
    MemoryContext,
    current_memory_context,
    memory_context,
)


def test_memory_context_set_and_read() -> None:
    with memory_context(company_id="comp_1", user_id="user_1", session_id="sess_1") as ctx:
        assert ctx.company_id == "comp_1"
        assert ctx.user_id == "user_1"
        assert ctx.session_id == "sess_1"
        current = current_memory_context()
        assert current is ctx


def test_memory_context_cleared_after_block() -> None:
    with memory_context(company_id="comp_1", user_id="user_1", session_id="sess_1"):
        pass
    assert current_memory_context() is None


def test_nested_memory_contexts_restore_outer() -> None:
    with memory_context(company_id="outer", user_id="u_outer", session_id="s_outer"):
        with memory_context(company_id="inner", user_id="u_inner", session_id="s_inner"):
            assert current_memory_context().company_id == "inner"
        assert current_memory_context().company_id == "outer"
    assert current_memory_context() is None


@pytest.mark.asyncio
async def test_memory_context_isolation_across_tasks() -> None:
    results: dict[str, str | None] = {}

    async def worker(label: str, company: str, user: str) -> None:
        with memory_context(company_id=company, user_id=user, session_id=f"s-{label}"):
            await asyncio.sleep(0.01)
            ctx = current_memory_context()
            results[label] = ctx.company_id if ctx else None

    await asyncio.gather(
        worker("a", "comp_a", "user_brandon"),
        worker("b", "comp_b", "user_ryan"),
        worker("c", "comp_a", "user_casey"),
    )
    assert results == {"a": "comp_a", "b": "comp_b", "c": "comp_a"}


def test_to_context_shape() -> None:
    ctx = MemoryContext(company_id="comp_1", user_id="user_1", session_id="sess_1")
    # dataclass is frozen — mutation should fail
    with pytest.raises(Exception):
        ctx.company_id = "other"  # type: ignore[misc]
