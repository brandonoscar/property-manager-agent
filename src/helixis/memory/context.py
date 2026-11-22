"""Per-request memory context propagated via ContextVar.

Same pattern as observability/context.py — concurrent agent runs each
carry their own memory scope (company + user + session) without leaking
into one another. Memory tools read from this ContextVar instead of
threading the IDs through explicit parameters.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class MemoryContext:
    """Identifying metadata for memory scoping within an agent run."""

    company_id: str
    user_id: str
    session_id: str


_memory_var: ContextVar[MemoryContext | None] = ContextVar("helixis_memory_context", default=None)


def current_memory_context() -> MemoryContext | None:
    """Return the memory context for the current task, if any."""
    return _memory_var.get()


@contextmanager
def memory_context(
    *,
    company_id: str,
    user_id: str,
    session_id: str,
) -> Iterator[MemoryContext]:
    """Bind a memory context for the duration of a block.

    Safe to use inside async code — contextvars are copied per asyncio task,
    so concurrent requests each see their own context.
    """
    ctx = MemoryContext(
        company_id=company_id,
        user_id=user_id,
        session_id=session_id,
    )
    token = _memory_var.set(ctx)
    try:
        yield ctx
    finally:
        _memory_var.reset(token)
