"""Observability — Langfuse tracing for LLM calls.

`configure_langfuse(settings)` registers a LiteLLM success/failure callback
that ships every LLM call to a Langfuse instance (self-hosted or cloud).
Per-request tenant + session attribution is propagated via a contextvar
that a thin wrapper around `litellm.acompletion` reads on each call.

If keys are missing the whole subsystem is a no-op — the rest of the
backend keeps working unchanged.
"""

from helixis.observability.context import (
    TraceContext,
    current_trace_context,
    trace_context,
)
from helixis.observability.langfuse_setup import (
    configure_langfuse,
    shutdown_langfuse,
)
from helixis.observability.latency import (
    check_latency,
    configure_latency,
)
from helixis.observability.sentry_setup import (
    configure_sentry,
    shutdown_sentry,
)

__all__ = [
    "TraceContext",
    "check_latency",
    "configure_langfuse",
    "configure_latency",
    "configure_sentry",
    "current_trace_context",
    "shutdown_langfuse",
    "shutdown_sentry",
    "trace_context",
]
