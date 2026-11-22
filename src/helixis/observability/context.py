"""Per-request trace context propagated to LiteLLM calls.

Uses a ``contextvars.ContextVar`` so concurrent agent runs each carry their
own trace metadata without leaking into one another. The
``trace_context()`` async context manager sets the metadata for the
duration of an agent run; the LiteLLM wrapper installed by
``langfuse_setup`` reads from the same contextvar and injects the
metadata into every ``litellm.acompletion`` / ``litellm.completion`` call.

Process-wide env vars are deliberately NOT used — they would leak
between concurrent requests.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass(frozen=True)
class TraceContext:
    """Identifying metadata for a single agent run.

    Attribute names mirror what Langfuse expects on the LLM call's
    ``metadata`` dict (Langfuse maps ``trace_user_id`` to its User ID,
    ``session_id`` to its Session, etc.).
    """

    company_id: str
    session_id: str
    trace_name: str = "helixis-agent"
    tags: tuple[str, ...] = field(default_factory=tuple)
    trace_id: str = ""

    def to_metadata(self) -> dict:
        md = {
            "trace_user_id": self.company_id,
            "session_id": self.session_id,
            "trace_name": self.trace_name,
            "tags": list(self.tags),
        }
        # Share the run's trace id with LiteLLM's Langfuse callback so the
        # LLM generations attach to the same trace as the tool/handoff
        # spans (see ``observability.tracing``). ``existing_trace_id`` is
        # the key LiteLLM's langfuse logger reads to attach rather than
        # create.
        if self.trace_id:
            md["trace_id"] = self.trace_id
            md["existing_trace_id"] = self.trace_id
        return md


_trace_var: ContextVar[TraceContext | None] = ContextVar("helixis_trace_context", default=None)


def current_trace_context() -> TraceContext | None:
    """Return the trace context for the current task, if any."""
    return _trace_var.get()


@contextmanager
def trace_context(
    *,
    company_id: str,
    session_id: str,
    trace_name: str = "helixis-agent",
    tags: tuple[str, ...] = (),
    trace_id: str = "",
) -> Iterator[TraceContext]:
    """Bind a trace context for the duration of a block.

    Safe to use inside async code because contextvars are copied per
    asyncio task — concurrent requests each see their own context.
    """
    ctx = TraceContext(
        company_id=company_id,
        session_id=session_id,
        trace_name=trace_name,
        tags=tags,
        trace_id=trace_id,
    )
    token = _trace_var.set(ctx)
    try:
        yield ctx
    finally:
        _trace_var.reset(token)
