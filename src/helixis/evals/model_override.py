"""Optional cheaper-model override for eval runs — a Gemini-cost lever.

``HELIXIS_EVAL_MODEL`` (env) swaps the model that eval Settings hand LiteLLM —
e.g. a Flash tier for the per-PR gate runs, which mostly assert MECHANICS
(routing targets, forbidden-action prohibitions, tool-call caps) rather than
prose quality. CI wires it from the ``HELIXIS_EVAL_PR_MODEL`` repository
variable on pull_request runs only, so:

* unset variable (the default) → no behavior change anywhere;
* set (e.g. ``gemini/gemini-2.5-flash``) → PR-gate evals run on the cheap
  tier while main-push full-suite runs stay on the production model —
  drift between the tiers surfaces post-merge, not silently.

Calibrate before trusting a cheaper tier as the gate: run the suite 3× with
the override exported locally and confirm the safety cases hold — a tier that
flunks safety mechanics is a false alarm generator, not a saving.
"""

from __future__ import annotations

import os
from typing import Any


def eval_default_model(base_model: str) -> str:
    """The model eval Settings should use: the ``HELIXIS_EVAL_MODEL`` override
    when set (whitespace-stripped), else the configured default."""
    return os.environ.get("HELIXIS_EVAL_MODEL", "").strip() or base_model


def eval_models(base_models: dict[str, Any] | None) -> dict[str, Any]:
    """Copy of the per-role model map with every slot's ``model`` replaced by
    the ``HELIXIS_EVAL_MODEL`` override when set.

    Real completions resolve through ``llm/router.get_model``, which reads the
    ``reasoning`` slot of ``llm.models`` and only falls back to
    ``default_model`` when the slot is absent — so an override that touched
    only ``default_model`` silently never applied (the first Haiku-tier CI run
    kept calling Gemini). Every eval Settings builder must route its ``models``
    map through here alongside ``eval_default_model``."""
    override = os.environ.get("HELIXIS_EVAL_MODEL", "").strip()
    models = {name: dict(slot) for name, slot in (base_models or {}).items()}
    if not override:
        return models
    for slot in models.values():
        if slot.get("model"):
            slot["model"] = override
    return models


def apply_eval_model_override(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Mutate a ``litellm.acompletion`` kwargs dict in place so the call runs
    on the ``HELIXIS_EVAL_MODEL`` override (no-op when unset).

    Covers the eval paths that call LiteLLM directly instead of building
    Settings — the planner/summarizer and LLM-backed tool_invocation tools all
    route through ``runner._eval_completion_fn``. Callers pass ``api_key``
    resolved for their ORIGINAL model (the Gemini key), which would be sent to
    the override's provider verbatim — so when the provider changes, the stale
    key is dropped: a Gemini-tier override re-resolves from
    ``HELIXIS_LLM__GOOGLE_API_KEY``; any other provider falls back to
    LiteLLM's native env lookup (e.g. ``ANTHROPIC_API_KEY``, which ci.yml
    passes through)."""
    override = os.environ.get("HELIXIS_EVAL_MODEL", "").strip()
    if not override or not kwargs.get("model") or kwargs["model"] == override:
        return kwargs
    kwargs["model"] = override
    kwargs.pop("api_key", None)
    if override.lower().startswith(("gemini/", "google/", "vertex_ai/")):
        provider_key = os.environ.get("HELIXIS_LLM__GOOGLE_API_KEY", "").strip()
    elif override.lower().startswith(("anthropic/", "claude/")):
        provider_key = os.environ.get("HELIXIS_LLM__ANTHROPIC_API_KEY", "").strip()
    else:
        provider_key = ""
    if provider_key:
        kwargs["api_key"] = provider_key
    return kwargs


__all__ = ["apply_eval_model_override", "eval_default_model", "eval_models"]
