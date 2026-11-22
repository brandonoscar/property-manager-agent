"""Unit tests for the memory tool implementations (`agents/tools.py`).

These cover the deterministic ``_search_memory_impl`` / ``_store_memory_impl``
coroutines that back the ``search_memory`` / ``store_memory`` tools — split out
(PR for #341) so the eval harness can mock them. No LLM involved: the manager
is mocked, so these run in CI without a Gemini key (unlike the 08x golden cases).
"""

from __future__ import annotations

import types
from unittest.mock import AsyncMock

import pytest

from helixis.agents.tools import _search_memory_impl, _store_memory_impl
from helixis.memory.manager import MemoryRead


def _ctx(company_id: str = "co-1", user_id: str = "alice") -> types.SimpleNamespace:
    return types.SimpleNamespace(company_id=company_id, user_id=user_id)


def _ok(hits: list) -> AsyncMock:
    """A manager whose search REACHED the backend and returned ``hits``.

    ⚠ Say which state a fake means (gotchas 72/73). ``ok=True`` with no hits is
    "searched, found nothing"; ``ok=False`` is "the search never ran". They are
    different facts and the tool must say different things about them, so a
    catch-all fake that answers both would make the distinction unfalsifiable.
    """
    return AsyncMock(return_value=MemoryRead(tuple(hits), ok=True, reason=""))


def _broken() -> AsyncMock:
    """A manager whose search could NOT reach the backend — the Voyage 429 case."""
    return AsyncMock(return_value=MemoryRead((), ok=False, reason="backend_error"))


@pytest.mark.asyncio
async def test_search_returns_note_when_memory_unconfigured(monkeypatch) -> None:
    monkeypatch.setattr("helixis.memory.manager.get_memory_manager", lambda: None)
    assert await _search_memory_impl("anything") == "Memory system not configured."


@pytest.mark.asyncio
async def test_search_returns_note_when_no_results(monkeypatch) -> None:
    manager = types.SimpleNamespace(search=AsyncMock(return_value=[]), search_checked=_ok([]))
    monkeypatch.setattr("helixis.memory.manager.get_memory_manager", lambda: manager)
    monkeypatch.setattr("helixis.memory.context.current_memory_context", _ctx)
    assert await _search_memory_impl("vendor") == "No relevant memories found."


@pytest.mark.asyncio
async def test_search_formats_category_and_author_prefix(monkeypatch) -> None:
    manager = types.SimpleNamespace(
        search=AsyncMock(
            return_value=[
                {
                    "memory": "Sarah prefers email",
                    "metadata": {"category": "preference", "stored_by": "bob"},
                },
                {
                    "content": "Vendor: Acme Plumbing",
                    "metadata": {"category": "vendor", "stored_by": "system"},
                },
            ]
        ),
        search_checked=_ok(
            [
                {
                    "memory": "Sarah prefers email",
                    "metadata": {"category": "preference", "stored_by": "bob"},
                },
                {
                    "content": "Vendor: Acme Plumbing",
                    "metadata": {"category": "vendor", "stored_by": "system"},
                },
            ]
        ),
    )
    monkeypatch.setattr("helixis.memory.manager.get_memory_manager", lambda: manager)
    monkeypatch.setattr("helixis.memory.context.current_memory_context", _ctx)

    out = await _search_memory_impl("sarah")
    # category prefix + author (non-system) on the first; system author suppressed on the second.
    # Recalled memory is fenced as untrusted data (LED-018), so assert the formatted
    # body appears inside the fence rather than equalling the whole return.
    assert "⟦BEGIN_UNTRUSTED⟧" in out and out.rstrip().endswith("⟦END_UNTRUSTED⟧")
    assert "- [preference] (from bob) Sarah prefers email\n- [vendor] Vendor: Acme Plumbing" in out
    # The tool reads search_checked, not search — that IS the fix, so the
    # assertion follows it. Left on the old method it would pass against a
    # tool that had quietly reverted to the false-absence path.
    manager.search_checked.assert_awaited_once_with("sarah", company_id="co-1", searched_by="alice")


@pytest.mark.asyncio
async def test_search_refused_without_company_context(monkeypatch) -> None:
    """No company on the run → the tool answers honestly and never queries
    the manager (the shared 'default' partition fallback is gone)."""
    manager = types.SimpleNamespace(search=AsyncMock(), search_checked=AsyncMock())
    monkeypatch.setattr("helixis.memory.manager.get_memory_manager", lambda: manager)
    monkeypatch.setattr("helixis.memory.context.current_memory_context", lambda: None)

    out = await _search_memory_impl("vendor")
    assert out == "Memory is unavailable for this session (no company context)."
    manager.search.assert_not_awaited()


