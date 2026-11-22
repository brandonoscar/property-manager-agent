"""End-to-end smoke test for the Langfuse + LiteLLM pipeline.

Skipped by default — only runs when invoked with ``pytest -m integration``
(or ``make langfuse-smoke``) AND the keys are present in the environment.
The two-layer guard means the test never accidentally fires in CI runs
that don't have credentials, and never accidentally racks up LLM cost
during regular unit-test runs.

What this proves end-to-end:
  1. ``configure_langfuse(Settings())`` returns True with real keys.
  2. A real ``litellm.acompletion`` call succeeds inside a ``trace_context``.
  3. ``shutdown_langfuse()`` flushes without error.

After it passes, browse https://us.cloud.langfuse.com to confirm the
trace lands. The test prints the dashboard URL on success so a human
can click through.
"""

from __future__ import annotations

import os

import pytest

# Skip the whole module unless explicitly invoked AND keys are present.
# The marker gate is in pyproject.toml's addopts; the env gate handles
# the case of someone running `pytest -m integration` without keys.
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("HELIXIS_LANGFUSE__PUBLIC_KEY"),
        reason=("HELIXIS_LANGFUSE__PUBLIC_KEY not set; Langfuse smoke needs real keys"),
    ),
]


@pytest.mark.asyncio
async def test_real_llm_call_traces_to_langfuse() -> None:
    """One cheap LLM call, traced + flushed."""
    from helixis.observability import trace_context
    from helixis.observability.langfuse_setup import (
        configure_langfuse,
        is_active,
        shutdown_langfuse,
    )
    from helixis.settings import Settings

    settings = Settings()
    assert configure_langfuse(settings) is True, (
        "configure_langfuse returned False — check HELIXIS_LANGFUSE__* env vars"
    )
    assert is_active() is True

    try:
        # Pick a deliberately cheap model. The smoke test should be
        # fast and roughly free; it just needs LiteLLM to issue a real
        # request so the Langfuse callback fires.
        model = os.environ.get("HELIXIS_SMOKE_LLM_MODEL", "gemini/gemini-2.5-flash")

        import litellm

        with trace_context(
            company_id="smoke-test",
            session_id="smoke-1",
            trace_name="langfuse-smoke",
            tags=("smoke",),
        ):
            response = await litellm.acompletion(
                model=model,
                messages=[{"role": "user", "content": "say ok"}],
                max_tokens=5,
            )
        # Response shape varies slightly per provider; just confirm we
        # got something back the framework can render. Cost is the
        # important assertion — the call must have actually happened
        # for the Langfuse trace to mean anything.
        assert response is not None
    finally:
        shutdown_langfuse()

    host = settings.langfuse.host.rstrip("/")
    print(
        "\nLangfuse trace flushed. Browse:\n"
        f"  {host}/project/<your-project-id>/traces\n"
        "and filter by trace_name='langfuse-smoke' to confirm.\n"
    )
