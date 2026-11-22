"""LiteLLM model provider for the OpenAI Agents SDK.

Routes to the right LLM provider (GPT-4o, Claude, Gemini) based on task
type, and passes the matching Helixis-prefixed API key into the
LitellmModel constructor so we don't have to duplicate keys across
LiteLLM's GEMINI_API_KEY / OPENAI_API_KEY / ANTHROPIC_API_KEY env vars.

Helixis stores its keys under double-underscore Pydantic env vars
(HELIXIS_LLM__GOOGLE_API_KEY, etc.). LiteLLM's default lookup uses bare
provider env vars (GEMINI_API_KEY, etc.), so handing it the model name
without an api_key would either fail or force operators to set the
same secret twice. Resolving the key here keeps the source of truth in
``Settings`` and the deployment surface in ``HELIXIS_LLM__*``.

PR #71: this module now also configures LiteLLM's retry + fallback
behaviour at import time. The hard rule (Anthropic-only in prod since
the 2026-07-21 Gemini exit; no OpenAI) is enforced by
:func:`_assert_anthropic_only` so a future edit that adds another
provider to the fallback list fails the unit-test suite before the
bad config ever boots.
"""

from __future__ import annotations

import logging
from typing import Any

import litellm
from agents.extensions.models.litellm_model import LitellmModel

from helixis.llm.call_policy import CallPolicy, set_call_policy
from helixis.settings import Settings

logger = logging.getLogger(__name__)


# ── LiteLLM-wide retry + fallback config ─────────────────────────────


_TRANSIENT_STATUSES: tuple[int, ...] = (502, 503, 504)
"""HTTP statuses LiteLLM may surface from Gemini/Vertex that we want
retried. Codes outside this set are treated as deterministic and not
retried — 4xx is the user's problem to fix, not a 503-style blip."""


def _assert_anthropic_only(model_names: list[str]) -> None:
    """Defensive: refuse to install any fallback list that includes a
    non-Anthropic provider. Brandon's hard rule since the Gemini exit
    (2026-07-21, the Google account is gone): Anthropic-only in prod, no
    OpenAI, no Gemini. The startup guard at ``main.assert_no_openai_default``
    already covers the *primary* model; this covers the fallback list."""
    for name in model_names:
        lowered = name.lower()
        if not lowered.startswith(("anthropic/", "claude/")):
            raise ValueError(
                f"Refusing to install non-Anthropic fallback {name!r}. "
                "Helixis policy forbids OpenAI/Gemini in prod fallback "
                "chains; see src/helixis/main.py::assert_no_openai_default."
            )


def configure_litellm_retries(settings: Settings) -> dict[str, Any]:
    """Configure LiteLLM's module-level retry + fallback behaviour.

    Called from ``main.lifespan`` startup and from the worker's main
    so both processes share the same policy. Safe to call multiple
    times — idempotent (just sets attributes).

    Returns a dict describing what got configured, so the test suite
    can pin the shape without touching ``litellm``'s globals.
    """
    # Retry count from settings (default 3, prod 5). Vertex 503s and Gemini
    # stream-mid-error are common enough that 0 retries makes one bad upstream
    # minute eat the user's turn; each retry adds ~0.5-2s LiteLLM backoff.
    num_retries = settings.llm.max_retries
    litellm.num_retries = num_retries

    # Per-request timeout ceiling from settings (default 120s, prod 180s).
    # This is the global default for EVERY litellm call that doesn't pass its
    # own timeout — critically the interactive orchestrator's streamed turn,
    # which passed none, so a hung Gemini stream could block up to litellm's
    # ~6000s default before failing. Paths that DO set an explicit timeout
    # (planner 8s, summarizer 30s, recon 45s) override this global and keep
    # their tighter budgets.
    litellm.request_timeout = settings.llm.request_timeout

    # Build the cross-SKU fallback list. LiteLLM expects
    # ``litellm.fallbacks`` as a list of single-key dicts mapping
    # ``primary_model_name`` to a fallback list. We map the reasoning
    # model to {browser, analysis} (or whatever non-empty siblings the
    # settings declare), so a 503 on the primary slot falls through to
    # the next Claude SKU rather than failing the call.
    models = settings.llm.models or {}
    reasoning_model = (models.get("reasoning") or {}).get("model") or settings.llm.default_model
    fallback_models: list[str] = []
    for key in ("browser", "analysis"):
        candidate = (models.get(key) or {}).get("model")
        if candidate and candidate != reasoning_model:
            fallback_models.append(candidate)

    # Defensive: refuse to install a non-Anthropic fallback no matter what
    # settings somehow shipped — the unit test pins this contract.
    if fallback_models:
        _assert_anthropic_only(fallback_models)
        litellm.fallbacks = [{reasoning_model: fallback_models}]
    else:
        # No siblings declared — at least clear any stale fallback list
        # so a previous boot's config can't leak forward in a long-
        # lived test process.
        litellm.fallbacks = []

    # ⚠ THE TWO GLOBALS ABOVE ARE INERT ON OUR CALL PATH, and are kept only
    # because a reader who greps for them should find them beside the reason.
    # Verified against the installed library, not reasoned about:
    # ``litellm.fallbacks`` is read at exactly one site (router.py:585, inside
    # ``litellm.Router`` — a class this repo never instantiates), our path
    # reads ``kwargs["fallbacks"]`` or the deprecated ``litellm.model_fallbacks``
    # instead, and the value's SHAPE is the Router's map form rather than the
    # flat list ``acompletion`` wants. ``litellm.num_retries`` is separately
    # zeroed by the library after the first failure (utils.py:1516/1555/1826/
    # 1856) and never restored, so a boot-time assignment is single-use in a
    # long-lived process.
    #
    # The policy below is what actually applies, per call, at the one seam
    # every completion in this product passes through. See llm/call_policy.
    set_call_policy(
        CallPolicy(
            fallbacks={reasoning_model: tuple(fallback_models)} if fallback_models else {},
            num_retries=num_retries,
        )
    )

    # Prompt caching rides the same boot chokepoint so the API process, the
    # worker, and the eval runner all get it without separate wiring. No-op
    # for non-Anthropic models; idempotent (#27 cost lever).
    from helixis.llm.prompt_cache import configure_prompt_caching

    prompt_caching = configure_prompt_caching()

    logger.info(
        "litellm retry config: num_retries=%d request_timeout=%ss fallbacks=%r prompt_caching=%s",
        num_retries,
        settings.llm.request_timeout,
        litellm.fallbacks,
        prompt_caching,
    )
    return {
        "num_retries": num_retries,
        "request_timeout": settings.llm.request_timeout,
        "fallbacks": list(litellm.fallbacks),
        "primary": reasoning_model,
        "prompt_caching": prompt_caching,
    }