@pytest.mark.asyncio
async def test_store_refused_without_company_context(monkeypatch) -> None:
    manager = types.SimpleNamespace(add=AsyncMock())
    monkeypatch.setattr("helixis.memory.manager.get_memory_manager", lambda: manager)
    monkeypatch.setattr(
        "helixis.memory.context.current_memory_context",
        lambda: types.SimpleNamespace(company_id="", user_id="alice"),
    )

    out = await _store_memory_impl("a fact", "tenant")
    assert out == (
        "Memory is unavailable for this session (no company context) — nothing was stored."
    )
    manager.add.assert_not_awaited()


@pytest.mark.asyncio
async def test_store_returns_note_when_memory_unconfigured(monkeypatch) -> None:
    monkeypatch.setattr("helixis.memory.manager.get_memory_manager", lambda: None)
    assert await _store_memory_impl("x", "tenant") == "Memory system not configured."


@pytest.mark.asyncio
async def test_store_forwards_scope_and_confirms(monkeypatch) -> None:
    # ⚠ return_value is a real id, not None. This fixture used to return None —
    # mem0's FAILURE signal — and assert the tool confirmed the write anyway,
    # so it pinned the defect as the contract (same shape as the tests that
    # passed on the signature bypass, gotchas 68/74).
    manager = types.SimpleNamespace(add=AsyncMock(return_value="mem-abc123"))
    monkeypatch.setattr("helixis.memory.manager.get_memory_manager", lambda: manager)
    monkeypatch.setattr("helixis.memory.context.current_memory_context", _ctx)

    out = await _store_memory_impl("Sarah Chen prefers email", "preference")
    assert out.startswith("Stored in company memory: Sarah Chen prefers email")
    manager.add.assert_awaited_once_with(
        "Sarah Chen prefers email",
        company_id="co-1",
        stored_by="alice",
        category="preference",
    )


async def test_a_failed_write_is_never_reported_as_stored(monkeypatch) -> None:
    """`add` returns None on every failure — unreachable embedder, mem0 raising,
    a spaCy SystemExit. Confirming a write that did not happen is worse than
    failing loudly, because nobody goes back to check."""
    manager = types.SimpleNamespace(add=AsyncMock(return_value=None))
    monkeypatch.setattr("helixis.memory.manager.get_memory_manager", lambda: manager)
    monkeypatch.setattr("helixis.memory.context.current_memory_context", _ctx)

    out = await _store_memory_impl("Sarah Chen prefers email", "preference")
    assert "NOT stored" in out
    assert not out.startswith("Stored in company memory")


async def test_a_failed_search_is_never_reported_as_an_absence(monkeypatch) -> None:
    """The false-absence guard. A search that could not run must not render as
    "No relevant memories found" — that is a fact about the company's memory
    that we never established (gotchas 26/42), and it was firing on the
    majority of calls under load."""
    manager = types.SimpleNamespace(search=AsyncMock(return_value=[]), search_checked=_broken())
    monkeypatch.setattr("helixis.memory.manager.get_memory_manager", lambda: manager)
    monkeypatch.setattr("helixis.memory.context.current_memory_context", _ctx)

    out = await _search_memory_impl("what does Sarah prefer")
    assert "No relevant memories found" not in out
    assert "could not be searched" in out
    # It must also actively steer the model off the wrong conclusion, not just
    # decline to state it — the model is what turns this into a sentence.
    assert "NOTHING" in out or "not" in out.lower()


async def test_an_empty_search_that_did_run_still_says_so(monkeypatch) -> None:
    """The other direction. Marking every empty result as a failure would be a
    fresh false claim about every company that genuinely has no such memory
    (gotcha 38 — guard both directions)."""
    manager = types.SimpleNamespace(search=AsyncMock(return_value=[]), search_checked=_ok([]))
    monkeypatch.setattr("helixis.memory.manager.get_memory_manager", lambda: manager)
    monkeypatch.setattr("helixis.memory.context.current_memory_context", _ctx)

    out = await _search_memory_impl("what does Sarah prefer")
    assert out == "No relevant memories found."
