"""Langfuse setup — registers LiteLLM callbacks and a per-call metadata
injector backed by ``observability.context``.

Why a wrapper instead of just env vars: env vars are process-global and
would leak between concurrent requests. Wrapping ``litellm.acompletion``
(and ``litellm.completion``) lets us read the request-scoped contextvar
on every call and merge the metadata into kwargs, where LiteLLM's
Langfuse callback picks it up.

The wrapper is idempotent — calling ``configure_langfuse`` multiple times
won't double-wrap.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from helixis.observability.context import current_trace_context
from helixis.settings import Settings

logger = logging.getLogger(__name__)

_WRAP_FLAG = "_helixis_langfuse_wrapped"
_active = False


def configure_langfuse(settings: Settings) -> bool:
    """Enable Langfuse tracing for all LiteLLM calls.

    Returns True if tracing is active. Returns False (with a single log
    line) if tracing is disabled or keys are missing — the rest of the
    backend keeps working.
    """
    global _active

    cfg = settings.langfuse
    if not cfg.enabled:
        logger.info("Langfuse disabled in settings — tracing off")
        _active = False
        return False

    if not cfg.public_key or not cfg.secret_key:
        logger.warning(
            "Langfuse keys not configured (HELIXIS_LANGFUSE__PUBLIC_KEY / "
            "HELIXIS_LANGFUSE__SECRET_KEY) — tracing off"
        )
        _active = False
        return False

    # LiteLLM's Langfuse callback reads these env vars on first use.
    os.environ["LANGFUSE_PUBLIC_KEY"] = cfg.public_key
    os.environ["LANGFUSE_SECRET_KEY"] = cfg.secret_key
    os.environ["LANGFUSE_HOST"] = cfg.host

    try:
        import litellm

        if "langfuse" not in (litellm.success_callback or []):
            litellm.success_callback = [
                *(litellm.success_callback or []),
                "langfuse",
            ]
        if "langfuse" not in (litellm.failure_callback or []):
            litellm.failure_callback = [
                *(litellm.failure_callback or []),
                "langfuse",
            ]

        # Optionally mask prompt/completion CONTENT before it reaches Langfuse
        # Cloud. LiteLLM's turn_off_message_logging redacts messages + response
        # text for EVERY logging integration (Langfuse included) while leaving
        # trace structure, latency, tokens, and metadata intact — so run-level
        # debugging survives but tenant PII in prompts does not leave. Default
        # OFF; flip on before the first real customer (see LangfuseSettings).
        if cfg.mask_generation_content:
            litellm.turn_off_message_logging = True

        _install_metadata_injector(litellm, list(cfg.default_tags))
    except Exception:  # noqa: BLE001 — never let observability break the app
        logger.exception("Failed to configure Langfuse — tracing off")
        _active = False
        return False

    logger.info(
        "Langfuse tracing enabled → %s (generation content %s)",
        cfg.host,
        "MASKED" if cfg.mask_generation_content else "raw",
    )
    _active = True
    return True


def shutdown_langfuse() -> None:
    """Best-effort flush on shutdown so in-flight events aren't lost."""
    if not _active:
        return
    try:
        import litellm

        client = getattr(litellm, "_langfuse_client_instance", None)
        if client is not None and hasattr(client, "flush"):
            client.flush()
    except Exception:
        # Shutting down — never raise from here.
        logger.debug("Langfuse flush failed (non-fatal)", exc_info=True)


def _install_metadata_injector(litellm_module: Any, default_tags: list[str]) -> None:
    """Wrap litellm.acompletion / litellm.completion once to inject the
    current trace context's metadata into every call.

    Idempotent — re-calling configure_langfuse is safe.
    """
    if getattr(litellm_module, _WRAP_FLAG, False):
        return

    original_acompletion = litellm_module.acompletion
    original_completion = litellm_module.completion

    def _merge_metadata(kwargs: dict) -> dict:
        ctx = current_trace_context()
        if ctx is None:
            return kwargs
        existing = dict(kwargs.get("metadata") or {})
        ctx_meta = ctx.to_metadata()
        merged_tags = list(default_tags)
        for tag in ctx_meta.get("tags", []) or []:
            if tag not in merged_tags:
                merged_tags.append(tag)
        for tag in existing.get("tags", []) or []:
            if tag not in merged_tags:
                merged_tags.append(tag)
        ctx_meta["tags"] = merged_tags
        # Caller-provided metadata wins on conflicts other than tags.
        merged = {**ctx_meta, **existing}
        merged["tags"] = merged_tags
        kwargs["metadata"] = merged
        return kwargs

    async def wrapped_acompletion(*args: Any, **kwargs: Any) -> Any:
        return await original_acompletion(*args, **_merge_metadata(kwargs))

    def wrapped_completion(*args: Any, **kwargs: Any) -> Any:
        return original_completion(*args, **_merge_metadata(kwargs))

    litellm_module.acompletion = wrapped_acompletion
    litellm_module.completion = wrapped_completion
    setattr(litellm_module, _WRAP_FLAG, True)


def is_active() -> bool:
    """Return whether tracing was successfully configured."""
    return _active