def _api_key_for_model(settings: Settings, model_name: str) -> str | None:
    """Return the matching Helixis-configured API key for a LiteLLM model name.

    LiteLLM model names are namespaced by provider, e.g. ``gemini/gemini-2.0-flash``,
    ``openai/gpt-4o``, ``anthropic/claude-sonnet-4``. We prefix-match on the
    provider segment and return whichever Helixis key is configured for it.

    Returns ``None`` if there's no recognised prefix or the matching key is
    blank, which signals the caller to omit ``api_key`` and let LiteLLM fall
    back to its own env var lookup.
    """
    name = model_name.lower()
    llm = settings.llm
    if name.startswith(("gemini/", "google/", "vertex_ai/")):
        return llm.google_api_key or None
    if name.startswith("openai/"):
        return llm.openai_api_key or None
    if name.startswith(("anthropic/", "claude/")):
        return llm.anthropic_api_key or None
    return None


def get_model(settings: Settings, task_type: str = "reasoning") -> LitellmModel:
    """Get a LiteLLM model configured for the given task type.

    Task types map to model aliases defined in config/default.yaml:
      - "reasoning" -> default model for orchestration / planning
      - "analysis"  -> deep-thinking model for complex PM tasks

    The ``"browser"`` / ``"analysis"`` slots also serve as the reasoning
    model's cross-SKU fallback list (see :func:`configure_litellm_retries`).
    There is no live ``get_browser_model`` caller — the browser agent runs
    on the reasoning model (``browser_agent.py`` uses ``get_reasoning_model``).
    """
    models = settings.llm.models
    model_config = models.get(task_type, {})
    model_name = model_config.get("model", settings.llm.default_model)

    # Dev-only local-model override (e.g. ollama/llama3.3) — lets a developer
    # run the agent MACHINERY on a free local model without spending Anthropic
    # tokens. HARD-GATED to non-production so it can never accidentally ship:
    # a production env with the var set is IGNORED with a loud warning. Only
    # the PRIMARY model selection is overridden — the boot-time fallback list
    # (configure_litellm_retries / _assert_anthropic_only) is untouched, so
    # the Anthropic-only guard still holds.
    local_model = settings.dev.local_model.strip()
    if local_model:
        if settings.env == "production":
            logger.warning(
                "HELIXIS_DEV__LOCAL_MODEL=%r is set but ENV is production — "
                "IGNORING it and using the Anthropic model %r. The local-model "
                "override is a dev-only plumbing tool and never ships.",
                local_model,
                model_name,
            )
        else:
            logger.warning(
                "Dev local-model override ACTIVE (env=%s): reasoning model is "
                "%r, NOT the Anthropic default. Plumbing-only — answer quality "
                "on a local model says nothing about prod.",
                settings.env,
                local_model,
            )
            api_key = _api_key_for_model(settings, local_model)
            return (
                LitellmModel(model=local_model)
                if api_key is None
                else LitellmModel(model=local_model, api_key=api_key)
            )

    api_key = _api_key_for_model(settings, model_name)
    if api_key is None:
        logger.warning(
            "No Helixis-configured API key matched model %r — LiteLLM will "
            "fall back to its own env var lookup, which may fail.",
            model_name,
        )
        return LitellmModel(model=model_name)

    return LitellmModel(model=model_name, api_key=api_key)


def get_reasoning_model(settings: Settings) -> LitellmModel:
    """Model for orchestration and general reasoning."""
    return get_model(settings, "reasoning")


def get_analysis_model(settings: Settings) -> LitellmModel:
    """Deep analysis model for complex PM tasks."""
    return get_model(settings, "analysis")


def get_lookup_model(settings: Settings) -> LitellmModel | None:
    """Cheap tier for questions ``classify_effort`` recognises, or ``None``.

    ``None`` means "leave the model alone", which is the default: the setting
    ships empty. Callers pass the result straight to ``Agent.clone(model=...)``
    only when it is not None, so an unconfigured deployment is byte-for-byte
    unchanged.

    Goes through ``_api_key_for_model`` like every other slot so the key
    resolution and the Anthropic-only posture (gotcha 18) stay in one place —
    a second, hand-rolled key lookup here is how a model slot quietly acquires
    different behaviour from the rest of the fleet.
    """
    name = settings.agent.lookup_model.strip()
    if not name:
        return None
    api_key = _api_key_for_model(settings, name)
    return (
        LitellmModel(model=name) if api_key is None else LitellmModel(model=name, api_key=api_key)
    )


# ── Public re-exports so callers can find the retry knobs without
#    reaching into the module's privates. ─────────────────────────────


__all__ = [
    "configure_litellm_retries",
    "get_analysis_model",
    "get_lookup_model",
    "get_model",
    "get_reasoning_model",
]
