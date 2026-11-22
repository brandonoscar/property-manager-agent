"""Send ``temperature`` only where the model will accept it.

The Claude 5-series removed sampling params. ``anthropic/claude-sonnet-5`` —
the production model — rejects **any** explicit ``temperature`` other than 1,
including ``0``, with a client-side 400 raised before the request leaves the
process::

    litellm.UnsupportedParamsError: claude-sonnet-5 does not support
    temperature=0.3. Only temperature=1 is supported.

Omitting the parameter works everywhere. Haiku 4.5 still accepts one.

This was not theoretical. Every background LLM path passed an explicit
temperature and every one of them had been failing since the 2026-07-21
Anthropic migration: 417 of 452 webhook events with ``planner_status='failed'``
AND ``summary_status='failed'``, the last fully-successful event on
**2026-05-22**. The Inbox, email triage, risk-flag cards, document extraction
and the CRM/renewal drafts were all dead for five weeks, and nothing surfaced
it because each failure was recorded on the row rather than raised.

⚠ **The provider prefix hides it.** ``_supports_sampling_params`` answers
``True`` for ``"anthropic/claude-sonnet-5"`` and ``False`` for bare
``"claude-sonnet-5"`` — litellm strips the prefix before the real check, so
asking it with the configured name says the call will succeed when it will not.
:func:`supports_temperature` strips first, which is why its test drives the
actual ``acompletion`` path (``UnsupportedParamsError`` raises client-side, so
that costs no network) instead of trusting the helper's own oracle.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Providers litellm namespaces with a ``prefix/`` that must come off before
#: any capability lookup. Only the ones this fleet can be pointed at.
_PROVIDER_PREFIXES = ("anthropic/", "claude/", "gemini/", "google/", "vertex_ai/", "openai/")


def _bare(model: str) -> str:
    lowered = model.lower()
    for prefix in _PROVIDER_PREFIXES:
        if lowered.startswith(prefix):
            return model[len(prefix) :]
    return model


def supports_temperature(model: str) -> bool:
    """Will ``model`` accept an explicit ``temperature`` other than 1?

    Fails OPEN (``True``) whenever litellm's capability table cannot answer.
    A wrong ``True`` costs one clear 400 that names the model and the param; a
    wrong ``False`` silently drops a temperature the caller chose, on every
    call, forever. The loud failure is the recoverable one.
    """
    try:
        from litellm.llms.anthropic.common_utils import AnthropicModelInfo

        return bool(AnthropicModelInfo._supports_sampling_params(_bare(model)))
    except (Exception, SystemExit):  # noqa: BLE001 — a capability hint, never load-bearing
        logger.debug("could not resolve sampling support for %r — assuming supported", model)
        return True


def sampling_kwargs(model: str, temperature: float | None) -> dict[str, Any]:
    """``{"temperature": t}`` when the model accepts it, else ``{}``.

    Splat into an ``acompletion`` call in place of a literal ``temperature=``::

        kwargs = {"model": name, "messages": msgs, **sampling_kwargs(name, 0.3)}

    Dropping it is not a behaviour change on these models: they run at
    temperature 1 regardless, so the only difference is whether the request is
    accepted at all. The value is preserved for the models that do honour it
    (Haiku 4.5, Gemini) rather than deleted at every call site, so pointing a
    path at a cheaper tier keeps the tuning that was chosen for it.
    """
    if temperature is None:
        return {}
    if not supports_temperature(model):
        logger.debug("dropping temperature=%s — %s does not accept it", temperature, model)
        return {}
    return {"temperature": temperature}


__all__ = ["sampling_kwargs", "supports_temperature"]
